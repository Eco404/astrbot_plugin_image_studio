"""Durable gallery, reference, and staging storage."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import io
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from .config import HistorySettings
from .database_schema import DATABASE_VERSION, ensure_development_schema
from .models import (
    GeneratedImage,
    GenerationRequest,
    ImageProvider,
    ReferenceImage,
    WorkflowImageAsset,
    WorkflowImageLoadResult,
    parameter_flag,
)

_IMAGE_SUFFIXES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
_SAFE_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_COMFY_PROJECTION_CACHE_LIMIT = 64
_COMFY_PROJECTION_CACHE_BYTES = 16 * 1024 * 1024
_COMFY_PROJECTION_CACHE: OrderedDict[tuple[Any, ...], bytes] = OrderedDict()
_COMFY_PROJECTION_CACHE_LOCK = threading.Lock()
_GALLERY_RETENTION_CACHE_SECONDS = 3.0
_EXTERNAL_ACTIONS = ("favorite", "delete", "download", "reference")
_EXTERNAL_ACTION_LABELS = {
    "favorite": "修改收藏",
    "delete": "删除原图",
    "download": "下载或导出原图",
    "reference": "用作参考图",
}
_LOGGER = logging.getLogger(__name__)
_THUMBNAIL_REVISION_SQL = (
    "COALESCE(t.max_edge, 0) || ':' || COALESCE(t.quality, 0) || ':' || "
    "COALESCE(t.size_bytes, 0)"
)


class ImportDuplicateError(ValueError):
    """A recoverable import conflict identified by exact content hashes."""

    def __init__(self, duplicate_hashes: list[str], *, batch: bool = False) -> None:
        self.duplicate_hashes = list(dict.fromkeys(duplicate_hashes))
        self.code = "batch_duplicates" if batch else "gallery_duplicates"
        message = (
            f"本次上传包含 {len(duplicate_hashes)} 张批内重复图片，整批上传已取消，请移除重复项"
            if batch
            else f"本次上传包含 {len(duplicate_hashes)} 张画廊已有图片，整批上传已取消，请移除重复项"
        )
        super().__init__(message)

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": False,
            "code": self.code,
            "duplicate_hashes": self.duplicate_hashes,
            "message": str(self),
        }


class ImportEditConflictError(ValueError):
    """The imported record changed after an editor read its snapshot."""


class ExternalPermissionError(ValueError):
    """At least one selected source forbids the requested gallery operation."""

    def __init__(self, denied: list[dict[str, Any]]):
        self.denied = denied
        super().__init__("；".join(item["message"] for item in denied))


class ExternalDeleteError(ValueError):
    """An explicit external deletion failed, possibly after removing some files."""

    def __init__(self, generation_id: str, message: str, *, deleted_files=None):
        super().__init__(message)
        self.generation_id = generation_id
        self.deleted_files = deleted_files or []

    def as_dict(self) -> dict[str, Any]:
        return {
            "generation_id": self.generation_id,
            "code": "external_delete_failed",
            "message": str(self),
            "partial": bool(self.deleted_files),
            "generation_deleted": bool(self.deleted_files),
            "deleted_files": self.deleted_files,
        }


class GenerationStore:
    """Store plugin-owned gallery files and queryable generation metadata."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir.resolve()
        self.history_dir = self.data_dir / "history"
        self.assets_dir = self.history_dir / "assets"
        self.thumbnails_dir = self.history_dir / "thumbnails"
        self.staging_dir = self.data_dir / "staging_references"
        self.imports_dir = self.data_dir / "import_staging"
        self.exports_dir = self.data_dir / "exports"
        self.delivery_dir = self.data_dir / "delivery_staging"
        self.db_path = self.data_dir / "history.sqlite3"
        self._lock = asyncio.Lock()
        self._revision_lock = threading.Lock()
        self._revision_connection: sqlite3.Connection | None = None
        self._revision_identity: tuple[int, int] | None = None
        self._revision_instance = ""
        self._retention_cache_lock = asyncio.Lock()
        self._retention_cache: tuple[Any, float, float, dict[str, Any]] | None = None
        self._last_maintenance_report: dict[str, Any] = {
            "status": "never",
            "running": False,
            "checked_at": 0,
            "duration_ms": 0,
            "deep": False,
            "stats": {},
            "repaired": {},
            "errors": [],
        }

    async def initialize(self) -> None:
        """Create directories and database tables."""

        await asyncio.to_thread(self._initialize_sync)

    async def close(self) -> None:
        """Release the read-only database observer used by gallery caches."""
        await asyncio.to_thread(self._close_revision_connection_sync)

    def _close_revision_connection_sync(self) -> None:
        with self._revision_lock:
            if self._revision_connection is not None:
                self._revision_connection.close()
                self._revision_connection = None
            self._retention_cache = None

    async def gallery_revision(self) -> str:
        """Identify committed database changes without scanning gallery records."""
        return await asyncio.to_thread(self._gallery_revision_sync)

    def _gallery_revision_sync(self) -> str:
        with self._revision_lock:
            stat = self.db_path.stat()
            identity = (stat.st_dev, stat.st_ino)
            if self._revision_connection is None or identity != self._revision_identity:
                if self._revision_connection is not None:
                    self._revision_connection.close()
                self._revision_connection = sqlite3.connect(
                    self.db_path.as_uri() + "?mode=ro",
                    uri=True,
                    check_same_thread=False,
                )
                self._revision_identity = identity
                self._revision_instance = uuid.uuid4().hex
            version = self._revision_connection.execute(
                "PRAGMA data_version"
            ).fetchone()[0]
            return f"{self._revision_instance}:{version}"

    def _initialize_sync(self) -> None:
        for directory in (
            self.data_dir,
            self.assets_dir,
            self.thumbnails_dir,
            self.staging_dir,
            self.imports_dir,
            self.exports_dir,
            self.delivery_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            ensure_development_schema(conn, backup_dir=self.data_dir / "backups")
        self._repair_derived_fields_sync()
        self._backfill_metadata_sync()
        self._delete_expired_leases_sync(time.time())
        self._delete_expired_import_batches_sync(time.time())
        self._purge_unreferenced_assets_sync()
        self._cleanup_orphaned_asset_files_sync()
        self._cleanup_orphaned_thumbnails_sync()

    def _repair_derived_fields_sync(self) -> None:
        """Repair incomplete dimensions and indexed aliases independently of schema upgrades."""

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, path FROM image_assets WHERE width <= 0 OR height <= 0"
            ).fetchall()
        for row in rows:
            path = self.data_dir / str(row["path"])
            if not _is_within(path, self.assets_dir) or not path.is_file():
                continue
            try:
                dimensions = _image_dimensions(path.read_bytes())
            except OSError:
                continue
            with self._connect() as conn:
                conn.execute(
                    "UPDATE image_assets SET width = ?, height = ? WHERE id = ? "
                    "AND (width <= 0 OR height <= 0)",
                    (*dimensions, str(row["id"])),
                )
        with self._connect() as conn:
            conn.execute(
                "UPDATE generations SET generation_engine = CASE provider_kind "
                "WHEN 'nai_direct' THEN 'novelai' WHEN '' THEN 'unknown' ELSE provider_kind END "
                "WHERE generation_engine = 'unknown' AND source != 'import' "
                "AND provider_kind != ''"
            )
            # Preserve raw requests and per-image metadata; normalize only the gallery projection.
            conn.execute(
                "UPDATE generations SET generation_engine = 'novelai' "
                "WHERE lower(trim(generation_engine)) IN ('nai', 'novelai') "
                "AND generation_engine != 'novelai'"
            )
            conn.execute(
                "UPDATE generations SET generation_engine = 'novelai' "
                "WHERE generation_engine = 'mixed' AND EXISTS ("
                "SELECT 1 FROM generation_images i WHERE i.generation_id = generations.id) "
                "AND NOT EXISTS (SELECT 1 FROM generation_images i "
                "WHERE i.generation_id = generations.id AND "
                "lower(trim(COALESCE(json_extract(i.supplemental_json, '$.generation_engine'), 'unknown'))) "
                "NOT IN ('nai', 'novelai'))"
            )

    async def maintenance_report(self) -> dict[str, Any]:
        """Return the latest maintenance report plus current lightweight stats."""

        async with self._lock:
            report = dict(self._last_maintenance_report)
            report["stats"] = await asyncio.to_thread(self._storage_stats_sync)
        return report

    async def configure_external_source(
        self,
        source_id: str,
        name: str,
        root_path: str | Path,
        enabled: bool,
        *,
        source_type: str = "nai",
        recursive: bool = False,
        permissions: dict[str, bool] | None = None,
    ) -> dict[str, Any]:
        await self._external_mutation(
            self._configure_external_source_sync,
            source_id,
            name,
            root_path,
            enabled,
            source_type,
            recursive,
            permissions,
        )
        return next(
            item
            for item in await self.external_sources_status()
            if item["id"] == source_id
        )

    async def _external_mutation(self, function, *args):
        # A cancelled scanner must not release the store lock while its worker still commits.
        async with self._lock:
            task = asyncio.create_task(asyncio.to_thread(function, *args))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                try:
                    await task
                except Exception:
                    _LOGGER.exception(
                        "External gallery mutation failed while cancellation was pending"
                    )
                raise

    def _configure_external_source_sync(
        self,
        source_id,
        name,
        root_path,
        enabled,
        source_type="nai",
        recursive=False,
        permissions=None,
    ):
        root = Path(root_path).absolute()
        if (
            not source_id
            or root == Path(root.anchor)
            or source_type not in {"nai", "directory"}
        ):
            raise ValueError("外部图库来源或目录无效")
        allowed = {
            action: bool(
                (permissions or {}).get(
                    action, action != "delete" or source_type == "nai"
                )
            )
            for action in _EXTERNAL_ACTIONS
        }
        with self._connect() as conn:
            previous = conn.execute(
                "SELECT root_path,type,recursive FROM external_sources WHERE id = ?",
                (source_id,),
            ).fetchone()
            index_changed = previous and (
                previous["root_path"] != str(root)
                or previous["type"] != source_type
                or bool(previous["recursive"]) != bool(recursive)
            )
            if index_changed:
                # File identity is rooted in this exact directory; changing it starts a new index.
                ids = [
                    row[0]
                    for row in conn.execute(
                        "SELECT generation_id FROM external_records WHERE source_id=?",
                        (source_id,),
                    )
                ]
                conn.execute(
                    "DELETE FROM generations WHERE id IN (SELECT generation_id FROM external_records WHERE source_id = ?)",
                    (source_id,),
                )
                self._delete_legacy_search_sync(conn, ids)
            conn.execute(
                "INSERT INTO external_sources (id,name,root_path,enabled,type,recursive,permissions_json) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name,root_path=excluded.root_path,enabled=excluded.enabled,"
                "type=excluded.type,recursive=excluded.recursive,permissions_json=excluded.permissions_json",
                (
                    source_id,
                    name,
                    str(root),
                    int(enabled),
                    source_type,
                    int(recursive),
                    json.dumps(allowed),
                ),
            )
            if index_changed:
                conn.execute(
                    "UPDATE external_sources SET status_json='{}' WHERE id=?",
                    (source_id,),
                )
        if not enabled:
            self._trim_disabled_external_thumbnails_sync()
        self._purge_unreferenced_assets_sync()

    async def remove_external_source(self, source_id: str) -> None:
        """Remove source registration/cache only; never delete source-owned originals."""
        await self._external_mutation(self._remove_external_source_sync, source_id)

    def _remove_external_source_sync(self, source_id: str) -> None:
        with self._connect() as conn:
            ids = [
                row[0]
                for row in conn.execute(
                    "SELECT generation_id FROM external_records WHERE source_id=?",
                    (source_id,),
                )
            ]
            conn.executemany(
                "DELETE FROM generations WHERE id=?", [(item,) for item in ids]
            )
            self._delete_legacy_search_sync(conn, ids)
            conn.execute("DELETE FROM external_sources WHERE id=?", (source_id,))
        self._purge_unreferenced_assets_sync()

    async def external_scan_snapshot(self, source_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._external_scan_snapshot_sync, source_id)

    def _external_scan_snapshot_sync(self, source_id):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT e.*,t.path AS thumbnail_path,t.max_edge AS thumbnail_max_edge,t.quality AS thumbnail_quality,m.parser_version "
                "FROM external_records e LEFT JOIN image_thumbnails t ON t.asset_id=e.asset_id "
                "LEFT JOIN image_metadata m ON m.asset_id=e.asset_id WHERE e.source_id=?",
                (source_id,),
            ).fetchall()
        result = {}
        for row in rows:
            item = dict(row)
            thumbnail = item.pop("thumbnail_path")
            item["thumbnail_available"] = bool(
                thumbnail and (self.data_dir / thumbnail).is_file()
            )
            result[item["relative_path"]] = item
        return result

    async def set_external_status(self, source_id: str, status: dict[str, Any]) -> None:
        await self._external_mutation(self._set_external_status_sync, source_id, status)

    def _set_external_status_sync(self, source_id, status):
        with self._connect() as conn:
            conn.execute(
                "UPDATE external_sources SET status_json=? WHERE id=?",
                (
                    json.dumps(status, ensure_ascii=False, separators=(",", ":")),
                    source_id,
                ),
            )

    async def external_sources_status(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._external_sources_status_sync)

    def _external_sources_status_sync(self):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT s.*,COUNT(e.generation_id) AS indexed_count,COALESCE(SUM(e.size_bytes),0) AS size_bytes "
                "FROM external_sources s LEFT JOIN external_records e ON e.source_id=s.id AND e.available=1 GROUP BY s.id ORDER BY s.id"
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                thumbs = conn.execute(
                    "SELECT COUNT(*),COALESCE(SUM(t.size_bytes),0) FROM image_thumbnails t WHERE t.asset_id IN "
                    "(SELECT asset_id FROM external_records WHERE source_id=? AND available=1)",
                    (item["id"],),
                ).fetchone()
                item["thumbnail_count"], item["thumbnail_bytes"] = map(int, thumbs)
                item["enabled"] = bool(item["enabled"])
                item["recursive"] = bool(item["recursive"])
                item["permissions"] = _load_json(item.pop("permissions_json"))
                status = _load_json(item.pop("status_json"))
                item = {**status, **item}
                item.setdefault("status", "idle" if item["enabled"] else "disabled")
                item["path"] = item.pop("root_path")
                item["counts"] = {
                    "images": item["indexed_count"],
                    "size_bytes": item["size_bytes"],
                    "thumbnail_size_bytes": item["thumbnail_bytes"],
                }
                result.append(item)
        return result

    @staticmethod
    def _external_path(row: dict[str, Any] | sqlite3.Row, *, verify=True) -> Path:
        root = Path(row["root_path"])
        relative = Path(row["relative_path"])
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ValueError("外部图片路径超出图库目录")
        path = root / relative
        # Reject symlinks even when their current target happens to be inside the root.
        if any(parent.is_symlink() for parent in (root, *root.parents)) or any(
            (root.joinpath(*relative.parts[:index])).is_symlink()
            for index in range(1, len(relative.parts) + 1)
        ):
            raise ValueError("外部图片不能使用符号链接")
        if not _is_within(path, root):
            raise ValueError("外部图片路径超出图库目录")
        if verify:
            stat = path.stat()
            if (
                not path.is_file()
                or stat.st_size != row["size_bytes"]
                or stat.st_mtime_ns != row["mtime_ns"]
            ):
                raise ValueError("外部图片已经变化，请重新扫描后再操作")
            if "fingerprint" in row.keys():
                GenerationStore._verify_external_fingerprint(
                    path, _load_json(row["fingerprint"])
                )
        return path

    def _external_record_sync(self, generation_id: str):
        with self._connect() as conn:
            return conn.execute(
                "SELECT e.*,s.root_path,s.enabled,s.name,s.type,s.permissions_json FROM external_records e JOIN external_sources s ON s.id=e.source_id WHERE e.generation_id=?",
                (generation_id,),
            ).fetchone()

    def _external_generation_enabled_sync(self, generation_id: str) -> bool:
        row = self._external_record_sync(generation_id)
        return row is None or bool(row["enabled"] and row["available"])

    def _external_display_sync(self, generation_id: str, source: Any) -> dict[str, Any]:
        if source != "external":
            return {
                "is_external": False,
                "external_source": None,
                "allowed_actions": dict.fromkeys(_EXTERNAL_ACTIONS, True),
            }
        row = self._external_record_sync(generation_id)
        return {
            "is_external": True,
            "external_source": {"id": row["source_id"], "name": row["name"]}
            if row
            else None,
            "allowed_actions": self._external_allowed_actions(row),
            "time_source": row["time_source"] if row else "",
        }

    @staticmethod
    def _external_allowed_actions(row) -> dict[str, bool]:
        permissions = _load_json(row["permissions_json"]) if row else {}
        available = bool(row and row["enabled"] and row["available"])
        return {
            action: available and permissions.get(action, True) is True
            for action in _EXTERNAL_ACTIONS
        }

    def _allowed_actions_for_generation_sync(
        self, generation_id: str
    ) -> dict[str, bool]:
        row = self._external_record_sync(generation_id)
        return (
            self._external_allowed_actions(row)
            if row
            else dict.fromkeys(_EXTERNAL_ACTIONS, True)
        )

    def _resolve_image_asset_sync(self, row: dict[str, Any]) -> Path | None:
        generation_id = str(row.get("generation_id") or "")
        external = self._external_record_sync(generation_id) if generation_id else None
        if external is not None and not (external["enabled"] and external["available"]):
            return None
        local = self.data_dir / str(row["path"])
        if _is_within(local, self.assets_dir) and local.is_file():
            return local
        if external is None:
            return None
        try:
            return self._external_path(external)
        except (OSError, ValueError):
            return None

    async def upsert_external_image(
        self,
        source_id: str,
        relative_path: str,
        data: bytes,
        *,
        fingerprint: str,
        sidecar_fingerprint: str,
        size_bytes: int,
        mtime_ns: int,
        parameters: dict[str, Any] | None = None,
        created_at: float | None = None,
        preview_max_edge: int = 1024,
        preview_quality: int = 80,
        generation_engine: str = "unknown",
        time_source: str = "",
        metadata_created_at: float | None = None,
        file_birthtime: float | None = None,
        time_policy_version: int = 0,
    ) -> dict[str, str] | None:
        return await self._external_mutation(
            self._upsert_external_image_sync,
            source_id,
            relative_path,
            data,
            fingerprint,
            sidecar_fingerprint,
            size_bytes,
            mtime_ns,
            parameters or {},
            created_at,
            preview_max_edge,
            preview_quality,
            generation_engine,
            time_source,
            metadata_created_at,
            file_birthtime,
            time_policy_version,
        )

    def _upsert_external_image_sync(
        self,
        source_id,
        relative_path,
        data,
        fingerprint,
        sidecar_fingerprint,
        size_bytes,
        mtime_ns,
        parameters,
        created_at,
        preview_max_edge,
        preview_quality,
        generation_engine,
        time_source,
        metadata_created_at,
        file_birthtime,
        time_policy_version,
    ):
        with self._connect() as conn:
            source = conn.execute(
                "SELECT * FROM external_sources WHERE id=?", (source_id,)
            ).fetchone()
            previous = conn.execute(
                "SELECT * FROM external_records WHERE source_id=? AND relative_path=?",
                (source_id, relative_path),
            ).fetchone()
        if source is None or not source["enabled"]:
            return None
        if size_bytes != len(data):
            raise ValueError("外部图片读取时发生变化，请重试扫描")
        original = self._external_path(
            {
                **dict(source),
                "relative_path": relative_path,
                "size_bytes": size_bytes,
                "mtime_ns": mtime_ns,
                "fingerprint": fingerprint,
            }
        )
        _validate_import_content(data)
        digest = hashlib.sha256(data).hexdigest()
        mime = detect_mime_type(data)
        width, height = _image_dimensions(data)
        metadata = self._metadata_for_asset_sync(digest, data)
        engine = _canonical_engine(generation_engine)
        if engine == "unknown":
            engine = _canonical_engine(
                metadata.get("normalized", {}).get("generation_engine")
                or metadata.get("format")
            )
        sort_time = created_at if created_at is not None else mtime_ns / 1e9
        overrides = {
            "generation_engine": engine,
            "generated_at": sort_time
            if time_source in {"", "nai_filename", "metadata"}
            else None,
        }
        for source_key, target in (
            ("tag", "prompt"),
            ("negative", "negative_prompt"),
            ("model", "model"),
        ):
            if source_key in parameters:
                overrides[target] = str(parameters[source_key])
        if parameters:
            overrides["parameters"] = dict(parameters)
        overrides, parameter_warnings = _external_parameter_overrides(
            overrides, metadata
        )
        if parameter_warnings:
            metadata = {
                **metadata,
                "warnings": [*(metadata.get("warnings") or []), *parameter_warnings],
            }
        supplemental = _import_supplemental(
            Path(relative_path).name, overrides, metadata, allow_unresolved_output=True
        )
        supplemental["external_parameters"] = dict(parameters)
        supplemental["time_source"] = time_source
        asset = {
            "id": digest,
            "path": f"external/{digest}{_image_suffix(mime, data)}",
            "mime_type": mime,
            "size_bytes": size_bytes,
            "width": width,
            "height": height,
            "original_path": original,
        }
        thumbnail = self._prepare_thumbnail_sync(
            asset, max_edge=preview_max_edge, quality=preview_quality
        )
        generation_id = str(previous["generation_id"]) if previous else uuid.uuid4().hex
        # Thumbnail work can take long enough for the source to replace its file.
        self._verify_external_fingerprint(original, _load_json(fingerprint))
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO image_assets(id,path,mime_type,size_bytes,width,height,created_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET width=excluded.width,height=excluded.height,file_state='available'",
                (digest, asset["path"], mime, size_bytes, width, height, time.time()),
            )
            self._upsert_thumbnails_sync(conn, [thumbnail])
            self._save_metadata_sync(conn, {digest: metadata})
            conn.execute(
                "INSERT INTO generations(id,created_at,source,status,mode,provider_id,provider_name,provider_kind,model,original_prompt,final_prompt,parameters_json,elapsed_ms,generation_engine,generated_at,supplemental_json) "
                "VALUES (?,?,'external','succeeded',?,'','','',?,?,?, ?,0,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET created_at=excluded.created_at,mode=excluded.mode,model=excluded.model,original_prompt=excluded.original_prompt,final_prompt=excluded.final_prompt,parameters_json=excluded.parameters_json,generation_engine=excluded.generation_engine,generated_at=excluded.generated_at,supplemental_json=excluded.supplemental_json",
                (
                    generation_id,
                    sort_time,
                    supplemental["mode"],
                    supplemental["model"],
                    supplemental["prompt"],
                    str(
                        metadata.get("normalized", {}).get("prompt")
                        or supplemental["prompt"]
                    ),
                    json.dumps(parameters, ensure_ascii=False),
                    engine,
                    supplemental["generated_at"],
                    json.dumps(supplemental, ensure_ascii=False),
                ),
            )
            image = conn.execute(
                "SELECT id FROM generation_images WHERE generation_id=?",
                (generation_id,),
            ).fetchone()
            if previous and previous["asset_id"] != digest:
                conn.execute(
                    "DELETE FROM generation_images WHERE generation_id=?",
                    (generation_id,),
                )
                image = None
            image_id = str(image["id"]) if image else uuid.uuid4().hex
            conn.execute(
                "INSERT INTO generation_images(id,generation_id,ordinal,asset_id,supplemental_json) VALUES (?,?,0,?,?) "
                "ON CONFLICT(id) DO UPDATE SET asset_id=excluded.asset_id,supplemental_json=excluded.supplemental_json",
                (
                    image_id,
                    generation_id,
                    digest,
                    json.dumps(supplemental, ensure_ascii=False),
                ),
            )
            conn.execute(
                "INSERT INTO external_records(generation_id,source_id,relative_path,asset_id,fingerprint,sidecar_fingerprint,size_bytes,mtime_ns,available,time_source,metadata_created_at,file_birthtime,time_policy_version) VALUES (?,?,?,?,?,?,?,?,1,?,?,?,?) "
                "ON CONFLICT(generation_id) DO UPDATE SET asset_id=excluded.asset_id,fingerprint=excluded.fingerprint,sidecar_fingerprint=excluded.sidecar_fingerprint,size_bytes=excluded.size_bytes,mtime_ns=excluded.mtime_ns,available=1,"
                "time_source=excluded.time_source,metadata_created_at=excluded.metadata_created_at,file_birthtime=excluded.file_birthtime,time_policy_version=excluded.time_policy_version",
                (
                    generation_id,
                    source_id,
                    relative_path,
                    digest,
                    fingerprint,
                    sidecar_fingerprint,
                    size_bytes,
                    mtime_ns,
                    time_source,
                    metadata_created_at,
                    file_birthtime,
                    time_policy_version,
                ),
            )
            self._refresh_search_sync(conn, generation_id)
        if previous and previous["asset_id"] != digest:
            self._purge_unreferenced_assets_sync([str(previous["asset_id"])])
        return {
            "generation_id": generation_id,
            "image_id": image_id,
            "asset_id": digest,
        }

    async def reconcile_external_source(self, source_id: str, seen_paths) -> int:
        return await self._external_mutation(
            self._reconcile_external_source_sync, source_id, set(seen_paths)
        )

    def _reconcile_external_source_sync(self, source_id, seen_paths):
        with self._connect() as conn:
            source = conn.execute(
                "SELECT enabled FROM external_sources WHERE id=?", (source_id,)
            ).fetchone()
            if not source or not source["enabled"]:
                return 0
            missing = [
                row
                for row in conn.execute(
                    "SELECT generation_id,relative_path FROM external_records WHERE source_id=?",
                    (source_id,),
                )
                if row["relative_path"] not in seen_paths
            ]
            conn.executemany(
                "DELETE FROM generations WHERE id=?",
                [(row["generation_id"],) for row in missing],
            )
            self._delete_legacy_search_sync(
                conn, [row["generation_id"] for row in missing]
            )
        self._purge_unreferenced_assets_sync()
        return len(missing)

    @staticmethod
    def _delete_legacy_search_sync(conn, generation_ids):
        try:
            conn.executemany(
                "DELETE FROM generation_search WHERE generation_id=?",
                [(item,) for item in generation_ids],
            )
        except sqlite3.OperationalError:
            pass

    def _trim_disabled_external_thumbnails_sync(self):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT t.asset_id,t.path FROM image_thumbnails t JOIN image_assets a ON a.id=t.asset_id "
                "WHERE a.path LIKE 'external/%' AND NOT EXISTS (SELECT 1 FROM external_records e JOIN external_sources s ON s.id=e.source_id "
                "WHERE e.asset_id=t.asset_id AND s.enabled=1 AND e.available=1)"
            ).fetchall()
            conn.executemany(
                "DELETE FROM image_thumbnails WHERE asset_id=?",
                [(row["asset_id"],) for row in rows],
            )
        for row in rows:
            _unlink_if_owned(self.data_dir / row["path"], self.thumbnails_dir)

    async def stage_gallery_reference(self, image_id: str) -> dict[str, str]:
        # Resolve and read while holding the same lock as scanning and explicit deletion.
        async with self._lock:
            if not _SAFE_ID_RE.fullmatch(str(image_id or "")):
                raise ValueError("图片 ID 无效")
            resolved = await asyncio.to_thread(
                self._gallery_image_file_sync, image_id, "reference"
            )
            if resolved is None:
                raise ValueError("图片不存在、已被删除或外部图库已关闭")
            path, mime, filename = resolved
            content = await asyncio.to_thread(path.read_bytes)
            return await self.stage_reference(
                filename=filename, content=content, mime_type=mime
            )

    async def external_delete_preview(
        self, generation_ids: list[str]
    ) -> dict[str, Any]:
        ids = _validate_generation_selection(generation_ids)
        return await asyncio.to_thread(self._external_delete_preview_sync, ids)

    def _external_delete_preview_sync(self, ids):
        preview = self._external_action_preview_sync(ids, "delete")
        return {key: preview[key] for key in ("external_count", "external_sources")}

    async def external_action_preview(
        self, generation_ids: list[str], action: str
    ) -> dict[str, Any]:
        ids = _validate_generation_selection(generation_ids)
        return await asyncio.to_thread(self._external_action_preview_sync, ids, action)

    def _external_action_preview_sync(self, ids, action):
        if action not in _EXTERNAL_ACTIONS:
            raise ValueError("外部图库操作无效")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT e.generation_id,e.source_id,e.available,s.name,s.enabled,s.permissions_json FROM external_records e JOIN external_sources s ON s.id=e.source_id "
                f"WHERE e.generation_id IN ({','.join('?' for _ in ids)})",
                ids,
            ).fetchall()
        denied = []
        for row in rows:
            if self._external_allowed_actions(row)[action]:
                continue
            reason = (
                "已停用或图片不可用"
                if not (row["enabled"] and row["available"])
                else f"不允许{_EXTERNAL_ACTION_LABELS[action]}"
            )
            denied.append(
                {
                    "id": row["generation_id"],
                    "source_id": row["source_id"],
                    "source_name": row["name"],
                    "action": action,
                    "message": f"外部图库「{row['name']}」{reason}（记录 {row['generation_id']}）",
                }
            )
        return {
            "allowed": not denied,
            "denied": denied,
            "external_count": len(rows),
            "external_sources": sorted({row["name"] for row in rows}),
        }

    async def assert_external_action(
        self, generation_ids: list[str], action: str
    ) -> None:
        ids = _validate_generation_selection(generation_ids)
        await asyncio.to_thread(self._assert_external_action_sync, ids, action)

    def _assert_external_action_sync(
        self, generation_ids: list[str], action: str
    ) -> None:
        preview = self._external_action_preview_sync(generation_ids, action)
        if not preview["allowed"]:
            raise ExternalPermissionError(preview["denied"])

    def _delete_external_original_sync(
        self, generation_id: str
    ) -> ExternalDeleteError | None:
        row = self._external_record_sync(generation_id)
        if row is None:
            return None
        self._assert_external_action_sync([generation_id], "delete")
        if not row["enabled"]:
            raise ExternalDeleteError(generation_id, "外部图库已关闭，请重新启用后删除")
        try:
            path = self._external_path(row)
            fingerprint = _load_json(row["fingerprint"])
            self._verify_external_fingerprint(path, fingerprint)
            if hashlib.sha256(path.read_bytes()).hexdigest() != row["asset_id"]:
                raise ValueError("外部图片内容已经变化，请重新扫描后再删除")
        except FileNotFoundError:
            # The source's retention cleaner won the race; remove only our index.
            return None
        except (OSError, ValueError) as exc:
            raise ExternalDeleteError(generation_id, str(exc)) from exc
        sidecars = (
            _load_json(row["sidecar_fingerprint"]) if row["type"] == "nai" else {}
        )
        candidates: list[Path] = []
        try:
            for filename, fingerprint in sidecars.items():
                if filename not in {
                    path.with_suffix(".yaml").name,
                    path.with_suffix(".json").name,
                }:
                    continue
                candidate = path.with_name(filename)
                if candidate.is_symlink():
                    raise ValueError("外部参数文件不能使用符号链接")
                try:
                    self._verify_external_fingerprint(candidate, fingerprint)
                except FileNotFoundError:
                    continue
                candidates.append(candidate)
        except (OSError, ValueError) as exc:
            raise ExternalDeleteError(
                generation_id, f"外部参数文件已经变化，未删除原图：{exc}"
            ) from exc
        deleted: list[str] = []
        try:
            self._external_path(row)
            path.unlink()
            deleted.append(path.name)
            for candidate in candidates:
                # Do not unlink a sidecar replaced since the first validation.
                try:
                    if candidate.is_symlink():
                        raise ValueError("外部参数文件不能使用符号链接")
                    self._verify_external_fingerprint(
                        candidate, sidecars[candidate.name]
                    )
                    candidate.unlink()
                    deleted.append(candidate.name)
                except FileNotFoundError:
                    continue
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            if not deleted:
                raise ExternalDeleteError(
                    generation_id, f"外部图片删除失败：{exc}"
                ) from exc
            return ExternalDeleteError(
                generation_id,
                f"原图已删除，但部分外部参数文件未删除：{exc}",
                deleted_files=deleted,
            )
        return None

    @staticmethod
    def _verify_external_fingerprint(path: Path, fingerprint: Any) -> None:
        if not isinstance(fingerprint, dict):
            raise ValueError("外部文件身份无效，请重新扫描")
        stat = path.stat()
        actual = {
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "ctime_ns": stat.st_ctime_ns,
            "device": stat.st_dev,
            "inode": stat.st_ino,
        }
        if any(
            actual[key] != value for key, value in fingerprint.items() if key in actual
        ):
            raise ValueError("文件已经被替换或修改，请重新扫描")

    def _metadata_for_asset_sync(
        self, asset_id: str, data: bytes, *, strict: bool = False
    ) -> dict[str, Any]:
        from .image_metadata import PARSER_VERSION, parse_image_metadata

        with self._connect() as conn:
            row = conn.execute(
                "SELECT metadata_json FROM image_metadata WHERE asset_id = ?",
                (asset_id,),
            ).fetchone()
        if row is not None:
            cached = _load_json(str(row["metadata_json"]))
            if int(cached.get("parser_version", 0)) >= PARSER_VERSION:
                return cached
        try:
            return parse_image_metadata(data)
        except ValueError as exc:
            if strict:
                raise
            return {
                "format": "unknown",
                "parser_version": 0,
                "raw": {},
                "normalized": {},
                "warnings": [str(exc)],
            }

    @staticmethod
    def _save_metadata_sync(
        conn: sqlite3.Connection, metadata: dict[str, dict[str, Any]]
    ) -> None:
        conn.executemany(
            "INSERT INTO image_metadata (asset_id, format, parser_version, metadata_json) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(asset_id) DO UPDATE SET "
            "format=excluded.format, parser_version=excluded.parser_version, "
            "metadata_json=excluded.metadata_json "
            "WHERE image_metadata.parser_version < excluded.parser_version",
            [
                (
                    asset_id,
                    str(item.get("format") or "unknown"),
                    int(item.get("parser_version", 1)),
                    json.dumps(item, ensure_ascii=False, separators=(",", ":")),
                )
                for asset_id, item in metadata.items()
            ],
        )

    def _backfill_metadata_sync(self) -> None:
        """Parse old asset metadata outside the schema migration transaction."""

        from .image_metadata import PARSER_VERSION

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT a.id, a.path FROM image_assets a LEFT JOIN image_metadata m "
                "ON m.asset_id = a.id WHERE m.asset_id IS NULL OR m.parser_version < ?",
                (PARSER_VERSION,),
            ).fetchall()
        for row in rows:
            path = self.data_dir / str(row["path"])
            if not _is_within(path, self.assets_dir) or not path.is_file():
                continue
            try:
                metadata = self._metadata_for_asset_sync(
                    str(row["id"]), path.read_bytes()
                )
            except (OSError, ValueError):
                continue
            with self._connect() as conn:
                self._save_metadata_sync(conn, {str(row["id"]): metadata})
                generation_ids = conn.execute(
                    "SELECT generation_id FROM generation_images WHERE asset_id = ?",
                    (row["id"],),
                ).fetchall()
                for item in generation_ids:
                    generation_id = str(item["generation_id"])
                    self._refresh_import_projection_sync(conn, generation_id)
                    self._refresh_search_sync(conn, generation_id)

    @staticmethod
    def _refresh_import_projection_sync(
        conn: sqlite3.Connection, generation_id: str
    ) -> None:
        """Refresh derived import fields without guessing whether old overrides were manual."""

        record = conn.execute(
            "SELECT source, supplemental_json FROM generations WHERE id = ?",
            (generation_id,),
        ).fetchone()
        if record is None or record["source"] != "import":
            return
        record_supplemental = _load_json(record["supplemental_json"])
        images = conn.execute(
            "SELECT i.id, i.supplemental_json, m.metadata_json FROM generation_images i "
            "LEFT JOIN image_metadata m ON m.asset_id = i.asset_id "
            "WHERE i.generation_id = ? ORDER BY i.ordinal, i.id",
            (generation_id,),
        ).fetchall()
        prepared: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        for image in images:
            previous = _load_json(image["supplemental_json"]) or record_supplemental
            metadata = _load_json(image["metadata_json"])
            overrides = previous.get("overrides")
            # An absent override snapshot does not establish which old values were user edits.
            if (
                not isinstance(overrides, dict)
                or int(metadata.get("parser_version", 0)) <= 0
            ):
                return
            try:
                projected = _import_supplemental(
                    str(previous.get("original_filename") or ""), overrides, metadata
                )
            except (TypeError, ValueError):
                return
            prepared.append((str(image["id"]), {**previous, **projected}, metadata))
        if not prepared:
            return
        conn.executemany(
            "UPDATE generation_images SET supplemental_json = ? WHERE id = ?",
            [
                (
                    json.dumps(supplemental, ensure_ascii=False, separators=(",", ":")),
                    image_id,
                )
                for image_id, supplemental, _ in prepared
            ],
        )
        GenerationStore._update_import_summary_sync(
            conn, generation_id, record_supplemental, prepared
        )

    @staticmethod
    def _update_import_summary_sync(
        conn: sqlite3.Connection,
        generation_id: str,
        record_supplemental: dict[str, Any],
        prepared: list[tuple[str, dict[str, Any], dict[str, Any]]],
    ) -> None:
        first = prepared[0][1]
        modes = {supplemental["mode"] for _, supplemental, _ in prepared}
        engines = {supplemental["generation_engine"] for _, supplemental, _ in prepared}
        # Keep the original ordered group manifest for retry idempotency after partial deletion.
        summary = {**record_supplemental, **first}
        conn.execute(
            "UPDATE generations SET model = ?, mode = ?, original_prompt = ?, final_prompt = ?, "
            "generation_engine = ?, generated_at = ?, supplemental_json = ? WHERE id = ?",
            (
                first["model"],
                first["mode"] if len(modes) == 1 else "unknown",
                first["prompt"],
                str(
                    prepared[0][2].get("normalized", {}).get("prompt")
                    or first["prompt"]
                ),
                first["generation_engine"] if len(engines) == 1 else "mixed",
                first["generated_at"],
                json.dumps(summary, ensure_ascii=False, separators=(",", ":")),
                generation_id,
            ),
        )

    @staticmethod
    def _refresh_search_sync(conn: sqlite3.Connection, generation_id: str) -> None:
        row = conn.execute(
            "SELECT parameters_json, supplemental_json FROM generations WHERE id = ?",
            (generation_id,),
        ).fetchone()
        if row is None:
            return
        values: list[Any] = [
            _load_json(row["parameters_json"]),
            _canonical_supplemental(_load_json(row["supplemental_json"])),
        ]
        for item in conn.execute(
            "SELECT m.metadata_json, i.supplemental_json FROM generation_images i LEFT JOIN image_metadata m "
            "ON m.asset_id = i.asset_id WHERE i.generation_id = ? ORDER BY i.ordinal",
            (generation_id,),
        ).fetchall():
            values.append(_load_json(item["metadata_json"]).get("normalized", {}))
            values.append(
                _canonical_supplemental(_load_json(item["supplemental_json"]))
            )
        # Only normalized fields are searchable; entire workflow graphs stay out.
        conn.execute(
            "UPDATE generations SET search_text = ? WHERE id = ?",
            (_search_projection(values), generation_id),
        )

    async def run_maintenance(
        self,
        history: HistorySettings,
        *,
        preview_max_edge: int,
        preview_quality: int,
        deep: bool = False,
    ) -> dict[str, Any]:
        """Check and repair the complete plugin-owned storage graph."""

        async with self._lock:
            self._last_maintenance_report = {
                **self._last_maintenance_report,
                "running": True,
            }
            try:
                report = await asyncio.to_thread(
                    self._run_maintenance_sync,
                    history,
                    preview_max_edge,
                    preview_quality,
                    bool(deep),
                )
            except Exception as exc:
                report = {
                    "status": "error",
                    "running": False,
                    "checked_at": time.time(),
                    "duration_ms": 0,
                    "deep": bool(deep),
                    "stats": self._storage_stats_sync(),
                    "repaired": {},
                    "errors": [f"存储维护失败：{type(exc).__name__}"],
                }
            self._last_maintenance_report = report
            return dict(report)

    def _run_maintenance_sync(
        self,
        history: HistorySettings,
        preview_max_edge: int,
        preview_quality: int,
        deep: bool,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        checked_at = time.time()
        errors: list[str] = []
        repaired = {
            "expired_leases": 0,
            "expired_import_batches": 0,
            "broken_assets": 0,
            "rebuilt_thumbnails": 0,
            "unreferenced_assets": 0,
            "orphan_files": 0,
            "stale_temporary_files": 0,
        }

        try:
            with self._connect() as conn:
                quick = str(conn.execute("PRAGMA quick_check").fetchone()[0])
                if quick != "ok":
                    errors.append(f"SQLite quick_check：{quick}")
                foreign_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
                if foreign_rows:
                    errors.append(f"SQLite 外键异常：{len(foreign_rows)} 项")
        except sqlite3.Error as exc:
            errors.append(f"SQLite 检查失败：{type(exc).__name__}")

        repaired["expired_leases"] = self._delete_expired_leases_sync(checked_at)
        repaired["expired_import_batches"] = self._delete_expired_import_batches_sync(
            checked_at
        )
        self._repair_derived_fields_sync()
        self._backfill_metadata_sync()
        broken_ids: list[str] = []
        with self._connect() as conn:
            asset_rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT id, path, mime_type, size_bytes FROM image_assets WHERE path NOT LIKE 'external/%'"
                ).fetchall()
            ]
        for asset in asset_rows:
            path = self.data_dir / str(asset["path"])
            try:
                valid = (
                    _is_within(path, self.assets_dir)
                    and path.is_file()
                    and path.stat().st_size == int(asset["size_bytes"])
                )
                if valid and deep:
                    valid = hashlib.sha256(path.read_bytes()).hexdigest() == asset["id"]
            except OSError:
                valid = False
            if not valid:
                broken_ids.append(str(asset["id"]))
                continue
            thumbnail_missing = not (
                self.thumbnails_dir / f"{asset['id']}.webp"
            ).is_file()
            thumbnail = self._prepare_thumbnail_sync(
                asset,
                max_edge=preview_max_edge,
                quality=preview_quality,
            )
            with self._connect() as conn:
                previous = conn.execute(
                    "SELECT path, size_bytes, max_edge, quality FROM image_thumbnails "
                    "WHERE asset_id = ?",
                    (asset["id"],),
                ).fetchone()
                self._upsert_thumbnails_sync(conn, [thumbnail])
                conn.execute(
                    "UPDATE image_assets SET file_state = 'available' WHERE id = ?",
                    (asset["id"],),
                )
            if (
                thumbnail_missing
                or previous is None
                or int(previous["size_bytes"]) != thumbnail["size_bytes"]
                or int(previous["max_edge"]) != thumbnail["max_edge"]
                or int(previous["quality"]) != thumbnail["quality"]
            ):
                repaired["rebuilt_thumbnails"] += 1

        for asset_id in broken_ids:
            repaired["broken_assets"] += self._remove_broken_asset_sync(asset_id)
        if broken_ids:
            errors.append(f"原图不可用：{len(broken_ids)} 项，已有历史和元数据已保留")

        self._cleanup_sync(history)
        before_assets = self._asset_count_sync()
        self._purge_unreferenced_assets_sync()
        repaired["unreferenced_assets"] = max(
            0, before_assets - self._asset_count_sync()
        )
        grace_cutoff = checked_at - 600
        repaired["orphan_files"] += self._cleanup_orphaned_asset_files_sync(
            older_than=grace_cutoff
        )
        repaired["orphan_files"] += self._cleanup_orphaned_thumbnails_sync(
            older_than=grace_cutoff
        )
        repaired["stale_temporary_files"] = self._cleanup_stale_files_sync(checked_at)
        try:
            with self._connect() as conn:
                conn.execute("PRAGMA optimize")
        except sqlite3.Error as exc:
            errors.append(f"SQLite optimize 失败：{type(exc).__name__}")
        return {
            "status": "warning" if errors else "healthy",
            "running": False,
            "checked_at": checked_at,
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "deep": deep,
            "stats": self._storage_stats_sync(),
            "repaired": repaired,
            "errors": errors,
        }

    async def stage_reference(
        self,
        *,
        filename: str,
        content: bytes,
        mime_type: str,
    ) -> dict[str, str]:
        """Persist a WebUI upload until the generation request consumes it."""

        if not content:
            raise ValueError("参考图为空")
        if len(content) > 20 * 1024 * 1024:
            raise ValueError("参考图不能超过 20 MB")
        ref_id = uuid.uuid4().hex
        suffix = _image_suffix(mime_type, content)
        target = self.staging_dir / f"{ref_id}{suffix}"
        await asyncio.to_thread(_atomic_write, target, content)
        return {
            "id": ref_id,
            "filename": _safe_filename(filename) or f"reference{suffix}",
            "mime_type": detect_mime_type(content, mime_type),
            "preview_data_url": image_data_url(
                content, detect_mime_type(content, mime_type)
            ),
        }

    async def load_staged_references(
        self, reference_ids: list[str]
    ) -> tuple[ReferenceImage, ...]:
        """Load selected plugin-owned staged references by opaque IDs."""

        return await asyncio.to_thread(self._load_staged_references_sync, reference_ids)

    def _load_staged_references_sync(
        self, reference_ids: list[str]
    ) -> tuple[ReferenceImage, ...]:
        refs: list[ReferenceImage] = []
        for ref_id in reference_ids[:8]:
            clean_id = str(ref_id or "").strip().lower()
            if not _SAFE_ID_RE.fullmatch(clean_id):
                raise ValueError("参考图 ID 无效")
            matches = list(self.staging_dir.glob(f"{clean_id}.*"))
            if len(matches) != 1 or not matches[0].is_file():
                raise ValueError("参考图已不存在，请重新上传")
            path = matches[0]
            raw = path.read_bytes()
            refs.append(
                ReferenceImage(
                    id=clean_id,
                    filename=path.name,
                    data=raw,
                    mime_type=detect_mime_type(raw, ""),
                )
            )
        return tuple(refs)

    async def discard_staged_references(
        self, reference_ids: tuple[ReferenceImage, ...]
    ) -> None:
        """Remove request staging files after a request reaches a terminal state."""

        await asyncio.to_thread(
            self._discard_staged_references_sync,
            [reference.id for reference in reference_ids],
        )

    def _discard_staged_references_sync(self, reference_ids: list[str]) -> None:
        for ref_id in reference_ids:
            if not _SAFE_ID_RE.fullmatch(ref_id):
                continue
            for path in self.staging_dir.glob(f"{ref_id}.*"):
                _unlink_if_owned(path, self.staging_dir)

    async def lease_agent_images(
        self,
        images: tuple[GeneratedImage, ...],
        *,
        scope_id: str,
        create_preview: bool,
        preview_max_edge: int,
        preview_quality: int,
        retention_hours: int,
    ) -> tuple[WorkflowImageAsset, ...]:
        """Store generated assets once and lease them to an Agent session."""

        async with self._lock:
            return await asyncio.to_thread(
                self._lease_agent_images_sync,
                images,
                scope_id,
                create_preview,
                preview_max_edge,
                preview_quality,
                retention_hours,
            )

    def _lease_agent_images_sync(
        self,
        images: tuple[GeneratedImage, ...],
        scope_id: str,
        create_preview: bool,
        preview_max_edge: int,
        preview_quality: int,
        retention_hours: int,
    ) -> tuple[WorkflowImageAsset, ...]:
        normalized_scope = str(scope_id or "").strip()
        if not normalized_scope:
            raise ValueError("Agent 资产缺少会话范围")
        now = time.time()
        retention_seconds = max(1, min(168, int(retention_hours))) * 3600
        self._delete_expired_leases_sync(now)
        max_edge = max(256, min(2048, int(preview_max_edge)))
        quality = max(40, min(95, int(preview_quality)))
        assets: list[WorkflowImageAsset] = []
        prepared_assets: dict[str, dict[str, Any]] = {}
        prepared_thumbnails: dict[str, dict[str, Any]] = {}
        for image in images:
            asset = self._prepare_asset_sync(image.data, image.mime_type)
            prepared_assets[asset["id"]] = asset
            preview: GeneratedImage | None = None
            if create_preview:
                thumbnail = self._prepare_thumbnail_sync(
                    asset,
                    max_edge=max_edge,
                    quality=quality,
                )
                prepared_thumbnails[asset["id"]] = thumbnail
                preview_data = (self.data_dir / thumbnail["path"]).read_bytes()
                preview = GeneratedImage(
                    data=preview_data,
                    mime_type=detect_mime_type(preview_data, "image/webp"),
                )

            assets.append(
                WorkflowImageAsset(
                    asset_id=asset["id"],
                    mime_type=asset["mime_type"],
                    size_bytes=len(image.data),
                    preview=preview,
                )
            )
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO image_assets (id, path, mime_type, size_bytes, width, height, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                "path=excluded.path, mime_type=excluded.mime_type, size_bytes=excluded.size_bytes, "
                "width=excluded.width, height=excluded.height, file_state='available'",
                [
                    (
                        asset["id"],
                        asset["path"],
                        asset["mime_type"],
                        asset["size_bytes"],
                        asset["width"],
                        asset["height"],
                        now,
                    )
                    for asset in prepared_assets.values()
                ],
            )
            self._upsert_thumbnails_sync(conn, prepared_thumbnails.values())
            for asset in prepared_assets.values():
                hard_expires_at = now + 7 * 24 * 3600
                conn.execute(
                    "INSERT INTO agent_asset_leases "
                    "(id, asset_id, scope_id, created_at, last_accessed_at, expires_at, hard_expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(scope_id, asset_id) DO UPDATE SET "
                    "last_accessed_at=excluded.last_accessed_at, "
                    "expires_at=excluded.expires_at, hard_expires_at=excluded.hard_expires_at",
                    (
                        uuid.uuid4().hex,
                        asset["id"],
                        normalized_scope,
                        now,
                        now,
                        now + retention_seconds,
                        hard_expires_at,
                    ),
                )
        return tuple(assets)

    async def load_workflow_image(
        self,
        asset_id: str,
        *,
        scope_id: str,
        detail: str,
        preview_max_edge: int,
        preview_quality: int,
        retention_hours: int,
    ) -> tuple[GeneratedImage, str] | None:
        """Load an original or lightweight preview from the temporary asset layer."""

        result = await self.load_workflow_image_detailed(
            asset_id,
            scope_id=scope_id,
            detail=detail,
            preview_max_edge=preview_max_edge,
            preview_quality=preview_quality,
            retention_hours=retention_hours,
        )
        if not result.ok or result.image is None:
            return None
        return result.image, result.internal_path

    async def load_workflow_image_detailed(
        self,
        asset_id: str,
        *,
        scope_id: str,
        detail: str,
        preview_max_edge: int,
        preview_quality: int,
        retention_hours: int,
    ) -> WorkflowImageLoadResult:
        """Load a scoped workflow asset while preserving its failure reason."""

        async with self._lock:
            return await asyncio.to_thread(
                self._load_workflow_image_detailed_sync,
                asset_id,
                scope_id,
                detail,
                preview_max_edge,
                preview_quality,
                retention_hours,
            )

    def _load_workflow_image_detailed_sync(
        self,
        asset_id: str,
        scope_id: str,
        detail: str,
        preview_max_edge: int,
        preview_quality: int,
        retention_hours: int,
    ) -> WorkflowImageLoadResult:
        clean_id = str(asset_id or "").strip().lower()
        normalized_scope = str(scope_id or "").strip()
        if not _SHA256_RE.fullmatch(clean_id):
            return WorkflowImageLoadResult(clean_id, "invalid_asset_id")
        if not normalized_scope:
            return WorkflowImageLoadResult(clean_id, "access_denied")
        now = time.time()
        retention_seconds = max(1, min(168, int(retention_hours))) * 3600
        with self._connect() as conn:
            lease = conn.execute(
                "SELECT expires_at, hard_expires_at FROM agent_asset_leases "
                "WHERE asset_id = ? AND scope_id = ?",
                (clean_id, normalized_scope),
            ).fetchone()
            row = conn.execute(
                "SELECT path, mime_type, size_bytes FROM image_assets WHERE id = ?",
                (clean_id,),
            ).fetchone()
        if row is None:
            return WorkflowImageLoadResult(clean_id, "not_found")
        if lease is None:
            return WorkflowImageLoadResult(clean_id, "access_denied")
        if float(lease["expires_at"]) <= now or float(lease["hard_expires_at"]) <= now:
            return WorkflowImageLoadResult(clean_id, "expired")
        original = self.data_dir / str(row["path"])
        if not original.is_file() or not _is_within(original, self.assets_dir):
            return WorkflowImageLoadResult(clean_id, "file_missing")
        try:
            raw = original.read_bytes()
        except FileNotFoundError:
            return WorkflowImageLoadResult(clean_id, "file_missing")
        if not _image_is_decodable(raw):
            return WorkflowImageLoadResult(clean_id, "decode_failed")
        mime_type = detect_mime_type(raw, str(row["mime_type"]))
        if detail == "original":
            image = GeneratedImage(data=raw, mime_type=mime_type)
        else:
            max_edge = max(256, min(2048, int(preview_max_edge)))
            quality = max(40, min(95, int(preview_quality)))
            thumbnail = self._prepare_thumbnail_sync(
                {
                    "id": clean_id,
                    "path": str(row["path"]),
                    "mime_type": mime_type,
                    "size_bytes": int(row["size_bytes"]),
                },
                max_edge=max_edge,
                quality=quality,
            )
            with self._connect() as conn:
                self._upsert_thumbnails_sync(conn, [thumbnail])
            preview_path = self.data_dir / thumbnail["path"]
            preview_data = preview_path.read_bytes()
            image = GeneratedImage(
                data=preview_data,
                mime_type=detect_mime_type(preview_data, "image/webp"),
            )
        with self._connect() as conn:
            conn.execute(
                "UPDATE agent_asset_leases SET last_accessed_at = ?, expires_at = ? "
                "WHERE asset_id = ? AND scope_id = ?",
                (
                    now,
                    min(now + retention_seconds, float(lease["hard_expires_at"])),
                    clean_id,
                    normalized_scope,
                ),
            )
        return WorkflowImageLoadResult(
            clean_id,
            "ok",
            image=image,
            internal_path=str(original.resolve(strict=False)),
        )

    def _delete_expired_leases_sync(self, now: float) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM agent_asset_leases WHERE expires_at <= ? OR hard_expires_at <= ?",
                (now, now),
            )
        return max(0, int(cursor.rowcount))

    async def record_success(
        self,
        *,
        provider: ImageProvider,
        request: GenerationRequest,
        images: tuple[GeneratedImage, ...],
        elapsed_ms: int,
        history: HistorySettings,
        preview_max_edge: int = 768,
        preview_quality: int = 80,
        batch_failures: tuple[tuple[int, str], ...] = (),
    ) -> str:
        """Persist a successful generation when history is enabled.

        Returns:
            A gallery generation ID, or an empty string when retention is off.
        """

        if not history.enabled:
            return ""
        async with self._lock:
            generation_id = await asyncio.to_thread(
                self._record_success_sync,
                provider,
                request,
                images,
                elapsed_ms,
                history.retain_reference_images,
                history.record_invocation_identity,
                preview_max_edge,
                preview_quality,
                batch_failures,
            )
            await asyncio.to_thread(self._cleanup_sync, history)
        return generation_id

    def _record_success_sync(
        self,
        provider: ImageProvider,
        request: GenerationRequest,
        images: tuple[GeneratedImage, ...],
        elapsed_ms: int,
        retain_references: bool,
        record_invocation_identity: bool,
        preview_max_edge: int,
        preview_quality: int,
        batch_failures: tuple[tuple[int, str], ...] = (),
    ) -> str:
        generation_id = uuid.uuid4().hex
        created_at = time.time()
        assets: dict[str, dict[str, Any]] = {}
        thumbnails: dict[str, dict[str, Any]] = {}
        image_rows: list[tuple[str, int, str]] = []
        reference_rows: list[tuple[str, int, str, str, int, int, str]] = []
        metadata: dict[str, dict[str, Any]] = {}
        try:
            for ordinal, image in enumerate(images):
                image_id = uuid.uuid4().hex
                asset = self._prepare_asset_sync(image.data, image.mime_type)
                assets[asset["id"]] = asset
                metadata[asset["id"]] = self._metadata_for_asset_sync(
                    asset["id"], image.data
                )
                thumbnails[asset["id"]] = self._prepare_thumbnail_sync(
                    asset,
                    max_edge=preview_max_edge,
                    quality=preview_quality,
                )
                image_rows.append((image_id, ordinal, asset["id"]))
            if retain_references:
                for ordinal, reference in enumerate(request.references):
                    reference_id = uuid.uuid4().hex
                    asset = self._prepare_asset_sync(
                        reference.data, reference.mime_type
                    )
                    assets[asset["id"]] = asset
                    metadata[asset["id"]] = self._metadata_for_asset_sync(
                        asset["id"], reference.data
                    )
                    thumbnails[asset["id"]] = self._prepare_thumbnail_sync(
                        asset,
                        max_edge=preview_max_edge,
                        quality=preview_quality,
                    )
                    suffix = _image_suffix(asset["mime_type"], reference.data)
                    reference_rows.append(
                        (
                            reference_id,
                            ordinal,
                            _safe_filename(reference.filename) or f"reference{suffix}",
                            asset["mime_type"],
                            asset["size_bytes"],
                            1,
                            asset["id"],
                        )
                    )
            parameters = _redact_sensitive(
                {
                    "negative_prompt": request.negative_prompt,
                    "size": request.size,
                    "count": request.count,
                    "native_batch_size": request.native_batch_size,
                    "max_concurrent_requests": request.max_concurrent_requests,
                    "parameters": {**request.parameters, **request.local_parameters},
                    "selection_source": request.selection_source,
                }
            )
            model = provider.get_model(request.model)
            denied: set[str] = set()
            for name, descriptor in model.parameters.items():
                if not parameter_flag(descriptor, "record_in_history"):
                    denied.update({name, str(descriptor.get("request_key") or name)})
            if "n" in denied:
                denied.add("count")
            parameters["parameters"] = {
                name: value
                for name, value in parameters["parameters"].items()
                if name not in denied
            }
            for name in denied & {"size", "count", "negative_prompt"}:
                parameters.pop(name, None)
            invocation = (
                request.invocation_source.public_dict()
                if record_invocation_identity
                else {
                    "context_type": "",
                    "platform_name": "",
                    "platform_id": "",
                    "group_id": "",
                    "group_name": "",
                    "user_id": "",
                    "user_name": "",
                }
            )
            with self._connect() as conn:
                conn.executemany(
                    "INSERT INTO image_assets (id, path, mime_type, size_bytes, width, height, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                    "path=excluded.path, width=excluded.width, height=excluded.height, file_state='available'",
                    [
                        (
                            asset["id"],
                            asset["path"],
                            asset["mime_type"],
                            asset["size_bytes"],
                            asset["width"],
                            asset["height"],
                            created_at,
                        )
                        for asset in assets.values()
                    ],
                )
                self._upsert_thumbnails_sync(conn, thumbnails.values())
                self._save_metadata_sync(conn, metadata)
                first_metadata = (
                    metadata.get(image_rows[0][2], {}) if image_rows else {}
                )
                final_prompt = str(
                    first_metadata.get("normalized", {}).get("prompt") or request.prompt
                )
                conn.execute(
                    "INSERT INTO generations (id, created_at, source, status, mode, provider_id, provider_name, "
                    "provider_kind, model, original_prompt, final_prompt, parameters_json, elapsed_ms, "
                    "context_type, platform_name, platform_id, group_id, group_name, user_id, user_name) "
                    "VALUES (?, ?, ?, 'succeeded', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        generation_id,
                        created_at,
                        request.source,
                        request.mode,
                        provider.id,
                        provider.name,
                        provider.kind,
                        request.model or provider.model,
                        request.prompt,
                        final_prompt,
                        json.dumps(
                            parameters, ensure_ascii=False, separators=(",", ":")
                        ),
                        elapsed_ms,
                        invocation["context_type"],
                        invocation["platform_name"],
                        invocation["platform_id"],
                        invocation["group_id"],
                        invocation["group_name"],
                        invocation["user_id"],
                        invocation["user_name"],
                    ),
                )
                conn.executemany(
                    "INSERT INTO generation_images (id, generation_id, ordinal, asset_id) "
                    "VALUES (?, ?, ?, ?)",
                    [
                        (image_id, generation_id, ordinal, asset_id)
                        for image_id, ordinal, asset_id in image_rows
                    ],
                )
                if reference_rows:
                    conn.executemany(
                        "INSERT INTO generation_references (id, generation_id, ordinal, filename, mime_type, size_bytes, available, asset_id) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        [
                            (reference_id, generation_id, *row)
                            for reference_id, *row in reference_rows
                        ],
                    )
                conn.execute(
                    "UPDATE generations SET generation_engine = ? WHERE id = ?",
                    (
                        "novelai" if provider.kind == "nai_direct" else provider.kind,
                        generation_id,
                    ),
                )
                if (
                    batch_failures
                    or request.count > request.native_batch_size
                    or len(images) != request.count
                ):
                    request_sizes = [
                        min(request.native_batch_size, request.count - offset)
                        for offset in range(0, request.count, request.native_batch_size)
                    ]
                    conn.execute(
                        "UPDATE generations SET supplemental_json = ? WHERE id = ?",
                        (
                            json.dumps(
                                {
                                    "batch": {
                                        "requested": request.count,
                                        "succeeded": len(images),
                                        "failed": len(batch_failures),
                                        "request_count": len(request_sizes),
                                        "request_sizes": request_sizes,
                                        "succeeded_requests": len(request_sizes)
                                        - len(batch_failures),
                                        "failed_requests": len(batch_failures),
                                        "returned_images": len(images),
                                        "failures": [
                                            {"index": index, "error": reason}
                                            for index, reason in batch_failures
                                        ],
                                    }
                                },
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                            generation_id,
                        ),
                    )
                self._refresh_search_sync(conn, generation_id)
                try:
                    conn.execute(
                        "INSERT INTO generation_search (generation_id, original_prompt, final_prompt, provider_name, model) VALUES (?, ?, ?, ?, ?)",
                        (
                            generation_id,
                            request.prompt,
                            final_prompt,
                            provider.name,
                            request.model or provider.model,
                        ),
                    )
                except sqlite3.OperationalError:
                    pass
            return generation_id
        except Exception:
            self._cleanup_orphaned_asset_files_sync()
            self._cleanup_orphaned_thumbnails_sync()
            raise

    @staticmethod
    def _import_edit_rows_sync(
        conn: sqlite3.Connection, generation_id: str, image_id: str = ""
    ) -> tuple[sqlite3.Row, list[sqlite3.Row]]:
        if not isinstance(generation_id, str) or not _SAFE_ID_RE.fullmatch(
            generation_id
        ):
            raise ValueError("导入记录 ID 无效")
        if image_id:
            # Modern imports carry complete per-image snapshots. Do not transfer
            # the group's potentially large manifest for an individual editor tab.
            record = conn.execute(
                "SELECT g.id, g.source, g.model, g.mode, g.original_prompt, "
                "g.generation_engine, g.generated_at, CASE WHEN EXISTS ("
                "SELECT 1 FROM generation_images i WHERE i.id = ? "
                "AND trim(i.supplemental_json) NOT IN ('', '{}', 'null')) "
                "THEN '{}' ELSE g.supplemental_json END AS supplemental_json "
                "FROM generations g WHERE g.id = ?",
                (image_id, generation_id),
            ).fetchone()
        else:
            record = conn.execute(
                "SELECT * FROM generations WHERE id = ?", (generation_id,)
            ).fetchone()
        if record is None:
            raise LookupError("导入记录不存在或已被删除")
        if record["source"] != "import":
            raise ValueError("只能编辑手动导入的图片记录")
        rows = conn.execute(
            "SELECT i.*, a.path, a.mime_type, a.size_bytes, a.id AS sha256, "
            "a.width, a.height, a.file_state, m.metadata_json, "
            "COALESCE(NULLIF(json_extract(i.supplemental_json, '$.original_filename'), ''), "
            "CASE WHEN trim(i.supplemental_json) IN ('', '{}', 'null') THEN "
            "(SELECT json_extract(g.supplemental_json, '$.original_filename') "
            "FROM generations g WHERE g.id = i.generation_id) END, i.id) AS original_filename, "
            "t.path AS thumbnail_path, t.mime_type AS thumbnail_mime_type, "
            f"{_THUMBNAIL_REVISION_SQL} AS thumbnail_revision "
            "FROM generation_images i JOIN image_assets a ON a.id = i.asset_id "
            "LEFT JOIN image_metadata m ON m.asset_id = i.asset_id "
            "LEFT JOIN image_thumbnails t ON t.asset_id = i.asset_id "
            "WHERE i.generation_id = ? "
            + ("AND i.id = ? " if image_id else "")
            + "ORDER BY i.ordinal, i.id",
            (generation_id, image_id) if image_id else (generation_id,),
        ).fetchall()
        if not rows:
            if image_id:
                raise LookupError("导入图片不存在或已被删除")
            raise ValueError("导入记录已无可编辑图片")
        return record, rows

    @staticmethod
    def _import_edit_revision(record: sqlite3.Row, rows: list[sqlite3.Row]) -> str:
        # Include stored JSON, not its display projection, so concurrent edits,
        # metadata repairs, appends and deletions cannot silently replace changes.
        snapshot = [
            record["id"],
            record["supplemental_json"],
            [
                [
                    row[key]
                    for key in (
                        "id",
                        "ordinal",
                        "asset_id",
                        "supplemental_json",
                        "metadata_json",
                    )
                ]
                for row in rows
            ],
        ]
        return hashlib.sha256(
            json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()

    @staticmethod
    def _import_edit_previous(record: sqlite3.Row, row: sqlite3.Row) -> dict[str, Any]:
        previous = _load_json(row["supplemental_json"]) or _load_json(
            record["supplemental_json"]
        )
        if not previous:
            # Very old single-image imports stored only record-level fields.
            previous = {
                "model": record["model"],
                "mode": record["mode"],
                "prompt": record["original_prompt"],
                "generation_engine": record["generation_engine"],
                "generated_at": record["generated_at"],
            }
        return previous

    @staticmethod
    def _import_edit_item_revision(record: sqlite3.Row, row: sqlite3.Row) -> str:
        snapshot = [
            record["id"],
            [
                row[key]
                for key in (
                    "id",
                    "ordinal",
                    "asset_id",
                    "supplemental_json",
                    "metadata_json",
                )
            ],
        ]
        if str(row["supplemental_json"] or "").strip() in {"", "{}", "null"}:
            snapshot.append(
                [
                    record[key]
                    for key in (
                        "supplemental_json",
                        "model",
                        "mode",
                        "original_prompt",
                        "generation_engine",
                        "generated_at",
                    )
                ]
            )
        return hashlib.sha256(
            json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()

    async def import_edit_snapshot(
        self,
        generation_id: str,
        *,
        light: bool = False,
        image_id: str = "",
        item_revision: str = "",
        include_preview: bool = True,
    ) -> dict[str, Any]:
        """Read an edit snapshot without changing files, metadata, or import receipts."""
        if image_id:
            if not _SAFE_ID_RE.fullmatch(image_id):
                raise ValueError("导入图片 ID 无效")
            if not _SHA256_RE.fullmatch(item_revision):
                raise ValueError("图片编辑版本无效，请重新打开编辑窗口")
        # The read transaction provides a coherent snapshot. Projection and
        # thumbnail IO must not hold the store's mutation lock.
        return await asyncio.to_thread(
            self._import_edit_snapshot_sync,
            generation_id,
            light,
            image_id,
            item_revision,
            include_preview,
        )

    def _import_edit_snapshot_sync(
        self,
        generation_id: str,
        light: bool = False,
        image_id: str = "",
        item_revision: str = "",
        include_preview: bool = True,
    ) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute("BEGIN")
            record, rows = self._import_edit_rows_sync(conn, generation_id, image_id)
            revision = "" if image_id else self._import_edit_revision(record, rows)
        if image_id:
            current_revision = self._import_edit_item_revision(record, rows[0])
            if item_revision != current_revision:
                raise ImportEditConflictError(
                    "图片已被修改或重新排序，请重新打开编辑窗口"
                )
        if light and not image_id:
            return {
                "generation_id": generation_id,
                "revision": revision,
                "items": [
                    {
                        "image_id": row["id"],
                        "filename": str(row["original_filename"]),
                        "sha256": row["sha256"],
                        "thumbnail_revision": row["thumbnail_revision"],
                        "size_bytes": row["size_bytes"],
                        "width": row["width"],
                        "height": row["height"],
                        "item_revision": self._import_edit_item_revision(record, row),
                    }
                    for row in rows
                ],
            }
        items = []
        for row in rows:
            previous = self._import_edit_previous(record, row)
            metadata = _load_json(row["metadata_json"])
            overrides = _import_edit_existing_overrides(previous, metadata)
            metadata = project_import_metadata(metadata, overrides)
            fields = _import_edit_fields(previous, metadata, overrides)
            items.append(
                {
                    "image_id": row["id"],
                    "filename": str(previous.get("original_filename") or row["id"]),
                    "sha256": row["sha256"],
                    "thumbnail_revision": row["thumbnail_revision"],
                    "size_bytes": row["size_bytes"],
                    "width": row["width"],
                    "height": row["height"],
                    "thumbnail_data_url": _path_data_url(
                        self.data_dir / str(row["thumbnail_path"] or ""),
                        str(row["thumbnail_mime_type"] or "image/webp"),
                    )
                    if include_preview
                    else "",
                    "metadata": metadata,
                    "fields": fields,
                    "parameters_json": json.dumps(
                        fields["parameters"], ensure_ascii=False, indent=2
                    ),
                    "edited_fields": list(overrides),
                    "output_node_id": str(
                        overrides.get("comfy_output_node")
                        or previous.get("comfy_output_node")
                        or metadata.get("normalized", {}).get("selected_output_node")
                        or ""
                    ),
                }
            )
        if image_id:
            items[0]["item_revision"] = current_revision
            return {
                "generation_id": generation_id,
                "item_revision": current_revision,
                "items": items,
            }
        return {"generation_id": generation_id, "revision": revision, "items": items}

    async def project_import_edit_image(
        self, generation_id: str, image_id: str, item_revision: str, output_node_id: str
    ) -> dict[str, Any]:
        """Project a stored workflow after validating the editor's item snapshot."""
        if not isinstance(image_id, str) or not _SAFE_ID_RE.fullmatch(image_id):
            raise ValueError("导入图片 ID 无效")
        if not isinstance(item_revision, str) or not _SHA256_RE.fullmatch(
            item_revision
        ):
            raise ValueError("图片编辑版本无效，请重新打开编辑窗口")
        if not isinstance(output_node_id, str):
            raise ValueError("ComfyUI 输出节点 ID 必须是字符串")
        return await asyncio.to_thread(
            self._project_import_edit_image_sync,
            generation_id,
            image_id,
            item_revision,
            output_node_id,
        )

    def _project_import_edit_image_sync(
        self, generation_id: str, image_id: str, item_revision: str, output_node_id: str
    ) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute("BEGIN")
            record, rows = self._import_edit_rows_sync(conn, generation_id, image_id)
        if self._import_edit_item_revision(record, rows[0]) != item_revision:
            raise ImportEditConflictError("图片已被修改或重新排序，请重新打开编辑窗口")
        metadata = project_import_metadata(
            _load_json(rows[0]["metadata_json"]), {"comfy_output_node": output_node_id}
        )
        return {key: value for key, value in metadata.items() if key != "raw"}

    async def edit_import(
        self, generation_id: str, revision: str, items: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Atomically update supplemental parameters and the complete image order."""
        if not isinstance(revision, str) or not _SHA256_RE.fullmatch(revision):
            raise ValueError("编辑版本无效，请重新打开编辑窗口")
        if not isinstance(items, list) or not 1 <= len(items) <= 100:
            raise ValueError("编辑记录需要 1 至 100 张图片")
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict) or set(item) - {"image_id", "overrides"}:
                raise ValueError("编辑项目格式无效")
            image_id = item.get("image_id")
            if (
                not isinstance(image_id, str)
                or not _SAFE_ID_RE.fullmatch(image_id)
                or image_id in seen
            ):
                raise ValueError("编辑图片 ID 无效或重复")
            seen.add(image_id)
            _validate_import_edit_overrides(item.get("overrides", {}))
        if len(json.dumps(items, ensure_ascii=False).encode()) > 4 * 1024 * 1024:
            raise ValueError("编辑参数不能超过 4 MB")
        async with self._lock:
            return await asyncio.to_thread(
                self._edit_import_sync, generation_id, revision, items
            )

    def _edit_import_sync(
        self, generation_id: str, revision: str, items: list[dict[str, Any]]
    ) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            record, rows = self._import_edit_rows_sync(conn, generation_id)
            if self._import_edit_revision(record, rows) != revision:
                raise ImportEditConflictError(
                    "记录已被修改、追加或删除图片，请重新打开编辑窗口后再保存"
                )
            by_id = {row["id"]: row for row in rows}
            if {item["image_id"] for item in items} != set(by_id):
                raise ValueError("编辑必须包含记录中的全部图片，不能增加或移除图片")
            prepared = []
            updates = []
            for ordinal, item in enumerate(items):
                image_id = item["image_id"]
                row = by_id[image_id]
                previous = self._import_edit_previous(record, row)
                metadata = _load_json(row["metadata_json"])
                overrides = _import_edit_existing_overrides(previous, metadata)
                changes = item.get("overrides", {})
                if changes:
                    overrides = {**overrides, **changes}
                    _validate_import_overrides(overrides)
                    supplemental = {
                        **previous,
                        **_import_supplemental(
                            str(previous.get("original_filename") or ""),
                            overrides,
                            metadata,
                        ),
                    }
                    encoded = json.dumps(
                        supplemental, ensure_ascii=False, separators=(",", ":")
                    )
                else:
                    # Reordering never rewrites an unchanged per-image snapshot.
                    supplemental = dict(previous)
                    encoded = row["supplemental_json"]
                metadata = project_import_metadata(metadata, overrides)
                fields = _import_edit_fields(supplemental, metadata, overrides)
                prepared.append(
                    (
                        image_id,
                        {
                            **supplemental,
                            **{key: fields[key] for key in _IMPORT_EDIT_FIELDS},
                        },
                        metadata,
                    )
                )
                updates.append((ordinal, encoded, image_id))
            if any(
                ordinal != by_id[image_id]["ordinal"]
                or encoded != by_id[image_id]["supplemental_json"]
                for ordinal, encoded, image_id in updates
            ):
                conn.executemany(
                    "UPDATE generation_images SET ordinal = ?, supplemental_json = ? WHERE id = ?",
                    updates,
                )
                self._update_import_summary_sync(
                    conn,
                    generation_id,
                    _load_json(record["supplemental_json"]),
                    prepared,
                )
                self._refresh_search_sync(conn, generation_id)
            record, rows = self._import_edit_rows_sync(conn, generation_id)
            return {
                "generation_id": generation_id,
                "revision": self._import_edit_revision(record, rows),
                "image_ids": [row["id"] for row in rows],
            }

    async def import_image(
        self,
        data: bytes,
        filename: str,
        overrides: dict[str, Any],
        *,
        import_key: str = "",
        preview_max_edge: int = 768,
        preview_quality: int = 80,
    ) -> dict[str, Any]:
        """Archive one image independently of automatic generation retention."""

        _validate_import_content(data)
        _validate_import_overrides(overrides)
        async with self._lock:
            return await asyncio.to_thread(
                self._import_image_sync,
                data,
                filename,
                overrides,
                str(import_key or "").strip()[:160],
                preview_max_edge,
                preview_quality,
            )

    def _import_image_sync(
        self,
        data: bytes,
        filename: str,
        overrides: dict[str, Any],
        import_key: str,
        preview_max_edge: int,
        preview_quality: int,
    ) -> dict[str, Any]:
        result = self._commit_import_batch_sync(
            [
                {
                    "data": data,
                    "filename": filename,
                    "overrides": overrides,
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            ],
            import_key or uuid.uuid4().hex,
            "separate",
            "",
            "",
            preview_max_edge,
            preview_quality,
        )
        return {
            "generation_id": result["generation_id"],
            "duplicate": result["duplicate"],
        }

    async def stage_import_file(self, path: Path, data: bytes) -> None:
        """Validate and stage an import while excluding concurrent maintenance."""

        async with self._lock:
            await asyncio.to_thread(self._stage_import_file_sync, path, data)

    def _stage_import_file_sync(self, path: Path, data: bytes) -> None:
        if not isinstance(path, Path) or not _is_within(path, self.imports_dir):
            raise ValueError("待导入图片路径不属于导入暂存目录")
        if path.resolve() == self.imports_dir.resolve():
            raise ValueError("待导入图片路径必须是文件路径")
        _validate_import_content(data)
        self._metadata_for_asset_sync(
            hashlib.sha256(data).hexdigest(), data, strict=True
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, data)

    async def import_group(
        self,
        entries: list[dict[str, Any]],
        *,
        import_key: str,
        preview_max_edge: int = 768,
        preview_quality: int = 80,
    ) -> dict[str, Any]:
        """Atomically archive an ordered group of staged images of the same model."""

        if not isinstance(entries, list) or not 1 <= len(entries) <= 100:
            raise ValueError("每个图组需要 1 至 100 张图片")
        if (
            not isinstance(import_key, str)
            or not import_key.strip()
            or len(import_key) > 160
        ):
            raise ValueError("图组导入请求标识无效")
        async with self._lock:
            return await asyncio.to_thread(
                self._import_group_sync,
                entries,
                import_key.strip(),
                preview_max_edge,
                preview_quality,
            )

    def _read_import_file_sync(self, path: Any) -> bytes:
        if not isinstance(path, (str, Path)):
            raise ValueError("待导入图片路径无效")
        source = Path(path)
        if not _is_within(source, self.imports_dir) or not source.is_file():
            raise ValueError("待导入图片不存在或不属于导入暂存目录")
        with source.open("rb") as stream:
            data = stream.read(30 * 1024 * 1024 + 1)
        _validate_import_content(data)
        return data

    def _import_group_sync(
        self,
        entries: list[dict[str, Any]],
        import_key: str,
        preview_max_edge: int,
        preview_quality: int,
    ) -> dict[str, Any]:
        result = self._commit_import_batch_sync(
            self._declare_import_entries_sync(entries),
            import_key,
            "group",
            "",
            "",
            preview_max_edge,
            preview_quality,
        )
        return {
            "generation_id": result["generation_id"],
            "duplicate": result["duplicate"],
        }

    def _declare_import_entries_sync(
        self, entries: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        declared = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("导入项目必须为对象")
            item = dict(entry)
            if "sha256" not in item:
                try:
                    item["sha256"] = hashlib.sha256(
                        self._read_import_file_sync(item.get("path"))
                    ).hexdigest()
                except (ValueError, OSError) as exc:
                    raise ValueError(
                        f"{item.get('filename') or '待导入图片'}：{exc}"
                    ) from exc
            declared.append(item)
        return declared

    def _prepare_import_entries_sync(
        self, entries: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        prepared: list[dict[str, Any]] = []
        errors: list[str] = []
        # First pass retains metadata only, so a large batch never holds every image in RAM.
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                errors.append(f"第 {index + 1} 张：导入项目必须是对象")
                continue
            filename = str(entry.get("filename") or f"第 {index + 1} 张图片")[:512]
            try:
                overrides = entry.get("overrides", {})
                _validate_import_overrides(overrides)
                data = (
                    entry.get("data")
                    if isinstance(entry.get("data"), bytes)
                    else self._read_import_file_sync(entry.get("path"))
                )
                _validate_import_content(data)
                digest = hashlib.sha256(data).hexdigest()
                if digest != entry.get("sha256"):
                    raise ValueError("实际图片 SHA-256 与声明不一致，请重新选择图片")
                metadata = self._metadata_for_asset_sync(digest, data, strict=True)
                supplemental = _import_supplemental(filename, overrides, metadata)
                prepared.append(
                    {
                        "path": entry.get("path"),
                        "data": entry.get("data"),
                        "digest": digest,
                        "metadata": metadata,
                        "supplemental": supplemental,
                    }
                )
                del data
            except (ValueError, OSError) as exc:
                errors.append(f"第 {index + 1} 张（{filename}）：{exc}")
        if errors:
            raise ValueError("图组导入失败：" + "；".join(errors))
        return prepared

    def _prepare_import_assets_sync(
        self,
        prepared: list[dict[str, Any]],
        preview_max_edge: int,
        preview_quality: int,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        assets: dict[str, dict[str, Any]] = {}
        thumbnails: dict[str, dict[str, Any]] = {}
        for item in prepared:
            if item["digest"] in assets:
                continue
            data = (
                item.get("data")
                if isinstance(item.get("data"), bytes)
                else self._read_import_file_sync(item["path"])
            )
            if hashlib.sha256(data).hexdigest() != item["digest"]:
                raise ValueError("待导入图片在准备过程中发生变化，请重新上传")
            asset = self._prepare_asset_sync(data, "")
            assets[asset["id"]] = asset
            thumbnails[asset["id"]] = self._prepare_thumbnail_sync(
                asset,
                max_edge=preview_max_edge,
                quality=preview_quality,
            )
        return assets, thumbnails

    async def list_import_merge_targets(
        self, engine: str, *, limit: int = 24, offset: int = 0, query: str = ""
    ) -> dict[str, Any]:
        """List imported records whose surviving images all have the requested source."""

        engine = _known_merge_engine(engine)
        async with self._lock:
            return await asyncio.to_thread(
                self._list_generations_sync,
                {
                    "source": "import",
                    "_merge_target_engine": engine,
                    "limit": limit,
                    "offset": offset,
                    "query": query,
                },
            )

    @staticmethod
    def _merge_target_sync(
        conn: sqlite3.Connection, target_id: str, expected_engine: str
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        row = conn.execute(
            "SELECT * FROM generations WHERE id = ?", (target_id,)
        ).fetchone()
        if row is None:
            raise ValueError("目标导入图组不存在或已被删除，请重新选择")
        target = dict(row)
        if target["source"] != "import":
            raise ValueError("只能合并到手动导入的图片或图组")
        images = [
            dict(item)
            for item in conn.execute(
                "SELECT id, ordinal, asset_id, supplemental_json FROM generation_images "
                "WHERE generation_id = ? ORDER BY ordinal, id",
                (target_id,),
            ).fetchall()
        ]
        if not images:
            raise ValueError("目标导入图组已没有图片，请重新选择")
        mismatched = []
        for index, image in enumerate(images):
            supplemental = _load_json(image["supplemental_json"])
            engine = _canonical_engine(
                supplemental.get("generation_engine", target["generation_engine"])
            )
            if engine != expected_engine:
                mismatched.append(f"第 {index + 1} 张：{engine}")
        if mismatched:
            raise ValueError(
                "目标图组来源不一致或已发生变化，不能合并：" + "；".join(mismatched)
            )
        return target, images

    async def append_import_group(
        self,
        entries: list[dict[str, Any]],
        target_id: str,
        *,
        import_key: str,
        expected_engine: str,
        preview_max_edge: int = 768,
        preview_quality: int = 80,
    ) -> dict[str, Any]:
        """Append a same-source batch to an existing import, with durable retry receipts."""

        if not isinstance(entries, list) or not 1 <= len(entries) <= 100:
            raise ValueError("每次合并需要 1 至 100 张图片")
        if not isinstance(target_id, str) or not _SAFE_ID_RE.fullmatch(target_id):
            raise ValueError("目标导入图组 ID 无效")
        if (
            not isinstance(import_key, str)
            or not import_key.strip()
            or len(import_key) > 160
        ):
            raise ValueError("合并导入请求标识无效")
        engine = _known_merge_engine(expected_engine)
        async with self._lock:
            return await asyncio.to_thread(
                self._append_import_group_sync,
                entries,
                target_id,
                import_key.strip(),
                engine,
                preview_max_edge,
                preview_quality,
            )

    def _append_import_group_sync(
        self,
        entries: list[dict[str, Any]],
        target_id: str,
        import_key: str,
        expected_engine: str,
        preview_max_edge: int,
        preview_quality: int,
    ) -> dict[str, Any]:
        result = self._commit_import_batch_sync(
            self._declare_import_entries_sync(entries),
            import_key,
            "merge",
            target_id,
            expected_engine,
            preview_max_edge,
            preview_quality,
        )
        return {
            key: result[key]
            for key in ("generation_id", "duplicate", "added", "image_count")
        }

    async def check_import_hashes(self, hashes: list[str]) -> dict[str, Any]:
        """Check exact gallery membership and duplicates within an import selection."""

        hashes = _validate_import_hashes(hashes)
        async with self._lock:
            return await asyncio.to_thread(self._check_import_hashes_sync, hashes)

    def _check_import_hashes_sync(self, hashes: list[str]) -> dict[str, Any]:
        with self._connect() as conn:
            try:
                self._assert_import_hashes_available(conn, hashes)
            except ImportDuplicateError as exc:
                return exc.as_dict()
        return {"allowed": True, "duplicate_hashes": []}

    @staticmethod
    def _assert_import_hashes_available(
        conn: sqlite3.Connection, hashes: list[str]
    ) -> None:
        seen: set[str] = set()
        repeated: set[str] = set()
        for digest in hashes:
            if digest in seen:
                repeated.add(digest)
            seen.add(digest)
        placeholders = ",".join("?" for _ in seen)
        existing = {
            str(row[0])
            for row in conn.execute(
                "SELECT DISTINCT i.asset_id FROM generation_images i JOIN generations g ON g.id=i.generation_id "
                f"WHERE i.asset_id IN ({placeholders}) AND (g.source!='external' OR EXISTS ("
                "SELECT 1 FROM external_records e JOIN external_sources s ON s.id=e.source_id "
                "WHERE e.generation_id=g.id AND e.available=1 AND s.enabled=1))",
                list(seen),
            ).fetchall()
        }
        duplicates = [
            digest for digest in hashes if digest in existing or digest in repeated
        ]
        if duplicates:
            raise ImportDuplicateError(duplicates, batch=bool(repeated))

    async def commit_import_batch(
        self,
        entries: list[dict[str, Any]],
        *,
        import_key: str,
        mode: str = "separate",
        target_id: str = "",
        expected_engine: str = "",
        preview_max_edge: int = 768,
        preview_quality: int = 80,
    ) -> dict[str, Any]:
        """Commit separate imports, a new group, or an append in one database transaction."""

        if not isinstance(entries, list) or not 1 <= len(entries) <= 100:
            raise ValueError("每次导入需要 1 至 100 张图片")
        for entry in entries:
            if not isinstance(entry, dict) or "data" in entry:
                raise ValueError("批量导入项目必须使用已暂存的图片")
            path = entry.get("path")
            if not isinstance(path, (str, Path)) or not _is_within(
                Path(path), self.imports_dir
            ):
                raise ValueError("待导入图片路径不属于导入暂存目录")
        async with self._lock:
            return await asyncio.to_thread(
                self._commit_import_batch_sync,
                entries,
                import_key,
                mode,
                target_id,
                expected_engine,
                preview_max_edge,
                preview_quality,
            )

    def _import_batch_receipt_sync(
        self, conn: sqlite3.Connection, import_key: str, fingerprint: str
    ) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT fingerprint, result_json FROM import_batches WHERE id = ? AND expires_at > ?",
            (import_key, time.time()),
        ).fetchone()
        if row is None:
            return None
        if row["fingerprint"] != fingerprint:
            raise ValueError("导入请求标识已用于其他图片、顺序或参数，请重新准备导入")
        result = _load_json(row["result_json"])
        ids = result.get("generation_ids") or []
        placeholders = ",".join("?" for _ in ids)
        count = (
            int(
                conn.execute(
                    f"SELECT COUNT(*) FROM generation_images WHERE generation_id IN ({placeholders})",
                    ids,
                ).fetchone()[0]
            )
            if ids
            else 0
        )
        return {**result, "duplicate": True, "added": 0, "image_count": count}

    async def get_import_batch_result(self, import_key: str) -> dict[str, Any] | None:
        """Recover a successful import response after an API process restart."""

        if not isinstance(import_key, str) or not import_key or len(import_key) > 160:
            return None
        async with self._lock:
            return await asyncio.to_thread(
                self._get_import_batch_result_sync, import_key
            )

    def _get_import_batch_result_sync(self, import_key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT result_json FROM import_batches WHERE id = ? AND expires_at > ?",
                (import_key, time.time()),
            ).fetchone()
        return _load_json(row["result_json"]) if row is not None else None

    def _commit_import_batch_sync(
        self,
        entries: list[dict[str, Any]],
        import_key: str,
        mode: str,
        target_id: str,
        expected_engine: str,
        preview_max_edge: int,
        preview_quality: int,
    ) -> dict[str, Any]:
        if (
            not isinstance(import_key, str)
            or not import_key.strip()
            or len(import_key) > 160
        ):
            raise ValueError("导入请求标识无效")
        import_key = import_key.strip()
        if mode not in {"separate", "group", "merge"}:
            raise ValueError("导入方式无效")
        if not isinstance(entries, list) or not 1 <= len(entries) <= 100:
            raise ValueError("每次导入需要 1 至 100 张图片")
        if mode == "merge":
            if not isinstance(target_id, str) or not _SAFE_ID_RE.fullmatch(target_id):
                raise ValueError("目标导入图组 ID 无效")
            expected_engine = _known_merge_engine(expected_engine)
        elif target_id or expected_engine:
            raise ValueError("只有合并导入可以指定目标和预期来源")
        if any(not isinstance(entry, dict) for entry in entries):
            raise ValueError("导入项目必须是对象")
        hashes = _validate_import_hashes([entry.get("sha256") for entry in entries])
        descriptors = []
        for entry, digest in zip(entries, hashes, strict=True):
            _validate_import_overrides(entry.get("overrides", {}))
            descriptors.append(
                {
                    "sha256": digest,
                    "filename": str(entry.get("filename") or ""),
                    "overrides": entry.get("overrides", {}),
                }
            )
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "mode": mode,
                    "target_id": target_id,
                    "expected_engine": expected_engine,
                    "entries": descriptors,
                },
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        with self._connect() as conn:
            previous = self._import_batch_receipt_sync(conn, import_key, fingerprint)
            if previous is not None:
                return previous
        prepared = self._prepare_import_entries_sync(entries)
        if mode == "group":
            models = {item["supplemental"]["model"] for item in prepared}
            if "" in models or len(models) != 1:
                descriptions = [
                    f"第 {index + 1} 张（{item['supplemental']['original_filename']}）：{item['supplemental']['model'] or '未填写模型'}"
                    for index, item in enumerate(prepared)
                ]
                raise ValueError(
                    "图组中的模型必须全部填写且完全相同。" + "；".join(descriptions)
                )
        if mode == "merge":
            mismatched = [
                f"第 {index + 1} 张（{item['supplemental']['original_filename']}）：{item['supplemental']['generation_engine']}"
                for index, item in enumerate(prepared)
                if item["supplemental"]["generation_engine"] != expected_engine
            ]
            if mismatched:
                raise ValueError(
                    "待合并图片必须全部具有相同的已知来源：" + "；".join(mismatched)
                )
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                previous = self._import_batch_receipt_sync(
                    conn, import_key, fingerprint
                )
                if previous is not None:
                    return previous
                self._assert_import_hashes_available(conn, hashes)
                target = None
                existing_images: list[dict[str, Any]] = []
                if mode == "merge":
                    target, existing_images = self._merge_target_sync(
                        conn, target_id, expected_engine
                    )
                    if len(existing_images) + len(prepared) > 100:
                        raise ValueError(
                            f"目标图组已有 {len(existing_images)} 张，追加 {len(prepared)} 张后超过 100 张上限"
                        )
                elif conn.execute(
                    "SELECT 1 FROM generations WHERE import_key = ?", (import_key,)
                ).fetchone():
                    raise ValueError("该导入请求标识已被旧请求使用，请重新准备导入")
                # Hold SQLite's writer reservation during preparation as well as insertion.
                assets, thumbnails = self._prepare_import_assets_sync(
                    prepared, preview_max_edge, preview_quality
                )
                now = time.time()
                conn.executemany(
                    "INSERT INTO image_assets (id, path, mime_type, size_bytes, width, height, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET path=excluded.path, file_state='available'",
                    [
                        (
                            asset["id"],
                            asset["path"],
                            asset["mime_type"],
                            asset["size_bytes"],
                            asset["width"],
                            asset["height"],
                            now,
                        )
                        for asset in assets.values()
                    ],
                )
                self._upsert_thumbnails_sync(conn, thumbnails.values())
                self._save_metadata_sync(
                    conn, {item["digest"]: item["metadata"] for item in prepared}
                )
                if target is not None:
                    next_ordinal = (
                        max(int(item["ordinal"]) for item in existing_images) + 1
                    )
                    self._insert_import_images_sync(
                        conn, target_id, prepared, next_ordinal
                    )
                    modes = {
                        _load_json(item["supplemental_json"]).get(
                            "mode", target["mode"]
                        )
                        for item in existing_images
                    } | {item["supplemental"]["mode"] for item in prepared}
                    conn.execute(
                        "UPDATE generations SET mode = ?, generation_engine = ? WHERE id = ?",
                        (
                            next(iter(modes)) if len(modes) == 1 else "unknown",
                            expected_engine,
                            target_id,
                        ),
                    )
                    self._refresh_search_sync(conn, target_id)
                    generation_ids = [target_id]
                else:
                    groups = (
                        [[item] for item in prepared]
                        if mode == "separate"
                        else [prepared]
                    )
                    generation_ids = [
                        self._insert_import_record_sync(
                            conn,
                            group,
                            import_key if len(groups) == 1 else None,
                            grouped=mode == "group",
                        )
                        for group in groups
                    ]
                result = {
                    "mode": mode,
                    "generation_ids": generation_ids,
                    "generation_id": generation_ids[0]
                    if len(generation_ids) == 1
                    else "",
                    "duplicate": False,
                    "added": len(prepared),
                    "image_count": len(existing_images) + len(prepared),
                }
                conn.execute(
                    "INSERT INTO import_batches (id, fingerprint, result_json, expires_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET fingerprint=excluded.fingerprint, result_json=excluded.result_json, expires_at=excluded.expires_at",
                    (
                        import_key,
                        fingerprint,
                        json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                        now + 24 * 3600,
                    ),
                )
            return result
        except Exception:
            with self._connect() as cleanup_conn:
                cleanup_conn.execute("BEGIN IMMEDIATE")
                self._cleanup_orphaned_asset_files_sync()
                self._cleanup_orphaned_thumbnails_sync()
            raise

    def _insert_import_record_sync(
        self,
        conn: sqlite3.Connection,
        prepared: list[dict[str, Any]],
        import_key: str | None,
        *,
        grouped: bool,
    ) -> str:
        generation_id = uuid.uuid4().hex
        first = prepared[0]["supplemental"]
        modes = {item["supplemental"]["mode"] for item in prepared}
        engines = {item["supplemental"]["generation_engine"] for item in prepared}
        supplemental = dict(first)
        if grouped:
            supplemental.update(
                is_import_group=True,
                group_manifest=[item["digest"] for item in prepared],
                group_output_nodes=[
                    item["supplemental"]["overrides"].get("comfy_output_node", "")
                    for item in prepared
                ],
            )
        conn.execute(
            "INSERT INTO generations (id, created_at, source, status, mode, provider_id, provider_name, provider_kind, model, original_prompt, final_prompt, parameters_json, elapsed_ms, generation_engine, generated_at, supplemental_json, import_key) "
            "VALUES (?, ?, 'import', 'succeeded', ?, '', '', '', ?, ?, ?, '{}', 0, ?, ?, ?, ?)",
            (
                generation_id,
                time.time(),
                first["mode"] if len(modes) == 1 else "unknown",
                first["model"],
                first["prompt"],
                str(
                    prepared[0]["metadata"].get("normalized", {}).get("prompt")
                    or first["prompt"]
                ),
                first["generation_engine"] if len(engines) == 1 else "mixed",
                first["generated_at"],
                json.dumps(supplemental, ensure_ascii=False, separators=(",", ":")),
                import_key,
            ),
        )
        self._insert_import_images_sync(conn, generation_id, prepared, 0)
        self._refresh_search_sync(conn, generation_id)
        return generation_id

    @staticmethod
    def _insert_import_images_sync(
        conn: sqlite3.Connection,
        generation_id: str,
        prepared: list[dict[str, Any]],
        next_ordinal: int,
    ) -> None:
        conn.executemany(
            "INSERT INTO generation_images (id, generation_id, ordinal, asset_id, supplemental_json) VALUES (?, ?, ?, ?, ?)",
            [
                (
                    uuid.uuid4().hex,
                    generation_id,
                    next_ordinal + index,
                    item["digest"],
                    json.dumps(
                        item["supplemental"], ensure_ascii=False, separators=(",", ":")
                    ),
                )
                for index, item in enumerate(prepared)
            ],
        )

    def _delete_expired_import_batches_sync(self, now: float) -> int:
        with self._connect() as conn:
            return max(
                0,
                conn.execute(
                    "DELETE FROM import_batches WHERE expires_at <= ?", (now,)
                ).rowcount,
            )

    async def list_generations(self, filters: dict[str, Any]) -> dict[str, Any]:
        """Return a paginated gallery collection and aggregate filter values."""

        return await asyncio.to_thread(self._list_generations_sync, filters)

    @staticmethod
    def _gallery_filters(filters: dict[str, Any]) -> tuple[str, list[Any]]:
        where: list[str] = [
            "(g.source != 'external' OR EXISTS (SELECT 1 FROM external_records e "
            "JOIN external_sources s ON s.id=e.source_id WHERE e.generation_id=g.id "
            "AND s.enabled=1 AND e.available=1 AND NOT EXISTS (SELECT 1 FROM generation_images local_image "
            "JOIN generations local_generation ON local_generation.id=local_image.generation_id "
            "WHERE local_image.asset_id=e.asset_id AND local_generation.source != 'external')))"
        ]
        args: list[Any] = []
        query = str(filters.get("query") or "").strip()[:240]
        for key, plural, limit in (
            ("provider_id", "provider_ids", 64),
            ("mode", "modes", 20),
            ("source", "sources", 30),
            ("generation_engine", "generation_engines", 80),
        ):
            if plural in filters:
                values = filters[plural]
                if isinstance(values, str):
                    if len(values) > 32768:
                        raise ValueError(f"{plural} 筛选内容过长")
                    try:
                        values = json.loads(values)
                    except (ValueError, RecursionError) as exc:
                        raise ValueError(f"{plural} 必须是 JSON 字符串数组") from exc
                if not isinstance(values, list) or len(values) > 256:
                    raise ValueError(f"{plural} 必须是最多 256 项的字符串数组")
                if any(
                    not isinstance(value, str) or len(value) > limit for value in values
                ):
                    raise ValueError(f"{plural} 每项必须是最多 {limit} 字符的字符串")
                values = list(dict.fromkeys(value.strip() for value in values))
            else:
                value = str(filters.get(key) or "").strip()[:limit]
                if (
                    not value
                    or key == "mode"
                    and value not in {"text2img", "img2img", "unknown"}
                ):
                    continue
                values = [value]
            if not values:
                where.append("0 = 1")
                continue
            column = f"g.{key}"
            if key == "generation_engine":
                values = list(
                    dict.fromkeys(_canonical_engine(value) for value in values)
                )
                if "novelai" in values:
                    values.append("nai")
                column = "CASE WHEN lower(trim(g.generation_engine)) IN ('nai', 'novelai') THEN lower(trim(g.generation_engine)) ELSE g.generation_engine END"
            where.append(f"{column} IN ({','.join('?' for _ in values)})")
            args.extend(values)
        if filters.get("_merge_target_engine"):
            where.append(
                "EXISTS (SELECT 1 FROM generation_images selected WHERE selected.generation_id = g.id) "
                "AND NOT EXISTS (SELECT 1 FROM generation_images selected WHERE selected.generation_id = g.id "
                "AND (CASE lower(trim(COALESCE(json_extract(selected.supplemental_json, '$.generation_engine'), g.generation_engine))) "
                "WHEN 'nai' THEN 'novelai' WHEN 'novelai' THEN 'novelai' "
                "ELSE trim(COALESCE(json_extract(selected.supplemental_json, '$.generation_engine'), g.generation_engine)) END) != ?)"
            )
            args.append(filters["_merge_target_engine"])
        if filters.get("favorite") in (True, 1, "1", "true"):
            where.append("g.is_favorite = 1")
        if query:
            where.append(
                "(g.original_prompt LIKE ? OR g.final_prompt LIKE ? OR g.provider_name LIKE ? OR g.model LIKE ? "
                "OR g.platform_name LIKE ? OR g.platform_id LIKE ? OR g.group_id LIKE ? "
                "OR g.group_name LIKE ? OR g.user_id LIKE ? OR g.user_name LIKE ? "
                "OR g.search_text LIKE ? OR g.generation_engine LIKE ?)"
            )
            token = f"%{query}%"
            args.extend([token] * 12)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        return clause, args

    def _list_generations_sync(self, filters: dict[str, Any]) -> dict[str, Any]:
        revision = self._gallery_revision_sync()
        limit = max(1, min(60, _as_int(filters.get("limit"), 24)))
        offset = max(0, _as_int(filters.get("offset"), 0))
        clause, args = self._gallery_filters(filters)
        with self._connect() as conn:
            total = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM generations g {clause}", args
                ).fetchone()[0]
            )
            rows = conn.execute(
                f"SELECT g.id, g.created_at, g.source, g.mode, g.provider_id, g.provider_name, "
                f"g.model, g.original_prompt, g.elapsed_ms, g.context_type, g.platform_name, "
                f"g.platform_id, g.group_id, g.group_name, g.user_id, g.user_name, "
                f"g.is_favorite, g.cleanup_protected_until, g.generation_engine, g.generated_at, "
                f"i.id AS image_id, t.path AS thumbnail_path, "
                f"a.id AS sha256, {_THUMBNAIL_REVISION_SQL} AS thumbnail_revision, "
                f"t.mime_type AS thumbnail_mime_type, a.size_bytes, a.mime_type, a.file_state, "
                f"(SELECT COUNT(*) FROM generation_images counted WHERE counted.generation_id = g.id) AS image_count "
                f"FROM generations g JOIN generation_images i ON i.generation_id = g.id "
                f"AND i.id = (SELECT cover.id FROM generation_images cover WHERE cover.generation_id = g.id ORDER BY cover.ordinal, cover.id LIMIT 1) "
                f"JOIN image_assets a ON a.id = i.asset_id "
                f"LEFT JOIN image_thumbnails t ON t.asset_id = a.id "
                f"{clause} ORDER BY g.created_at DESC, g.id DESC LIMIT ? OFFSET ?",
                [*args, limit, offset],
            ).fetchall()
            provider_options = [
                dict(row)
                for row in conn.execute(
                    "SELECT provider_id AS id, MAX(provider_name) AS name FROM generations WHERE provider_id != '' GROUP BY provider_id ORDER BY name"
                ).fetchall()
            ]
            engines = sorted(
                {
                    _canonical_engine(row[0])
                    for row in conn.execute(
                        "SELECT DISTINCT generation_engine FROM generations ORDER BY generation_engine"
                    ).fetchall()
                }
            )
        items = [self._gallery_item(dict(row)) for row in rows]
        return {
            "items": items,
            "revision": revision,
            "total": total,
            "offset": offset,
            "limit": limit,
            "filters": {
                "providers": provider_options,
                "modes": ["text2img", "img2img", "unknown"],
                "sources": ["webui", "command", "llm_tool", "import", "external"],
                "generation_engines": engines,
            },
        }

    async def generation_detail(
        self, generation_id: str, *, include_assets: bool = True, light: bool = False
    ) -> dict[str, Any] | None:
        """Return a generation plus its result and reference assets."""

        if light:
            return await asyncio.to_thread(
                self._generation_manifest_sync, generation_id
            )
        return await asyncio.to_thread(
            self._generation_detail_sync, generation_id, include_assets
        )

    def _generation_manifest_sync(self, generation_id: str) -> dict[str, Any] | None:
        if not _SAFE_ID_RE.fullmatch(generation_id):
            return None
        if not self._external_generation_enabled_sync(generation_id):
            return None
        with self._connect() as conn:
            conn.execute("BEGIN")
            record = conn.execute(
                "SELECT id, created_at, source, status, mode, provider_id, provider_name, "
                "provider_kind, model, elapsed_ms, error_message, is_favorite, cleanup_protected_until, "
                "generation_engine, generated_at, context_type, platform_name, platform_id, "
                "group_id, group_name, user_id, user_name FROM generations WHERE id = ?",
                (generation_id,),
            ).fetchone()
            if record is None:
                return None
            rows = conn.execute(
                "SELECT i.id, i.generation_id, i.ordinal, a.mime_type, a.size_bytes, "
                "a.id AS sha256, a.width, a.height, a.file_state, a.path, "
                f"{_THUMBNAIL_REVISION_SQL} AS thumbnail_revision, "
                "json_extract(i.supplemental_json, '$.model') AS model, "
                "json_extract(i.supplemental_json, '$.mode') AS mode "
                "FROM generation_images i JOIN image_assets a ON a.id = i.asset_id "
                "LEFT JOIN image_thumbnails t ON t.asset_id = a.id "
                "WHERE i.generation_id = ? ORDER BY i.ordinal, i.id",
                (generation_id,),
            ).fetchall()
            references = conn.execute(
                "SELECT r.id, r.filename, r.mime_type, r.size_bytes, r.available, r.deleted_at, "
                "a.file_state FROM generation_references r LEFT JOIN image_assets a ON a.id = r.asset_id "
                "WHERE r.generation_id = ? ORDER BY r.ordinal, r.id",
                (generation_id,),
            ).fetchall()
        result = self._generation_header(dict(record))
        result["lightweight"] = True
        result["images"] = []
        used_stems: set[str] = set()
        for index, row in enumerate(rows, start=1):
            item = dict(row)
            item["allowed_actions"] = dict(result["allowed_actions"])
            for key in ("model", "mode"):
                if item[key] is None:
                    item[key] = result[key]
            item["download_filename"] = export_image_filename(
                {**result, **item},
                image_index=index,
                image_count=len(rows),
                mime_type=item["mime_type"],
                suffix=Path(item.pop("path")).suffix,
                used_stems=used_stems,
            )
            result["images"].append(item)
        result["references"] = [
            {
                **dict(row),
                "available": bool(row["available"])
                and row["file_state"] == "available",
            }
            for row in references
        ]
        return result

    def _generation_header(self, result: dict[str, Any]) -> dict[str, Any]:
        result.update(
            self._external_display_sync(
                str(result.get("id") or ""), result.get("source")
            )
        )
        if "parameters_json" in result:
            result["parameters"] = _load_json(result.pop("parameters_json"))
        if "supplemental_json" in result:
            result["supplemental"] = _canonical_supplemental(
                _load_json(result.pop("supplemental_json"))
            )
        result["generation_engine"] = _canonical_engine(result["generation_engine"])
        result["is_favorite"] = bool(result["is_favorite"])
        result.pop("search_text", None)
        result.pop("import_key", None)
        result["invocation_source"] = {
            key: str(result.pop(key, "") or "")
            for key in (
                "context_type",
                "platform_name",
                "platform_id",
                "group_id",
                "group_name",
                "user_id",
                "user_name",
            )
        }
        return result

    async def gallery_image_info(
        self, image_id: str, *, include_preview: bool = True
    ) -> dict[str, Any] | None:
        if not _SAFE_ID_RE.fullmatch(image_id):
            return None
        return await asyncio.to_thread(
            self._gallery_image_info_sync, image_id, include_preview
        )

    def _gallery_image_info_sync(
        self,
        image_id: str,
        include_preview: bool = True,
        connection: sqlite3.Connection | None = None,
        *,
        include_group_manifest: bool = False,
    ) -> dict[str, Any] | None:
        with (
            nullcontext(connection)
            if connection is not None
            else self._connect() as conn
        ):
            if connection is None:
                conn.execute("BEGIN")
            row = conn.execute(
                "SELECT i.*, a.path, a.mime_type, a.size_bytes, a.id AS sha256, "
                "a.width, a.height, a.file_state, m.metadata_json, "
                "t.path AS thumbnail_path, t.mime_type AS thumbnail_mime_type, "
                f"{_THUMBNAIL_REVISION_SQL} AS thumbnail_revision "
                "FROM generation_images i JOIN image_assets a ON a.id = i.asset_id "
                "LEFT JOIN image_metadata m ON m.asset_id = i.asset_id "
                "LEFT JOIN image_thumbnails t ON t.asset_id = i.asset_id WHERE i.id = ?",
                (image_id,),
            ).fetchone()
            if row is None:
                return None
            if not self._external_generation_enabled_sync(row["generation_id"]):
                return None
            record = conn.execute(
                "SELECT id, created_at, source, status, mode, provider_id, provider_name, "
                "provider_kind, model, original_prompt, final_prompt, parameters_json, "
                "supplemental_json, elapsed_ms, error_message, is_favorite, "
                "cleanup_protected_until, generation_engine, generated_at, context_type, "
                "platform_name, platform_id, group_id, group_name, user_id, user_name "
                "FROM generations WHERE id = ?",
                (row["generation_id"],),
            ).fetchone()
            position = conn.execute(
                "SELECT COUNT(*) AS image_count, COALESCE(SUM(ordinal < ?), 0) + 1 AS image_index "
                "FROM generation_images WHERE generation_id = ?",
                (row["ordinal"], row["generation_id"]),
            ).fetchone()
        detail = self._generation_header(dict(record))
        if not include_group_manifest:
            detail["supplemental"].pop("group_manifest", None)
        image = self._asset_item(
            dict(row), preview_full=False, include_preview=include_preview
        )
        if detail["source"] == "import" and not image["supplemental"]:
            image["supplemental"] = detail["supplemental"]
        image["generation_id"] = row["generation_id"]
        image["allowed_actions"] = dict(detail["allowed_actions"])
        image["ordinal"] = row["ordinal"]
        image["download_filename"] = export_image_filename(
            {
                **detail,
                **{
                    key: image["supplemental"].get(key, detail[key])
                    for key in ("mode", "model")
                },
            },
            image_index=position["image_index"],
            image_count=position["image_count"],
            mime_type=image["mime_type"],
            suffix=Path(image["path"]).suffix,
            used_stems=set(),
        )
        return {"image": image, "detail_fields": detail}

    async def generation_image_context(
        self, generation_id: str, image_id: str = ""
    ) -> dict[str, Any] | None:
        """Read one image's parameter context without image or thumbnail bytes."""
        if not isinstance(generation_id, str) or not _SAFE_ID_RE.fullmatch(
            generation_id
        ):
            return None
        if image_id and (
            not isinstance(image_id, str) or not _SAFE_ID_RE.fullmatch(image_id)
        ):
            raise ValueError("所选图片不属于当前生成记录或已被删除")
        return await asyncio.to_thread(
            self._generation_image_context_sync, generation_id, image_id
        )

    def _generation_image_context_sync(
        self, generation_id: str, image_id: str
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.execute("BEGIN")
            if (
                conn.execute(
                    "SELECT 1 FROM generations WHERE id = ?", (generation_id,)
                ).fetchone()
                is None
            ):
                return None
            selected = conn.execute(
                "SELECT id FROM generation_images WHERE generation_id = ? "
                + ("AND id = ? " if image_id else "")
                + "ORDER BY ordinal, id LIMIT 1",
                (generation_id, image_id) if image_id else (generation_id,),
            ).fetchone()
            if selected is None:
                raise ValueError("所选图片不属于当前生成记录或已被删除")
            payload = self._gallery_image_info_sync(
                str(selected["id"]), False, conn, include_group_manifest=True
            )
            if payload is None:
                raise ValueError("所选图片不属于当前生成记录或已被删除")
            references = conn.execute(
                "SELECT r.*, a.path, a.file_state FROM generation_references r "
                "LEFT JOIN image_assets a ON a.id = r.asset_id "
                "WHERE r.generation_id = ? ORDER BY r.ordinal, r.id",
                (generation_id,),
            ).fetchall()
        return {
            **payload["detail_fields"],
            "images": [payload["image"]],
            "references": [
                self._reference_item(dict(row), include_data=False)
                for row in references
            ],
        }

    async def gallery_reference_image(self, reference_id: str) -> dict[str, Any] | None:
        if not _SAFE_ID_RE.fullmatch(reference_id):
            return None
        return await asyncio.to_thread(self._gallery_reference_image_sync, reference_id)

    def _gallery_reference_image_sync(self, reference_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT r.*, a.path, a.file_state, t.path AS thumbnail_path, "
                "t.mime_type AS thumbnail_mime_type FROM generation_references r "
                "LEFT JOIN image_assets a ON a.id = r.asset_id "
                "LEFT JOIN image_thumbnails t ON t.asset_id = r.asset_id WHERE r.id = ?",
                (reference_id,),
            ).fetchone()
        return self._reference_item(dict(row)) if row is not None else None

    async def gallery_image_sequence(
        self, filters: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Return lightweight image cursors in the active gallery order."""

        return await asyncio.to_thread(self._gallery_image_sequence_sync, filters)

    def _gallery_image_sequence_sync(
        self, filters: dict[str, Any]
    ) -> list[dict[str, Any]]:
        clause, args = self._gallery_filters(filters)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT g.id AS generation_id, g.created_at, g.source, "
                "COALESCE(json_extract(i.supplemental_json, '$.mode'), g.mode) AS mode, g.provider_id, "
                "COALESCE(json_extract(i.supplemental_json, '$.model'), g.model) AS model, "
                "i.id AS image_id, i.ordinal, a.mime_type, a.size_bytes, "
                f"a.id AS sha256, {_THUMBNAIL_REVISION_SQL} AS thumbnail_revision, "
                "a.width, a.height, a.path, a.file_state, "
                "COUNT(*) OVER (PARTITION BY g.id) AS image_count "
                "FROM generations g JOIN generation_images i ON i.generation_id = g.id "
                "JOIN image_assets a ON a.id = i.asset_id "
                "LEFT JOIN image_thumbnails t ON t.asset_id = a.id "
                f"{clause} ORDER BY g.created_at DESC, g.id DESC, i.ordinal ASC",
                args,
            ).fetchall()
        used_stems: dict[str, set[str]] = {}
        generation_positions: dict[str, int] = {}
        image_positions: dict[str, int] = {}
        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            generation_id = str(item["generation_id"])
            item.update(self._external_display_sync(generation_id, item.pop("source")))
            if generation_id not in generation_positions:
                generation_positions[generation_id] = len(generation_positions)
            used = used_stems.setdefault(generation_id, set())
            position = image_positions.get(generation_id, 0)
            image_positions[generation_id] = position + 1
            item["download_filename"] = export_image_filename(
                item,
                image_index=position + 1,
                image_count=int(item["image_count"]),
                mime_type=str(item.get("mime_type") or ""),
                suffix=Path(str(item.pop("path", ""))).suffix,
                used_stems=used,
            )
            item.pop("ordinal")
            item["image_index"] = position
            item["generation_position"] = generation_positions[generation_id]
            item["width"] = max(1, int(item.get("width") or 1))
            item["height"] = max(1, int(item.get("height") or 1))
            items.append(item)
        return items

    async def gallery_image_data(
        self, image_id: str, *, detail: str
    ) -> dict[str, Any] | None:
        """Load one gallery result as a preview or original data URL."""

        if not _SAFE_ID_RE.fullmatch(image_id):
            return None
        return await asyncio.to_thread(self._gallery_image_data_sync, image_id, detail)

    async def gallery_image_file(self, image_id: str) -> tuple[Path, str, str] | None:
        """Resolve one generated image for an authenticated download."""

        if not _SAFE_ID_RE.fullmatch(image_id):
            return None
        return await asyncio.to_thread(self._gallery_image_file_sync, image_id)

    def _gallery_image_file_sync(
        self, image_id: str, action: str = "download"
    ) -> tuple[Path, str, str] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT a.path, a.mime_type, i.generation_id, g.created_at, g.provider_id, "
                "COALESCE(json_extract(i.supplemental_json, '$.model'), g.model) AS model, "
                "COALESCE(json_extract(i.supplemental_json, '$.mode'), g.mode) AS mode, "
                "(SELECT COUNT(*) FROM generation_images n WHERE n.generation_id = i.generation_id) AS image_count, "
                "(SELECT COUNT(*) + 1 FROM generation_images n WHERE n.generation_id = i.generation_id "
                "AND n.ordinal < i.ordinal) AS image_index "
                "FROM generation_images i JOIN generations g ON g.id = i.generation_id "
                "JOIN image_assets a ON a.id = i.asset_id WHERE i.id = ?",
                (image_id,),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        path = self._resolve_image_asset_sync(item)
        if path is None:
            return None
        self._assert_external_action_sync([str(item["generation_id"])], action)
        return (
            path,
            str(item["mime_type"]),
            export_image_filename(
                item,
                image_index=item["image_index"],
                image_count=item["image_count"],
                mime_type=item["mime_type"],
                suffix=path.suffix,
                used_stems=set(),
            ),
        )

    def _gallery_image_data_sync(
        self, image_id: str, detail: str
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT i.id AS image_id, i.generation_id, i.ordinal, a.path, "
                "(SELECT COUNT(*) FROM generation_images previous WHERE previous.generation_id = i.generation_id "
                "AND previous.ordinal < i.ordinal) AS image_index, "
                "a.mime_type, a.size_bytes, a.width, a.height, t.path AS thumbnail_path, "
                f"a.id AS sha256, {_THUMBNAIL_REVISION_SQL} AS thumbnail_revision, "
                "t.mime_type AS thumbnail_mime_type, t.size_bytes AS thumbnail_size_bytes "
                "FROM generation_images i JOIN image_assets a ON a.id = i.asset_id "
                "JOIN image_thumbnails t ON t.asset_id = a.id WHERE i.id = ?",
                (image_id,),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        if not self._external_generation_enabled_sync(item["generation_id"]):
            return None
        original = self._resolve_image_asset_sync(item)
        thumbnail = self.data_dir / str(item["thumbnail_path"])
        use_original = detail == "original"
        path = original if use_original else thumbnail
        mime_type = str(
            item["mime_type"] if use_original else item["thumbnail_mime_type"]
        )
        size_bytes = int(
            item["size_bytes"] if use_original else item["thumbnail_size_bytes"]
        )
        if (
            path is None
            or not path.is_file()
            or (not use_original and not _is_within(path, self.thumbnails_dir))
        ):
            return None
        return {
            "image_id": str(item["image_id"]),
            "allowed_actions": self._allowed_actions_for_generation_sync(
                str(item["generation_id"])
            ),
            "sha256": str(item["sha256"]),
            "thumbnail_revision": str(item["thumbnail_revision"]),
            "generation_id": str(item["generation_id"]),
            "image_index": int(item["image_index"]),
            "mime_type": mime_type,
            "size_bytes": size_bytes,
            "width": max(1, int(item.get("width") or 1)),
            "height": max(1, int(item.get("height") or 1)),
            "data_url": _path_data_url(path, mime_type),
        }

    async def stage_generation_references(
        self, generation_id: str
    ) -> list[dict[str, str]]:
        """Copy retained references into the transient input area for reproduction."""

        if not _SAFE_ID_RE.fullmatch(generation_id):
            return []
        return await asyncio.to_thread(
            self._stage_generation_references_sync, generation_id
        )

    def _stage_generation_references_sync(
        self, generation_id: str
    ) -> list[dict[str, str]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT r.id, r.filename, a.path, a.mime_type "
                "FROM generation_references r JOIN image_assets a ON a.id = r.asset_id "
                "WHERE r.generation_id = ? AND r.available = 1 "
                "AND a.file_state = 'available' ORDER BY r.ordinal",
                (generation_id,),
            ).fetchall()
        staged: list[dict[str, str]] = []
        for row in rows:
            source = self.data_dir / str(row["path"])
            if not source.is_file() or not _is_within(source, self.assets_dir):
                continue
            raw = source.read_bytes()
            staging_id = uuid.uuid4().hex
            mime_type = detect_mime_type(raw, str(row["mime_type"]))
            suffix = _image_suffix(mime_type, raw)
            _atomic_write(self.staging_dir / f"{staging_id}{suffix}", raw)
            staged.append(
                {
                    "id": staging_id,
                    "filename": str(row["filename"]),
                    "mime_type": mime_type,
                    "preview_data_url": image_data_url(raw, mime_type),
                }
            )
        return staged

    def _generation_detail_sync(
        self, generation_id: str, include_assets: bool = True
    ) -> dict[str, Any] | None:
        if not _SAFE_ID_RE.fullmatch(generation_id):
            return None
        if not self._external_generation_enabled_sync(generation_id):
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM generations WHERE id = ?", (generation_id,)
            ).fetchone()
            if row is None:
                return None
            image_rows = conn.execute(
                "SELECT i.*, a.path, a.mime_type, a.size_bytes, a.id AS sha256, "
                "a.width, a.height, a.file_state, m.metadata_json, "
                "t.path AS thumbnail_path, t.mime_type AS thumbnail_mime_type, "
                f"{_THUMBNAIL_REVISION_SQL} AS thumbnail_revision "
                "FROM generation_images i "
                "JOIN image_assets a ON a.id = i.asset_id "
                "LEFT JOIN image_thumbnails t ON t.asset_id = a.id "
                "LEFT JOIN image_metadata m ON m.asset_id = a.id "
                "WHERE i.generation_id = ? ORDER BY i.ordinal",
                (generation_id,),
            ).fetchall()
            reference_rows = conn.execute(
                "SELECT r.*, a.path, a.file_state, t.path AS thumbnail_path, "
                "t.mime_type AS thumbnail_mime_type FROM generation_references r "
                "LEFT JOIN image_assets a ON a.id = r.asset_id "
                "LEFT JOIN image_thumbnails t ON t.asset_id = a.id "
                "WHERE r.generation_id = ? ORDER BY r.ordinal",
                (generation_id,),
            ).fetchall()
        result = self._generation_header(dict(row))
        result["images"] = [
            self._asset_item(dict(item), preview_full=include_assets)
            for item in image_rows
        ]
        used_stems: set[str] = set()
        for image_index, image in enumerate(result["images"], start=1):
            image["allowed_actions"] = dict(result["allowed_actions"])
            if result["source"] == "import" and not image["supplemental"]:
                image["supplemental"] = result["supplemental"]
            image["download_filename"] = export_image_filename(
                {
                    **result,
                    "mode": image["supplemental"].get("mode", result["mode"]),
                    "model": image["supplemental"].get("model", result["model"]),
                },
                image_index=image_index,
                image_count=len(result["images"]),
                mime_type=str(image.get("mime_type") or ""),
                suffix=Path(str(image.get("path") or "")).suffix,
                used_stems=used_stems,
            )
        result["references"] = [
            self._reference_item(dict(item), include_data=include_assets)
            for item in reference_rows
        ]
        return result

    async def set_favorite(self, generation_id: str, favorite: bool) -> dict[str, Any]:
        """Protect the complete generation or grant 24 hours after unfavoriting."""

        if not _SAFE_ID_RE.fullmatch(generation_id):
            raise ValueError("生成记录 ID 无效")
        return await self._external_mutation(
            self._set_favorite_sync, generation_id, bool(favorite)
        )

    def _set_favorite_sync(self, generation_id: str, favorite: bool) -> dict[str, Any]:
        self._assert_external_action_sync([generation_id], "favorite")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT is_favorite, cleanup_protected_until, source FROM generations WHERE id = ?",
                (generation_id,),
            ).fetchone()
            if row is None:
                raise ValueError("生成记录不存在")
            protected = float(row["cleanup_protected_until"])
            if favorite:
                protected = 0.0
            elif row["is_favorite"] and row["source"] not in {"import", "external"}:
                protected = time.time() + 24 * 3600
            conn.execute(
                "UPDATE generations SET is_favorite = ?, cleanup_protected_until = ? WHERE id = ?",
                (int(favorite), protected, generation_id),
            )
        return {
            "id": generation_id,
            "is_favorite": favorite,
            "cleanup_protected_until": protected,
        }

    async def favorite_status(self, generation_ids: list[str]) -> dict[str, Any]:
        """Read favorite states for a selection spanning gallery pages."""

        ids = _validate_generation_selection(generation_ids)
        async with self._lock:
            return await asyncio.to_thread(self._favorite_status_sync, ids)

    def _favorite_status_sync(self, generation_ids: list[str]) -> dict[str, Any]:
        with self._connect() as conn:
            return _favorite_summary(self._selected_favorite_rows(conn, generation_ids))

    @staticmethod
    def _selected_favorite_rows(
        conn: sqlite3.Connection, generation_ids: list[str]
    ) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in generation_ids)
        rows = {
            str(row["id"]): dict(row)
            for row in conn.execute(
                "SELECT id, is_favorite, cleanup_protected_until, source FROM generations "
                f"WHERE id IN ({placeholders})",
                generation_ids,
            ).fetchall()
        }
        missing = [
            identifier for identifier in generation_ids if identifier not in rows
        ]
        if missing:
            raise ValueError("以下生成记录不存在或已被删除：" + ", ".join(missing))
        return [rows[identifier] for identifier in generation_ids]

    async def toggle_favorites(self, generation_ids: list[str]) -> dict[str, Any]:
        """Favorite missing selections, or unfavorite when all are already favorites."""

        ids = _validate_generation_selection(generation_ids)
        return await self._external_mutation(self._toggle_favorites_sync, ids)

    def _toggle_favorites_sync(self, generation_ids: list[str]) -> dict[str, Any]:
        self._assert_external_action_sync(generation_ids, "favorite")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = self._selected_favorite_rows(conn, generation_ids)
            favorite = not all(bool(row["is_favorite"]) for row in rows)
            changed_ids: list[str] = []
            protected_until = time.time() + 24 * 3600
            for row in rows:
                if bool(row["is_favorite"]) == favorite:
                    continue
                protected = (
                    protected_until
                    if not favorite and row["source"] not in {"import", "external"}
                    else 0.0
                )
                conn.execute(
                    "UPDATE generations SET is_favorite = ?, cleanup_protected_until = ? WHERE id = ?",
                    (int(favorite), protected, row["id"]),
                )
                row.update(is_favorite=int(favorite), cleanup_protected_until=protected)
                changed_ids.append(str(row["id"]))
        return {
            **_favorite_summary(rows),
            "action": "favorite" if favorite else "unfavorite",
            "changed_ids": changed_ids,
        }

    async def delete_images(
        self, generation_id: str, image_ids: list[str]
    ) -> dict[str, Any]:
        """Atomically delete selected result links without changing the request snapshot."""

        if not _SAFE_ID_RE.fullmatch(generation_id):
            raise ValueError("生成记录 ID 无效")
        if not isinstance(image_ids, list) or not image_ids or len(image_ids) > 100:
            raise ValueError("请选择要删除的图片")
        if any(
            not isinstance(item, str) or not _SAFE_ID_RE.fullmatch(item)
            for item in image_ids
        ):
            raise ValueError("图片 ID 无效")
        return await self._external_mutation(
            self._delete_images_sync, generation_id, list(dict.fromkeys(image_ids))
        )

    def _delete_images_sync(
        self, generation_id: str, image_ids: list[str]
    ) -> dict[str, Any]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, asset_id FROM generation_images WHERE generation_id = ?",
                (generation_id,),
            ).fetchall()
            owned = {str(row["id"]): str(row["asset_id"]) for row in rows}
            invalid = [item for item in image_ids if item not in owned]
            if invalid:
                raise ValueError(
                    "以下图片不存在或不属于本次生成：" + ", ".join(invalid)
                )
            external_error = self._delete_external_original_sync(generation_id)
            asset_ids = [owned[item] for item in image_ids]
            conn.executemany(
                "DELETE FROM generation_images WHERE id = ? AND generation_id = ?",
                [(item, generation_id) for item in image_ids],
            )
            remaining = len(owned) - len(image_ids)
            if remaining == 0:
                asset_ids.extend(
                    str(row[0])
                    for row in conn.execute(
                        "SELECT asset_id FROM generation_references WHERE generation_id = ? AND asset_id IS NOT NULL",
                        (generation_id,),
                    ).fetchall()
                )
                conn.execute("DELETE FROM generations WHERE id = ?", (generation_id,))
                try:
                    conn.execute(
                        "DELETE FROM generation_search WHERE generation_id = ?",
                        (generation_id,),
                    )
                except sqlite3.OperationalError:
                    pass
            else:
                self._refresh_search_sync(conn, generation_id)
        self._purge_unreferenced_assets_sync(asset_ids)
        if external_error is not None:
            raise external_error
        return {
            "deleted": image_ids,
            "remaining": remaining,
            "generation_deleted": remaining == 0,
        }

    async def delete_generation(self, generation_id: str) -> bool:
        """Delete one generation and every plugin-owned result/reference file."""

        if not _SAFE_ID_RE.fullmatch(generation_id):
            return False
        return await self._external_mutation(
            self._delete_generation_sync, generation_id
        )

    async def delete_generations(self, generation_ids: list[str]) -> dict[str, Any]:
        """Validate all source permissions before deleting any selected record."""
        ids = _validate_generation_selection(generation_ids)
        return await self._external_mutation(self._delete_generations_sync, ids)

    def _delete_generations_sync(self, generation_ids: list[str]) -> dict[str, Any]:
        self._assert_external_action_sync(generation_ids, "delete")
        result: dict[str, Any] = {"deleted": [], "failed": [], "errors": []}
        for generation_id in generation_ids:
            try:
                if self._delete_generation_sync(generation_id):
                    result["deleted"].append(generation_id)
                else:
                    result["failed"].append(generation_id)
                    result["errors"].append(
                        {"id": generation_id, "message": "记录不存在或已删除"}
                    )
            except ExternalDeleteError as exc:
                details = exc.as_dict()
                result["deleted" if details["generation_deleted"] else "failed"].append(
                    generation_id
                )
                result["errors"].append({**details, "id": generation_id})
            except (ValueError, OSError) as exc:
                result["failed"].append(generation_id)
                result["errors"].append({"id": generation_id, "message": str(exc)})
        return result

    def _delete_generation_sync(self, generation_id: str) -> bool:
        with self._connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM generations WHERE id = ?", (generation_id,)
            ).fetchone()[0]
            if not count:
                return False
            external_error = self._delete_external_original_sync(generation_id)
            asset_rows = conn.execute(
                "SELECT asset_id FROM generation_images WHERE generation_id = ? "
                "UNION SELECT asset_id FROM generation_references "
                "WHERE generation_id = ? AND asset_id IS NOT NULL",
                (generation_id, generation_id),
            ).fetchall()
            conn.execute("DELETE FROM generations WHERE id = ?", (generation_id,))
            try:
                conn.execute(
                    "DELETE FROM generation_search WHERE generation_id = ?",
                    (generation_id,),
                )
            except sqlite3.OperationalError:
                pass
        self._purge_unreferenced_assets_sync(
            [str(row["asset_id"]) for row in asset_rows if row["asset_id"]]
        )
        if external_error is not None:
            raise external_error
        return True

    async def delete_reference(self, reference_id: str) -> bool:
        """Delete one retained reference without deleting its parent history record."""

        if not _SAFE_ID_RE.fullmatch(reference_id):
            return False
        async with self._lock:
            return await asyncio.to_thread(self._delete_reference_sync, reference_id)

    def _delete_reference_sync(self, reference_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT asset_id, available FROM generation_references WHERE id = ?",
                (reference_id,),
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                "UPDATE generation_references SET available = 0, deleted_at = ?, asset_id = NULL WHERE id = ?",
                (time.time(), reference_id),
            )
        if row["asset_id"]:
            self._purge_unreferenced_assets_sync([str(row["asset_id"])])
        return True

    async def export_generations(self, generation_ids: list[str]) -> Path:
        """Build a flat ZIP containing each result and its own metadata JSON."""

        valid_ids = [
            item for item in generation_ids if _SAFE_ID_RE.fullmatch(str(item or ""))
        ][:200]
        if not valid_ids:
            raise ValueError("没有可导出的生成记录")
        return await self._external_mutation(self._export_generations_sync, valid_ids)

    def _export_generations_sync(self, generation_ids: list[str]) -> Path:
        import zipfile

        self._assert_external_action_sync(generation_ids, "download")
        stamp = time.strftime("%Y%m%d_%H%M%S")
        target = self.exports_dir / f"image_studio_{stamp}_{uuid.uuid4().hex[:8]}.zip"
        used_stems: set[str] = set()
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for generation_id in generation_ids:
                detail = self._generation_detail_sync(
                    generation_id, include_assets=False
                )
                if not detail:
                    continue
                images = detail["images"]
                for image_index, image in enumerate(images, start=1):
                    path = self._resolve_image_asset_sync(
                        {**image, "generation_id": generation_id}
                    )
                    if path is not None:
                        image_filename = export_image_filename(
                            {
                                **detail,
                                "mode": image.get("supplemental", {}).get(
                                    "mode", detail["mode"]
                                ),
                                "model": image.get("supplemental", {}).get(
                                    "model", detail["model"]
                                ),
                            },
                            image_index=image_index,
                            image_count=len(images),
                            mime_type=image["mime_type"],
                            suffix=path.suffix,
                            used_stems=used_stems,
                        )
                        stem = Path(image_filename).stem
                        archive.write(path, arcname=image_filename)
                        metadata = {
                            key: value
                            for key, value in detail.items()
                            if key not in {"images", "references"}
                        }
                        supplemental = image.get("supplemental") or {}
                        if detail["source"] == "import" and supplemental:
                            metadata.update(
                                {
                                    "model": supplemental.get("model", detail["model"]),
                                    "mode": supplemental.get("mode", detail["mode"]),
                                    "generation_engine": supplemental.get(
                                        "generation_engine", detail["generation_engine"]
                                    ),
                                    "original_prompt": supplemental.get(
                                        "prompt", detail["original_prompt"]
                                    ),
                                    "final_prompt": image.get("metadata", {})
                                    .get("normalized", {})
                                    .get("prompt", supplemental.get("prompt", "")),
                                    "generated_at": supplemental.get("generated_at"),
                                    "supplemental": supplemental,
                                }
                            )
                        metadata["image"] = {
                            "id": image["id"],
                            "filename": image_filename,
                            "mime_type": image["mime_type"],
                            "size_bytes": image["size_bytes"],
                            "sha256": image["sha256"],
                            "metadata": image.get("metadata", {}),
                            "width": image.get("width"),
                            "height": image.get("height"),
                            "supplemental": supplemental,
                        }
                        archive.writestr(
                            f"{stem}.json",
                            json.dumps(metadata, ensure_ascii=False, indent=2),
                        )
        return target

    async def cleanup_exports(self) -> None:
        """Remove stale export archives after one hour."""

        await asyncio.to_thread(self._cleanup_exports_sync)

    def _cleanup_exports_sync(self) -> None:
        cutoff = time.time() - 3600
        for path in self.exports_dir.glob("*.zip"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue

    def _gallery_item(self, row: dict[str, Any]) -> dict[str, Any]:
        thumbnail = self.data_dir / str(row.get("thumbnail_path") or "")
        invocation_source = {
            key: str(row.get(key) or "")
            for key in (
                "context_type",
                "platform_name",
                "platform_id",
                "group_id",
                "group_name",
                "user_id",
                "user_name",
            )
        }
        return {
            **self._external_display_sync(row["id"], row["source"]),
            "id": row["id"],
            "created_at": row["created_at"],
            "source": row["source"],
            "generation_engine": _canonical_engine(row["generation_engine"]),
            "is_favorite": bool(row["is_favorite"]),
            "cleanup_protected_until": row["cleanup_protected_until"],
            "generated_at": row["generated_at"],
            "file_state": row["file_state"],
            "image_count": row["image_count"],
            "mode": row["mode"],
            "provider_id": row["provider_id"],
            "provider_name": row["provider_name"],
            "model": row["model"],
            "prompt_preview": row["original_prompt"][:180],
            "elapsed_ms": row["elapsed_ms"],
            "image_id": row["image_id"],
            "sha256": row["sha256"],
            "thumbnail_revision": row["thumbnail_revision"],
            "mime_type": row["mime_type"],
            "size_bytes": row["size_bytes"],
            "invocation_source": invocation_source,
            "thumbnail_data_url": _path_data_url(
                thumbnail, str(row.get("thumbnail_mime_type") or "image/webp")
            ),
        }

    def _asset_item(
        self, row: dict[str, Any], *, preview_full: bool, include_preview: bool = True
    ) -> dict[str, Any]:
        path = self._resolve_image_asset_sync(row) if preview_full else None
        thumbnail = self.data_dir / str(row.get("thumbnail_path") or "")
        preview = _path_data_url(path, row["mime_type"]) if path is not None else ""
        supplemental = _canonical_supplemental(
            _load_json(row.get("supplemental_json") or "{}")
        )
        metadata = _load_json(row.get("metadata_json") or "{}")
        try:
            metadata = project_import_metadata(
                metadata, supplemental.get("overrides") or {}
            )
        except ValueError as exc:
            metadata = {
                **metadata,
                "warnings": [*(metadata.get("warnings") or []), str(exc)],
            }
        return {
            "id": row["id"],
            "path": row["path"],
            "mime_type": row["mime_type"],
            "size_bytes": row["size_bytes"],
            "sha256": row["sha256"],
            "thumbnail_revision": row.get("thumbnail_revision", ""),
            "width": row.get("width", 1),
            "height": row.get("height", 1),
            "file_state": row.get("file_state", "available"),
            "metadata": metadata,
            "supplemental": supplemental,
            "data_url": preview,
            # Keep summaries lightweight while allowing the carousel to render
            # every result before original assets arrive.
            "thumbnail_data_url": (
                _path_data_url(
                    thumbnail, str(row.get("thumbnail_mime_type") or "image/webp")
                )
                if not preview_full and include_preview
                else ""
            ),
        }

    def _reference_item(
        self, row: dict[str, Any], *, include_data: bool = True
    ) -> dict[str, Any]:
        path = self.data_dir / str(row.get("path") or "")
        thumbnail = self.data_dir / str(row.get("thumbnail_path") or "")
        available = (
            bool(row["available"])
            and row.get("file_state") != "unavailable"
            and path.is_file()
            and _is_within(path, self.assets_dir)
        )
        return {
            "id": row["id"],
            "filename": row["filename"],
            "mime_type": row["mime_type"],
            "size_bytes": row["size_bytes"],
            "available": available,
            "deleted_at": row["deleted_at"],
            "data_url": (
                _path_data_url(
                    thumbnail if thumbnail.is_file() else path,
                    str(row.get("thumbnail_mime_type") or row["mime_type"]),
                )
                if available and include_data
                else ""
            ),
        }

    def _cleanup_sync(self, settings: HistorySettings) -> None:
        while True:
            status = self._retention_status_sync(settings)
            if not status["over_limit"] or not status["candidate_ids"]:
                break
            if not self._delete_generation_sync(status["candidate_ids"][0]):
                break

    async def retention_status(self, history: HistorySettings) -> dict[str, Any]:
        """Return global quota accounting and the same candidates used by cleanup."""

        async with self._lock:
            return await asyncio.to_thread(self._retention_status_sync, history)

    async def gallery_retention_status(
        self, history: HistorySettings
    ) -> dict[str, Any]:
        """Briefly reuse display-only quota accounting across gallery requests."""
        async with self._retention_cache_lock:
            revision = await self.gallery_revision()
            now = time.time()
            key = (history, revision)
            cached = self._retention_cache
            if cached is not None and cached[0] == key and cached[1] <= now < cached[2]:
                return copy.deepcopy(cached[3])
            async with self._lock:
                status, started_at, expires_at, revision = await asyncio.to_thread(
                    self._gallery_retention_snapshot_sync, history
                )
            if revision:
                self._retention_cache = (
                    (history, revision),
                    started_at,
                    expires_at,
                    status,
                )
            else:
                self._retention_cache = None
            return copy.deepcopy(status)

    def _gallery_retention_snapshot_sync(
        self, history: HistorySettings
    ) -> tuple[dict[str, Any], float, float, str]:
        revision = self._gallery_revision_sync()
        now = time.time()
        status = self._retention_status_sync(history)
        with self._connect() as conn:
            protected_until = conn.execute(
                "SELECT MIN(cleanup_protected_until) FROM generations "
                "WHERE source NOT IN ('import','external') AND is_favorite = 0 AND cleanup_protected_until > ?",
                (now,),
            ).fetchone()[0]
        expires_at = now + _GALLERY_RETENTION_CACHE_SECONDS
        if protected_until is not None:
            expires_at = min(expires_at, float(protected_until))
        # An external writer can commit despite the store's asyncio lock.
        # Never retain a calculation that straddled such a database change.
        if self._gallery_revision_sync() != revision:
            revision = ""
        return status, now, expires_at, revision

    def _retention_status_sync(self, history: HistorySettings) -> dict[str, Any]:
        now = time.time()
        with self._connect() as conn:
            count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM generations WHERE source NOT IN ('import','external') AND is_favorite = 0"
                ).fetchone()[0]
            )
            protected = int(
                conn.execute(
                    "SELECT COUNT(*) FROM generations WHERE source NOT IN ('import','external') "
                    "AND is_favorite = 0 AND cleanup_protected_until > ?",
                    (now,),
                ).fetchone()[0]
            )
            candidates = [
                str(row[0])
                for row in conn.execute(
                    "SELECT id FROM generations WHERE source NOT IN ('import','external') AND is_favorite = 0 "
                    "AND cleanup_protected_until <= ? ORDER BY created_at ASC, id ASC LIMIT 10",
                    (now,),
                ).fetchall()
            ]
            exempt = conn.execute(
                "SELECT SUM(CASE WHEN source = 'import' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN is_favorite = 1 THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN source = 'import' OR is_favorite = 1 THEN 1 ELSE 0 END) FROM generations WHERE source != 'external'"
            ).fetchone()
        usage = self._history_asset_usage_sync()
        size_bytes = usage["counted_size_bytes"]
        limit_records = max(0, history.max_records)
        limit_bytes = max(0, history.max_megabytes) * 1024 * 1024
        near = (limit_records > 0 and count >= limit_records * 0.9) or (
            limit_bytes > 0 and size_bytes >= limit_bytes * 0.9
        )
        over = (limit_records > 0 and count > limit_records) or (
            limit_bytes > 0 and size_bytes > limit_bytes
        )
        return {
            "record_count": count,
            "limit_records": limit_records,
            "size_bytes": size_bytes,
            "limit_bytes": limit_bytes,
            "near_limit": bool(near),
            "over_limit": bool(over),
            "candidate_ids": candidates if near else [],
            "protected_records": protected,
            "imported_records": int(exempt[0] or 0),
            "favorite_records": int(exempt[1] or 0),
            "exempt_record_count": int(exempt[2] or 0),
            "exempt_size_bytes": usage["exempt_size_bytes"],
            "total_records": count + int(exempt[2] or 0),
            "gallery_size_bytes": size_bytes + usage["exempt_size_bytes"],
            "total_size_bytes": self._storage_stats_sync()["size_bytes"],
        }

    def _prepare_asset_sync(self, data: bytes, mime_hint: str) -> dict[str, Any]:
        digest = hashlib.sha256(data).hexdigest()
        mime_type = detect_mime_type(data, mime_hint)
        suffix = _image_suffix(mime_type, data)
        relative_path = Path("history") / "assets" / digest[:2] / f"{digest}{suffix}"
        target = self.data_dir / relative_path
        try:
            current_size = target.stat().st_size
        except OSError:
            current_size = -1
        if current_size != len(data):
            _atomic_write(target, data)
        width, height = _image_dimensions(data)
        return {
            "id": digest,
            "path": str(relative_path),
            "mime_type": mime_type,
            "size_bytes": len(data),
            "width": width,
            "height": height,
        }

    def _prepare_thumbnail_sync(
        self,
        asset: dict[str, Any],
        *,
        max_edge: int,
        quality: int,
    ) -> dict[str, Any]:
        max_edge = max(256, min(2048, int(max_edge)))
        quality = max(40, min(95, int(quality)))
        relative_path = Path("history") / "thumbnails" / f"{asset['id']}.webp"
        target = self.data_dir / relative_path
        with self._connect() as conn:
            current = conn.execute(
                "SELECT size_bytes, max_edge, quality FROM image_thumbnails WHERE asset_id = ?",
                (asset["id"],),
            ).fetchone()
        if (
            not target.is_file()
            or current is None
            or target.stat().st_size != int(current["size_bytes"])
            or int(current["max_edge"]) != max_edge
            or int(current["quality"]) != quality
        ):
            _create_thumbnail(
                asset.get("original_path") or self.data_dir / asset["path"],
                target,
                max_edge=max_edge,
                quality=quality,
            )
        raw = target.read_bytes()
        return {
            "asset_id": asset["id"],
            "path": str(relative_path),
            "mime_type": detect_mime_type(raw, "image/webp"),
            "size_bytes": len(raw),
            "max_edge": max_edge,
            "quality": quality,
        }

    @staticmethod
    def _upsert_thumbnails_sync(conn: sqlite3.Connection, thumbnails: Any) -> None:
        conn.executemany(
            "INSERT INTO image_thumbnails "
            "(asset_id, path, mime_type, size_bytes, max_edge, quality) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(asset_id) DO UPDATE SET "
            "path=excluded.path, mime_type=excluded.mime_type, "
            "size_bytes=excluded.size_bytes, max_edge=excluded.max_edge, "
            "quality=excluded.quality",
            [
                (
                    thumbnail["asset_id"],
                    thumbnail["path"],
                    thumbnail["mime_type"],
                    thumbnail["size_bytes"],
                    thumbnail["max_edge"],
                    thumbnail["quality"],
                )
                for thumbnail in thumbnails
            ],
        )

    def _history_asset_bytes_sync(self) -> int:
        """Count automatic-history assets once, excluding imported/favorite shares."""

        return self._history_asset_usage_sync()["counted_size_bytes"]

    def _history_asset_usage_sync(self) -> dict[str, int]:
        """Assign each gallery asset to counted or exempt ownership exactly once."""

        with self._connect() as conn:
            row = conn.execute(
                "WITH links AS ("
                "SELECT asset_id, generation_id FROM generation_images UNION "
                "SELECT asset_id, generation_id FROM generation_references WHERE asset_id IS NOT NULL"
                "), ownership AS ("
                "SELECT l.asset_id, MAX(CASE WHEN g.source = 'import' OR g.is_favorite = 1 THEN 1 ELSE 0 END) AS exempt "
                "FROM links l JOIN generations g ON g.id = l.generation_id WHERE g.source != 'external' GROUP BY l.asset_id"
                ") SELECT "
                "COALESCE(SUM(CASE WHEN o.exempt = 0 THEN a.size_bytes + COALESCE(t.size_bytes, 0) ELSE 0 END), 0), "
                "COALESCE(SUM(CASE WHEN o.exempt = 1 THEN a.size_bytes + COALESCE(t.size_bytes, 0) ELSE 0 END), 0) "
                "FROM ownership o JOIN image_assets a ON a.id = o.asset_id "
                "LEFT JOIN image_thumbnails t ON t.asset_id = a.id"
            ).fetchone()
        return {"counted_size_bytes": int(row[0]), "exempt_size_bytes": int(row[1])}

    def _asset_count_sync(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM image_assets").fetchone()[0])

    def _storage_stats_sync(self) -> dict[str, Any]:
        now = time.time()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT "
                "(SELECT COUNT(*) FROM generations WHERE source != 'external'), "
                "(SELECT COUNT(*) FROM image_assets WHERE path NOT LIKE 'external/%'), "
                "(SELECT COUNT(*) FROM image_thumbnails), "
                "(SELECT COUNT(*) FROM agent_asset_leases "
                " WHERE expires_at > ? AND hard_expires_at > ?), "
                "COALESCE((SELECT SUM(size_bytes) FROM image_assets WHERE path NOT LIKE 'external/%'), 0) + "
                "COALESCE((SELECT SUM(size_bytes) FROM image_thumbnails), 0)",
                (now, now),
            ).fetchone()
        return {
            "generations": int(row[0]),
            "assets": int(row[1]),
            "thumbnails": int(row[2]),
            "active_leases": int(row[3]),
            "size_bytes": int(row[4]),
            "database_version": DATABASE_VERSION,
            "external_sources": [
                item for item in self._external_sources_status_sync() if item["enabled"]
            ],
        }

    def _remove_broken_asset_sync(self, asset_id: str) -> int:
        if not _SHA256_RE.fullmatch(asset_id):
            return 0
        with self._connect() as conn:
            row = conn.execute(
                "SELECT path FROM image_assets WHERE id = ?", (asset_id,)
            ).fetchone()
            if row is None:
                return 0
            conn.execute(
                "UPDATE image_assets SET file_state = 'unavailable' WHERE id = ?",
                (asset_id,),
            )
            conn.execute(
                "DELETE FROM agent_asset_leases WHERE asset_id = ?", (asset_id,)
            )
        return 1

    def _purge_unreferenced_assets_sync(
        self, asset_ids: list[str] | None = None
    ) -> None:
        candidates = list(
            dict.fromkeys(
                asset_id
                for asset_id in (asset_ids or [])
                if _SHA256_RE.fullmatch(asset_id)
            )
        )
        restriction = ""
        args: list[str] = []
        if asset_ids is not None:
            if not candidates:
                return
            restriction = f"AND a.id IN ({','.join('?' for _ in candidates)})"
            args = candidates
        with self._connect() as conn:
            # An external association must not keep an unreferenced owned copy alive.
            demoted = conn.execute(
                "SELECT a.id,a.path,a.mime_type FROM image_assets a WHERE a.path NOT LIKE 'external/%' "
                "AND EXISTS (SELECT 1 FROM external_records e WHERE e.asset_id=a.id) "
                "AND NOT EXISTS (SELECT 1 FROM generation_images i JOIN generations g ON g.id=i.generation_id WHERE i.asset_id=a.id AND g.source!='external') "
                "AND NOT EXISTS (SELECT 1 FROM generation_references r WHERE r.asset_id=a.id) "
                "AND NOT EXISTS (SELECT 1 FROM agent_asset_leases l WHERE l.asset_id=a.id AND l.expires_at>? AND l.hard_expires_at>?) "
                f"{restriction}",
                [time.time(), time.time(), *args],
            ).fetchall()
            conn.executemany(
                "UPDATE image_assets SET path=? WHERE id=?",
                [
                    (
                        f"external/{row['id']}{_IMAGE_SUFFIXES.get(row['mime_type'], '.img')}",
                        row["id"],
                    )
                    for row in demoted
                ],
            )
            rows = conn.execute(
                "SELECT a.id, a.path, t.path AS thumbnail_path FROM image_assets a "
                "LEFT JOIN image_thumbnails t ON t.asset_id = a.id "
                "WHERE NOT EXISTS (SELECT 1 FROM generation_images i WHERE i.asset_id = a.id) "
                "AND NOT EXISTS (SELECT 1 FROM generation_references r WHERE r.asset_id = a.id) "
                "AND NOT EXISTS (SELECT 1 FROM agent_asset_leases l WHERE l.asset_id = a.id "
                "AND l.expires_at > ? AND l.hard_expires_at > ?) "
                f"{restriction}",
                [time.time(), time.time(), *args],
            ).fetchall()
            conn.executemany(
                "DELETE FROM image_assets WHERE id = ?",
                [(str(row["id"]),) for row in rows],
            )
        for row in demoted:
            _unlink_if_owned(self.data_dir / str(row["path"]), self.assets_dir)
        for row in rows:
            _unlink_if_owned(self.data_dir / str(row["path"]), self.assets_dir)
            if row["thumbnail_path"]:
                _unlink_if_owned(
                    self.data_dir / str(row["thumbnail_path"]), self.thumbnails_dir
                )
        _remove_empty_directories(self.assets_dir)
        self._trim_disabled_external_thumbnails_sync()

    def _cleanup_orphaned_asset_files_sync(
        self, *, older_than: float | None = None
    ) -> int:
        """Delete content-addressed files that have no database asset row."""

        with self._connect() as conn:
            rows = conn.execute("SELECT path FROM image_assets").fetchall()
        referenced = {
            (self.data_dir / str(row["path"])).resolve()
            for row in rows
            if _is_within(self.data_dir / str(row["path"]), self.assets_dir)
        }
        removed = _delete_unreferenced_files(
            self.assets_dir, referenced, older_than=older_than
        )
        _remove_empty_directories(self.assets_dir)
        return removed

    def _cleanup_orphaned_thumbnails_sync(
        self, *, older_than: float | None = None
    ) -> int:
        """Delete thumbnail files no longer registered to a shared asset."""

        with self._connect() as conn:
            rows = conn.execute("SELECT path FROM image_thumbnails").fetchall()
        referenced = {
            (self.data_dir / str(row["path"])).resolve()
            for row in rows
            if _is_within(self.data_dir / str(row["path"]), self.thumbnails_dir)
        }
        return _delete_unreferenced_files(
            self.thumbnails_dir, referenced, older_than=older_than
        )

    def _cleanup_stale_files_sync(self, now: float) -> int:
        removed = 0
        for root, cutoff in (
            (self.staging_dir, now - 24 * 3600),
            (self.imports_dir, now - 3600),
            (self.exports_dir, now - 3600),
            (self.delivery_dir, now - 3600),
        ):
            if not root.is_dir():
                continue
            for path in root.rglob("*"):
                try:
                    if (
                        path.is_file()
                        and _is_within(path, root)
                        and path.stat().st_mtime < cutoff
                    ):
                        path.unlink()
                        removed += 1
                except OSError:
                    continue
            _remove_empty_directories(root)
        return removed

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn


def detect_mime_type(data: bytes, hint: str = "") -> str:
    """Return a safe image MIME type from bytes, with a bounded hint fallback."""

    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    return hint if hint in _IMAGE_SUFFIXES else "image/png"


def image_data_url(data: bytes, mime_type: str) -> str:
    """Encode an image for the authenticated plugin iframe."""

    return f"data:{mime_type};base64,{base64.b64encode(data).decode('ascii')}"


def _validate_import_hashes(hashes: Any) -> list[str]:
    if not isinstance(hashes, list) or not 1 <= len(hashes) <= 100:
        raise ValueError("每次查重需要 1 至 100 个图片哈希")
    if any(
        not isinstance(value, str) or not _SHA256_RE.fullmatch(value)
        for value in hashes
    ):
        raise ValueError("图片 SHA-256 必须为 64 位小写十六进制字符串")
    return list(hashes)


def _validate_generation_selection(generation_ids: Any) -> list[str]:
    if not isinstance(generation_ids, list) or not 1 <= len(generation_ids) <= 1000:
        raise ValueError("请选择 1 至 1000 条生成记录")
    if any(
        not isinstance(item, str) or not _SAFE_ID_RE.fullmatch(item)
        for item in generation_ids
    ):
        raise ValueError("所选生成记录 ID 无效")
    return list(dict.fromkeys(generation_ids))


def _favorite_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    favorite_count = sum(bool(row["is_favorite"]) for row in rows)
    all_favorite = favorite_count == len(rows)
    return {
        "selected_count": len(rows),
        "favorite_count": favorite_count,
        "all_favorite": all_favorite,
        "action": "unfavorite" if all_favorite else "favorite",
        "items": [
            {
                "id": row["id"],
                "is_favorite": bool(row["is_favorite"]),
                "cleanup_protected_until": float(row["cleanup_protected_until"]),
            }
            for row in rows
        ],
    }


def _validate_import_content(data: bytes) -> None:
    from PIL import Image

    from .image_metadata import MAX_PIXELS

    if not data or len(data) > 30 * 1024 * 1024:
        raise ValueError("导入图片不能为空且不能超过 30 MB")
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in {"PNG", "JPEG", "WEBP", "GIF"}:
                raise ValueError("导入仅支持 PNG、JPEG、WebP 或 GIF 图片")
            if image.width * image.height > MAX_PIXELS:
                raise ValueError("导入图片超过 6400 万像素限制")
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError("无法读取导入图片或图片尺寸超出限制") from exc


_IMPORT_EDIT_FIELDS = (
    "generation_engine",
    "model",
    "mode",
    "prompt",
    "negative_prompt",
    "generated_at",
)


def _validate_import_edit_overrides(overrides: Any) -> None:
    _validate_import_overrides(overrides)
    if set(overrides) - {*_IMPORT_EDIT_FIELDS, "parameters", "comfy_output_node"}:
        raise ValueError("编辑参数包含不支持的字段")
    for key in (*_IMPORT_EDIT_FIELDS[:-1], "comfy_output_node"):
        if key in overrides and not isinstance(overrides[key], str):
            raise ValueError(f"编辑字段 {key} 必须是字符串")
    if len(overrides.get("generation_engine", "")) > 80:
        raise ValueError("生图来源不能超过 80 个字符")
    if "generated_at" in overrides and (
        isinstance(overrides["generated_at"], bool)
        or not isinstance(overrides["generated_at"], (str, int, float, type(None)))
    ):
        raise ValueError("原始生成时间无效")


def _import_edit_existing_overrides(
    previous: dict[str, Any], metadata: dict[str, Any]
) -> dict[str, Any]:
    saved = previous.get("overrides")
    if isinstance(saved, dict):
        return dict(saved)
    # Legacy snapshots do not record manual provenance. Conservatively retain
    # their current values when an edit first introduces an override snapshot.
    result = {key: previous[key] for key in _IMPORT_EDIT_FIELDS if key in previous}
    if previous.get("comfy_output_node"):
        result["comfy_output_node"] = previous["comfy_output_node"]
    normalized = project_import_metadata(metadata, result).get("normalized", {})
    display = previous.get("display_parameters")
    if isinstance(previous.get("parameters"), dict):
        result["parameters"] = previous["parameters"]
    elif isinstance(display, dict):
        changes = {
            key: value
            for key, value in display.items()
            if key not in _IMPORT_EDIT_FIELDS
            and (key not in normalized or value != normalized[key])
        }
        if changes:
            result["parameters"] = changes
    return result


def _import_edit_fields(
    previous: dict[str, Any], metadata: dict[str, Any], overrides: dict[str, Any]
) -> dict[str, Any]:
    normalized = metadata.get("normalized", {})
    fields = {
        key: previous.get(key, overrides.get(key, normalized.get(key)))
        for key in _IMPORT_EDIT_FIELDS
    }
    for key in ("model", "prompt", "negative_prompt"):
        fields[key] = str(fields[key] or "")
    fields["generation_engine"] = _canonical_engine(
        fields["generation_engine"] or metadata.get("format")
    )
    fields["mode"] = fields["mode"] or "unknown"
    parameters = overrides.get("parameters", normalized.get("parameters", {}))
    fields["parameters"] = parameters if isinstance(parameters, dict) else {}
    return fields


def _validate_import_overrides(overrides: Any) -> None:
    if not isinstance(overrides, dict):
        raise ValueError("导入补充信息必须为对象")
    if "parameters" in overrides and not isinstance(overrides["parameters"], dict):
        raise ValueError("导入参数必须为 JSON 对象")
    if "comfy_output_node" in overrides and not isinstance(
        overrides["comfy_output_node"], str
    ):
        raise ValueError("ComfyUI 输出节点 ID 必须是字符串")
    try:
        encoded = json.dumps(overrides, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > 1024 * 1024:
            raise ValueError("导入补充信息不能超过 1 MB")
    except (TypeError, RecursionError) as exc:
        raise ValueError("导入补充信息必须是有效 JSON") from exc


def _canonical_engine(value: Any) -> str:
    engine = str(value or "unknown").strip()
    return "novelai" if engine.lower() in {"nai", "novelai"} else engine


def _known_merge_engine(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("合并来源必须是字符串")
    engine = _canonical_engine(value)
    if not value.strip() or engine.lower() in {"unknown", "mixed"}:
        raise ValueError("仅支持合并具有相同已知生图来源的图片")
    return engine


def _canonical_supplemental(value: dict[str, Any]) -> dict[str, Any]:
    result = dict(value)
    result.pop("merge_operations", None)
    if "generation_engine" in result:
        result["generation_engine"] = _canonical_engine(result["generation_engine"])
    display = result.get("display_parameters")
    if isinstance(display, dict) and "generation_engine" in display:
        result["display_parameters"] = {
            **display,
            "generation_engine": _canonical_engine(display["generation_engine"]),
        }
    return result


def project_import_metadata(
    metadata: dict[str, Any], overrides: dict[str, Any]
) -> dict[str, Any]:
    """Project a selected ComfyUI output without changing the shared asset metadata."""

    if "comfy_output_node" not in overrides:
        return metadata
    output_node = overrides["comfy_output_node"]
    if not isinstance(output_node, str):
        raise ValueError("ComfyUI 输出节点 ID 必须是字符串")
    if not output_node:
        return metadata
    if metadata.get("format") != "comfyui":
        raise ValueError("仅 ComfyUI 图片支持选择输出节点")
    from .image_metadata import PARSER_VERSION, parse_metadata_fields

    raw = metadata.get("raw")
    if not isinstance(raw, dict):
        raise ValueError("ComfyUI 原始工作流不可用，无法选择输出节点")
    normalized = metadata.get("normalized") or {}
    if not isinstance(normalized, dict):
        raise ValueError("metadata.normalized 必须是对象")
    dimensions = normalized.get("file_dimensions") or {}
    if not isinstance(dimensions, dict):
        raise ValueError("图片尺寸信息必须是对象")
    # Cache only the parser's pure projection. Manual prompt/model/parameter
    # overrides are applied by callers and never become shared cached state.
    raw_bytes = json.dumps(
        raw, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode()
    key = (
        PARSER_VERSION,
        metadata.get("parser_version"),
        hashlib.sha256(raw_bytes).digest(),
        dimensions.get("width", 0),
        dimensions.get("height", 0),
        output_node,
    )
    with _COMFY_PROJECTION_CACHE_LOCK:
        cached = _COMFY_PROJECTION_CACHE.get(key)
        if cached is not None:
            _COMFY_PROJECTION_CACHE.move_to_end(key)
    if cached is not None:
        return json.loads(cached)
    projected = parse_metadata_fields(
        raw,
        width=dimensions.get("width", 0),
        height=dimensions.get("height", 0),
        output_node_id=output_node,
    )
    encoded = json.dumps(projected, ensure_ascii=False, separators=(",", ":")).encode()
    if len(encoded) <= _COMFY_PROJECTION_CACHE_BYTES:
        with _COMFY_PROJECTION_CACHE_LOCK:
            _COMFY_PROJECTION_CACHE[key] = encoded
            _COMFY_PROJECTION_CACHE.move_to_end(key)
            while (
                len(_COMFY_PROJECTION_CACHE) > _COMFY_PROJECTION_CACHE_LIMIT
                or sum(map(len, _COMFY_PROJECTION_CACHE.values()))
                > _COMFY_PROJECTION_CACHE_BYTES
            ):
                _COMFY_PROJECTION_CACHE.popitem(last=False)
    return projected


def _external_parameter_overrides(
    overrides: dict[str, Any], metadata: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Keep readable external images indexable even when parameter fields are malformed."""
    result = dict(overrides)
    normalized = metadata.get("normalized") or {}
    warnings = []
    for key in ("model", "prompt", "negative_prompt"):
        value = overrides.get(key, normalized.get(key) or "")
        if not isinstance(value, str):
            warnings.append(
                f"外部图片的 {key} 参数格式无效，已忽略该字段；原始元数据仍保留"
            )
            value = ""
        if key == "model" and len(value.strip()) > 240:
            warnings.append(
                "外部图片的模型名称超过 240 个字符，显示名称已截短；原始元数据仍保留"
            )
            value = value.strip()[:240]
        result[key] = value
    mode = overrides.get("mode", normalized.get("mode") or "unknown")
    if not isinstance(mode, str) or mode not in {"text2img", "img2img", "unknown"}:
        warnings.append("外部图片的生图模式无法识别，已标记为未知")
        mode = "unknown"
    result["mode"] = mode
    return result, warnings


def _import_supplemental(
    filename: str,
    overrides: dict[str, Any],
    metadata: dict[str, Any],
    *,
    allow_unresolved_output: bool = False,
) -> dict[str, Any]:
    metadata = project_import_metadata(metadata, overrides)
    normalized = metadata.get("normalized", {})
    if (
        not allow_unresolved_output
        and metadata.get("format") == "comfyui"
        and normalized.get("requires_output_selection")
    ):
        raise ValueError("图片包含多个 ComfyUI 保存输出，请先选择对应的最终保存节点")
    mode = str(overrides.get("mode") or normalized.get("mode") or "unknown")
    if mode not in {"text2img", "img2img", "unknown"}:
        raise ValueError("导入图片模式无效")
    engine = _canonical_engine(
        overrides.get("generation_engine")
        or normalized.get("generation_engine")
        or metadata.get("format")
        or "unknown"
    )[:80]
    model_value = overrides.get("model", normalized.get("model") or "")
    if not isinstance(model_value, str):
        raise ValueError("导入模型名称必须是字符串")
    model = model_value.strip()
    if len(model) > 240:
        raise ValueError("导入模型名称不能超过 240 个字符")
    prompt = str(overrides.get("prompt", normalized.get("prompt") or ""))
    negative = str(
        overrides.get("negative_prompt", normalized.get("negative_prompt") or "")
    )
    generated_at = overrides.get("generated_at", normalized.get("generated_at"))
    if generated_at not in (None, ""):
        try:
            generated_at = float(generated_at)
            if not 0 <= generated_at < 253402300800:
                raise ValueError
        except (ValueError, TypeError):
            raise ValueError("原始生成时间无效") from None
    else:
        generated_at = None
    return {
        "original_filename": str(filename or "")[:512],
        "overrides": overrides,
        "comfy_output_node": overrides.get(
            "comfy_output_node", normalized.get("selected_output_node", "")
        ),
        "model": model,
        "mode": mode,
        "prompt": prompt,
        "negative_prompt": negative,
        "generation_engine": engine,
        "generated_at": generated_at,
        "display_parameters": {
            **normalized,
            **(overrides.get("parameters") or {}),
            "model": model,
            "mode": mode,
            "generation_engine": engine,
            "generated_at": generated_at,
            "prompt": prompt,
            "negative_prompt": negative,
        },
    }


def _path_data_url(path: Path, mime_type: str) -> str:
    try:
        if path.is_file():
            return image_data_url(path.read_bytes(), mime_type)
    except OSError:
        pass
    return ""


def _image_dimensions(data: bytes) -> tuple[int, int]:
    try:
        from PIL import Image, ImageOps

        with Image.open(io.BytesIO(data)) as image:
            transposed = ImageOps.exif_transpose(image)
            width, height = transposed.size
        return max(1, int(width)), max(1, int(height))
    except Exception:
        return 1, 1


def _image_is_decodable(data: bytes) -> bool:
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            return bool(image.format) and width > 0 and height > 0
    except Exception:
        return False


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _create_thumbnail(
    source: Path,
    target: Path,
    *,
    max_edge: int,
    quality: int,
) -> None:
    try:
        from PIL import Image, ImageOps

        with Image.open(source) as image:
            image = ImageOps.exif_transpose(image)
            has_alpha = image.mode in {"RGBA", "LA"} or (
                image.mode == "P" and "transparency" in image.info
            )
            thumbnail = image.convert("RGBA" if has_alpha else "RGB")
            thumbnail.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            thumbnail.save(output, "WEBP", quality=quality, method=4)
            encoded = output.getvalue()
        original = source.read_bytes()
        _atomic_write(target, encoded if len(encoded) < len(original) else original)
    except Exception:
        # Keep gallery and Agent viewing usable for uncommon image formats.
        _atomic_write(target, source.read_bytes())


def _image_suffix(mime_type: str, data: bytes) -> str:
    return _IMAGE_SUFFIXES.get(detect_mime_type(data, mime_type), ".png")


def _safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "")).strip("._")[:120]


def _export_stem(
    detail: dict[str, Any],
    *,
    image_index: int,
    image_count: int,
    used_stems: set[str],
) -> str:
    timestamp = time.strftime(
        "%Y%m%d%H%M%S", time.localtime(float(detail.get("created_at") or 0))
    )
    mode = {"img2img": "i2i", "text2img": "t2i"}.get(detail.get("mode"), "unknown")
    model = (
        _safe_filename(
            str(detail.get("model") or detail.get("provider_id") or "unknown")
        )
        or "unknown"
    )
    base = f"{timestamp}_{mode}_{model}"
    if image_count > 1:
        base = f"{base}_{image_index:02d}"
    stem = base
    collision = 2
    while stem in used_stems:
        stem = f"{base}_{collision:02d}"
        collision += 1
    used_stems.add(stem)
    return stem


def export_image_filename(
    detail: dict[str, Any],
    *,
    image_index: int,
    image_count: int,
    mime_type: str = "",
    suffix: str = "",
    used_stems: set[str] | None = None,
) -> str:
    """Return the shared WebUI and ZIP filename for one generated image."""

    stem = _export_stem(
        detail,
        image_index=image_index,
        image_count=image_count,
        used_stems=used_stems if used_stems is not None else set(),
    )
    extension = str(suffix or "").strip().lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,8}", extension):
        extension = _IMAGE_SUFFIXES.get(str(mime_type or "").lower(), ".png")
    return f"{stem}{extension}"


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _unlink_if_owned(path: Path, root: Path) -> None:
    try:
        if _is_within(path, root):
            path.unlink(missing_ok=True)
    except OSError:
        pass


def _delete_unreferenced_files(
    root: Path,
    referenced: set[Path],
    *,
    older_than: float | None = None,
) -> int:
    removed = 0
    for path in root.rglob("*"):
        try:
            if (
                path.is_file()
                and _is_within(path, root)
                and path.resolve() not in referenced
                and (older_than is None or path.stat().st_mtime < older_than)
            ):
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def _remove_empty_directories(root: Path) -> None:
    directories = sorted(
        (path for path in root.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for path in directories:
        try:
            path.rmdir()
        except OSError:
            continue


def _load_json(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _search_projection(value: Any) -> str:
    values: list[str] = []

    def collect(item: Any, depth: int = 0) -> None:
        if depth > 10:
            return
        if isinstance(item, dict):
            for key, child in item.items():
                if key not in {"workflow", "raw", "api_prompt", "nodes", "links"}:
                    values.append(str(key))
                    collect(child, depth + 1)
        elif isinstance(item, list):
            for child in item[:1000]:
                collect(child, depth + 1)
        elif item is not None:
            values.append(str(item))

    collect(value)
    return " ".join(values)[:200000]


def _redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "***"
            if re.search(
                r"(?:api[_-]?key|token|secret|authorization|password)", str(key), re.I
            )
            else _redact_sensitive(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    return value


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
