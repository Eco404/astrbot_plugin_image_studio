"""Public metadata parsing entrypoints and format dispatch.

This layer keeps the stable result contract. Format readers are read-only and
must not import provider workflow execution/normalization implementations.
"""

from __future__ import annotations
import json
import re
from .common import (
    PARSER_VERSION as PARSER_VERSION,
    MAX_IMAGE_BYTES as MAX_IMAGE_BYTES,
    MAX_METADATA_BYTES as MAX_METADATA_BYTES,
    MAX_PIXELS as MAX_PIXELS,
    MAX_NODES as MAX_NODES,
    MAX_DEPTH as MAX_DEPTH,
    MAX_SAFE_INTEGER as MAX_SAFE_INTEGER,
    _creation_timestamp as _creation_timestamp,
    _decode_comment as _decode_comment,
    _is_api_graph,
    _json,
    _safe,
    _unpack_container_fields,
    _warning,
)
from .comfyui import _comfyui
from .novelai import _merge_novelai_stealth, _novelai
from .readers import read_image_metadata
from .stable_diffusion import _a1111_parameters, _parse_infotext


def parse_image_metadata(data: bytes) -> dict:
    """Read embedded text without executing workflows or fetching resources."""
    fields, width, height, stealth, stealth_warning = read_image_metadata(
        data,
        max_image_bytes=MAX_IMAGE_BYTES,
        max_pixels=MAX_PIXELS,
        max_metadata_bytes=MAX_METADATA_BYTES,
    )
    result = parse_metadata_fields(fields, width=width, height=height)
    if stealth is not None:
        try:
            result = _merge_novelai_stealth(
                result,
                stealth,
                parse_fields=parse_metadata_fields,
                max_metadata_bytes=MAX_METADATA_BYTES,
            )
        except (ValueError, RecursionError) as exc:
            stealth_warning = f"NovelAI 隐写元数据无效，已忽略：{exc}"
    if stealth_warning:
        _warning(result, stealth_warning)
    return result


def parse_metadata_fields(
    fields: dict, *, width: int = 0, height: int = 0, output_node_id: str = ""
) -> dict:
    if not isinstance(fields, dict):
        raise ValueError("元数据必须是对象")
    if not isinstance(output_node_id, str):
        raise ValueError("保存输出节点 ID 必须是字符串")
    raw = {
        str(key): _decode_comment(value) if isinstance(value, bytes) else _safe(value)
        for key, value in fields.items()
    }
    if len(json.dumps(raw, ensure_ascii=False).encode("utf-8")) > MAX_METADATA_BYTES:
        raise ValueError("图片元数据超过 4 MiB 限制")
    fields = raw = _unpack_container_fields(raw)
    result = {
        "format": "unknown",
        "parser_version": PARSER_VERSION,
        "raw": raw,
        "normalized": {"mode": "unknown"},
        "warnings": [],
    }
    if width > 0 and height > 0:
        result["normalized"]["file_dimensions"] = {"width": width, "height": height}
    _creation_timestamp(fields, result)
    comment = _json(fields.get("Comment"))
    software = str(fields.get("Software", "")).lower()
    if "novelai" in software or (
        comment is not None
        and any(k in comment for k in ("v4_prompt", "uc", "request_type"))
        and any(k in comment for k in ("steps", "scale", "sampler"))
    ):
        result["format"] = "novelai"
        if comment is None:
            _warning(result, "已识别 NovelAI 来源，但 Comment 不是可解析的 JSON")
            if isinstance(fields.get("Description"), str):
                result["normalized"]["prompt"] = fields["Description"]
        else:
            _novelai(comment, result)
        if "model" not in result["normalized"] and fields.get("Source"):
            result["normalized"]["model"] = str(fields["Source"])
    elif "workflow" in fields or _is_api_graph(_json(fields.get("prompt"))):
        result["format"] = "comfyui"
        _comfyui(fields, result, output_node_id=output_node_id)
    else:
        for key in ("parameters", "UserComment", "ImageDescription", "Description"):
            value = fields.get(key)
            if isinstance(value, str) and _parse_infotext(value, result):
                result["format"] = "a1111"
                break
    if output_node_id and result["format"] != "comfyui":
        raise ValueError("只有 ComfyUI 工作流可以选择保存输出节点")
    return _safe(result)


