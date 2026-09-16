"""NovelAI parameter mapping and bounded alpha-channel stealth metadata."""

from __future__ import annotations
import gzip
import io
import json
from collections.abc import Callable
from typing import Any
from PIL import Image
from .common import MAX_METADATA_BYTES, _safe, _json, _warning


def _read_novelai_stealth(
    image: Image.Image, *, max_metadata_bytes: int = MAX_METADATA_BYTES
) -> dict[str, Any] | None:
    """Read NovelAI's column-major alpha LSB gzip payload without changing pixels."""
    if "A" not in image.getbands():
        return None
    width, height = image.size
    capacity = width * height
    magic = b"stealth_pngcomp"
    if capacity < len(magic) * 8:
        return None
    pixels = image.load()
    alpha_index = image.getbands().index("A")
    position = 0

    def read_bytes(count: int) -> bytes:
        nonlocal position
        if count * 8 > capacity - position:
            raise ValueError("隐写数据超过图片容量或已被截断")
        output = bytearray(count)
        for index in range(count):
            value = 0
            for _ in range(8):
                value = (value << 1) | (
                    pixels[position // height, position % height][alpha_index] & 1
                )
                position += 1
            output[index] = value
        return bytes(output)

    # Ordinary RGBA images only need a short magic probe, not a full alpha scan.
    if read_bytes(len(magic)) != magic:
        return None
    bit_length = int.from_bytes(read_bytes(4), "big")
    if not bit_length or bit_length % 8:
        raise ValueError("隐写数据位长度必须为正数且是 8 的倍数")
    compressed = read_bytes(bit_length // 8)
    with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
        payload = stream.read(max_metadata_bytes + 1)
    if len(payload) > max_metadata_bytes:
        raise ValueError("隐写元数据解压后超过 4 MiB 限制")
    metadata = json.loads(payload.decode("utf-8"))
    if not isinstance(metadata, dict):
        raise TypeError("隐写元数据必须为 JSON 对象")
    return metadata


def _metadata_values_equal(left: Any, right: Any) -> bool:
    """Ignore JSON text whitespace and key order when comparing metadata sources."""

    def decoded(value: Any) -> Any:
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, (dict, list)):
                    return _safe(parsed)
            except (ValueError, RecursionError):
                pass
        return value

    return decoded(left) == decoded(right)


def _merge_novelai_stealth(
    result: dict,
    stealth: dict[str, Any],
    *,
    parse_fields: Callable[[dict], dict],
    max_metadata_bytes: int = MAX_METADATA_BYTES,
) -> dict:
    hidden = parse_fields(stealth)
    ordinary_raw = result["raw"]
    raw = {**hidden["raw"], **ordinary_raw}
    provenance = {"protocol": "stealth_pngcomp", "fields": hidden["raw"]}
    if "StealthMetadata" in ordinary_raw:
        provenance["ordinary_field"] = ordinary_raw["StealthMetadata"]
    raw["StealthMetadata"] = provenance
    if len(json.dumps(raw, ensure_ascii=False).encode("utf-8")) > max_metadata_bytes:
        raise ValueError("合并后的图片元数据超过 4 MiB 限制")
    merged = {
        **result,
        "raw": raw,
        "normalized": dict(result["normalized"]),
        "warnings": list(result["warnings"]),
    }
    if (
        "Comment" not in ordinary_raw
        and _json(hidden["raw"].get("Comment")) is not None
    ):
        merged["warnings"] = [
            message
            for message in merged["warnings"]
            if message != "已识别 NovelAI 来源，但 Comment 不是可解析的 JSON"
        ]
    for key, value in hidden["raw"].items():
        if key in ordinary_raw and not _metadata_values_equal(ordinary_raw[key], value):
            _warning(
                merged,
                f"常规元数据与 NovelAI 隐写元数据的 {key} 不一致；"
                "优先采用常规字段，隐写原值保留在 StealthMetadata",
            )
    if result["format"] != "unknown" and hidden["format"] not in {
        "unknown",
        result["format"],
    }:
        _warning(merged, "常规元数据与隐写元数据的生成格式不同，未混合生成参数")
        return merged
    if merged["format"] == "unknown":
        merged["format"] = hidden["format"]
    for key, value in hidden["normalized"].items():
        if key not in merged["normalized"] or (
            key == "mode" and merged["normalized"][key] == "unknown"
        ):
            merged["normalized"][key] = value
    for message in hidden["warnings"]:
        _warning(merged, f"NovelAI 隐写元数据：{message}")
    return merged


def _novelai(parameters: dict, result: dict) -> None:
    target = result["normalized"]
    for source, destination in {
        "prompt": "prompt",
        "uc": "negative_prompt",
        "negative_prompt": "negative_prompt",
        "model_name": "model",
        "model": "model",
        "width": "width",
        "height": "height",
        "seed": "seed",
        "steps": "steps",
        "scale": "guidance_scale",
        "cfg_rescale": "cfg_rescale",
        "sampler": "sampler",
        "noise_schedule": "scheduler",
        "n_samples": "count",
        "strength": "strength",
        "noise": "noise",
        "extra_noise_seed": "extra_noise_seed",
        "image_format": "image_format",
    }.items():
        if source in parameters and parameters[source] is not None:
            target[destination] = parameters[source]
    for source, destination in (
        ("v4_prompt", "prompt"),
        ("v4_negative_prompt", "negative_prompt"),
    ):
        value = parameters.get(source)
        if isinstance(value, dict):
            caption = value.get("caption", {})
            if isinstance(caption, dict):
                if destination not in target and isinstance(
                    caption.get("base_caption"), str
                ):
                    target[destination] = caption["base_caption"]
                if caption.get("char_captions"):
                    target.setdefault("characters", {})[destination] = caption[
                        "char_captions"
                    ]
                    _warning(
                        result,
                        "包含多角色描述，简要提示词不能表达全部角色信息，请保留原始 NovelAI 参数",
                    )
    action = parameters.get("action")
    request_type = parameters.get("request_type")
    if action == "img2img" or request_type == "ImageGenerateRequest":
        target["mode"] = "img2img"
    elif action == "generate" or request_type == "PromptGenerateRequest":
        target["mode"] = "text2img"
