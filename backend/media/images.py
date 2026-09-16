"""Image formats, thumbnails, and filenames independent of gallery storage."""

from __future__ import annotations

import base64
import io
import re
import time
from pathlib import Path
from typing import Any

from .files import _atomic_write

_IMAGE_SUFFIXES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


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


def _validate_import_content(data: bytes) -> None:
    from PIL import Image

    from ..metadata.parser import MAX_PIXELS

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
