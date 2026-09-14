"""Validate and prepare advanced NovelAI inputs without performing network I/O."""

from __future__ import annotations

import base64
import io
import json
import math
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError

from .novelai_catalog import model_capabilities

ADVANCED_KEYS = frozenset(
    {
        "reference_mode",
        "reference_settings",
        "characters",
        "use_coords",
        "color_correct",
        "normalize_reference_strength_multiple",
        "variety_boost",
        "quality_preset",
        "straight_alpha",
        "tag_hint_transparent_background",
    }
)
PRECISE_ROLES = {"character", "style", "character_style"}


def boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"NovelAI 参数 {name} 必须是布尔值")
    return value


def number(value: Any, name: str, low: float = 0, high: float = 1) -> float:
    if isinstance(value, bool):
        raise ValueError(f"NovelAI 参数 {name} 必须是 {low}–{high} 的数字")
    try:
        result = float(value)
    except (ValueError, TypeError):
        raise ValueError(f"NovelAI 参数 {name} 必须是 {low}–{high} 的数字") from None
    if not math.isfinite(result) or not low <= result <= high:
        raise ValueError(f"NovelAI 参数 {name} 必须是 {low}–{high} 的数字")
    return result


def array(value: Any, name: str) -> list:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise ValueError(f"NovelAI 参数 {name} 必须是 JSON 数组") from None
    if not isinstance(value, list):
        raise ValueError(f"NovelAI 参数 {name} 必须是数组")
    return value