def parse_parameter_text(content: str) -> dict:
    """Recognize copied parameter formats; unsupported text is not a prompt."""
    if not isinstance(content, str) or not content.strip():
        raise ValueError("请粘贴完整参数")
    if len(content.encode("utf-8")) > MAX_METADATA_BYTES:
        raise ValueError("参数内容超过 4 MiB 限制")
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*\n?|\n?```$", "", stripped).strip()
    obj = _json(stripped)
    if obj is None:
        result = parse_metadata_fields({"parameters": stripped})
        if result["format"] != "unknown":
            return result
        raise ValueError(
            "无法识别参数格式，请粘贴 Image Studio、NovelAI、ComfyUI 或 Stable Diffusion 完整参数"
        )
    if obj.get("format") == "image_studio":
        if obj.get("version") != 1 or not isinstance(obj.get("data"), dict):
            raise ValueError("Image Studio 参数版本或数据结构不受支持")
        request = _safe(obj["data"])
        result = parse_metadata_fields({"image_studio": content})
        result["request"] = request
        result["envelope"] = _safe(obj)
        parameters = request.get("parameters", {})
        if not isinstance(parameters, dict):
            parameters = {}
        result["normalized"].update(
            {
                key: request[key]
                for key in (
                    "prompt",
                    "negative_prompt",
                    "model",
                    "mode",
                    "width",
                    "height",
                    "count",
                )
                if key in request
            }
        )
        result["normalized"].update(
            {
                key: parameters[key]
                for key in ("seed", "steps", "sampler")
                if key in parameters
            }
        )
        return result
    if obj.get("format") == "a1111" and isinstance(obj.get("parameters"), dict):
        if obj.get("version") != 1:
            raise ValueError("Stable Diffusion 参数版本不受支持")
        raw = obj.get("raw")
        result = parse_metadata_fields(raw if isinstance(raw, dict) else {})
        result["format"] = "a1111"
        result["normalized"].update(_safe(obj["parameters"]))
        return result
    if any(
        key in obj for key in ("Comment", "workflow", "Software", "UserComment")
    ) or _is_api_graph(obj.get("prompt")):
        result = parse_metadata_fields(obj)
        if result["format"] != "unknown":
            return result
    if _is_api_graph(obj):
        return parse_metadata_fields({"prompt": stripped})
    if isinstance(obj.get("nodes"), list) and "links" in obj:
        return parse_metadata_fields({"workflow": stripped})
    if (
        isinstance(obj.get("input"), str)
        and isinstance(obj.get("parameters"), dict)
        and str(obj.get("model", "")).startswith("nai-diffusion-")
    ):
        parameters = {
            **obj["parameters"],
            "prompt": obj["input"],
            "model": obj["model"],
            "action": obj.get("action", "generate"),
        }
        return parse_metadata_fields(
            {
                "Software": "NovelAI",
                "Comment": json.dumps(parameters, ensure_ascii=False),
            }
        )
    if "tag" in obj or ("artist" in obj and "prompt" in obj):
        result = parse_metadata_fields({"nai_request": content})
        result["format"] = "novelai"
        result["request"] = _safe(obj)
        mapped = {**obj, "prompt": obj.get("tag", obj.get("prompt", ""))}
        if "negative" in obj:
            mapped["uc"] = obj["negative"]
        if "cfg" in obj:
            mapped["cfg_rescale"] = obj["cfg"]
        _novelai(mapped, result)
        return _safe(result)
    if any(key in obj for key in ("uc", "v4_prompt", "cfg_rescale", "request_type")):
        return parse_metadata_fields({"Software": "NovelAI", "Comment": stripped})
    if "Steps" in obj or "CFG scale" in obj:
        result = parse_metadata_fields({"parameters_json": content})
        result["format"] = "a1111"
        _a1111_parameters(obj, result["normalized"])
        result["normalized"]["parameters"] = _safe(obj)
        return _safe(result)
    if obj.get("format") in {"novelai", "comfyui", "a1111", "unknown"} and isinstance(
        obj.get("normalized"), dict
    ):
        raw = obj.get("raw", {})
        result = parse_metadata_fields(raw if isinstance(raw, dict) else {})
        result["format"] = obj["format"]
        result["normalized"].update(_safe(obj["normalized"]))
        return result
    raise ValueError("无法识别 JSON 参数格式")
