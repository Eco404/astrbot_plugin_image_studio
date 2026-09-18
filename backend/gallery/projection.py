"""Gallery metadata projection, validation, and request display fields."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import OrderedDict
from typing import Any

from .constants import (
    _SAFE_ID_RE,
    _SHA256_RE,
)

_COMFY_PROJECTION_CACHE_LIMIT = 64
_COMFY_PROJECTION_CACHE_BYTES = 16 * 1024 * 1024
_COMFY_PROJECTION_CACHE: OrderedDict[tuple[Any, ...], bytes] = OrderedDict()
_COMFY_PROJECTION_CACHE_LOCK = threading.Lock()
_IMPORT_EDIT_FIELDS = (
    "generation_engine",
    "model",
    "mode",
    "prompt",
    "negative_prompt",
    "generated_at",
)


def _validate_import_hashes(hashes: Any) -> list[str]:
    if not isinstance(hashes, list) or not 1 <= len(hashes) <= 100:
        raise ValueError("每次查重需要 1 至 100 个图片哈希")
    if any(
        not isinstance(value, str) or not _SHA256_RE.fullmatch(value)
        for value in hashes
    ):
        raise ValueError("图片 SHA-256 必须为 64 位小写十六进制字符串")
    return list(hashes)


def _validate_generation_selection(generation_ids: Any) -> list[str]:
    if not isinstance(generation_ids, list) or not 1 <= len(generation_ids) <= 1000:
        raise ValueError("请选择 1 至 1000 条生成记录")
    if any(
        not isinstance(item, str) or not _SAFE_ID_RE.fullmatch(item)
        for item in generation_ids
    ):
        raise ValueError("所选生成记录 ID 无效")
    return list(dict.fromkeys(generation_ids))


def _favorite_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    favorite_count = sum(bool(row["is_favorite"]) for row in rows)
    all_favorite = favorite_count == len(rows)
    return {
        "selected_count": len(rows),
        "favorite_count": favorite_count,
        "all_favorite": all_favorite,
        "action": "unfavorite" if all_favorite else "favorite",
        "items": [
            {
                "id": row["id"],
                "is_favorite": bool(row["is_favorite"]),
                "cleanup_protected_until": float(row["cleanup_protected_until"]),
            }
            for row in rows
        ],
    }


def _validate_import_edit_overrides(overrides: Any) -> None:
    _validate_import_overrides(overrides)
    if set(overrides) - {*_IMPORT_EDIT_FIELDS, "parameters", "comfy_output_node"}:
        raise ValueError("编辑参数包含不支持的字段")
    for key in (*_IMPORT_EDIT_FIELDS[:-1], "comfy_output_node"):
        if key in overrides and not isinstance(overrides[key], str):
            raise ValueError(f"编辑字段 {key} 必须是字符串")
    if len(overrides.get("generation_engine", "")) > 80:
        raise ValueError("生图来源不能超过 80 个字符")
    if "generated_at" in overrides and (
        isinstance(overrides["generated_at"], bool)
        or not isinstance(overrides["generated_at"], (str, int, float, type(None)))
    ):
        raise ValueError("原始生成时间无效")


def _import_edit_existing_overrides(
    previous: dict[str, Any], metadata: dict[str, Any]
) -> dict[str, Any]:
    saved = previous.get("overrides")
    if isinstance(saved, dict):
        return dict(saved)
    # Legacy snapshots do not record manual provenance. Conservatively retain
    # their current values when an edit first introduces an override snapshot.
    result = {key: previous[key] for key in _IMPORT_EDIT_FIELDS if key in previous}
    if previous.get("comfy_output_node"):
        result["comfy_output_node"] = previous["comfy_output_node"]
    normalized = project_import_metadata(metadata, result).get("normalized", {})
    display = previous.get("display_parameters")
    if isinstance(previous.get("parameters"), dict):
        result["parameters"] = previous["parameters"]
    elif isinstance(display, dict):
        changes = {
            key: value
            for key, value in display.items()
            if key not in _IMPORT_EDIT_FIELDS
            and (key not in normalized or value != normalized[key])
        }
        if changes:
            result["parameters"] = changes
    return result


def _import_edit_fields(
    previous: dict[str, Any], metadata: dict[str, Any], overrides: dict[str, Any]
) -> dict[str, Any]:
    normalized = metadata.get("normalized", {})
    fields = {
        key: previous.get(key, overrides.get(key, normalized.get(key)))
        for key in _IMPORT_EDIT_FIELDS
    }
    for key in ("model", "prompt", "negative_prompt"):
        fields[key] = str(fields[key] or "")
    fields["generation_engine"] = _canonical_engine(
        fields["generation_engine"] or metadata.get("format")
    )
    fields["mode"] = fields["mode"] or "unknown"
    parameters = overrides.get("parameters", normalized.get("parameters", {}))
    fields["parameters"] = parameters if isinstance(parameters, dict) else {}
    return fields


def _validate_import_overrides(overrides: Any) -> None:
    if not isinstance(overrides, dict):
        raise ValueError("导入补充信息必须为对象")
    if "parameters" in overrides and not isinstance(overrides["parameters"], dict):
        raise ValueError("导入参数必须为 JSON 对象")
    if "comfy_output_node" in overrides and not isinstance(
        overrides["comfy_output_node"], str
    ):
        raise ValueError("ComfyUI 输出节点 ID 必须是字符串")
    try:
        encoded = json.dumps(overrides, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > 1024 * 1024:
            raise ValueError("导入补充信息不能超过 1 MB")
    except (TypeError, RecursionError) as exc:
        raise ValueError("导入补充信息必须是有效 JSON") from exc


def _canonical_engine(value: Any) -> str:
    engine = str(value or "unknown").strip() or "unknown"
    return "novelai" if engine.lower() in {"nai", "novelai"} else engine


def _known_merge_engine(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("合并来源必须是字符串")
    engine = _canonical_engine(value)
    if not value.strip() or engine.lower() in {"unknown", "mixed"}:
        raise ValueError("仅支持合并具有相同已知生图来源的图片")
    return engine


def _canonical_supplemental(value: dict[str, Any]) -> dict[str, Any]:
    result = dict(value)
    result.pop("merge_operations", None)
    if "generation_engine" in result:
        result["generation_engine"] = _canonical_engine(result["generation_engine"])
    display = result.get("display_parameters")
    if isinstance(display, dict) and "generation_engine" in display:
        result["display_parameters"] = {
            **display,
            "generation_engine": _canonical_engine(display["generation_engine"]),
        }
    return result


def project_import_metadata(
    metadata: dict[str, Any], overrides: dict[str, Any]
) -> dict[str, Any]:
    """Project a selected ComfyUI output without changing the shared asset metadata."""

    from ..metadata.node_rules import get_rules
    from ..metadata.parser import PARSER_VERSION, parse_metadata_fields

    rules = get_rules()
    rules_fingerprint = rules.fingerprint
    stale_rules = metadata.get("format") == "comfyui" and (
        bool(metadata.get("rules_fingerprint") or rules.user_rules)
        and metadata.get("rules_fingerprint") != rules_fingerprint
    )
    output_node = overrides.get("comfy_output_node", "")
    if not isinstance(output_node, str):
        raise ValueError("ComfyUI 输出节点 ID 必须是字符串")
    if not output_node and not stale_rules:
        return metadata
    if metadata.get("format") != "comfyui":
        raise ValueError("仅 ComfyUI 图片支持选择输出节点")
    raw = metadata.get("raw")
    if not isinstance(raw, dict):
        raise ValueError("ComfyUI 原始工作流不可用，无法选择输出节点")
    normalized = metadata.get("normalized") or {}
    if not isinstance(normalized, dict):
        raise ValueError("metadata.normalized 必须是对象")
    dimensions = normalized.get("file_dimensions") or {}
    if not isinstance(dimensions, dict):
        raise ValueError("图片尺寸信息必须是对象")
    # Cache only the parser's pure projection. Manual prompt/model/parameter
    # overrides are applied by callers and never become shared cached state.
    raw_bytes = json.dumps(
        raw, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode()
    key = (
        PARSER_VERSION,
        rules_fingerprint,
        metadata.get("parser_version"),
        hashlib.sha256(raw_bytes).digest(),
        dimensions.get("width", 0),
        dimensions.get("height", 0),
        output_node,
    )
    with _COMFY_PROJECTION_CACHE_LOCK:
        cached = _COMFY_PROJECTION_CACHE.get(key)
        if cached is not None:
            _COMFY_PROJECTION_CACHE.move_to_end(key)
    if cached is not None:
        return json.loads(cached)
    projected = parse_metadata_fields(
        raw,
        width=dimensions.get("width", 0),
        height=dimensions.get("height", 0),
        output_node_id=output_node,
    )
    encoded = json.dumps(projected, ensure_ascii=False, separators=(",", ":")).encode()
    if len(encoded) <= _COMFY_PROJECTION_CACHE_BYTES:
        with _COMFY_PROJECTION_CACHE_LOCK:
            _COMFY_PROJECTION_CACHE[key] = encoded
            _COMFY_PROJECTION_CACHE.move_to_end(key)
            while (
                len(_COMFY_PROJECTION_CACHE) > _COMFY_PROJECTION_CACHE_LIMIT
                or sum(map(len, _COMFY_PROJECTION_CACHE.values()))
                > _COMFY_PROJECTION_CACHE_BYTES
            ):
                _COMFY_PROJECTION_CACHE.popitem(last=False)
    return projected


def refresh_import_supplemental(
    supplemental: dict[str, Any], metadata: dict[str, Any]
) -> dict[str, Any]:
    """Refresh derived fields while retaining explicitly entered import values."""
    overrides = supplemental.get("overrides")
    if (
        metadata.get("format") != "comfyui"
        or not isinstance(overrides, dict)
        or metadata.get("normalized", {}).get("requires_output_selection")
    ):
        return supplemental
    return {
        **supplemental,
        **_import_supplemental(
            str(supplemental.get("original_filename") or ""),
            overrides,
            metadata,
            allow_unresolved_output=True,
        ),
    }


def _external_parameter_overrides(
    overrides: dict[str, Any], metadata: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Keep readable external images indexable even when parameter fields are malformed."""
    result = dict(overrides)
    normalized = metadata.get("normalized") or {}
    warnings = []
    for key in ("model", "prompt", "negative_prompt"):
        value = overrides.get(key, normalized.get(key) or "")
        if not isinstance(value, str):
            warnings.append(
                f"外部图片的 {key} 参数格式无效，已忽略该字段；原始元数据仍保留"
            )
            value = ""
        if key == "model" and len(value.strip()) > 240:
            warnings.append(
                "外部图片的模型名称超过 240 个字符，显示名称已截短；原始元数据仍保留"
            )
            value = value.strip()[:240]
        result[key] = value
    mode = overrides.get("mode", normalized.get("mode") or "unknown")
    if not isinstance(mode, str) or mode not in {"text2img", "img2img", "unknown"}:
        warnings.append("外部图片的生图模式无法识别，已标记为未知")
        mode = "unknown"
    result["mode"] = mode
    return result, warnings


