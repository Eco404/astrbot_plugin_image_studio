"""Lossy metadata projection of known UI nodes; never an executable API graph."""

from __future__ import annotations
from ..common import MAX_NODES, _warning
from .user_rules import get_rules


def _workflow_graph(workflow: dict, result: dict) -> dict:
    nodes = workflow.get("nodes")
    if not isinstance(nodes, list) or len(nodes) > MAX_NODES:
        raise ValueError("ComfyUI 工作流节点无效或超过 2048 个")
    workflow_links = workflow.get("links", [])
    if not isinstance(workflow_links, list):
        raise ValueError("ComfyUI 工作流 links 必须是数组")
    links = {}
    for link in workflow_links:
        if isinstance(link, list) and len(link) >= 6:
            links[str(link[0])] = [str(link[1]), link[2]]
    widget_names = get_rules().catalog["widgets"]
    graph = {}
    for node in nodes:
        if not isinstance(node, dict) or "id" not in node:
            continue
        kind = node.get("type", "")
        if not isinstance(kind, str):
            raise ValueError("ComfyUI 节点 type 必须是字符串")
        input_entries = node.get("inputs", [])
        if not isinstance(input_entries, list):
            raise ValueError("ComfyUI 节点 inputs 必须是数组")
        inputs = {}
        widgets = node.get("widgets_values", [])
        if isinstance(widgets, list):
            inputs.update(zip(widget_names.get(kind, ()), widgets))
        elif isinstance(widgets, dict):
            inputs.update(
                (name, widgets[name])
                for name in widget_names.get(kind, ())
                if name in widgets
            )
        named = node.get("widgets_values_named")
        if isinstance(named, dict):
            # Runtime nodes can write the executed value only to the positional
            # array (e.g. rgthree), leaving a next-run sentinel in named values.
            # Fill missing known inputs without overriding those observations.
            for name in widget_names.get(kind, ()):
                if name in named:
                    inputs.setdefault(name, named[name])
        for entry in input_entries:
            if isinstance(entry, dict) and str(entry.get("link")) in links:
                name = entry.get("name", "")
                if not isinstance(name, str):
                    raise ValueError("ComfyUI 节点输入 name 必须是字符串")
                inputs[name] = links[str(entry["link"])]
        graph[str(node["id"])] = {"class_type": kind, "inputs": inputs}
    _warning(
        result,
        "仅有 ComfyUI 界面工作流，按已知标准节点读取参数；自定义节点控件与执行状态可能无法还原",
    )
    return graph
