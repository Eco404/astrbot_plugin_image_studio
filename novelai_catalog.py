"""Model-specific NovelAI capabilities and advanced generation parameter presets."""

from __future__ import annotations

import copy
from typing import Any

MODEL_NAMES = {
    "nai-diffusion-4-5-full": "NovelAI V4.5 完整版",
    "nai-diffusion-4-5-curated": "NovelAI V4.5 精选版",
    "nai-diffusion-5-full": "NovelAI V5 完整版",
    "nai-diffusion-5-curated": "NovelAI V5 精选版",
}


def model_capabilities(model_id: str) -> dict[str, Any]:
    if model_id not in MODEL_NAMES:
        return {}
    v5 = model_id.startswith("nai-diffusion-5-")
    return {
        "precise_reference": not v5,
        "vibe_transfer": not v5,
        "inpainting": True,
        "transparency": v5,
        "max_characters": 32 if v5 else 6,
        "character_position_grid": 0 if v5 else 5,
        "max_reference_images": 2 if v5 else 8,
        "reference_modes": ["img2img", "inpaint"]
        if v5
        else ["img2img", "precise", "vibe", "inpaint"],
        "inpainting_model": "nai-diffusion-4-5-curated-inpainting"
        if model_id == "nai-diffusion-5-curated"
        else model_id + "-inpainting",
        "inpainting_max_characters": 6
        if model_id == "nai-diffusion-5-curated"
        else 32
        if v5
        else 6,
        "inpainting_character_position_grid": 5
        if model_id == "nai-diffusion-5-curated" or not v5
        else 0,
        "variety_boost": not v5,
        "noise_schedules": ["karras"]
        if v5
        else ["karras", "exponential", "polyexponential"],
    }


def advanced_parameters(model_id: str) -> dict[str, dict[str, Any]]:
    caps = model_capabilities(model_id)
    if not caps:
        return {}
    labels = {
        "img2img": "图生图",
        "precise": "角色 / 风格参考",
        "vibe": "Vibe Transfer",
        "inpaint": "局部重绘",
    }
    result = {
        "reference_mode": {
            "type": "select",
            "label": "参考图用途",
            "default": "img2img",
            "description": "图生图工作区内选择参考用途。img2img=底图重绘，precise=角色/风格参考，vibe=氛围参考，inpaint=底图+黑白蒙版局部重绘。",
            "choices": [
                {"value": mode, "label": labels[mode]}
                for mode in caps["reference_modes"]
            ],
            "modes": ["img2img"],
        },
        "reference_settings": {
            "type": "json",
            "label": "逐图参考设置",
            "default": [],
            "ui_widget": "novelai_references",
            "modes": ["img2img"],
            "description": "与 references 按顺序一一对应的数组，每项 {type,strength,fidelity,information_extracted}。type: base底图、mask蒙版、character角色、style风格、character_style角色与风格、vibe氛围；其余数值0–1。省略/空数组按reference_mode自动分配。仅一张底图及一张蒙版；角色/风格不能与vibe混用。不得传入图片Base64。",
        },
        "characters": {
            "type": "json",
            "label": "角色提示词",
            "default": [],
            "ui_widget": "novelai_characters",
            "description": f"最多{caps['max_characters']}个角色，数组每项 {{prompt,negative_prompt,x,y}}。正反向角色按序配对；x/y在0–1之间，仅use_coords=true时使用。"
            + (
                "V4.5位置自动对齐到5×5网格。"
                if caps["character_position_grid"]
                else "V5支持自由坐标。"
            ),
        },
        "use_coords": {
            "type": "boolean",
            "label": "指定角色位置",
            "default": False,
            "description": "开启后使用各角色的x/y位置；关闭时由模型自动安排。",
        },
        "color_correct": {
            "type": "boolean",
            "label": "底图颜色校正",
            "default": True,
            "modes": ["img2img"],
            "description": "图生图和局部重绘时校正底图颜色。",
        },
        "quality_preset": {
            "type": "select",
            "label": "质量标签预设",
            "default": "none",
            "choices": ["none", "standard", "light"]
            if caps["transparency"]
            else ["none", "standard"],
            "description": "none不追加标签；standard追加该模型官方质量标签；V5另支持light。实际展开的提示词会随图片参数保留。",
        },
    }
    if caps["variety_boost"]:
        result["variety_boost"] = {
            "type": "boolean",
            "label": "Variety Boost",
            "default": False,
            "description": "V4.5按图片尺寸缩放官方CFG延迟阈值以增加构图变化；V5不支持。",
        }
    if caps["vibe_transfer"]:
        result["normalize_reference_strength_multiple"] = {
            "type": "boolean",
            "label": "归一化 Vibe 强度",
            "default": True,
            "modes": ["img2img"],
            "description": "将多个Vibe的强度总和归一化，避免参考图影响过强。",
        }
    if caps["transparency"]:
        result.update(
            {
                "straight_alpha": {
                    "type": "boolean",
                    "label": "透明通道输出",
                    "default": False,
                    "description": "V5输出带独立透明通道的PNG/WebP。",
                },
                "tag_hint_transparent_background": {
                    "type": "boolean",
                    "label": "透明背景提示",
                    "default": False,
                    "description": "V5提示模型生成透明背景，配合透明通道输出使用。",
                },
            }
        )
    for name, descriptor in result.items():
        descriptor.update(
            request_key=name,
            webui_visible=True,
            record_in_history=True,
            refill_from_history=True,
        )
    return copy.deepcopy(result)
