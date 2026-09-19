"""Atomic image imports, idempotent batch receipts, merges, and edit revisions."""

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
    _atomic_write,
    _is_within,
)
from ..media.images import (
    _path_data_url,
    _validate_import_content,
)
from .constants import (
    _SAFE_ID_RE,
    _THUMBNAIL_REVISION_SQL,
)
from .context import GalleryContext
from .errors import (
    ImportDuplicateError,
    ImportEditConflictError,
)
from .projection import (
    _IMPORT_EDIT_FIELDS,
    _canonical_engine,
    _import_edit_existing_overrides,
    _import_edit_fields,
    _import_supplemental,
    _known_merge_engine,
    _load_json,
    _validate_import_hashes,
    _validate_import_overrides,
    project_import_metadata,
    refresh_import_supplemental,
)
from .storage import decode_metadata, decode_supplemental


@dataclass(frozen=True)
class ImportServices:
    """Explicit cross-repository operations; connections stay with their caller."""

    cleanup_orphaned_asset_files: Callable[..., Any]
    cleanup_orphaned_thumbnails: Callable[..., Any]
    metadata_for_asset: Callable[..., Any]
    prepare_asset: Callable[..., Any]
    prepare_thumbnail: Callable[..., Any]
    refresh_search: Callable[..., Any]
    save_metadata: Callable[..., Any]
    upsert_thumbnails: Callable[..., Any]


