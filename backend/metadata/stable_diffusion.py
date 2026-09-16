"""Read Stable Diffusion infotext without guessing generation mode."""

from __future__ import annotations
import json
import math
import re
from typing import Any
from .common import _warning


def _split_settings(text: str) -> list[str]:
    parts: list[str] = []
    start, depth, quote, escaped = 0, 0, "", False
    for index, char in enumerate(text):
        if escaped:
            escaped = False
        elif char == "\\" and quote:
            escaped = True
        elif quote:
            if char == quote:
                quote = ""
        elif char == '"':
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            parts.append(text[start:index].strip())
            start = index + 1
    parts.append(text[start:].strip())
    return parts


def _scalar(value: str) -> Any:
    value = value.strip()
    if re.fullmatch(r"[-+]?\d+", value):
        return int(value)
    if re.fullmatch(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?", value):
        number = float(value)
        return number if math.isfinite(number) else value
    if value.startswith('"') and value.endswith('"'):
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


def _parse_infotext(text: str, result: dict) -> bool:
    matches = list(re.finditer(r"(?:^|\n)Steps:\s*\d+\s*(?:,|$)", text))
    if not matches:
        return False
    start = matches[-1].start()
    body = text[:start].strip()
    settings_text = text[start:].strip()
    parameters: dict[str, Any] = {}
    previous_key = ""
    for token in _split_settings(settings_text):
        match = re.match(r"^([\w][\w /().-]*):\s*(.*)$", token, flags=re.DOTALL)
        if match:
            previous_key = match[1]
            parameters[previous_key] = _scalar(match[2])
        elif previous_key:
            parameters[previous_key] = f"{parameters[previous_key]}, {token}"
    if not any(key in parameters for key in ("Sampler", "Seed", "Size", "CFG scale")):
        return False
    parts = re.split(r"(?:^|\n)Negative prompt:\s*", body, maxsplit=1)
    normalized = result["normalized"]
    normalized["prompt"] = parts[0].strip()
    if len(parts) == 2:
        normalized["negative_prompt"] = parts[1].strip()
    _a1111_parameters(parameters, normalized)
    normalized["parameters"] = parameters
    _warning(
        result,
        "Stable Diffusion 参数未必包含生成模式；降噪强度不能单独区分图生图和高清修复",
    )
    return True


def _a1111_parameters(parameters: dict, normalized: dict) -> None:
    for source, destination in {
        "Steps": "steps",
        "Sampler": "sampler",
        "Schedule type": "scheduler",
        "CFG scale": "guidance_scale",
        "Seed": "seed",
        "Model": "model",
        "Model hash": "model_hash",
        "Denoising strength": "denoising_strength",
        "prompt": "prompt",
        "negative_prompt": "negative_prompt",
    }.items():
        if source in parameters:
            normalized[destination] = parameters[source]
    size = re.fullmatch(r"(\d+)\s*[xX×]\s*(\d+)", str(parameters.get("Size", "")))
    if size:
        normalized.update(width=int(size[1]), height=int(size[2]))
    loras = []
    for match in re.finditer(
        r"<lora:([^<>]+):([-+]?\d*\.?\d+)>", str(normalized.get("prompt", ""))
    ):
        loras.append({"name": match[1], "strength": float(match[2])})
    if loras:
        normalized["loras"] = loras
