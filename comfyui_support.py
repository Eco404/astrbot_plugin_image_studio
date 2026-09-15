"""Import and present ComfyUI workflows without executing metadata guesses."""

from __future__ import annotations

import json

from .comfyui_workflows import inspect_workflow, normalize_workflow
from .image_metadata import parse_image_metadata
from .models import browser_safe_integers


def import_result(value):
    if isinstance(value, bytes):
        try:
            value = json.loads(value.decode("utf-8-sig"))
        except (ValueError, UnicodeError):
            value = parse_image_metadata(value).get("raw", {})
        return normalize_workflow(value)
    config = normalize_workflow(value)
    inspection = inspect_workflow(config)
    parameters = {}
    for key, binding in config["bindings"].items():
        if binding["source"] in {"reference", "prompt", "negative_prompt"}:
            continue
        target = binding["targets"][0]
        default = config["api_graph"][target["node_id"]]["inputs"][target["input_name"]]
        parameters[key] = {
            "label": binding.get("label", key),
            "type": binding["type"],
            "request_key": binding["source"] if binding["source"] == "count" else key,
            "default": binding.get("default", default),
            **{
                name: binding[name]
                for name in ("min", "max", "description")
                if name in binding
            },
            **({"choices": binding["options"]} if "options" in binding else {}),
        }
    sources = {item["source"] for item in config["bindings"].values()}
    refs = [
        item["reference_index"]
        for item in config["bindings"].values()
        if item["source"] == "reference"
    ]
    return browser_safe_integers(
        {
            "comfyui": config,
            "parameters": parameters,
            "suggestions": inspection["suggested_bindings"],
            "nodes": inspection["nodes"],
            "outputs": inspection["outputs"],
            "capabilities": {
                "supports_text2img": not refs,
                "supports_img2img": bool(refs),
                "supports_negative_prompt": "negative_prompt" in sources,
                "max_reference_images": max(refs, default=-1) + 1,
                "prompt_required": "prompt" in sources,
                "count_bound": "count" in sources,
            },
        }
    )
