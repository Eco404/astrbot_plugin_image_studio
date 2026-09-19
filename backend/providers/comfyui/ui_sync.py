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


# Core widget layouts include frontend controls, not just the Python inputs.
# Require their complete serialized shape rather than accepting a matching
# prefix of an unknown/customized layout. Third-party parsers' widget hints are
# deliberately not used as authority to modify saved workflows.
_WIDGET_LAYOUTS: dict[str, tuple[str, ...]] = {
    "KSampler": (
        "seed",
        "control_after_generate",
        "steps",
        "cfg",
        "sampler_name",
        "scheduler",
        "denoise",
    ),
    "KSamplerAdvanced": (
        "add_noise",
        "noise_seed",
        "control_after_generate",
        "steps",
        "cfg",
        "sampler_name",
        "scheduler",
        "start_at_step",
        "end_at_step",
        "return_with_leftover_noise",
    ),
    "RandomNoise": ("noise_seed", "control_after_generate"),
    "CLIPTextEncode": ("text",),
    "CLIPTextEncodeSDXL": (
        "width",
        "height",
        "crop_w",
        "crop_h",
        "target_width",
        "target_height",
        "text_g",
        "text_l",
    ),
    "CLIPTextEncodeSDXLRefiner": ("ascore", "width", "height", "text"),
    "CheckpointLoaderSimple": ("ckpt_name",),
    "UNETLoader": ("unet_name", "weight_dtype"),
    "VAELoader": ("vae_name",),
    "CLIPLoader": ("clip_name", "type", "device"),
    "DualCLIPLoader": ("clip_name1", "clip_name2", "type", "device"),
    "LoraLoader": ("lora_name", "strength_model", "strength_clip"),
    "LoraLoaderModelOnly": ("lora_name", "strength_model"),
    "EmptyLatentImage": ("width", "height", "batch_size"),
    "EmptySD3LatentImage": ("width", "height", "batch_size"),
    "LatentUpscaleBy": ("upscale_method", "scale_by"),
    "LatentUpscale": ("upscale_method", "width", "height", "crop"),
    "ImageScaleBy": ("upscale_method", "scale_by"),
    "ImageScale": ("upscale_method", "width", "height", "crop"),
    "SaveImage": ("filename_prefix",),
    "ConditioningAverage": ("conditioning_to_strength",),
    "ConditioningSetArea": ("width", "height", "x", "y", "strength"),
    "ConditioningSetAreaPercentage": ("width", "height", "x", "y", "strength"),
    "ConditioningSetMask": ("strength", "set_cond_area"),
    "ConditioningSetTimestepRange": ("start", "end"),
    "ConditioningSetAreaStrength": ("strength",),
}


def _widget_slot(node: dict, name: str) -> str | int:
    kind = node.get("type")
    if kind == "easy showAnything" and name == "text":
        raise ValueError("该字段是运行时显示缓存，不能覆盖界面显示快照")
    values = node.get("widgets_values")
    if isinstance(values, dict):
        if name in values:
            return name
        raise ValueError("界面控件中没有对应的具名字段")
    if not isinstance(values, list):
        raise ValueError("界面节点没有可定位的控件值")
    if kind == "Seed (rgthree)" and name == "seed":
        # rgthree serializes the seed then three frontend button values. Its
        # Python node rewrites the sentinel in widgets_values after execution.
        if len(values) == 4 and all(item == "" for item in values[1:]):
            return 0
        raise ValueError("rgthree 种子控件布局与已验证的格式不一致")
    if kind == "LoadImage" and name == "image":
        # The upload widget may be omitted (serialize:false), or persisted as
        # the frontend's "image" selector. Neither is an API input field.
        if len(values) == 1 or len(values) == 2 and values[1] == "image":
            return 0
        raise ValueError("加载图片控件布局与已验证的格式不一致")
    layout = _WIDGET_LAYOUTS.get(str(kind))
    if layout is None or name not in layout or name == "control_after_generate":
        raise ValueError("尚无可靠的 API 字段与界面控件对应关系")
    if len(values) != len(layout):
        raise ValueError("界面控件数量与已验证的布局不一致")
    return layout.index(name)


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
            slot = _widget_slot(node, name)
            node["widgets_values"][slot] = copy.deepcopy(value)
        except ValueError as exc:
            warnings.append(
                f"节点 #{node_id} 的 {name} 未同步到界面工作流：{exc}；"
                "拖回 ComfyUI 后请核对该参数。"
            )
    return {"workflow": workflow, "warnings": warnings}
