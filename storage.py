"""Durable gallery, reference, and staging storage."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from .config import HistorySettings
from .models import GeneratedImage, GenerationRequest, ImageProvider, ReferenceImage


_IMAGE_SUFFIXES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
_SAFE_ID_RE = re.compile(r"^[a-f0-9]{32}$")


class GenerationStore:
    """Store plugin-owned gallery files and queryable generation metadata."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir.resolve()
        self.history_dir = self.data_dir / "history"
        self.images_dir = self.history_dir / "images"
        self.thumbnails_dir = self.history_dir / "thumbnails"
        self.references_dir = self.history_dir / "references"
        self.staging_dir = self.data_dir / "staging_references"
        self.exports_dir = self.data_dir / "exports"
        self.db_path = self.data_dir / "history.sqlite3"
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Create directories and database tables."""

        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        for directory in (
            self.data_dir,
            self.images_dir,
            self.thumbnails_dir,
            self.references_dir,
            self.staging_dir,
            self.exports_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
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
                    error_message TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS generation_images (
                    id TEXT PRIMARY KEY,
                    generation_id TEXT NOT NULL REFERENCES generations(id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL,
                    path TEXT NOT NULL,
                    thumbnail_path TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS generation_references (
                    id TEXT PRIMARY KEY,
                    generation_id TEXT NOT NULL REFERENCES generations(id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL,
                    filename TEXT NOT NULL,
                    path TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    available INTEGER NOT NULL DEFAULT 1,
                    deleted_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_generations_created_at ON generations(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_generations_provider ON generations(provider_id);
                CREATE INDEX IF NOT EXISTS idx_generation_images_generation ON generation_images(generation_id);
                CREATE INDEX IF NOT EXISTS idx_generation_references_generation ON generation_references(generation_id);
                """
            )
            try:
                conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS generation_search USING fts5("
                    "generation_id UNINDEXED, original_prompt, final_prompt, provider_name, model)"
                )
            except sqlite3.OperationalError:
                pass

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

    async def record_success(
        self,
        *,
        provider: ImageProvider,
        request: GenerationRequest,
        images: tuple[GeneratedImage, ...],
        elapsed_ms: int,
        history: HistorySettings,
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
    ) -> str:
        generation_id = uuid.uuid4().hex
        created_at = time.time()
        image_rows: list[tuple[str, int, str, str, str, int, str]] = []
        reference_rows: list[tuple[str, int, str, str, str, int, int]] = []
        generation_root = self.images_dir / generation_id
        reference_root = self.references_dir / generation_id
        try:
            for ordinal, image in enumerate(images):
                image_id = uuid.uuid4().hex
                mime_type = detect_mime_type(image.data, image.mime_type)
                suffix = _image_suffix(mime_type, image.data)
                relative_path = (
                    Path("history") / "images" / generation_id / f"{image_id}{suffix}"
                )
                target = self.data_dir / relative_path
                _atomic_write(target, image.data)
                thumbnail_relative = Path("history") / "thumbnails" / f"{image_id}.webp"
                _create_thumbnail(target, self.data_dir / thumbnail_relative)
                image_rows.append(
                    (
                        image_id,
                        ordinal,
                        str(relative_path),
                        str(thumbnail_relative),
                        mime_type,
                        len(image.data),
                        hashlib.sha256(image.data).hexdigest(),
                    )
                )
            if retain_references:
                for ordinal, reference in enumerate(request.references):
                    reference_id = uuid.uuid4().hex
                    mime_type = detect_mime_type(reference.data, reference.mime_type)
                    suffix = _image_suffix(mime_type, reference.data)
                    relative_path = (
                        Path("history")
                        / "references"
                        / generation_id
                        / f"{reference_id}{suffix}"
                    )
                    _atomic_write(self.data_dir / relative_path, reference.data)
                    reference_rows.append(
                        (
                            reference_id,
                            ordinal,
                            _safe_filename(reference.filename) or f"reference{suffix}",
                            str(relative_path),
                            mime_type,
                            len(reference.data),
                            1,
                        )
                    )
            parameters = _redact_sensitive(
                {
                    "negative_prompt": request.negative_prompt,
                    "size": request.size,
                    "count": request.count,
                    "parameters": request.parameters,
                }
            )
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO generations (id, created_at, source, status, mode, provider_id, provider_name, "
                    "provider_kind, model, original_prompt, final_prompt, parameters_json, elapsed_ms) "
                    "VALUES (?, ?, ?, 'succeeded', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                        request.prompt,
                        json.dumps(
                            parameters, ensure_ascii=False, separators=(",", ":")
                        ),
                        elapsed_ms,
                    ),
                )
                conn.executemany(
                    "INSERT INTO generation_images (id, generation_id, ordinal, path, thumbnail_path, mime_type, size_bytes, sha256) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [(image_id, generation_id, *row) for image_id, *row in image_rows],
                )
                if reference_rows:
                    conn.executemany(
                        "INSERT INTO generation_references (id, generation_id, ordinal, filename, path, mime_type, size_bytes, available) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        [
                            (reference_id, generation_id, *row)
                            for reference_id, *row in reference_rows
                        ],
                    )
                try:
                    conn.execute(
                        "INSERT INTO generation_search (generation_id, original_prompt, final_prompt, provider_name, model) VALUES (?, ?, ?, ?, ?)",
                        (
                            generation_id,
                            request.prompt,
                            request.prompt,
                            provider.name,
                            request.model or provider.model,
                        ),
                    )
                except sqlite3.OperationalError:
                    pass
            return generation_id
        except Exception:
            shutil.rmtree(generation_root, ignore_errors=True)
            shutil.rmtree(reference_root, ignore_errors=True)
            for row in image_rows:
                _unlink_if_owned(self.data_dir / row[3], self.thumbnails_dir)
            raise

    async def list_generations(self, filters: dict[str, Any]) -> dict[str, Any]:
        """Return a paginated gallery collection and aggregate filter values."""

        return await asyncio.to_thread(self._list_generations_sync, filters)

    def _list_generations_sync(self, filters: dict[str, Any]) -> dict[str, Any]:
        limit = max(1, min(60, _as_int(filters.get("limit"), 24)))
        offset = max(0, _as_int(filters.get("offset"), 0))
        where: list[str] = []
        args: list[Any] = []
        query = str(filters.get("query") or "").strip()[:240]
        provider_id = str(filters.get("provider_id") or "").strip()[:64]
        mode = str(filters.get("mode") or "").strip()[:20]
        source = str(filters.get("source") or "").strip()[:30]
        if provider_id:
            where.append("g.provider_id = ?")
            args.append(provider_id)
        if mode in {"text2img", "img2img"}:
            where.append("g.mode = ?")
            args.append(mode)
        if source:
            where.append("g.source = ?")
            args.append(source)
        if query:
            where.append(
                "(g.original_prompt LIKE ? OR g.final_prompt LIKE ? OR g.provider_name LIKE ? OR g.model LIKE ?)"
            )
            token = f"%{query}%"
            args.extend([token, token, token, token])
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self._connect() as conn:
            total = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM generations g {clause}", args
                ).fetchone()[0]
            )
            rows = conn.execute(
                f"SELECT g.*, i.id AS image_id, i.thumbnail_path, i.size_bytes, i.mime_type "
                f"FROM generations g JOIN generation_images i ON i.generation_id = g.id "
                f"AND i.ordinal = 0 {clause} ORDER BY g.created_at DESC LIMIT ? OFFSET ?",
                [*args, limit, offset],
            ).fetchall()
            provider_options = [
                dict(row)
                for row in conn.execute(
                    "SELECT provider_id AS id, MAX(provider_name) AS name FROM generations GROUP BY provider_id ORDER BY name"
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
                "modes": ["text2img", "img2img"],
                "sources": ["webui", "command", "llm_tool"],
            },
        }

    async def generation_detail(
        self, generation_id: str, *, include_assets: bool = True
    ) -> dict[str, Any] | None:
        """Return a generation plus its result and reference assets."""

        return await asyncio.to_thread(
            self._generation_detail_sync, generation_id, include_assets
        )

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
                "SELECT id, filename, path, mime_type FROM generation_references "
                "WHERE generation_id = ? AND available = 1 ORDER BY ordinal",
                (generation_id,),
            ).fetchall()
        staged: list[dict[str, str]] = []
        for row in rows:
            source = self.data_dir / str(row["path"])
            if not source.is_file() or not _is_within(source, self.references_dir):
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
                "SELECT * FROM generation_images WHERE generation_id = ? ORDER BY ordinal",
                (generation_id,),
            ).fetchall()
            reference_rows = conn.execute(
                "SELECT * FROM generation_references WHERE generation_id = ? ORDER BY ordinal",
                (generation_id,),
            ).fetchall()
        result = dict(row)
        result["parameters"] = _load_json(result.pop("parameters_json", "{}"))
        result["images"] = [
            self._asset_item(dict(item), preview_full=include_assets)
            for item in image_rows
        ]
        result["references"] = [
            self._reference_item(dict(item), include_data=include_assets)
            for item in reference_rows
        ]
        return result

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
            conn.execute("DELETE FROM generations WHERE id = ?", (generation_id,))
            try:
                conn.execute(
                    "DELETE FROM generation_search WHERE generation_id = ?",
                    (generation_id,),
                )
            except sqlite3.OperationalError:
                pass
        shutil.rmtree(self.images_dir / generation_id, ignore_errors=True)
        shutil.rmtree(self.references_dir / generation_id, ignore_errors=True)
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
                "SELECT path, available FROM generation_references WHERE id = ?",
                (reference_id,),
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                "UPDATE generation_references SET available = 0, deleted_at = ? WHERE id = ?",
                (time.time(), reference_id),
            )
        _unlink_if_owned(self.data_dir / str(row["path"]), self.references_dir)
        return True

    async def export_generations(self, generation_ids: list[str]) -> Path:
        """Build a flat ZIP containing one redacted JSON beside every result."""

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
                    if path.is_file() and _is_within(path, self.images_dir):
                        stem = _export_stem(
                            detail,
                            image_index=image_index,
                            image_count=len(images),
                            used_stems=used_stems,
                        )
                        image_filename = f"{stem}{path.suffix.lower()}"
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
        return {
            "id": row["id"],
            "created_at": row["created_at"],
            "source": row["source"],
            "mode": row["mode"],
            "provider_id": row["provider_id"],
            "provider_name": row["provider_name"],
            "model": row["model"],
            "prompt_preview": row["original_prompt"][:180],
            "elapsed_ms": row["elapsed_ms"],
            "image_id": row["image_id"],
            "mime_type": row["mime_type"],
            "size_bytes": row["size_bytes"],
            "thumbnail_data_url": _path_data_url(thumbnail, "image/webp"),
        }

    def _asset_item(self, row: dict[str, Any], *, preview_full: bool) -> dict[str, Any]:
        path = self.data_dir / str(row["path"])
        preview = _path_data_url(path, row["mime_type"]) if preview_full else ""
        return {
            "id": row["id"],
            "path": row["path"],
            "mime_type": row["mime_type"],
            "size_bytes": row["size_bytes"],
            "sha256": row["sha256"],
            "data_url": preview,
        }

    def _reference_item(
        self, row: dict[str, Any], *, include_data: bool = True
    ) -> dict[str, Any]:
        path = self.data_dir / str(row["path"])
        available = (
            bool(row["available"])
            and path.is_file()
            and _is_within(path, self.references_dir)
        )
        return {
            "id": row["id"],
            "filename": row["filename"],
            "mime_type": row["mime_type"],
            "size_bytes": row["size_bytes"],
            "available": available,
            "deleted_at": row["deleted_at"],
            "data_url": (
                _path_data_url(path, row["mime_type"])
                if available and include_data
                else ""
            ),
        }

    def _cleanup_sync(self, settings: HistorySettings) -> None:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM generations ORDER BY created_at DESC"
            ).fetchall()
        remove_ids: list[str] = []
        if settings.max_records >= 0:
            remove_ids.extend(str(row["id"]) for row in rows[settings.max_records :])
        if settings.max_megabytes > 0:
            with self._connect() as conn:
                size_rows = conn.execute(
                    "SELECT g.id, g.created_at, COALESCE(SUM(i.size_bytes), 0) AS bytes "
                    "FROM generations g LEFT JOIN generation_images i ON i.generation_id = g.id "
                    "GROUP BY g.id ORDER BY g.created_at DESC"
                ).fetchall()
            total = 0
            for row in size_rows:
                total += int(row["bytes"])
                if total > settings.max_megabytes * 1024 * 1024:
                    remove_ids.append(str(row["id"]))
        for generation_id in dict.fromkeys(remove_ids):
            self._delete_generation_sync(generation_id)

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


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _create_thumbnail(source: Path, target: Path) -> None:
    try:
        from PIL import Image, ImageOps

        with Image.open(source) as image:
            thumbnail = ImageOps.exif_transpose(image).convert("RGB")
            thumbnail.thumbnail((420, 420), Image.Resampling.LANCZOS)
            target.parent.mkdir(parents=True, exist_ok=True)
            thumbnail.save(target, "WEBP", quality=82, method=4)
    except Exception:
        # Gallery stays functional when an upstream returns an unsupported image.
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
    mode = "i2i" if detail.get("mode") == "img2img" else "t2i"
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


def _load_json(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


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