class ImportRepository:
    def __init__(self, context: GalleryContext, services: ImportServices) -> None:
        self.context = context
        self.services = services

    @staticmethod
    def refresh_import_projection(conn: sqlite3.Connection, generation_id: str) -> None:
        """Refresh derived import fields without guessing whether old overrides were manual."""

        record = conn.execute(
            "SELECT source, supplemental_json FROM generations WHERE id = ?",
            (generation_id,),
        ).fetchone()
        if record is None or record["source"] != "import":
            return
        record_supplemental = decode_supplemental(conn, record["supplemental_json"])
        images = conn.execute(
            "SELECT i.id, i.supplemental_json, m.metadata_json FROM generation_images i "
            "LEFT JOIN image_metadata m ON m.asset_id = i.asset_id "
            "WHERE i.generation_id = ? ORDER BY i.ordinal, i.id",
            (generation_id,),
        ).fetchall()
        prepared: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        for image in images:
            metadata = decode_metadata(conn, image["metadata_json"])
            previous = (
                decode_supplemental(conn, image["supplemental_json"], metadata)
                or record_supplemental
            )
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
        ImportRepository.update_import_summary(
            conn, generation_id, record_supplemental, prepared
        )

    @staticmethod
    def update_import_summary(
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
    def import_edit_rows(
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
    def import_edit_revision(record: sqlite3.Row, rows: list[sqlite3.Row]) -> str:
        from ..metadata.comfyui.user_rules import get_rules

        # Include stored JSON, not its display projection, so concurrent edits,
        # metadata repairs, appends and deletions cannot silently replace changes.
        snapshot = [
            get_rules().fingerprint,
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
    def import_edit_previous(
        conn: sqlite3.Connection,
        record: sqlite3.Row,
        row: sqlite3.Row,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        previous = decode_supplemental(
            conn, row["supplemental_json"], metadata
        ) or decode_supplemental(conn, record["supplemental_json"], metadata)
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
    def import_edit_item_revision(record: sqlite3.Row, row: sqlite3.Row) -> str:
        from ..metadata.comfyui.user_rules import get_rules

        snapshot = [
            get_rules().fingerprint,
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

    def import_edit_snapshot(
        self,
        generation_id: str,
        light: bool = False,
        image_id: str = "",
        item_revision: str = "",
        include_preview: bool = True,
    ) -> dict[str, Any]:
        with self.context.connect() as conn:
            conn.execute("BEGIN")
            record, rows = self.import_edit_rows(conn, generation_id, image_id)
            revision = "" if image_id else self.import_edit_revision(record, rows)
            decoded = {}
            if not light or image_id:
                for row in rows:
                    metadata = decode_metadata(conn, row["metadata_json"])
                    decoded[row["id"]] = (
                        metadata,
                        self.import_edit_previous(conn, record, row, metadata),
                    )
        if image_id:
            current_revision = self.import_edit_item_revision(record, rows[0])
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
                        "item_revision": self.import_edit_item_revision(record, row),
                    }
                    for row in rows
                ],
            }
        items = []
        for row in rows:
            metadata, previous = decoded[row["id"]]
            overrides = _import_edit_existing_overrides(previous, metadata)
            metadata = project_import_metadata(metadata, overrides)
            previous = refresh_import_supplemental(previous, metadata)
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
                        self.context.data_dir / str(row["thumbnail_path"] or ""),
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

    def project_import_edit_image(
        self, generation_id: str, image_id: str, item_revision: str, output_node_id: str
    ) -> dict[str, Any]:
        with self.context.connect() as conn:
            conn.execute("BEGIN")
            record, rows = self.import_edit_rows(conn, generation_id, image_id)
            cached_metadata = decode_metadata(conn, rows[0]["metadata_json"])
        if self.import_edit_item_revision(record, rows[0]) != item_revision:
            raise ImportEditConflictError("图片已被修改或重新排序，请重新打开编辑窗口")
        metadata = project_import_metadata(
            cached_metadata, {"comfy_output_node": output_node_id}
        )
        return {key: value for key, value in metadata.items() if key != "raw"}

    def edit_import(
        self, generation_id: str, revision: str, items: list[dict[str, Any]]
    ) -> dict[str, Any]:
        with self.context.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            record, rows = self.import_edit_rows(conn, generation_id)
            if self.import_edit_revision(record, rows) != revision:
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
                metadata = decode_metadata(conn, row["metadata_json"])
                previous = self.import_edit_previous(conn, record, row, metadata)
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
                self.update_import_summary(
                    conn,
                    generation_id,
                    decode_supplemental(conn, record["supplemental_json"]),
                    prepared,
                )
                self.services.refresh_search(conn, generation_id)
            record, rows = self.import_edit_rows(conn, generation_id)
            return {
                "generation_id": generation_id,
                "revision": self.import_edit_revision(record, rows),
                "image_ids": [row["id"] for row in rows],
            }

    def import_image(
        self,
        data: bytes,
        filename: str,
        overrides: dict[str, Any],
        import_key: str,
        preview_max_edge: int,
        preview_quality: int,
    ) -> dict[str, Any]:
        result = self.commit_import_batch(
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

    def stage_import_file(self, path: Path, data: bytes) -> None:
        if not isinstance(path, Path) or not _is_within(path, self.context.imports_dir):
            raise ValueError("待导入图片路径不属于导入暂存目录")
        if path.resolve() == self.context.imports_dir.resolve():
            raise ValueError("待导入图片路径必须是文件路径")
        _validate_import_content(data)
        self.services.metadata_for_asset(
            hashlib.sha256(data).hexdigest(), data, strict=True
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, data)

    def read_import_file(self, path: Any) -> bytes:
        if not isinstance(path, (str, Path)):
            raise ValueError("待导入图片路径无效")
        source = Path(path)
        if not _is_within(source, self.context.imports_dir) or not source.is_file():
            raise ValueError("待导入图片不存在或不属于导入暂存目录")
        with source.open("rb") as stream:
            data = stream.read(30 * 1024 * 1024 + 1)
        _validate_import_content(data)
        return data

    def import_group(
        self,
        entries: list[dict[str, Any]],
        import_key: str,
        preview_max_edge: int,
        preview_quality: int,
    ) -> dict[str, Any]:
        result = self.commit_import_batch(
            self.declare_import_entries(entries),
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

    def declare_import_entries(
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
                        self.read_import_file(item.get("path"))
                    ).hexdigest()
                except (ValueError, OSError) as exc:
                    raise ValueError(
                        f"{item.get('filename') or '待导入图片'}：{exc}"
                    ) from exc
            declared.append(item)
        return declared

    def prepare_import_entries(
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
                    else self.read_import_file(entry.get("path"))
                )
                _validate_import_content(data)
                digest = hashlib.sha256(data).hexdigest()
                if digest != entry.get("sha256"):
                    raise ValueError("实际图片 SHA-256 与声明不一致，请重新选择图片")
                metadata = self.services.metadata_for_asset(digest, data, strict=True)
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

    def prepare_import_assets(
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
                else self.read_import_file(item["path"])
            )
            if hashlib.sha256(data).hexdigest() != item["digest"]:
                raise ValueError("待导入图片在准备过程中发生变化，请重新上传")
            asset = self.services.prepare_asset(data, "")
            assets[asset["id"]] = asset
            thumbnails[asset["id"]] = self.services.prepare_thumbnail(
                asset,
                max_edge=preview_max_edge,
                quality=preview_quality,
            )
        return assets, thumbnails

    @staticmethod
    def merge_target(
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

    def append_import_group(
        self,
        entries: list[dict[str, Any]],
        target_id: str,
        import_key: str,
        expected_engine: str,
        preview_max_edge: int,
        preview_quality: int,
    ) -> dict[str, Any]:
        result = self.commit_import_batch(
            self.declare_import_entries(entries),
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

    def check_import_hashes(self, hashes: list[str]) -> dict[str, Any]:
        with self.context.connect() as conn:
            try:
                self.assert_import_hashes_available(conn, hashes)
            except ImportDuplicateError as exc:
                return exc.as_dict()
        return {"allowed": True, "duplicate_hashes": []}

    @staticmethod
    def assert_import_hashes_available(
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

    def import_batch_receipt(
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

    def get_import_batch_result(self, import_key: str) -> dict[str, Any] | None:
        with self.context.connect() as conn:
            row = conn.execute(
                "SELECT result_json FROM import_batches WHERE id = ? AND expires_at > ?",
                (import_key, time.time()),
            ).fetchone()
        return _load_json(row["result_json"]) if row is not None else None

    def commit_import_batch(
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
        with self.context.connect() as conn:
            previous = self.import_batch_receipt(conn, import_key, fingerprint)
            if previous is not None:
                return previous
        prepared = self.prepare_import_entries(entries)
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
            with self.context.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                previous = self.import_batch_receipt(conn, import_key, fingerprint)
                if previous is not None:
                    return previous
                self.assert_import_hashes_available(conn, hashes)
                target = None
                existing_images: list[dict[str, Any]] = []
                if mode == "merge":
                    target, existing_images = self.merge_target(
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
                assets, thumbnails = self.prepare_import_assets(
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
                self.services.upsert_thumbnails(conn, thumbnails.values())
                self.services.save_metadata(
                    conn, {item["digest"]: item["metadata"] for item in prepared}
                )
                if target is not None:
                    next_ordinal = (
                        max(int(item["ordinal"]) for item in existing_images) + 1
                    )
                    self.insert_import_images(conn, target_id, prepared, next_ordinal)
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
                    self.services.refresh_search(conn, target_id)
                    generation_ids = [target_id]
                else:
                    groups = (
                        [[item] for item in prepared]
                        if mode == "separate"
                        else [prepared]
                    )
                    generation_ids = [
                        self.insert_import_record(
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
            with self.context.connect() as cleanup_conn:
                cleanup_conn.execute("BEGIN IMMEDIATE")
                self.services.cleanup_orphaned_asset_files()
                self.services.cleanup_orphaned_thumbnails()
            raise

    def insert_import_record(
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
        self.insert_import_images(conn, generation_id, prepared, 0)
        self.services.refresh_search(conn, generation_id)
        return generation_id

    @staticmethod
    def insert_import_images(
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

    def delete_expired_import_batches(self, now: float) -> int:
        with self.context.connect() as conn:
            return max(
                0,
                conn.execute(
                    "DELETE FROM import_batches WHERE expires_at <= ?", (now,)
                ).rowcount,
            )