def normalized_reference_settings(
    request, model_id: str
) -> tuple[str, list[dict[str, Any]]]:
    caps = model_capabilities(model_id)
    mode = request.parameters.get("reference_mode", "img2img")
    if not isinstance(mode, str) or mode not in caps["reference_modes"]:
        raise ValueError(
            f"NovelAI 模型 {model_id} 不支持参考用途 {mode}；可用：{'、'.join(caps['reference_modes'])}"
        )
    settings = array(
        request.parameters.get("reference_settings", []), "reference_settings"
    )
    if request.mode == "text2img":
        if request.references or settings or mode != "img2img":
            raise ValueError("文生图不能包含图片参考；请使用图生图模式并选择参考用途")
        return mode, []
    if not request.references:
        raise ValueError("NovelAI 图生图需要至少一张参考图")
    if len(request.references) > caps["max_reference_images"]:
        raise ValueError(
            f"当前模型最多接受 {caps['max_reference_images']} 张图片输入（含底图和蒙版）"
        )
    if settings and len(settings) != len(request.references):
        raise ValueError(
            f"reference_settings 有 {len(settings)} 项，但参考图有 {len(request.references)} 张；请按相同顺序一一对应"
        )
    if not settings:
        if mode == "img2img" and len(request.references) != 1:
            raise ValueError(
                "NovelAI 原生图生图需要且仅接受一张底图；多种参考组合请指定 reference_settings"
            )
        roles = {"img2img": "base", "precise": "character", "vibe": "vibe"}
        settings = [
            {
                "type": ("base" if i == 0 else "mask" if i == 1 else "character")
                if mode == "inpaint"
                else roles[mode]
            }
            for i in range(len(request.references))
        ]
    result = []
    for index, item in enumerate(settings, 1):
        if not isinstance(item, dict) or set(item) - {
            "type",
            "strength",
            "fidelity",
            "information_extracted",
        }:
            raise ValueError(
                f"reference_settings 第 {index} 项格式无效；仅允许 type、strength、fidelity、information_extracted"
            )
        role = item.get("type")
        if not isinstance(role, str) or role not in {
            "base",
            "mask",
            "vibe",
            *PRECISE_ROLES,
        }:
            raise ValueError(f"reference_settings 第 {index} 项的 type 无效")
        if role in PRECISE_ROLES and not caps["precise_reference"]:
            raise ValueError(
                f"第 {index} 张图片：{model_id} 不支持角色/风格参考，请选择 V4.5"
            )
        if role == "vibe" and not caps["vibe_transfer"]:
            raise ValueError(
                f"第 {index} 张图片：{model_id} 不支持 Vibe Transfer，请选择 V4.5"
            )
        parsed = {"type": role}
        # Validate all supplied controls, then retain only applicable values.
        for key in ("strength", "fidelity", "information_extracted"):
            if key in item:
                number(item[key], f"reference_settings[{index}].{key}")
        if role in PRECISE_ROLES or role == "vibe":
            info_default = 0.7 if role == "vibe" and model_id.endswith("full") else 1.0
            parsed.update(
                strength=number(item.get("strength", 0.6), f"参考图{index}强度"),
                information_extracted=number(
                    item.get("information_extracted", info_default),
                    f"参考图{index}信息提取",
                ),
            )
        if role in PRECISE_ROLES:
            parsed["fidelity"] = number(
                item.get("fidelity", 0.6), f"参考图{index}保真度"
            )
        result.append(parsed)
    roles = [item["type"] for item in result]
    if roles.count("base") > 1 or roles.count("mask") > 1:
        raise ValueError("同一次请求最多包含一张底图及一张蒙版")
    if "mask" in roles and ("base" not in roles or mode != "inpaint"):
        raise ValueError("蒙版需要搭配底图，并选择 inpaint 局部重绘用途")
    if mode == "inpaint" and not ("base" in roles and "mask" in roles):
        raise ValueError("局部重绘必须指定一张底图和一张黑白蒙版，白色重绘、黑色保留")
    if mode == "img2img" and "base" not in roles:
        raise ValueError(
            "img2img 用途需要一张底图；仅参考角色或氛围时请选择 precise 或 vibe"
        )
    if mode == "precise" and not PRECISE_ROLES.intersection(roles):
        raise ValueError("precise 用途需要至少一张角色或风格参考图")
    if mode == "vibe" and "vibe" not in roles:
        raise ValueError("vibe 用途需要至少一张 Vibe 参考图")
    if "vibe" in roles and PRECISE_ROLES.intersection(roles):
        raise ValueError("Vibe Transfer 与角色/风格参考不能在同一次生成中混用")
    if "mask" in roles and "vibe" in roles:
        raise ValueError("当前局部重绘不支持 Vibe Transfer；可改用角色/风格参考")
    return mode, result


def source_image(data: bytes) -> Image.Image:
    try:
        if not data or len(data) > 30 * 1024 * 1024:
            raise ValueError("参考图为空或超过 30 MB")
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > 64_000_000:
                raise ValueError("参考图超过 6400 万像素")
            return ImageOps.exif_transpose(image).copy()
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
        raise ValueError("参考图不是可读取的图片") from None


def image_base64(image: Image.Image, fmt: str = "PNG") -> str:
    target = io.BytesIO()
    image.save(target, format=fmt)
    return base64.b64encode(target.getvalue()).decode("ascii")


def precise_image(data: bytes) -> str:
    image = black_matte(source_image(data))
    ratio = image.width / image.height
    target = min(
        ((1536, 1024), (1472, 1472), (1024, 1536)),
        key=lambda size: abs(size[0] / size[1] - ratio),
    )
    return image_base64(
        ImageOps.pad(image, target, method=Image.Resampling.LANCZOS, color="black"),
        "PNG",
    )


def black_matte(image: Image.Image) -> Image.Image:
    background = Image.new("RGBA", image.size, "black")
    background.alpha_composite(image.convert("RGBA"))
    return background.convert("RGB")


