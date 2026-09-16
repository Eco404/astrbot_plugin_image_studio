"""Lossy metadata projection of known UI nodes; never an executable API graph."""

from __future__ import annotations
from .common import MAX_NODES, _warning


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
    widget_names = {
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
        "ConditioningAverage": ("conditioning_to_strength",),
        "ConditioningSetArea": ("width", "height", "x", "y", "strength"),
        "ConditioningSetAreaPercentage": ("width", "height", "x", "y", "strength"),
        "ConditioningSetMask": ("strength", "set_cond_area"),
        "ConditioningSetTimestepRange": ("start", "end"),
        "ConditioningSetAreaStrength": ("strength",),
        "CheckpointLoaderSimple": ("ckpt_name",),
        "UNETLoader": ("unet_name", "weight_dtype"),
        "LoraLoader": ("lora_name", "strength_model", "strength_clip"),
        "LoraLoaderModelOnly": ("lora_name", "strength_model"),
        "EmptyLatentImage": ("width", "height", "batch_size"),
        "EmptySD3LatentImage": ("width", "height", "batch_size"),
        "LatentUpscaleBy": ("upscale_method", "scale_by"),
        "LatentUpscale": ("upscale_method", "width", "height", "crop"),
        "ImageScaleBy": ("upscale_method", "scale_by"),
        "ImageScale": ("upscale_method", "width", "height", "crop"),
        "SaveImage": ("filename_prefix",),
        "Seed (rgthree)": ("seed", "control_after_generate"),
        "PrimitiveString": ("value",),
        "PrimitiveInt": ("value",),
        "TextInput_": ("text",),
        "TextInput": ("text",),
        "String": ("text",),
        "VAELoader": ("vae_name",),
    }
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
