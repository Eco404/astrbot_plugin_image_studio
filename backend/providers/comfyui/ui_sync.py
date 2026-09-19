"""Synchronize explicitly written API inputs into a submission-only UI copy.

UI widgets are not API inputs: their ordering can include frontend-only buttons
and seed controls. Only named widget dictionaries or verified serialized layouts
are writable. In particular, neither value equality nor object_info ordering is
evidence of a widget's index, and runtime display snapshots are left alone.
"""

from __future__ import annotations

from collections.abc import Iterable
import copy
import json
from typing import Any

from ...comfyui.catalog import node_adapter, positional_widget_slot


def _check_writable_field(node: dict, name: str) -> None:
    adapter = node_adapter(str(node.get("type")))
    if name in adapter.get("display", {}).get("write_protected", []):
        raise ValueError("该字段是运行时显示缓存，不能覆盖界面显示快照")
    if name == "control_after_generate" or name.startswith("__"):
        raise ValueError("该字段属于前端控件，不能作为 API 参数覆盖")


def _widget_slot(node: dict, name: str) -> str | int:
    _check_writable_field(node, name)
    values = node.get("widgets_values")
    if isinstance(values, dict):
        if name in values:
            return name
        raise ValueError("界面控件中没有对应的具名字段")
    if not isinstance(values, list):
        raise ValueError("界面节点没有可定位的控件值")
    return positional_widget_slot(str(node.get("type")), values, name)


def _widget_locations(
    node: dict, name: str
) -> tuple[list[tuple[str, str | int]], list[str]]:
    """Find representations independently, preserving unknown positional arrays.

    widgets_values_named is a sibling of the legacy array, not a replacement.
    Current ComfyUI can restore either depending on frontend/node settings, so
    every existing representation must be considered even after one succeeds.
    """
    _check_writable_field(node, name)
    locations, problems = [], []
    if "widgets_values" in node:
        try:
            locations.append(("widgets_values", _widget_slot(node, name)))
        except ValueError as exc:
            problems.append(str(exc))
    if "widgets_values_named" in node:
        named = node["widgets_values_named"]
        if isinstance(named, dict) and name in named:
            locations.append(("widgets_values_named", name))
        elif isinstance(named, dict):
            # A validated positional map independently identifies the widget.
            # Add its absent key so named restoration cannot silently omit it.
            if locations:
                locations.append(("widgets_values_named", name))
            else:
                problems.append("界面具名控件没有该字段，不能推测名称")
        else:
            problems.append("界面具名控件格式无效")
    if not locations and not problems:
        problems.append("界面节点没有可定位的控件值")
    return locations, problems


def read_widget_values(node: dict, name: str) -> list[Any]:
    """Read every represented value, failing closed for result repair checks."""
    locations, problems = _widget_locations(node, name)
    if problems or any(
        slot not in node[key] for key, slot in locations if isinstance(node[key], dict)
    ):
        raise ValueError("；".join(problems) or "界面具名控件缺少对应字段")
    return [copy.deepcopy(node[key][slot]) for key, slot in locations]


def _check_unlinked(workflow: dict, node: dict, name: str) -> None:
    inputs = node.get("inputs", [])
    if not isinstance(inputs, list) or any(not isinstance(row, dict) for row in inputs):
        raise ValueError("界面节点的输入端口信息无效")
    links = workflow.get("links", [])
    if not isinstance(links, list):
        raise ValueError("界面工作流的连线列表无效")
    matches = [
        index
        for index, row in enumerate(inputs)
        if row.get("name") == name
        or isinstance(row.get("widget"), dict)
        and row["widget"].get("name") == name
    ]
    if len(matches) > 1:
        raise ValueError("界面节点存在多个同名输入端口")
    if not matches:
        return
    index = matches[0]
    if inputs[index].get("link") is not None:
        raise ValueError("界面输入由连线或虚拟节点提供，不能只改控件值")
    for link in links:
        # Check both classic LiteGraph links and newer object serialization.
        if isinstance(link, list) and len(link) >= 5:
            target, slot = link[3:5]
        elif isinstance(link, dict):
            target, slot = link.get("target_id"), link.get("target_slot")
        else:
            continue
        if str(target) == str(node.get("id")) and slot == index:
            raise ValueError("界面输入仍有连线，不能只改控件值")


