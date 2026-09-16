"""Restore the untouched canvas around official NovelAI infill results."""

from __future__ import annotations

import base64
import binascii
import io
import json
import warnings
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from PIL import Image, PngImagePlugin

from ...metadata.parser import MAX_IMAGE_BYTES, MAX_PIXELS, parse_image_metadata

if TYPE_CHECKING:
    from ...models import GeneratedImage


def _load_image(data: bytes, label: str) -> Image.Image:
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"NovelAI 局部重绘{label}为空或超过 64 MiB 限制")
    image = None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image = Image.open(io.BytesIO(data))
            if image.width * image.height > MAX_PIXELS:
                image.close()
                raise ValueError(f"NovelAI 局部重绘{label}超过图片像素上限")
            image.load()
            return image
    except (
        OSError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        if image is not None:
            image.close()
        raise ValueError(f"NovelAI 局部重绘{label}无法读取") from exc


def _payload_image(value: Any, label: str) -> Image.Image:
    if not isinstance(value, str) or len(value) > (MAX_IMAGE_BYTES + 2) // 3 * 4:
        raise ValueError(f"NovelAI 局部重绘{label}缺失或超过大小上限")
    try:
        data = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError(f"NovelAI 局部重绘{label}不是有效 Base64") from exc
    return _load_image(data, label)


def _save_composite(
    composed: Image.Image,
    generated: Image.Image,
    data: bytes,
    image_format: str,
) -> bytes:
    """Preserve provider metadata without writing new alpha LSBs into the canvas."""

    raw = parse_image_metadata(data)["raw"]
    options: dict[str, Any] = {}
    for key in ("exif", "icc_profile"):
        if generated.info.get(key):
            options[key] = generated.info[key]
    if image_format == "png":
        info = PngImagePlugin.PngInfo()
        for key, value in raw.items():
            # Includes decoded stealth fields and its complete provenance snapshot.
            # Re-embedding alpha metadata would modify otherwise untouched pixels.
            text = (
                value
                if isinstance(value, str)
                else json.dumps(value, ensure_ascii=False)
            )
            info.add_itxt(key, text)
        options["pnginfo"] = info
        if generated.info.get("dpi"):
            options["dpi"] = generated.info["dpi"]
    else:
        options.update(lossless=True, exact=True)
        if generated.info.get("xmp"):
            options["xmp"] = generated.info["xmp"]
        if generated.format == "PNG" and raw:
            # WebP has no PNG text chunks. An EXIF Unicode UserComment keeps all
            # fields and is understood by our existing metadata container parser.
            snapshot: dict[str, Any] = {"png_text": raw}
            original_exif = generated.info.get("exif")
            exif = generated.getexif()
            if original_exif:
                snapshot["original_exif_base64"] = base64.b64encode(
                    original_exif
                ).decode("ascii")
            exif[37510] = b"UNICODE\x00" + json.dumps(
                snapshot, ensure_ascii=False
            ).encode("utf-16")
            options["exif"] = exif.tobytes()
    output = io.BytesIO()
    composed.save(output, image_format.upper(), **options)
    return output.getvalue()


def composite_inpaint_results(
    images: tuple[GeneratedImage, ...], payload: dict[str, Any]
) -> tuple[GeneratedImage, ...]:
    """Composite white-mask edits over the exact input canvas for an infill request.

    Black-mask pixels, including alpha and transparent RGB, remain unchanged.
    The input payload is the prepared wire request, before any temporary image
    fields are removed. Other actions are returned unchanged. A bad response
    size raises a positional error instead of silently stretching the image.
    """

    if payload.get("action") != "infill" or not images:
        return images
    parameters = payload.get("parameters")
    if not isinstance(parameters, dict):
        raise ValueError("NovelAI 局部重绘请求缺少 parameters")  # noqa: TRY004 -- malformed prepared payload
    width, height = parameters.get("width"), parameters.get("height")
    if any(type(value) is not int or value < 1 for value in (width, height)):
        raise ValueError("NovelAI 局部重绘请求尺寸无效")
    size = (width, height)
    with (
        _payload_image(parameters.get("image"), "底图") as source,
        _payload_image(parameters.get("mask"), "蒙版") as mask_source,
    ):
        if source.size != size:
            raise ValueError(
                f"NovelAI 局部重绘底图尺寸 {source.size} 与请求尺寸 {size} 不一致"
            )
        if mask_source.size != size:
            raise ValueError(
                f"NovelAI 局部重绘蒙版尺寸 {mask_source.size} 与请求尺寸 {size} 不一致"
            )
        original = source.convert("RGBA")
        # The wire mask is already block-aligned black/white, not an alpha mask.
        mask = mask_source.convert("L")
    results: list[GeneratedImage] = []
    try:
        for position, item in enumerate(images, 1):
            label = f"第 {position} 张返回图片"
            with _load_image(item.data, label) as generated:
                if generated.size != size:
                    raise ValueError(
                        f"NovelAI 局部重绘{label}尺寸 {generated.size} 与请求尺寸 {size} 不一致"
                    )
                image_format = (
                    parameters.get("image_format") or generated.format.lower()
                )
                if image_format not in {"png", "webp"}:
                    raise ValueError("NovelAI 局部重绘仅支持 PNG 或 WebP 输出")
                with (
                    generated.convert("RGBA") as edited,
                    Image.composite(edited, original, mask) as composed,
                ):
                    output = _save_composite(
                        composed, generated, item.data, image_format
                    )
                results.append(
                    replace(item, data=output, mime_type=f"image/{image_format}")
                )
    finally:
        original.close()
        mask.close()
    return tuple(results)
