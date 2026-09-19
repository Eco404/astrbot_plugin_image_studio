"""Canonical image assets, staged references, thumbnails, and session access."""

from __future__ import annotations

import hashlib
import sqlite3
import time
import uuid
from typing import Any

from ..media.files import (
    _atomic_write,
    _is_within,
    _unlink_if_owned,
)
from ..media.images import (
    _create_thumbnail,
    _image_dimensions,
    _image_is_decodable,
    _image_suffix,
    detect_mime_type,
)
from ..models import (
    GeneratedImage,
    ReferenceImage,
    WorkflowImageAsset,
    WorkflowImageLoadResult,
)
from .constants import (
    _SAFE_ID_RE,
    _SHA256_RE,
    WORKFLOW_ASSET_RETENTION_SECONDS,
)
from .context import GalleryContext


class AssetRepository:
    def __init__(self, context: GalleryContext) -> None:
        self.context = context

    def load_staged_references(
        self, reference_ids: list[str]
    ) -> tuple[ReferenceImage, ...]:
        refs: list[ReferenceImage] = []
        for ref_id in reference_ids:
            clean_id = str(ref_id or "").strip().lower()
            if not _SAFE_ID_RE.fullmatch(clean_id):
                raise ValueError("参考图 ID 无效")
            matches = list(self.context.staging_dir.glob(f"{clean_id}.*"))
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

    def discard_staged_references(self, reference_ids: list[str]) -> None:
        for ref_id in reference_ids:
            if not _SAFE_ID_RE.fullmatch(ref_id):
                continue
            for path in self.context.staging_dir.glob(f"{ref_id}.*"):
                _unlink_if_owned(path, self.context.staging_dir)

    def lease_agent_images(
        self,
        images: tuple[GeneratedImage, ...],
        scope_id: str,
        create_preview: bool,
        preview_max_edge: int,
        preview_quality: int,
    ) -> tuple[WorkflowImageAsset, ...]:
        normalized_scope = str(scope_id or "").strip()
        if not normalized_scope:
            raise ValueError("Agent 资产缺少会话范围")
        self.expire_asset_retention(time.time())
        max_edge = max(256, min(2048, int(preview_max_edge)))
        quality = max(40, min(95, int(preview_quality)))
        assets: list[WorkflowImageAsset] = []
        prepared_assets: dict[str, dict[str, Any]] = {}
        prepared_thumbnails: dict[str, dict[str, Any]] = {}
        for image in images:
            asset = self.prepare_asset(image.data, image.mime_type)
            prepared_assets[asset["id"]] = asset
            preview: GeneratedImage | None = None
            if create_preview:
                thumbnail = self.prepare_thumbnail(
                    asset,
                    max_edge=max_edge,
                    quality=quality,
                )
                prepared_thumbnails[asset["id"]] = thumbnail
                preview_data = (self.context.data_dir / thumbnail["path"]).read_bytes()
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
        now = time.time()
        retention_deadline = now + WORKFLOW_ASSET_RETENTION_SECONDS
        with self.context.connect() as conn:
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
            self.upsert_thumbnails(conn, prepared_thumbnails.values())
            for asset in prepared_assets.values():
                # The legacy table stores two independent facts: the row grants
                # access until the asset is deleted, and its deadlines protect
                # otherwise unreferenced files. Both deadline columns now agree.
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
                        retention_deadline,
                        retention_deadline,
                    ),
                )
        return tuple(assets)

    def load_workflow_image_detailed(
        self,
        asset_id: str,
        scope_id: str,
        detail: str,
        preview_max_edge: int,
        preview_quality: int,
    ) -> WorkflowImageLoadResult:
        clean_id = str(asset_id or "").strip().lower()
        normalized_scope = str(scope_id or "").strip()
        if not _SHA256_RE.fullmatch(clean_id):
            return WorkflowImageLoadResult(clean_id, "invalid_asset_id")
        if not normalized_scope:
            return WorkflowImageLoadResult(clean_id, "access_denied")
        with self.context.connect() as conn:
            access = conn.execute(
                "SELECT 1 FROM agent_asset_leases WHERE asset_id = ? AND scope_id = ?",
                (clean_id, normalized_scope),
            ).fetchone()
            row = conn.execute(
                "SELECT path, mime_type, size_bytes FROM image_assets WHERE id = ?",
                (clean_id,),
            ).fetchone()
        if row is None:
            return WorkflowImageLoadResult(clean_id, "not_found")
        if access is None:
            return WorkflowImageLoadResult(clean_id, "access_denied")
        original = self.context.data_dir / str(row["path"])
        if not original.is_file() or not _is_within(original, self.context.assets_dir):
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
            thumbnail = self.prepare_thumbnail(
                {
                    "id": clean_id,
                    "path": str(row["path"]),
                    "mime_type": mime_type,
                    "size_bytes": int(row["size_bytes"]),
                },
                max_edge=max_edge,
                quality=quality,
            )
            with self.context.connect() as conn:
                self.upsert_thumbnails(conn, [thumbnail])
            preview_path = self.context.data_dir / thumbnail["path"]
            preview_data = preview_path.read_bytes()
            image = GeneratedImage(
                data=preview_data,
                mime_type=detect_mime_type(preview_data, "image/webp"),
            )
        now = time.time()
        with self.context.connect() as conn:
            conn.execute(
                "UPDATE agent_asset_leases SET last_accessed_at = ?, expires_at = ?, hard_expires_at = ? "
                "WHERE asset_id = ? AND scope_id = ?",
                (
                    now,
                    now + WORKFLOW_ASSET_RETENTION_SECONDS,
                    now + WORKFLOW_ASSET_RETENTION_SECONDS,
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

    def expire_asset_retention(self, now: float) -> int:
        """Release expired cleanup protection without revoking session access."""
        with self.context.connect() as conn:
            cursor = conn.execute(
                "UPDATE agent_asset_leases SET expires_at = 0, hard_expires_at = 0 "
                "WHERE (expires_at != 0 OR hard_expires_at != 0) "
                "AND (expires_at <= ? OR hard_expires_at <= ?)",
                (now, now),
            )
        return max(0, int(cursor.rowcount))

    def prepare_asset(self, data: bytes, mime_hint: str) -> dict[str, Any]:
        digest = hashlib.sha256(data).hexdigest()
        mime_type = detect_mime_type(data, mime_hint)
        suffix = _image_suffix(mime_type, data)
        relative_path = (
            self.context.assets_dir.relative_to(self.context.data_dir)
            / digest[:2]
            / f"{digest}{suffix}"
        )
        target = self.context.data_dir / relative_path
        try:
            current_size = target.stat().st_size
        except OSError:
            current_size = -1
        if current_size != len(data):
            _atomic_write(target, data)
        width, height = _image_dimensions(data)
        return {
            "id": digest,
            "path": relative_path.as_posix(),
            "mime_type": mime_type,
            "size_bytes": len(data),
            "width": width,
            "height": height,
        }

    def prepare_thumbnail(
        self,
        asset: dict[str, Any],
        *,
        max_edge: int,
        quality: int,
    ) -> dict[str, Any]:
        max_edge = max(256, min(2048, int(max_edge)))
        quality = max(40, min(95, int(quality)))
        relative_path = (
            self.context.thumbnails_dir.relative_to(self.context.data_dir)
            / f"{asset['id']}.webp"
        )
        target = self.context.data_dir / relative_path
        with self.context.connect() as conn:
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
                asset.get("original_path") or self.context.data_dir / asset["path"],
                target,
                max_edge=max_edge,
                quality=quality,
            )
        raw = target.read_bytes()
        return {
            "asset_id": asset["id"],
            "path": relative_path.as_posix(),
            "mime_type": detect_mime_type(raw, "image/webp"),
            "size_bytes": len(raw),
            "max_edge": max_edge,
            "quality": quality,
        }

    @staticmethod
    def upsert_thumbnails(conn: sqlite3.Connection, thumbnails: Any) -> None:
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