def _import_supplemental(
    filename: str,
    overrides: dict[str, Any],
    metadata: dict[str, Any],
    *,
    allow_unresolved_output: bool = False,
) -> dict[str, Any]:
    metadata = project_import_metadata(metadata, overrides)
    normalized = metadata.get("normalized", {})
    if (
        not allow_unresolved_output
        and metadata.get("format") == "comfyui"
        and normalized.get("requires_output_selection")
    ):
        raise ValueError("图片包含多个 ComfyUI 保存输出，请先选择对应的最终保存节点")
    mode = str(overrides.get("mode") or normalized.get("mode") or "unknown")
    if mode not in {"text2img", "img2img", "unknown"}:
        raise ValueError("导入图片模式无效")
    engine = _canonical_engine(
        overrides.get("generation_engine")
        or normalized.get("generation_engine")
        or metadata.get("format")
        or "unknown"
    )[:80]
    model_value = overrides.get("model", normalized.get("model") or "")
    if not isinstance(model_value, str):
        raise ValueError("导入模型名称必须是字符串")
    model = model_value.strip()
    if len(model) > 240:
        raise ValueError("导入模型名称不能超过 240 个字符")
    prompt = str(overrides.get("prompt", normalized.get("prompt") or ""))
    negative = str(
        overrides.get("negative_prompt", normalized.get("negative_prompt") or "")
    )
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
    return {
        "original_filename": str(filename or "")[:512],
        "overrides": overrides,
        "comfy_output_node": overrides.get(
            "comfy_output_node", normalized.get("selected_output_node", "")
        ),
        "model": model,
        "mode": mode,
        "prompt": prompt,
        "negative_prompt": negative,
        "generation_engine": engine,
        "generated_at": generated_at,
        "display_parameters": {
            **normalized,
            **(overrides.get("parameters") or {}),
            "model": model,
            "mode": mode,
            "generation_engine": engine,
            "generated_at": generated_at,
            "prompt": prompt,
            "negative_prompt": negative,
        },
    }


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


def has_request_value(value: Any) -> bool:
    """An empty field is different from a meaningful zero or false value."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (dict, list, tuple)):
        return bool(value)
    return True


def compact_comfy_request(value: dict[str, Any]) -> dict[str, Any]:
    """Project populated request fields without changing nested workflow values."""
    result = {key: item for key, item in value.items() if has_request_value(item)}
    parameters = value.get("parameters")
    if isinstance(parameters, dict):
        parameters = {
            key: item
            for key, item in parameters.items()
            if key != "_comfy_job_id" and has_request_value(item)
        }
        if parameters:
            result["parameters"] = parameters
        else:
            result.pop("parameters", None)
    return result


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
