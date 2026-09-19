"""External gallery indexing, permissions, and verified source-file deletion."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..media.files import (
    _is_within,
    _unlink_if_owned,
)
from ..media.images import (
    _image_dimensions,
    _image_suffix,
    _validate_import_content,
    detect_mime_type,
    image_data_url,
)
from .constants import (
    _EXTERNAL_ACTION_LABELS,
    _EXTERNAL_ACTIONS,
)
from .context import GalleryContext
from .errors import (
    ExternalDeleteError,
    ExternalPermissionError,
    _ExternalFileChangedError,
)
from .projection import (
    _canonical_engine,
    _external_parameter_overrides,
    _import_supplemental,
    _load_json,
)


@dataclass(frozen=True)
class ExternalServices:
    """Explicit cross-repository operations; connections stay with their caller."""

    metadata_for_asset: Callable[..., Any]
    prepare_thumbnail: Callable[..., Any]
    purge_unreferenced_assets: Callable[..., Any]
    refresh_search: Callable[..., Any]
    save_metadata: Callable[..., Any]
    upsert_thumbnails: Callable[..., Any]


class ExternalRecords:
    def __init__(self, context: GalleryContext, services: ExternalServices) -> None:
        self.context = context
        self.services = services

    def notify_external_issue(self, row, reason: str) -> None:
        handler = self.context.external_issue_handler
        if handler is not None and row is not None and row["enabled"]:
            handler(str(row["source_id"]), reason)

    def report_external_failure(self, generation_id: str, error: Exception) -> None:
        if isinstance(error, FileNotFoundError):
            reason = "missing"
        elif isinstance(error, _ExternalFileChangedError):
            reason = "changed"
        else:
            return
        if self.context.external_issue_handler is not None:
            self.notify_external_issue(self.external_record(generation_id), reason)

    def gallery_data_url(
        self, path: Path, mime_type: str, generation_id: str, *, thumbnail=False
    ) -> str:
        try:
            if path.is_file():
                return image_data_url(path.read_bytes(), mime_type)
            path.stat()
        except FileNotFoundError:
            if self.context.external_issue_handler is not None:
                self.notify_external_issue(
                    self.external_record(generation_id),
                    "thumbnail_missing" if thumbnail else "missing",
                )
        except OSError:
            # Permission and I/O failures are not evidence that a file moved.
            pass
        return ""

    def gallery_thumbnail(self, row: dict[str, Any], generation_id: str) -> str:
        if not row.get("thumbnail_path"):
            if self.context.external_issue_handler is not None:
                self.notify_external_issue(
                    self.external_record(generation_id), "thumbnail_missing"
                )
            return ""
        return self.gallery_data_url(
            self.context.data_dir / str(row["thumbnail_path"]),
            str(row.get("thumbnail_mime_type") or "image/webp"),
            generation_id,
            thumbnail=True,
        )

    def configure_external_source(
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
        with self.context.connect() as conn:
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
                conn.execute(
                    "DELETE FROM generations WHERE id IN (SELECT generation_id FROM external_records WHERE source_id = ?)",
                    (source_id,),
                )
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
            self.trim_disabled_external_thumbnails()
        self.services.purge_unreferenced_assets()

    def remove_external_source(self, source_id: str) -> None:
        with self.context.connect() as conn:
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
            conn.execute("DELETE FROM external_sources WHERE id=?", (source_id,))
        self.services.purge_unreferenced_assets()

    def external_scan_snapshot(self, source_id):
        with self.context.connect() as conn:
            rows = conn.execute(
                "SELECT e.*,t.path AS thumbnail_path,t.max_edge AS thumbnail_max_edge,t.quality AS thumbnail_quality,m.parser_version, "
                "m.format AS metadata_format, json_extract(m.metadata_json, '$.rules_fingerprint') AS rules_fingerprint "
                "FROM external_records e LEFT JOIN image_thumbnails t ON t.asset_id=e.asset_id "
                "LEFT JOIN image_metadata m ON m.asset_id=e.asset_id WHERE e.source_id=?",
                (source_id,),
            ).fetchall()
        result = {}
        for row in rows:
            item = dict(row)
            thumbnail = item.pop("thumbnail_path")
            item["thumbnail_available"] = bool(
                thumbnail and (self.context.data_dir / thumbnail).is_file()
            )
            result[item["relative_path"]] = item
        return result

    def set_external_status(self, source_id, status):
        with self.context.connect() as conn:
            conn.execute(
                "UPDATE external_sources SET status_json=? WHERE id=?",
                (
                    json.dumps(status, ensure_ascii=False, separators=(",", ":")),
                    source_id,
                ),
            )

    def external_sources_status(self):
        with self.context.connect() as conn:
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
    def external_path(row: dict[str, Any] | sqlite3.Row, *, verify=True) -> Path:
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
                raise _ExternalFileChangedError("外部图片已经变化，请重新扫描后再操作")
            if "fingerprint" in row.keys():
                ExternalRecords.verify_external_fingerprint(
                    path, _load_json(row["fingerprint"])
                )
        return path

    def external_record(self, generation_id: str):
        with self.context.connect() as conn:
            return conn.execute(
                "SELECT e.*,s.root_path,s.enabled,s.name,s.type,s.permissions_json FROM external_records e JOIN external_sources s ON s.id=e.source_id WHERE e.generation_id=?",
                (generation_id,),
            ).fetchone()

    def external_generation_enabled(self, generation_id: str) -> bool:
        row = self.external_record(generation_id)
        return row is None or bool(row["enabled"] and row["available"])

    def external_display(self, generation_id: str, source: Any) -> dict[str, Any]:
        if source != "external":
            return {
                "is_external": False,
                "external_source": None,
                "allowed_actions": dict.fromkeys(_EXTERNAL_ACTIONS, True),
            }
        row = self.external_record(generation_id)
        return {
            "is_external": True,
            "external_source": {
                "id": row["source_id"],
                "name": row["name"],
                "type": row["type"],
            }
            if row
            else None,
            "allowed_actions": self.external_allowed_actions(row),
            "time_source": row["time_source"] if row else "",
        }

    @staticmethod
    def external_allowed_actions(row) -> dict[str, bool]:
        permissions = _load_json(row["permissions_json"]) if row else {}
        available = bool(row and row["enabled"] and row["available"])
        return {
            action: available and permissions.get(action, True) is True
            for action in _EXTERNAL_ACTIONS
        }

    def allowed_actions_for_generation(self, generation_id: str) -> dict[str, bool]:
        row = self.external_record(generation_id)
        return (
            self.external_allowed_actions(row)
            if row
            else dict.fromkeys(_EXTERNAL_ACTIONS, True)
        )

    def resolve_image_asset(self, row: dict[str, Any]) -> Path | None:
        generation_id = str(row.get("generation_id") or "")
        external = self.external_record(generation_id) if generation_id else None
        if external is not None and not (external["enabled"] and external["available"]):
            return None
        local = self.context.data_dir / str(row["path"])
        if _is_within(local, self.context.assets_dir) and local.is_file():
            return local
        if external is None:
            return None
        try:
            return self.external_path(external)
        except (FileNotFoundError, _ExternalFileChangedError) as exc:
            self.notify_external_issue(
                external, "missing" if isinstance(exc, FileNotFoundError) else "changed"
            )
            return None
        except (OSError, ValueError):
            return None

    def upsert_external_image(
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
        with self.context.connect() as conn:
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
        original = self.external_path(
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
        metadata = self.services.metadata_for_asset(digest, data)
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
        thumbnail = self.services.prepare_thumbnail(
            asset, max_edge=preview_max_edge, quality=preview_quality
        )
        generation_id = str(previous["generation_id"]) if previous else uuid.uuid4().hex
        # Thumbnail work can take long enough for the source to replace its file.
        self.verify_external_fingerprint(original, _load_json(fingerprint))
        with self.context.connect() as conn:
            conn.execute(
                "INSERT INTO image_assets(id,path,mime_type,size_bytes,width,height,created_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET width=excluded.width,height=excluded.height,file_state='available'",
                (digest, asset["path"], mime, size_bytes, width, height, time.time()),
            )
            self.services.upsert_thumbnails(conn, [thumbnail])
            self.services.save_metadata(conn, {digest: metadata})
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
            self.services.refresh_search(conn, generation_id)
        if previous and previous["asset_id"] != digest:
            self.services.purge_unreferenced_assets([str(previous["asset_id"])])
        return {
            "generation_id": generation_id,
            "image_id": image_id,
            "asset_id": digest,
        }

    def reconcile_external_source(self, source_id, seen_paths):
        with self.context.connect() as conn:
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
        self.services.purge_unreferenced_assets()
        return len(missing)

    def trim_disabled_external_thumbnails(self):
        with self.context.connect() as conn:
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
            _unlink_if_owned(
                self.context.data_dir / row["path"], self.context.thumbnails_dir
            )

    def read_gallery_file(
        self, image_id: str, path: Path, max_bytes: int | None = None
    ) -> bytes:
        try:
            with path.open("rb") as stream:
                content = (
                    stream.read() if max_bytes is None else stream.read(max_bytes + 1)
                )
            if max_bytes is not None and len(content) > max_bytes:
                raise ValueError("工作流图片不得超过 30 MiB")
            return content
        except FileNotFoundError as exc:
            # An external cleaner can remove the file after path resolution.
            with self.context.connect() as conn:
                row = conn.execute(
                    "SELECT generation_id FROM generation_images WHERE id=?",
                    (image_id,),
                ).fetchone()
            if row is not None:
                self.report_external_failure(str(row["generation_id"]), exc)
            raise

    def external_delete_preview(self, ids):
        preview = self.external_action_preview(ids, "delete")
        return {key: preview[key] for key in ("external_count", "external_sources")}

    def external_action_preview(self, ids, action):
        if action not in _EXTERNAL_ACTIONS:
            raise ValueError("外部图库操作无效")
        with self.context.connect() as conn:
            rows = conn.execute(
                "SELECT e.generation_id,e.source_id,e.available,s.name,s.enabled,s.permissions_json FROM external_records e JOIN external_sources s ON s.id=e.source_id "
                f"WHERE e.generation_id IN ({','.join('?' for _ in ids)})",
                ids,
            ).fetchall()
        denied = []
        for row in rows:
            if self.external_allowed_actions(row)[action]:
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

    def assert_external_action(self, generation_ids: list[str], action: str) -> None:
        preview = self.external_action_preview(generation_ids, action)
        if not preview["allowed"]:
            raise ExternalPermissionError(preview["denied"])

    def delete_external_original(
        self, generation_id: str
    ) -> ExternalDeleteError | None:
        row = self.external_record(generation_id)
        if row is None:
            return None
        self.assert_external_action([generation_id], "delete")
        if not row["enabled"]:
            raise ExternalDeleteError(generation_id, "外部图库已关闭，请重新启用后删除")
        try:
            path = self.external_path(row)
            fingerprint = _load_json(row["fingerprint"])
            self.verify_external_fingerprint(path, fingerprint)
            if hashlib.sha256(path.read_bytes()).hexdigest() != row["asset_id"]:
                raise _ExternalFileChangedError(
                    "外部图片内容已经变化，请重新扫描后再删除"
                )
        except FileNotFoundError as exc:
            # The source's retention cleaner won the race; remove only our index.
            self.report_external_failure(generation_id, exc)
            return None
        except (OSError, ValueError) as exc:
            self.report_external_failure(generation_id, exc)
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
                    self.verify_external_fingerprint(candidate, fingerprint)
                except FileNotFoundError as exc:
                    self.report_external_failure(generation_id, exc)
                    continue
                candidates.append(candidate)
        except (OSError, ValueError) as exc:
            self.report_external_failure(generation_id, exc)
            raise ExternalDeleteError(
                generation_id, f"外部参数文件已经变化，未删除原图：{exc}"
            ) from exc
        deleted: list[str] = []
        try:
            self.external_path(row)
            path.unlink()
            deleted.append(path.name)
            for candidate in candidates:
                # Do not unlink a sidecar replaced since the first validation.
                try:
                    if candidate.is_symlink():
                        raise ValueError("外部参数文件不能使用符号链接")
                    self.verify_external_fingerprint(
                        candidate, sidecars[candidate.name]
                    )
                    candidate.unlink()
                    deleted.append(candidate.name)
                except FileNotFoundError as exc:
                    self.report_external_failure(generation_id, exc)
                    continue
        except FileNotFoundError as exc:
            self.report_external_failure(generation_id, exc)
        except (OSError, ValueError) as exc:
            self.report_external_failure(generation_id, exc)
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
    def verify_external_fingerprint(path: Path, fingerprint: Any) -> None:
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
            raise _ExternalFileChangedError("文件已经被替换或修改，请重新扫描")
