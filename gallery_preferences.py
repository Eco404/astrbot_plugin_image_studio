"""Browser cookie fallback for gallery preferences in opaque plugin iframes."""

from __future__ import annotations

import base64
import copy
import json
import zlib
from typing import Any

GALLERY_PREFERENCES_COOKIE = "image_studio_gallery_preferences_v1"
_FILTER_LIMITS = {
    "galleryProvider": 64,
    "galleryMode": 20,
    "gallerySource": 30,
    "galleryEngine": 80,
}
_MAX_COOKIE_BYTES = 3600
_MAX_JSON_BYTES = 65536


def _selection(value: Any, maximum: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("默认筛选必须是对象")
    if value.get("mode") == "all":
        return {"mode": "all"}
    values = value.get("values")
    if (
        value.get("mode") != "values"
        or not isinstance(values, list)
        or not 1 <= len(values) <= 256
    ):
        raise ValueError("请选择 1 至 256 项后再设为默认")
    if any(not isinstance(item, str) or len(item) > maximum for item in values):
        raise ValueError("默认筛选选项无效或过长")
    return {"mode": "values", "values": list(dict.fromkeys(values))}


def merge_gallery_preferences(current: dict[str, Any], patch: Any) -> dict[str, Any]:
    if not isinstance(patch, dict) or set(patch) - {"sort", "filters"}:
        raise ValueError("画廊显示偏好必须是包含 sort 或 filters 的对象")
    result = copy.deepcopy(current)
    if "sort" in patch:
        if patch["sort"] not in ("created", "latest_content"):
            raise ValueError("画廊排序方式无效")
        result["sort"] = patch["sort"]
    if "filters" in patch:
        filters = patch["filters"]
        if not isinstance(filters, dict) or set(filters) - set(_FILTER_LIMITS):
            raise ValueError("默认筛选名称无效")
        for name, value in filters.items():
            result["filters"][name] = _selection(value, _FILTER_LIMITS[name])
    return result


def encode_gallery_preferences(value: dict[str, Any]) -> str:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > _MAX_JSON_BYTES:
        raise ValueError("默认筛选内容过多，无法保存在浏览器中")
    encoded = "1." + base64.urlsafe_b64encode(zlib.compress(raw)).decode("ascii")
    if len(encoded) > _MAX_COOKIE_BYTES:
        raise ValueError("默认筛选内容过多，无法保存在浏览器中")
    return encoded


def decode_gallery_preferences(value: str | None) -> dict[str, Any]:
    defaults = {"sort": "created", "filters": {}}
    if not value or not value.startswith("1.") or len(value) > _MAX_COOKIE_BYTES:
        return defaults
    try:
        packed = base64.b64decode(value[2:], altchars=b"-_", validate=True)
        decoder = zlib.decompressobj()
        raw = decoder.decompress(packed, _MAX_JSON_BYTES + 1)
        if (
            len(raw) > _MAX_JSON_BYTES
            or decoder.unconsumed_tail
            or decoder.unused_data
            or not decoder.eof
        ):
            return defaults
        return merge_gallery_preferences(defaults, json.loads(raw))
    except (ValueError, TypeError, RecursionError, zlib.error):
        return defaults
