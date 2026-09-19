"""Immutable built-in node facts with separate read and write authorities.

The catalog is packaged code data, never an executable/user rule configuration.
Reading a metadata widget hint does not grant permission to overwrite that slot.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

CATALOG_PATH = Path(__file__).with_name("rules") / "node_adapters.json"
_RAW = CATALOG_PATH.read_bytes()
_CATALOG = json.loads(_RAW)
_NODES = _CATALOG["nodes"]


def catalog_fingerprint() -> str:
    """Invalidate parser caches when any packaged adapter fact changes."""
    return hashlib.sha256(_RAW).hexdigest()


def node_adapter(kind: str) -> dict[str, Any]:
    """Return a defensive copy; unknown types have no inferred rules."""
    return copy.deepcopy(_NODES.get(kind, {}))


def auxiliary_input_policy(kind: str, name: str) -> dict[str, Any]:
    """Known frontend payloads, not permission to remove or write fields."""
    adapter = _NODES.get(kind, {})
    display = adapter.get("display", {})
    if name == display.get("api_field"):
        return {
            "role": "display",
            "value_types": list(display.get("api_value_types", [])),
        }
    return copy.deepcopy(adapter.get("frontend_inputs", {}).get(name, {}))


def is_auxiliary_input_literal(policy: dict[str, Any], value: Any) -> bool:
    """Suppress warnings only for source-verified literal shapes."""
    shapes = policy.get("value_types", [])
    return (
        ("empty_string" in shapes and isinstance(value, str) and value == "")
        or ("string" in shapes and isinstance(value, str))
        or (
            "string_list" in shapes
            and isinstance(value, list)
            and all(isinstance(item, str) for item in value)
        )
    )


def metadata_widget_names(kind: str) -> tuple[str, ...]:
    return tuple(_NODES.get(kind, {}).get("metadata_widgets", ()))


def metadata_widget_catalog() -> dict[str, list[str]]:
    return {
        kind: list(adapter["metadata_widgets"])
        for kind, adapter in _NODES.items()
        if "metadata_widgets" in adapter
    }


def seed_policy(kind: str) -> dict[str, Any]:
    """Exact Python bounds avoid JavaScript uint64 rounding in the JSON source."""
    policy = copy.deepcopy(_NODES.get(kind, {}).get("seed", {}))
    for key in ("min", "max"):
        if key in policy:
            policy[key] = int(policy[key])
    return policy


def _matches_constraint(value: Any, constraint: dict) -> bool:
    if "one_of" in constraint and not any(
        type(value) is type(candidate) and value == candidate
        for candidate in constraint["one_of"]
    ):
        return False
    if constraint.get("type") == "boolean" and not isinstance(value, bool):
        return False
    return True


def positional_widget_slot(kind: str, values: list, name: str) -> int:
    """Use a complete, verified layout, never input/key order or value matches."""
    if name == "control_after_generate" or name.startswith("__"):
        raise ValueError("该字段属于前端控件，不能作为 API 参数覆盖")
    slots = set()
    for layout in _NODES.get(kind, {}).get("layouts", []):
        widgets = layout["widgets"]
        if len(values) != len(widgets) or name not in widgets:
            continue
        if not all(
            _matches_constraint(values[int(index)], constraint)
            for index, constraint in layout.get("constraints", {}).items()
        ):
            continue
        slots.add(widgets.index(name))
    if len(slots) != 1:
        raise ValueError("界面位置控件缺少匹配的已验证布局，不能推测字段位置")
    return slots.pop()
