"""Lossless gallery exports and capability-aware generation drafts."""

from __future__ import annotations

import copy
import json
import math
import re
from typing import Any

from .config import RuntimeSettings
from .image_metadata import parse_parameter_text
from .storage import project_import_metadata


def request_snapshot(detail: dict[str, Any]) -> dict[str, Any]:
    """Project a saved request without credentials or invocation identity."""

    parameters = detail.get("parameters") or {}
    return {
        "mode": detail.get("mode", "unknown"),
        "provider_id": detail.get("provider_id", ""),
        "model_ref": (
            f"{detail['provider_id']}:{detail['model']}"
            if detail.get("provider_id") and detail.get("model")
            else ""
        ),
        "model": detail.get("model", ""),
        "prompt": detail.get("original_prompt", ""),
        "negative_prompt": parameters.get("negative_prompt", ""),
        "size": parameters.get("size", ""),
        "count": parameters.get("count", 1),
        "parameters": copy.deepcopy(parameters.get("parameters", {})),
    }


def export_parameters(
    detail: dict[str, Any], image_id: str = "", format_name: str = "studio"
) -> dict[str, str]:
    """Build copy/download text while preserving original workflow JSON."""

    images = detail.get("images") or []
    image = next((item for item in images if item.get("id") == image_id), None)
    if not image_id and images:
        image = images[0]
    if image is None:
        raise ValueError("所选图片不属于当前生成记录或已被删除")
    if detail.get("source") == "import" and image.get("supplemental"):
        supplemental = image["supplemental"]
        detail = {**detail, "supplemental": supplemental}
        for key in ("model", "mode", "generation_engine", "generated_at"):
            if key in supplemental:
                detail[key] = supplemental[key]
        if "prompt" in supplemental:
            detail["original_prompt"] = supplemental["prompt"]
    metadata = project_import_metadata(
        image.get("metadata") or {},
        (detail.get("supplemental") or {}).get("overrides") or {},
    )
    raw = metadata.get("raw") or {}
    normalized = metadata.get("normalized") or {}
    request = request_snapshot(detail)
    if detail.get("source") == "import":
        supplemental = detail.get("supplemental") or {}
        overrides = supplemental.get("overrides") or {}
        display = {**normalized, **(supplemental.get("display_parameters") or {})}
        request = {
            "mode": detail.get("mode", "unknown"),
            "model": detail.get("model", ""),
            "prompt": detail.get("original_prompt", display.get("prompt", "")),
            "parameters": copy.deepcopy(overrides.get("parameters") or {}),
        }
        if "negative_prompt" in display:
            request["negative_prompt"] = display["negative_prompt"]
    content: Any
    if format_name == "studio":
        content = {
            "format": "image_studio",
            "version": 1,
            "generation_engine": detail.get("generation_engine", "unknown"),
            "data": request,
            "metadata": metadata if detail.get("source") == "import" else {},
            "supplemental": detail.get("supplemental", {}),
            "has_request_snapshot": detail.get("source") != "import",
        }
    elif format_name == "nai":
        if (
            detail.get("source") != "import"
            and detail.get("provider_kind") == "nai_direct"
        ):
            content = {
                **request["parameters"],
                "tag": request["prompt"],
                "model": request["model"],
                "size": request["size"],
                "negative": request["negative_prompt"],
            }
        elif metadata.get("format") == "novelai":
            supplemental = detail.get("supplemental") or {}
            content = _nai_projection(
                {**normalized, **(supplemental.get("display_parameters") or {})}
            )
            content.update(request.get("parameters") or {})
            content["tag"] = request["prompt"]
            content["model"] = request["model"]
            if "negative_prompt" in request:
                content["negative"] = request["negative_prompt"]
            content.setdefault("artist", "")
        else:
            raise ValueError("当前图片没有可转换的 NAI 参数")
    elif format_name == "novelai":
        content = raw.get("Comment") or raw.get("comment")
        if not content or metadata.get("format") != "novelai":
            raise ValueError("当前图片没有 NovelAI 原始元数据")
    elif format_name in {"workflow", "comfy_api"}:
        content = raw.get("workflow" if format_name == "workflow" else "prompt")
        if metadata.get("format") != "comfyui" or not content:
            raise ValueError("图片中没有该格式的 ComfyUI 工作流")
    elif format_name == "a1111":
        if metadata.get("format") != "a1111":
            raise ValueError("当前图片没有 Stable Diffusion 参数")
        content = {
            "format": "a1111",
            "version": 1,
            "parameters": normalized,
            "raw": raw,
        }
    else:
        raise ValueError("不支持的参数复制格式")
    filename = re.sub(
        r"[^\w.-]+", "_", str(image.get("download_filename") or detail["id"])
    )
    filename = filename.rsplit(".", 1)[0] + f"_{format_name}.json"
    return {
        "format": format_name,
        "filename": filename,
        "content": content
        if isinstance(content, str)
        else json.dumps(content, ensure_ascii=False, indent=2),
    }


