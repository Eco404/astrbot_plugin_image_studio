"""Durable gallery, reference, and staging storage."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from .config import HistorySettings
from .database_schema import (
    DATABASE_VERSION,
    check_database_version,
    finish_development_schema,
)
from .models import (
    GeneratedImage,
    GenerationRequest,
    ImageProvider,
    ReferenceImage,
    WorkflowImageAsset,
    WorkflowImageLoadResult,
)

_IMAGE_SUFFIXES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
_SAFE_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


class GenerationStore:
    """Store plugin-owned gallery files and queryable generation metadata."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir.resolve()
        self.history_dir = self.data_dir / "history"
        self.assets_dir = self.history_dir / "assets"
        self.thumbnails_dir = self.history_dir / "thumbnails"
        self.staging_dir = self.data_dir / "staging_references"
        self.exports_dir = self.data_dir / "exports"
        self.delivery_dir = self.data_dir / "delivery_staging"
        self.db_path = self.data_dir / "history.sqlite3"
        self._lock = asyncio.Lock()
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

    def _initialize_sync(self) -> None:
        for directory in (
            self.data_dir,
            self.assets_dir,
            self.thumbnails_dir,
            self.staging_dir,
            self.exports_dir,
            self.delivery_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            check_database_version(conn)
            conn.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS generations (
                    id TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    provider_id TEXT NOT NULL,
                    provider_name TEXT NOT NULL,
                    provider_kind TEXT NOT NULL,
                    model TEXT NOT NULL,
                    original_prompt TEXT NOT NULL,
                    final_prompt TEXT NOT NULL,
                    parameters_json TEXT NOT NULL,
                    elapsed_ms INTEGER NOT NULL,
                    error_message TEXT NOT NULL DEFAULT '',
                    context_type TEXT NOT NULL DEFAULT '',
                    platform_name TEXT NOT NULL DEFAULT '',
                    platform_id TEXT NOT NULL DEFAULT '',
                    group_id TEXT NOT NULL DEFAULT '',
                    group_name TEXT NOT NULL DEFAULT '',
                    user_id TEXT NOT NULL DEFAULT '',
                    user_name TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS image_assets (
                    id TEXT PRIMARY KEY,
                    path TEXT NOT NULL UNIQUE,
                    mime_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    width INTEGER NOT NULL DEFAULT 0,
                    height INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS image_thumbnails (
                    asset_id TEXT PRIMARY KEY REFERENCES image_assets(id) ON DELETE CASCADE,
                    path TEXT NOT NULL UNIQUE,
                    mime_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    max_edge INTEGER NOT NULL DEFAULT 0,
                    quality INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS generation_images (
                    id TEXT PRIMARY KEY,
                    generation_id TEXT NOT NULL REFERENCES generations(id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL,
                    asset_id TEXT NOT NULL REFERENCES image_assets(id)
                );
                CREATE TABLE IF NOT EXISTS generation_references (
                    id TEXT PRIMARY KEY,
                    generation_id TEXT NOT NULL REFERENCES generations(id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL,
                    filename TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    available INTEGER NOT NULL DEFAULT 1,
                    deleted_at REAL,
                    asset_id TEXT REFERENCES image_assets(id)
                );
                CREATE TABLE IF NOT EXISTS agent_asset_leases (
                    id TEXT PRIMARY KEY,
                    asset_id TEXT NOT NULL REFERENCES image_assets(id) ON DELETE CASCADE,
                    scope_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    last_accessed_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    hard_expires_at REAL NOT NULL,
                    UNIQUE(scope_id, asset_id)
                );
                CREATE TABLE IF NOT EXISTS image_metadata (
                    asset_id TEXT PRIMARY KEY REFERENCES image_assets(id) ON DELETE CASCADE,
                    format TEXT NOT NULL,
                    parser_version INTEGER NOT NULL,
                    metadata_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_generations_created_at ON generations(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_generations_provider ON generations(provider_id);
                CREATE INDEX IF NOT EXISTS idx_generation_images_generation ON generation_images(generation_id);
                CREATE INDEX IF NOT EXISTS idx_generation_references_generation ON generation_references(generation_id);
                CREATE INDEX IF NOT EXISTS idx_generation_images_asset ON generation_images(asset_id);
                CREATE INDEX IF NOT EXISTS idx_generation_references_asset ON generation_references(asset_id);
                CREATE INDEX IF NOT EXISTS idx_agent_asset_leases_asset ON agent_asset_leases(asset_id);
                CREATE INDEX IF NOT EXISTS idx_agent_asset_leases_expiry ON agent_asset_leases(expires_at);
                """
            )
            generation_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(generations)").fetchall()
            }
            for column in (
                "context_type",
                "platform_name",
                "platform_id",
                "group_id",
                "group_name",
                "user_id",
                "user_name",
            ):
                if column not in generation_columns:
                    conn.execute(
                        f"ALTER TABLE generations ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                    )
            for column, definition in {
                "is_favorite": "INTEGER NOT NULL DEFAULT 0",
                "cleanup_protected_until": "REAL NOT NULL DEFAULT 0",
                "generation_engine": "TEXT NOT NULL DEFAULT 'unknown'",
                "generated_at": "REAL",
                "supplemental_json": "TEXT NOT NULL DEFAULT '{}'",
                "import_key": "TEXT",
                "search_text": "TEXT NOT NULL DEFAULT ''",
            }.items():
                if column not in generation_columns:
                    conn.execute(
                        f"ALTER TABLE generations ADD COLUMN {column} {definition}"
                    )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_generations_import_key "
                "ON generations(import_key) WHERE import_key IS NOT NULL"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_generations_retention "
                "ON generations(source, is_favorite, cleanup_protected_until, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_generations_engine "
                "ON generations(generation_engine)"
            )
            thumbnail_columns = {
                str(row["name"])
                for row in conn.execute(
                    "PRAGMA table_info(image_thumbnails)"
                ).fetchall()
            }
            for column in ("max_edge", "quality"):
                if column not in thumbnail_columns:
                    conn.execute(
                        f"ALTER TABLE image_thumbnails ADD COLUMN {column} "
                        "INTEGER NOT NULL DEFAULT 0"
                    )
            asset_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(image_assets)").fetchall()
            }
            for column in ("width", "height"):
                if column not in asset_columns:
                    conn.execute(
                        f"ALTER TABLE image_assets ADD COLUMN {column} "
                        "INTEGER NOT NULL DEFAULT 0"
                    )
            if "file_state" not in asset_columns:
                conn.execute(
                    "ALTER TABLE image_assets ADD COLUMN file_state TEXT NOT NULL DEFAULT 'available'"
                )
            for row in conn.execute(
                "SELECT id, path FROM image_assets WHERE width <= 0 OR height <= 0"
            ).fetchall():
                path = self.data_dir / str(row["path"])
                try:
                    dimensions = _image_dimensions(path.read_bytes())
                except OSError:
                    dimensions = (1, 1)
                conn.execute(
                    "UPDATE image_assets SET width = ?, height = ? WHERE id = ?",
                    (*dimensions, str(row["id"])),
                )
            try:
                conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS generation_search USING fts5("
                    "generation_id UNINDEXED, original_prompt, final_prompt, provider_name, model)"
                )
            except sqlite3.OperationalError:
                pass
            conn.execute(
                "UPDATE generations SET generation_engine = CASE provider_kind "
                "WHEN 'nai_direct' THEN 'nai' WHEN '' THEN 'unknown' ELSE provider_kind END "
                "WHERE generation_engine = 'unknown' AND source != 'import'"
            )
            finish_development_schema(conn)
        self._backfill_metadata_sync()
        self._delete_expired_leases_sync(time.time())
        self._purge_unreferenced_assets_sync()
        self._cleanup_orphaned_asset_files_sync()
        self._cleanup_orphaned_thumbnails_sync()

    async def maintenance_report(self) -> dict[str, Any]:
        """Return the latest maintenance report plus current lightweight stats."""

        async with self._lock:
            report = dict(self._last_maintenance_report)
            report["stats"] = await asyncio.to_thread(self._storage_stats_sync)
        return report

    def _metadata_for_asset_sync(
        self, asset_id: str, data: bytes, *, strict: bool = False
    ) -> dict[str, Any]:
        from .image_metadata import parse_image_metadata

        with self._connect() as conn:
            row = conn.execute(
                "SELECT metadata_json FROM image_metadata WHERE asset_id = ?",
                (asset_id,),
            ).fetchone()
        if row is not None:
            cached = _load_json(str(row["metadata_json"]))
            if int(cached.get("parser_version", 0)) > 0:
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

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT a.id, a.path FROM image_assets a LEFT JOIN image_metadata m "
                "ON m.asset_id = a.id WHERE m.asset_id IS NULL OR m.parser_version = 0"
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
                    self._refresh_search_sync(conn, str(item["generation_id"]))

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
            _load_json(row["supplemental_json"]),
        ]
        for item in conn.execute(
            "SELECT m.metadata_json FROM generation_images i JOIN image_metadata m "
            "ON m.asset_id = i.asset_id WHERE i.generation_id = ? ORDER BY i.ordinal",
            (generation_id,),
        ).fetchall():
            values.append(_load_json(item["metadata_json"]).get("normalized", {}))
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
        self._backfill_metadata_sync()
        broken_ids: list[str] = []
        with self._connect() as conn:
            asset_rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT id, path, mime_type, size_bytes FROM image_assets"
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
                    "parameters": request.parameters,
                    "selection_source": request.selection_source,
                }
            )
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
                    "width=excluded.width, height=excluded.height, file_state='available'",
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
                        "nai" if provider.kind == "nai_direct" else provider.kind,
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

        if not data or len(data) > 30 * 1024 * 1024:
            raise ValueError("导入图片不能为空且不能超过 30 MB")
        if not _image_is_decodable(data):
            raise ValueError("无法读取导入图片")
        if not isinstance(overrides, dict):
            raise ValueError("导入补充信息必须为对象")
        if "parameters" in overrides and not isinstance(overrides["parameters"], dict):
            raise ValueError("导入参数必须为 JSON 对象")
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
        digest = hashlib.sha256(data).hexdigest()
        with self._connect() as conn:
            duplicate = None
            if import_key:
                duplicate = conn.execute(
                    "SELECT g.id, i.asset_id FROM generations g LEFT JOIN generation_images i "
                    "ON i.generation_id = g.id WHERE g.import_key = ?",
                    (import_key,),
                ).fetchone()
                if duplicate is not None and duplicate["asset_id"] != digest:
                    raise ValueError(
                        "导入请求标识已用于另一张图片，请重新选择文件后重试"
                    )
            if duplicate is None:
                duplicate = conn.execute(
                    "SELECT g.id FROM generations g JOIN generation_images i "
                    "ON i.generation_id = g.id WHERE g.source = 'import' "
                    "AND i.asset_id = ? ORDER BY g.created_at, g.id LIMIT 1",
                    (digest,),
                ).fetchone()
            if duplicate is not None:
                return {"generation_id": str(duplicate["id"]), "duplicate": True}
        metadata = self._metadata_for_asset_sync(digest, data, strict=True)
        normalized = metadata.get("normalized", {})
        generation_id = uuid.uuid4().hex
        created_at = time.time()
        engine = str(
            overrides.get("generation_engine")
            or normalized.get("generation_engine")
            or metadata.get("format")
            or "unknown"
        )[:80]
        mode = str(overrides.get("mode") or normalized.get("mode") or "unknown")
        if mode not in {"text2img", "img2img", "unknown"}:
            raise ValueError("导入图片模式无效")
        prompt = str(overrides.get("prompt", normalized.get("prompt") or ""))
        negative = str(
            overrides.get("negative_prompt", normalized.get("negative_prompt") or "")
        )
        model = str(overrides.get("model", normalized.get("model") or ""))[:240]
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
        supplemental = {
            "original_filename": str(filename or "")[:512],
            "overrides": overrides,
        }
        # Imported values are observations or user additions, never an original request.
        supplemental["display_parameters"] = {
            **normalized,
            **(overrides.get("parameters") or {}),
            "negative_prompt": negative,
        }
        try:
            asset = self._prepare_asset_sync(data, "")
            thumbnail = self._prepare_thumbnail_sync(
                asset, max_edge=preview_max_edge, quality=preview_quality
            )
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO image_assets (id, path, mime_type, size_bytes, width, height, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET file_state='available'",
                    (
                        digest,
                        asset["path"],
                        asset["mime_type"],
                        asset["size_bytes"],
                        asset["width"],
                        asset["height"],
                        created_at,
                    ),
                )
                self._upsert_thumbnails_sync(conn, [thumbnail])
                self._save_metadata_sync(conn, {digest: metadata})
                conn.execute(
                    "INSERT INTO generations (id, created_at, source, status, mode, "
                    "provider_id, provider_name, provider_kind, model, original_prompt, "
                    "final_prompt, parameters_json, elapsed_ms, generation_engine, generated_at, "
                    "supplemental_json, import_key) "
                    "VALUES (?, ?, 'import', 'succeeded', ?, '', '', '', ?, ?, ?, '{}', 0, ?, ?, ?, ?)",
                    (
                        generation_id,
                        created_at,
                        mode,
                        model,
                        prompt,
                        str(normalized.get("prompt") or prompt),
                        engine,
                        generated_at,
                        json.dumps(
                            supplemental, ensure_ascii=False, separators=(",", ":")
                        ),
                        import_key or None,
                    ),
                )
                conn.execute(
                    "INSERT INTO generation_images (id, generation_id, ordinal, asset_id) VALUES (?, ?, 0, ?)",
                    (uuid.uuid4().hex, generation_id, digest),
                )
                self._refresh_search_sync(conn, generation_id)
        except Exception:
            self._cleanup_orphaned_asset_files_sync()
            self._cleanup_orphaned_thumbnails_sync()
            raise
        return {"generation_id": generation_id, "duplicate": False}

    async def list_generations(self, filters: dict[str, Any]) -> dict[str, Any]:
        """Return a paginated gallery collection and aggregate filter values."""

        return await asyncio.to_thread(self._list_generations_sync, filters)

    @staticmethod
    def _gallery_filters(filters: dict[str, Any]) -> tuple[str, list[Any]]:
        where: list[str] = []
        args: list[Any] = []
        query = str(filters.get("query") or "").strip()[:240]
        provider_id = str(filters.get("provider_id") or "").strip()[:64]
        mode = str(filters.get("mode") or "").strip()[:20]
        source = str(filters.get("source") or "").strip()[:30]
        if provider_id:
            where.append("g.provider_id = ?")
            args.append(provider_id)
        if mode in {"text2img", "img2img", "unknown"}:
            where.append("g.mode = ?")
            args.append(mode)
        if source:
            where.append("g.source = ?")
            args.append(source)
        if filters.get("generation_engine"):
            where.append("g.generation_engine = ?")
            args.append(str(filters["generation_engine"])[:80])
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
            engines = [
                str(row[0])
                for row in conn.execute(
                    "SELECT DISTINCT generation_engine FROM generations ORDER BY generation_engine"
                ).fetchall()
            ]
        items = [self._gallery_item(dict(row)) for row in rows]
        return {
            "items": items,
            "total": total,
            "offset": offset,
            "limit": limit,
            "filters": {
                "providers": provider_options,
                "modes": ["text2img", "img2img", "unknown"],
                "sources": ["webui", "command", "llm_tool", "import"],
                "generation_engines": engines,
            },
        }

    async def generation_detail(
        self, generation_id: str, *, include_assets: bool = True
    ) -> dict[str, Any] | None:
        """Return a generation plus its result and reference assets."""

        return await asyncio.to_thread(
            self._generation_detail_sync, generation_id, include_assets
        )

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
                "SELECT g.id AS generation_id, g.created_at, g.mode, g.provider_id, "
                "g.model, i.id AS image_id, i.ordinal, a.mime_type, a.size_bytes, "
                "a.width, a.height, a.path, a.file_state, "
                "COUNT(*) OVER (PARTITION BY g.id) AS image_count "
                "FROM generations g JOIN generation_images i ON i.generation_id = g.id "
                "JOIN image_assets a ON a.id = i.asset_id "
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

    def _gallery_image_file_sync(self, image_id: str) -> tuple[Path, str, str] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT generation_id FROM generation_images WHERE id = ?",
                (image_id,),
            ).fetchone()
        if row is None:
            return None
        detail = self._generation_detail_sync(
            str(row["generation_id"]), include_assets=False
        )
        if detail is None:
            return None
        image = next(
            (item for item in detail["images"] if item["id"] == image_id), None
        )
        if image is None:
            return None
        path = self.data_dir / str(image["path"])
        if not path.is_file() or not _is_within(path, self.assets_dir):
            return None
        return (
            path,
            str(image["mime_type"]),
            str(image["download_filename"]),
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
                "t.mime_type AS thumbnail_mime_type, t.size_bytes AS thumbnail_size_bytes "
                "FROM generation_images i JOIN image_assets a ON a.id = i.asset_id "
                "JOIN image_thumbnails t ON t.asset_id = a.id WHERE i.id = ?",
                (image_id,),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        original = self.data_dir / str(item["path"])
        thumbnail = self.data_dir / str(item["thumbnail_path"])
        use_original = detail == "original"
        path = original if use_original else thumbnail
        mime_type = str(
            item["mime_type"] if use_original else item["thumbnail_mime_type"]
        )
        size_bytes = int(
            item["size_bytes"] if use_original else item["thumbnail_size_bytes"]
        )
        if not path.is_file() or not _is_within(
            path, self.assets_dir if use_original else self.thumbnails_dir
        ):
            return None
        return {
            "image_id": str(item["image_id"]),
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
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM generations WHERE id = ?", (generation_id,)
            ).fetchone()
            if row is None:
                return None
            image_rows = conn.execute(
                "SELECT i.*, a.path, a.mime_type, a.size_bytes, a.id AS sha256, "
                "a.width, a.height, a.file_state, m.metadata_json, "
                "t.path AS thumbnail_path, t.mime_type AS thumbnail_mime_type "
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
        result = dict(row)
        result["parameters"] = _load_json(result.pop("parameters_json", "{}"))
        result["supplemental"] = _load_json(result.pop("supplemental_json", "{}"))
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
        result["images"] = [
            self._asset_item(dict(item), preview_full=include_assets)
            for item in image_rows
        ]
        used_stems: set[str] = set()
        for image_index, image in enumerate(result["images"], start=1):
            image["download_filename"] = export_image_filename(
                result,
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
        async with self._lock:
            return await asyncio.to_thread(
                self._set_favorite_sync, generation_id, bool(favorite)
            )

    def _set_favorite_sync(self, generation_id: str, favorite: bool) -> dict[str, Any]:
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
            elif row["is_favorite"] and row["source"] != "import":
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
        async with self._lock:
            return await asyncio.to_thread(
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
        return {
            "deleted": image_ids,
            "remaining": remaining,
            "generation_deleted": remaining == 0,
        }

    async def delete_generation(self, generation_id: str) -> bool:
        """Delete one generation and every plugin-owned result/reference file."""

        if not _SAFE_ID_RE.fullmatch(generation_id):
            return False
        async with self._lock:
            return await asyncio.to_thread(self._delete_generation_sync, generation_id)

    def _delete_generation_sync(self, generation_id: str) -> bool:
        with self._connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM generations WHERE id = ?", (generation_id,)
            ).fetchone()[0]
            if not count:
                return False
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
        return await asyncio.to_thread(self._export_generations_sync, valid_ids)

    def _export_generations_sync(self, generation_ids: list[str]) -> Path:
        import zipfile

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
                    path = self.data_dir / image["path"]
                    if path.is_file() and _is_within(path, self.assets_dir):
                        image_filename = export_image_filename(
                            detail,
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
                        metadata["image"] = {
                            "id": image["id"],
                            "filename": image_filename,
                            "mime_type": image["mime_type"],
                            "size_bytes": image["size_bytes"],
                            "sha256": image["sha256"],
                            "metadata": image.get("metadata", {}),
                            "width": image.get("width"),
                            "height": image.get("height"),
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
            "id": row["id"],
            "created_at": row["created_at"],
            "source": row["source"],
            "generation_engine": row["generation_engine"],
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
            "mime_type": row["mime_type"],
            "size_bytes": row["size_bytes"],
            "invocation_source": invocation_source,
            "thumbnail_data_url": _path_data_url(
                thumbnail, str(row.get("thumbnail_mime_type") or "image/webp")
            ),
        }

    def _asset_item(self, row: dict[str, Any], *, preview_full: bool) -> dict[str, Any]:
        path = self.data_dir / str(row["path"])
        thumbnail = self.data_dir / str(row.get("thumbnail_path") or "")
        preview = _path_data_url(path, row["mime_type"]) if preview_full else ""
        return {
            "id": row["id"],
            "path": row["path"],
            "mime_type": row["mime_type"],
            "size_bytes": row["size_bytes"],
            "sha256": row["sha256"],
            "width": row.get("width", 1),
            "height": row.get("height", 1),
            "file_state": row.get("file_state", "available"),
            "metadata": _load_json(row.get("metadata_json") or "{}"),
            "data_url": preview,
            # Keep summaries lightweight while allowing the carousel to render
            # every result before original assets arrive.
            "thumbnail_data_url": (
                _path_data_url(
                    thumbnail, str(row.get("thumbnail_mime_type") or "image/webp")
                )
                if not preview_full
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

        return await asyncio.to_thread(self._retention_status_sync, history)

    def _retention_status_sync(self, history: HistorySettings) -> dict[str, Any]:
        now = time.time()
        with self._connect() as conn:
            count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM generations WHERE source != 'import' AND is_favorite = 0"
                ).fetchone()[0]
            )
            protected = int(
                conn.execute(
                    "SELECT COUNT(*) FROM generations WHERE source != 'import' "
                    "AND is_favorite = 0 AND cleanup_protected_until > ?",
                    (now,),
                ).fetchone()[0]
            )
            candidates = [
                str(row[0])
                for row in conn.execute(
                    "SELECT id FROM generations WHERE source != 'import' AND is_favorite = 0 "
                    "AND cleanup_protected_until <= ? ORDER BY created_at ASC, id ASC LIMIT 10",
                    (now,),
                ).fetchall()
            ]
            exempt = conn.execute(
                "SELECT SUM(CASE WHEN source = 'import' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN is_favorite = 1 THEN 1 ELSE 0 END) FROM generations"
            ).fetchone()
        size_bytes = self._history_asset_bytes_sync()
        limit_bytes = max(0, history.max_megabytes) * 1024 * 1024
        near = (history.max_records >= 0 and count >= history.max_records * 0.9) or (
            limit_bytes > 0 and size_bytes >= limit_bytes * 0.9
        )
        over = (history.max_records >= 0 and count > history.max_records) or (
            limit_bytes > 0 and size_bytes > limit_bytes
        )
        return {
            "record_count": count,
            "limit_records": history.max_records,
            "size_bytes": size_bytes,
            "limit_bytes": limit_bytes,
            "near_limit": bool(near),
            "over_limit": bool(over),
            "candidate_ids": candidates if near else [],
            "protected_records": protected,
            "imported_records": int(exempt[0] or 0),
            "favorite_records": int(exempt[1] or 0),
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
                self.data_dir / asset["path"],
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

        with self._connect() as conn:
            row = conn.execute(
                "WITH links AS ("
                "SELECT asset_id, generation_id FROM generation_images UNION "
                "SELECT asset_id, generation_id FROM generation_references WHERE asset_id IS NOT NULL"
                "), history_assets AS ("
                "SELECT DISTINCT l.asset_id FROM links l JOIN generations g ON g.id = l.generation_id "
                "WHERE g.source != 'import' AND g.is_favorite = 0 "
                "AND NOT EXISTS (SELECT 1 FROM links shared JOIN generations protected "
                "ON protected.id = shared.generation_id WHERE shared.asset_id = l.asset_id "
                "AND (protected.source = 'import' OR protected.is_favorite = 1))"
                ") SELECT "
                "COALESCE((SELECT SUM(a.size_bytes) FROM image_assets a "
                "JOIN history_assets h ON h.asset_id = a.id), 0) + "
                "COALESCE((SELECT SUM(t.size_bytes) FROM image_thumbnails t "
                "JOIN history_assets h ON h.asset_id = t.asset_id), 0)"
            ).fetchone()
        return int(row[0])

    def _asset_count_sync(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM image_assets").fetchone()[0])

    def _storage_stats_sync(self) -> dict[str, Any]:
        now = time.time()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT "
                "(SELECT COUNT(*) FROM generations), "
                "(SELECT COUNT(*) FROM image_assets), "
                "(SELECT COUNT(*) FROM image_thumbnails), "
                "(SELECT COUNT(*) FROM agent_asset_leases "
                " WHERE expires_at > ? AND hard_expires_at > ?), "
                "COALESCE((SELECT SUM(size_bytes) FROM image_assets), 0) + "
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
        for row in rows:
            _unlink_if_owned(self.data_dir / str(row["path"]), self.assets_dir)
            if row["thumbnail_path"]:
                _unlink_if_owned(
                    self.data_dir / str(row["thumbnail_path"]), self.thumbnails_dir
                )
        _remove_empty_directories(self.assets_dir)

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