# Stable helpers used to validate metadata before allowing server seed hooks to
# operate. Neither helper infers API links from UI primitives or widget values.
widget_slot = _widget_slot
check_unlinked = _check_unlinked


def synchronize_workflow(
    config: dict,
    graph: dict,
    targets: Iterable[tuple[str, str] | dict] = (),
) -> dict[str, Any]:
    """Return a UI copy and warnings, reading final values only for write targets.

    Persisted targets retain fixed edits across config normalization. Runtime
    targets include bindings actually written this run (including uploaded image
    names and concrete random seeds). Missing UI metadata is normal for API-only
    workflows; it is never synthesized from an execution graph.
    """
    ui = config.get("workflow_json")
    if ui is None or isinstance(ui, str) and not ui.strip():
        ui = config.get("workflow")
    if isinstance(ui, str):
        try:
            ui = json.loads(ui)
        except (ValueError, RecursionError):
            return {
                "workflow": None,
                "warnings": ["界面工作流不是有效 JSON，无法同步本次参数。"],
            }
    if ui is None:
        return {"workflow": None, "warnings": []}
    if not isinstance(ui, dict):
        return {
            "workflow": None,
            "warnings": ["界面工作流格式无效，无法同步本次参数。"],
        }
    workflow = copy.deepcopy(ui)
    warnings: list[str] = []
    requested: dict[tuple[str, str], set[str]] = {}
    persisted = config.get("workflow_sync_targets", [])
    if not isinstance(persisted, (list, tuple)):
        warnings.append("界面工作流同步目标列表格式无效，已跳过已保存的目标。")
        persisted = []
    for target in [*persisted, *targets]:
        expected = ""
        if isinstance(target, dict):
            node_id, name = target.get("node_id"), target.get("input_name")
            expected = str(target.get("class_type") or "")
        elif isinstance(target, (tuple, list)) and len(target) == 2:
            node_id, name = target
        else:
            warnings.append("界面工作流同步目标格式无效，已跳过该项。")
            continue
        if node_id is None or not isinstance(name, str) or not name:
            warnings.append("界面工作流同步目标缺少节点或字段，已跳过该项。")
            continue
        key = (str(node_id), name)
        requested.setdefault(key, set())
        if expected:
            requested[key].add(expected)
    nodes: dict[str, list[dict]] = {}
    ui_nodes = workflow.get("nodes", [])
    for node in ui_nodes if isinstance(ui_nodes, list) else []:
        if isinstance(node, dict) and node.get("id") is not None:
            nodes.setdefault(str(node["id"]), []).append(node)
    for (node_id, name), expected in sorted(requested.items()):
        try:
            api_node = graph.get(node_id)
            if not isinstance(api_node, dict):
                raise ValueError("执行图中不存在该节点")
            kind = api_node.get("class_type")
            if expected and expected != {kind}:
                raise ValueError("同步目标的节点类型已变化或存在冲突")
            inputs = api_node.get("inputs", {})
            if not isinstance(inputs, dict) or name not in inputs:
                raise ValueError("执行图中不存在该输入字段")
            value = inputs[name]
            if (
                isinstance(value, list)
                and len(value) == 2
                and str(value[0]) in graph
                and isinstance(value[1], int)
                and not isinstance(value[1], bool)
            ):
                raise ValueError("执行图字段是连线，不能作为控件值写入")
            matches = nodes.get(node_id, [])
            if not isinstance(ui_nodes, list):
                raise ValueError("界面节点列表无效")
            if len(matches) != 1:
                raise ValueError("界面节点缺失或节点 ID 重复")
            node = matches[0]
            if node.get("type") != kind:
                raise ValueError("执行图与界面节点类型不一致")
            _check_unlinked(workflow, node, name)
            locations, problems = _widget_locations(node, name)
            for key, slot in locations:
                node[key][slot] = copy.deepcopy(value)
            if problems:
                raise ValueError("；".join(problems))
        except ValueError as exc:
            warnings.append(
                f"节点 #{node_id} 的 {name} 未同步到界面工作流：{exc}；"
                "拖回 ComfyUI 后请核对该参数。"
            )
    return {"workflow": workflow, "warnings": warnings}