def _nai_projection(normalized: dict[str, Any]) -> dict[str, Any]:
    mapping = {
        "prompt": "tag",
        "negative_prompt": "negative",
        "model": "model",
        "steps": "steps",
        "guidance_scale": "scale",
        "cfg_rescale": "cfg",
        "sampler": "sampler",
        "scheduler": "noise_schedule",
        "seed": "seed",
    }
    result = {
        target: normalized[source]
        for source, target in mapping.items()
        if source in normalized
    }
    size = _declared_size(normalized, nai=True)
    if size:
        result["size"] = size
    return result


def _declared_size(values: dict[str, Any], *, nai: bool = False) -> str:
    width, height = values.get("width"), values.get("height")
    if not width or not height:
        return ""
    if nai:
        return {(832, 1216): "竖图", (1216, 832): "横图", (1024, 1024): "方图"}.get(
            (width, height), f"{width}x{height}"
        )
    return f"{width}x{height}"


def resolve_parameters(
    content: str, settings: RuntimeSettings, model_ref: str = ""
) -> dict[str, Any]:
    """Resolve a copy format to a model draft, reporting every rejected value."""

    parsed = parse_parameter_text(content)
    normalized = parsed.get("normalized") or {}
    source_format = parsed.get("format", "unknown")
    source = copy.deepcopy(parsed.get("request") or {})
    # An Image Studio export carries exact request intent; imported exports retain provenance.
    envelope = parsed.get("envelope")
    if isinstance(envelope, dict) and envelope.get("format") == "image_studio":
        for key in ("data", "metadata", "supplemental"):
            if key in envelope and not isinstance(envelope[key], dict):
                raise ValueError(f"{key} 必须是对象")
        supplemental = envelope.get("supplemental", {})
        for key in ("overrides", "display_parameters"):
            if key in supplemental and not isinstance(supplemental[key], dict):
                raise ValueError(f"supplemental.{key} 必须是对象")
        if not isinstance(
            supplemental.get("overrides", {}).get("parameters", {}), dict
        ):
            raise ValueError("人工补充 parameters 必须是对象")
        if not isinstance(envelope.get("metadata", {}).get("normalized", {}), dict):
            raise ValueError("metadata.normalized 必须是对象")
        source = copy.deepcopy(envelope.get("data") or {})
        source_format = envelope.get("generation_engine") or source_format
        if envelope.get("has_request_snapshot") is False:
            projected = project_import_metadata(
                envelope.get("metadata") or {}, supplemental.get("overrides") or {}
            )
            normalized = projected.get("normalized") or normalized
            supplemental = envelope.get("supplemental") or {}
            normalized = {
                **normalized,
                **(supplemental.get("display_parameters") or {}),
            }
            if supplemental.get("overrides", {}).get("comfy_output_node"):
                normalized = {
                    **(supplemental.get("display_parameters") or {}),
                    **(projected.get("normalized") or {}),
                    **(supplemental.get("overrides", {}).get("parameters") or {}),
                }
            overrides = supplemental.get("overrides") or {}
            source["parameters"] = {
                **(source.get("parameters") or {}),
                **(overrides.get("parameters") or {}),
            }
            for key in ("prompt", "negative_prompt", "model", "mode"):
                if key in overrides:
                    source[key] = overrides[key]
    if not isinstance(source, dict) or not isinstance(
        source.get("parameters", {}), dict
    ):
        raise ValueError("请求及 parameters 必须是对象")
    mode_aliases = {
        "text2img": "text2img",
        "txt2img": "text2img",
        "t2i": "text2img",
        "generate": "text2img",
        "img2img": "img2img",
        "image2image": "img2img",
        "i2i": "img2img",
    }
    explicit_mode = next(
        (
            mode_aliases[str(value).strip().lower()]
            for value in (
                source.get("mode"),
                normalized.get("mode"),
                source.get("action"),
            )
            if str(value).strip().lower() in mode_aliases
        ),
        "",
    )
    mode = explicit_mode or "text2img"
    requested_model = str(source.get("model") or normalized.get("model") or "")
    requested_ref = str(source.get("model_ref") or "")
    if not requested_ref and source.get("provider_id") and requested_model:
        requested_ref = f"{source['provider_id']}:{requested_model}"
    candidates = [
        {
            **model.public_dict(),
            "model_ref": f"{provider.id}:{model.id}",
            "provider_id": provider.id,
            "provider_name": provider.name,
            "provider_kind": provider.kind,
        }
        for provider, model in settings.models_for_mode(mode)
    ]
    selected = next((m for m in candidates if m["model_ref"] == model_ref), None)
    selection_reason = "user_choice" if selected else ""
    if model_ref and selected is None:
        raise ValueError("所选模型已停用、不存在或不支持该生成模式")
    if not selected:
        selected = next(
            (m for m in candidates if m["model_ref"] == requested_ref), None
        )
        if selected:
            selection_reason = "model_ref"
    if not selected and requested_model:
        matches = [m for m in candidates if m["id"] == requested_model]
        if not matches and source_format in {"novelai", "nai"}:
            version = re.search(
                r"(?:Diffusion\s+)?V(\d+(?:\.\d+)?)\b", requested_model, re.I
            )
            if version:
                prefix = "nai-diffusion-" + version[1].replace(".", "-") + "-"
                matches = [
                    m
                    for m in candidates
                    if m["provider_kind"] == "nai_direct" and m["id"].startswith(prefix)
                ]
        if len(matches) == 1:
            selected = matches[0]
            selection_reason = "model"
    if not selected and not requested_model and not requested_ref:
        family = {
            "nai": "nai_direct",
            "novelai": "nai_direct",
            "openai_images": "openai_images",
            "gemini": "gemini",
        }.get(source_format)
        provider_id = str(source.get("provider_id") or "")
        matches = [
            item
            for item in candidates
            if family
            and item["provider_kind"] == family
            and (not provider_id or item["provider_id"] == provider_id)
        ]
        if len(matches) == 1:
            selected = matches[0]
            selection_reason = "source_unique"
    warnings = list(parsed.get("warnings") or [])
    if source_format == "comfyui":
        warnings.append(
            "ComfyUI 文本候选仅供静态检查和人工采用，不会将全部候选自动加入提示词。"
        )
        if isinstance(envelope, dict) and envelope.get("has_request_snapshot") is False:
            warnings.extend(
                value
                for value in (envelope.get("metadata", {}).get("warnings") or [])
                if isinstance(value, str)
            )
        for key, label in (
            ("prompt_status", "正向"),
            ("negative_prompt_status", "反向"),
        ):
            if normalized.get(key) in {"summary", "partial"}:
                warnings.append(
                    f"ComfyUI {label}提示词是组合或多阶段条件的文本摘要，"
                    "不等价于原始条件；完整结构保留在采样阶段与条件信息中。"
                )
    if not explicit_mode:
        warnings.append("元数据未确定生成模式，暂按文生图准备草稿，请核对。")
    if selection_reason == "source_unique":
        warnings.append("参数未指定型号，已选择当前来源唯一可用的模型，请核对。")
    if mode == "img2img":
        warnings.append("参数文本不包含原始参考图，请补充参考图后生成。")
    draft = {
        "mode": mode,
        "model": requested_model,
        "model_ref": "",
        "provider_id": "",
        "prompt": source.get("prompt", source.get("tag", normalized.get("prompt", ""))),
        "negative_prompt": source.get(
            "negative_prompt",
            source.get("negative", normalized.get("negative_prompt", "")),
        ),
        "parameters": {},
        "references": [],
    }
    if selected is None:
        return {
            "draft": draft,
            "candidates": candidates,
            "requires_model_selection": True,
            "selection_reason": "",
            "warnings": warnings,
            "unmapped": {
                **{
                    key: value
                    for key, value in source.items()
                    if key
                    not in {
                        "tag",
                        "prompt",
                        "negative",
                        "negative_prompt",
                        "model",
                        "model_ref",
                        "provider_id",
                        "mode",
                        "parameters",
                    }
                },
                **{
                    key: value
                    for key, value in normalized.items()
                    if key
                    not in {
                        "prompt",
                        "negative_prompt",
                        "model",
                        "mode",
                        "file_dimensions",
                    }
                },
                **(source.get("parameters") or {}),
            },
        }
    draft.update(
        model_ref=selected["model_ref"],
        provider_id=selected["provider_id"],
        model=selected["id"],
    )
    is_nai = selected["provider_kind"] == "nai_direct"
    descriptors = selected.get("parameters") or {}
    values = {
        key: copy.deepcopy(desc["default"])
        for key, desc in descriptors.items()
        if "default" in desc
    }
    supplied = copy.deepcopy(source.get("parameters") or {})
    if source.get("tag") is not None:
        supplied.update(
            {
                key: value
                for key, value in source.items()
                if key
                not in {
                    "tag",
                    "negative",
                    "model",
                    "mode",
                    "provider_id",
                    "model_ref",
                    "parameters",
                    "prompt",
                    "negative_prompt",
                    "action",
                }
            }
        )
    normalized_mapping = {
        "steps": "steps",
        "seed": "seed",
        "sampler": "sampler",
        "scheduler": "noise_schedule" if is_nai else "scheduler",
        "guidance_scale": "scale" if is_nai else "cfg_scale",
        "cfg_rescale": "cfg" if is_nai else "cfg_rescale",
    }
    if not source or (
        isinstance(envelope, dict) and envelope.get("has_request_snapshot") is False
    ):
        for key, target in normalized_mapping.items():
            if key in normalized:
                supplied.setdefault(target, normalized[key])
        if is_nai:
            supplied.setdefault("artist", "")
        derived_size = _declared_size(normalized, nai=is_nai)
        if derived_size:
            supplied.setdefault("size", derived_size)
    if is_nai and source_format in {"nai", "novelai"}:
        supplied.setdefault("artist", source.get("artist", ""))
    elif source_format in {"nai", "novelai"}:
        for original, target in {
            "scale": "cfg_scale",
            "cfg": "cfg_rescale",
            "noise_schedule": "scheduler",
        }.items():
            if original in supplied:
                supplied[target] = supplied.pop(original)
    for key in ("size", "count"):
        if key in source:
            supplied[key] = source[key]
    unmapped: dict[str, Any] = {}
    for key, value in supplied.items():
        name = (
            key
            if key in descriptors
            else next(
                (
                    name
                    for name, desc in descriptors.items()
                    if desc.get("request_key") == key
                ),
                "",
            )
        )
        if not name:
            unmapped[key] = value
            continue
        descriptor = descriptors[name]
        choices = descriptor.get("choices")
        valid = not isinstance(choices, list) or any(
            str(item.get("value") if isinstance(item, dict) else item) == str(value)
            for item in choices
        )
        kind = str(descriptor.get("type") or "text")
        if kind in {"number", "int", "integer", "float"}:
            try:
                number = float(value)
                valid = valid and not isinstance(value, bool) and math.isfinite(number)
                valid = valid and (
                    descriptor.get("min") is None or number >= float(descriptor["min"])
                )
                valid = valid and (
                    descriptor.get("max") is None or number <= float(descriptor["max"])
                )
                if kind in {"int", "integer"}:
                    valid = valid and number.is_integer()
                # Keep seeds outside JavaScript's exact integer range as strings.
                if valid and abs(number) <= 9007199254740991:
                    value = int(number) if number.is_integer() else number
                elif valid:
                    valid = False
                    warnings.append(
                        f"{key} 超出网页数值输入的安全整数范围，已保留原值但未填入。"
                    )
            except (ValueError, TypeError, OverflowError):
                valid = False
        elif kind in {"boolean", "bool"}:
            valid = valid and isinstance(value, bool)
        elif kind in {"json", "object"}:
            valid = valid and isinstance(value, (dict, list))
        elif kind not in {"select", "preset"}:
            valid = valid and isinstance(value, (str, int, float)) and value is not None
        if not valid:
            unmapped[key] = value
            warnings.append(f"{key} 不符合所选模型的参数范围或类型，未覆盖模型默认值。")
            continue
        values[name] = value
    if not selected.get("supports_negative_prompt") and draft["negative_prompt"]:
        unmapped["negative_prompt"] = draft["negative_prompt"]
        draft["negative_prompt"] = ""
    elif (
        "negative_prompt" not in source
        and "negative" not in source
        and "negative_prompt" not in normalized
    ):
        draft["negative_prompt"] = selected.get("negative_prompt_default", "")
    for key in (
        "loras",
        "stages",
        "condition_nodes",
        "outputs",
        "prompt_candidates",
        "selected_output_node",
        "requires_output_selection",
        "characters",
        "character_prompts",
    ):
        if normalized.get(key):
            unmapped.setdefault(key, normalized[key])
    if source_format in {"comfyui", "a1111"}:
        warnings.append(
            "当前插件不执行 ComfyUI 或 Stable Diffusion 工作流；这里只填写目标模型可接受的字段。"
        )
    if is_nai and source_format not in {"nai", "novelai", "image_studio"}:
        warnings.append("目标为 NAI 标签模型，请核对提示词语法和参数含义。")
    draft["parameters"] = values
    if "size" in values:
        draft["size"] = values["size"]
    if "count" in values:
        draft["count"] = values["count"]
    if unmapped:
        warnings.append("部分参数无法映射，原值保留在未填写参数中。")
    return {
        "draft": draft,
        "candidates": candidates,
        "requires_model_selection": False,
        "selection_reason": selection_reason,
        "warnings": list(dict.fromkeys(warnings)),
        "unmapped": unmapped,
    }
