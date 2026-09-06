"""Browser-owned, non-sensitive appearance preferences for sandboxed pages."""

from __future__ import annotations

import json
import math
from typing import Any
from urllib.parse import quote, unquote

APPEARANCE_COOKIE = "image_studio_appearance_v1"
APPEARANCE_DEFAULTS = {
    "preference": "system",
    "accentHue": 168,
    "accentSaturation": 38,
    "accentLightness": 50,
    "glassOpacity": 0.68,
}


def normalize_appearance(value: Any) -> dict[str, Any]:
    value = value if isinstance(value, dict) else {}
    result = dict(APPEARANCE_DEFAULTS)
    if value.get("preference") in ("system", "light", "dark"):
        result["preference"] = value["preference"]
    for key, lower, upper in (
        ("accentHue", 0, 359.999999),
        ("accentSaturation", 0, 100),
        ("accentLightness", 0, 100),
        ("glassOpacity", 0.2, 1),
    ):
        number = value.get(key)
        if (
            isinstance(number, (int, float))
            and not isinstance(number, bool)
            and (isinstance(number, int) or math.isfinite(number))
        ):
            result[key] = min(upper, max(lower, number))
    return result


def decode_appearance_cookie(value: str | None) -> dict[str, Any]:
    try:
        if not value or len(value) > 1024:
            return dict(APPEARANCE_DEFAULTS)
        return normalize_appearance(json.loads(unquote(value)))
    except (TypeError, ValueError, RecursionError):
        return dict(APPEARANCE_DEFAULTS)


def encode_appearance_cookie(value: Any) -> str:
    return quote(json.dumps(normalize_appearance(value), separators=(",", ":")))