def mask_image(data: bytes, base: bytes, width: int, height: int) -> str:
    mask = source_image(data)
    original = source_image(base)
    if mask.size != original.size:
        raise ValueError("蒙版尺寸必须与底图原始尺寸一致；请重新绘制或上传对应蒙版")
    # Interpret brightness, never an opaque alpha channel, as the paint mask.
    black = Image.new("RGBA", mask.size, "black")
    black.alpha_composite(mask.convert("RGBA"))
    gray = black.convert("L").resize((width, height), Image.Resampling.NEAREST)
    gray = gray.resize((width // 8, height // 8), Image.Resampling.BOX)
    binary = gray.point(lambda value: 255 if value >= 155 else 0).resize(
        (width, height), Image.Resampling.NEAREST
    )
    if binary.getextrema()[1] == 0:
        raise ValueError("蒙版没有白色重绘区域，请先标出需要修改的位置")
    return image_base64(binary.convert("RGB"))


def character_prompts(value: Any, use_coords: bool, caps: dict[str, Any]):
    values = array(value, "characters")
    if len(values) > caps["max_characters"]:
        raise ValueError(f"当前模型最多支持 {caps['max_characters']} 个角色提示词")
    positive, negative, effective = [], [], []
    total = 0
    for index, entry in enumerate(values, 1):
        if not isinstance(entry, dict) or set(entry) - {
            "prompt",
            "negative_prompt",
            "x",
            "y",
        }:
            raise ValueError(
                f"characters 第 {index} 项仅允许 prompt、negative_prompt、x、y"
            )
        prompt, uc = entry.get("prompt", ""), entry.get("negative_prompt", "")
        if not isinstance(prompt, str) or not prompt.strip() or not isinstance(uc, str):
            raise ValueError(f"第 {index} 个角色需要非空提示词，反向提示词必须是字符串")
        if len(prompt) > 6000 or len(uc) > 4000:
            raise ValueError(f"第 {index} 个角色提示词过长（正向6000/反向4000字符）")
        total += len(prompt) + len(uc)
        if total > 48_000:
            raise ValueError("所有角色提示词合计不能超过 48000 字符")
        point = {
            axis: number(entry.get(axis, 0.5), f"角色{index}.{axis}")
            for axis in ("x", "y")
        }
        if use_coords and caps["character_position_grid"]:
            for axis in point:
                point[axis] = (0.1, 0.3, 0.5, 0.7, 0.9)[min(4, int(point[axis] * 5))]
        positive.append({"char_caption": prompt, "centers": [point]})
        negative.append({"char_caption": uc, "centers": [point]})
        effective.append({"prompt": prompt, "negative_prompt": uc, **point})
    return positive, negative, effective


def prepare_advanced(request, model_id: str, width: int, height: int):
    """Return safe fields, effective values and separate raw inputs needing Vibe encoding."""
    caps = model_capabilities(model_id)
    values = request.parameters
    mode, refs = normalized_reference_settings(request, model_id)
    inpaint = request.mode == "img2img" and mode == "inpaint"
    wire_model = caps["inpainting_model"] if inpaint else model_id
    actual_caps = model_capabilities(wire_model.removesuffix("-inpainting"))
    fields, effective, vibes = {}, {}, []
    use_coords = boolean(values.get("use_coords", False), "use_coords")
    positive, negative, characters = character_prompts(
        values.get("characters", []), use_coords, actual_caps
    )
    effective.update(characters=characters, use_coords=use_coords)
    quality = values.get("quality_preset", "none")
    if (
        not isinstance(quality, str)
        or quality not in {"none", "standard", "light"}
        or quality == "light"
        and not actual_caps["transparency"]
    ):
        raise ValueError("当前模型不支持所选质量标签预设")
    prompt = request.prompt
    if quality != "none":
        suffix = (
            "very aesthetic, amazing quality, no text"
            if quality == "light"
            else "very aesthetic, masterpiece, no text"
        )
        if wire_model.removesuffix("-inpainting") == "nai-diffusion-4-5-curated":
            suffix += ", -0.8::feet::, rating:general"
        prompt = prompt.rstrip(", ") + ", " + suffix
    effective["quality_preset"] = quality
    for key in ("straight_alpha", "tag_hint_transparent_background"):
        value = boolean(values.get(key, False), key)
        if value and not actual_caps["transparency"]:
            raise ValueError(f"当前实际模型 {wire_model} 不支持透明背景；请关闭 {key}")
        if actual_caps["transparency"]:
            fields[key] = value
            effective[key] = value
    if fields.get("tag_hint_transparent_background"):
        prompt = prompt.rstrip(", ") + ", transparent background"
    fields.update(
        qualityToggle=quality != "none",
        tag_hint_qt={"none": 0, "standard": 1, "light": 3}[quality],
        use_coords=use_coords,
    )
    fields["v4_prompt"] = {
        "caption": {"base_caption": prompt, "char_captions": positive},
        "use_coords": use_coords,
        "use_order": True,
    }
    fields["v4_negative_prompt"] = {
        "caption": {"base_caption": request.negative_prompt, "char_captions": negative},
        "legacy_uc": False,
    }
    # Expanded prompts stay in provider payload/PNG metadata; retaining the preset
    # plus original prompt avoids appending its tags again on reproduction.
    boost = boolean(values.get("variety_boost", False), "variety_boost")
    if boost and not actual_caps["variety_boost"]:
        raise ValueError("V5 不支持 Variety Boost")
    if actual_caps["variety_boost"]:
        effective["variety_boost"] = boost
        if boost:
            fields["skip_cfg_above_sigma"] = 58 * math.sqrt(
                ((width // 8) * (height // 8)) / (104 * 152)
            )
    if request.mode == "text2img":
        return fields, effective, vibes, wire_model, "generate", prompt
    effective.update(reference_mode=mode, reference_settings=refs)
    roles = [item["type"] for item in refs]
    action = "infill" if inpaint else "img2img" if "base" in roles else "generate"
    color_correct = boolean(values.get("color_correct", True), "color_correct")
    if "base" in roles:
        effective["color_correct"] = color_correct
        fields["color_correct"] = color_correct
    if "mask" in roles:
        fields["mask"] = mask_image(
            request.references[roles.index("mask")].data,
            request.references[roles.index("base")].data,
            width,
            height,
        )
    for index, settings in enumerate(refs):
        role = settings["type"]
        if role in PRECISE_ROLES:
            fields.setdefault("director_reference_images", []).append(
                precise_image(request.references[index].data)
            )
            fields.setdefault("director_reference_descriptions", []).append(
                {
                    "caption": {
                        "base_caption": "character&style"
                        if role == "character_style"
                        else role,
                        "char_captions": [],
                    },
                    "legacy_uc": False,
                }
            )
            fields.setdefault("director_reference_strength_values", []).append(
                settings["strength"]
            )
            fields.setdefault(
                "director_reference_secondary_strength_values", []
            ).append(round(1 - settings["fidelity"], 8))
            fields.setdefault("director_reference_information_extracted", []).append(
                settings["information_extracted"]
            )
        elif role == "vibe":
            image = black_matte(source_image(request.references[index].data))
            image.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
            vibes.append(
                {
                    "image": image_base64(image),
                    "model": model_id,
                    "information_extracted": settings["information_extracted"],
                }
            )
            fields.setdefault("reference_strength_multiple", []).append(
                settings["strength"]
            )
    if vibes:
        normalize = boolean(
            values.get("normalize_reference_strength_multiple", True),
            "normalize_reference_strength_multiple",
        )
        strengths = fields["reference_strength_multiple"]
        if normalize and sum(strengths) > 1:
            fields["reference_strength_multiple"] = [
                value / sum(strengths) for value in strengths
            ]
        fields["normalize_reference_strength_multiple"] = (
            False  # Already normalized deterministically.
        )
        effective["normalize_reference_strength_multiple"] = normalize
    return fields, effective, vibes, wire_model, action, prompt
