"""Bounded PNG, EXIF and stealth extraction; format interpretation stays separate."""

from __future__ import annotations
import io
import warnings
import zlib
from typing import Any
from PIL import Image
from .common import _decode_comment
from .novelai import _read_novelai_stealth


def read_image_metadata(
    data: bytes, *, max_image_bytes: int, max_pixels: int, max_metadata_bytes: int
) -> tuple[dict, int, int, dict | None, str]:
    """Read embedded text without executing workflows or fetching resources."""
    if not data or len(data) > max_image_bytes:
        raise ValueError("图片为空或超过 64 MiB 限制")
    fields: dict[str, Any] = {}
    stealth: dict[str, Any] | None = None
    stealth_warning = ""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                width, height = image.size
                if width * height > max_pixels:
                    raise ValueError("图片超过 6400 万像素限制")
                if image.format == "PNG":
                    # Pillow reads trailing PNG text chunks lazily.
                    fields.update(image.text)
                for key, value in image.info.items():
                    if isinstance(value, str):
                        fields.setdefault(key, value)
                    elif key in {"xmp", "XML:com.adobe.xmp"} and isinstance(
                        value, bytes
                    ):
                        fields[key] = value.decode("utf-8", errors="replace")
                exif = image.getexif()
                tags = dict(exif)
                if 34665 in exif:
                    tags.update(exif.get_ifd(34665))
                for tag, key in (
                    (270, "ImageDescription"),
                    (271, "Make"),
                    (272, "Model"),
                    (305, "Software"),
                    (306, "DateTime"),
                    (315, "Artist"),
                    (33432, "Copyright"),
                    (36867, "DateTimeOriginal"),
                    (36868, "DateTimeDigitized"),
                    (36880, "OffsetTime"),
                    (36881, "OffsetTimeOriginal"),
                    (36882, "OffsetTimeDigitized"),
                    (37520, "SubSecTime"),
                    (37521, "SubSecTimeOriginal"),
                    (37522, "SubSecTimeDigitized"),
                    (37510, "UserComment"),
                ):
                    value = tags.get(tag)
                    if isinstance(value, (str, bytes)):
                        fields.setdefault(key, _decode_comment(value))
                if image.format == "PNG":
                    try:
                        stealth = _read_novelai_stealth(
                            image, max_metadata_bytes=max_metadata_bytes
                        )
                    except (
                        ValueError,
                        TypeError,
                        OSError,
                        EOFError,
                        RecursionError,
                        zlib.error,
                    ) as exc:
                        stealth_warning = f"NovelAI 隐写元数据无效，已忽略：{exc}"
    except (
        OSError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise ValueError("无法读取图片或图片尺寸超出限制") from exc
    return fields, width, height, stealth, stealth_warning
