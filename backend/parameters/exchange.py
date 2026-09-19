"""Lossless gallery exports and capability-aware generation drafts."""

from __future__ import annotations

import copy
import json
import math
import re
from typing import Any

from ..config import RuntimeSettings
from ..metadata.parser import parse_parameter_text
from ..models import MODEL_SCHEDULING_KEYS, parameter_flag
from ..providers.novelai.catalog import model_capabilities
from ..gallery.projection import (
    compact_comfy_request,
    has_request_value,
    project_import_metadata,
)

_NOVELAI_IMAGE_FIELDS = frozenset(
    {
        "image",
        "mask",
        "reference_image",
        "reference_image_multiple",
        "director_reference_images",
        "reference_vibe_multiple",
        "controlnet_condition",
        "imageBase64",
        "image_base64",
        "encoded_image",
    }
)


def _without_image_payloads(value: Any, removed: list[str], path: str = "") -> Any:
    """Copied settings must never turn embedded image bytes into draft parameters."""

    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            item_path = f"{path}.{key}" if path else key
            if key in _NOVELAI_IMAGE_FIELDS:
                if item:
                    removed.append(item_path)
                continue
            result[key] = _without_image_payloads(item, removed, item_path)
        return result
    if isinstance(value, list):
        return [
            _without_image_payloads(item, removed, f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, str) and value.lstrip().lower().startswith("data:image/"):
        removed.append(path)
        return "[图片数据未导入，请重新选择原始参考图]"
    return value


def _novelai_metadata_parameters(metadata: dict[str, Any]) -> dict[str, Any]:
    raw = metadata.get("raw") or {}
    if not isinstance(raw, dict):
        return {}
    value = raw.get("Comment", raw.get("comment"))
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, RecursionError):
            return {}
    return value if isinstance(value, dict) else {}


def _novelai_reference_mode(parameters: dict[str, Any], model: str = "") -> str:
    action = str(parameters.get("action") or "").lower()
    if (
        action in {"infill", "inpaint", "inpainting"}
        or model.endswith("-inpainting")
        or parameters.get("mask")
    ):
        return "inpaint"
    if any(
        value
        for key, value in parameters.items()
        if key.startswith("director_reference_")
    ):
        return "precise"
    if any(
        parameters.get(key)
        for key in (
            "reference_image",
            "reference_image_multiple",
            "reference_vibe_multiple",
            "reference_strength_multiple",
            "reference_information_extracted_multiple",
        )
    ):
        return "vibe"
    return str(parameters.get("reference_mode") or "")


def _novelai_characters(
    parameters: dict[str, Any], normalized: dict[str, Any], warnings: list[str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Pair captions by their original index; refuse ambiguous coordinate groups."""

    captions = {}
    for field, name in (
        ("v4_prompt", "prompt"),
        ("v4_negative_prompt", "negative_prompt"),
    ):
        value = parameters.get(field)
        if isinstance(value, dict) and isinstance(value.get("caption"), dict):
            captions[name] = value["caption"].get("char_captions", [])
    if not captions and isinstance(normalized.get("characters"), dict):
        captions = normalized["characters"]
    positive, negative = captions.get("prompt", []), captions.get("negative_prompt", [])
    if not positive and not negative:
        return {}, {}
    invalid = (
        not isinstance(positive, list)
        or not isinstance(negative, list)
        or len(negative) > len(positive)
    )
    characters = []
    if not invalid:
        for index, caption in enumerate(positive):
            unwanted = negative[index] if index < len(negative) else {}
            if not isinstance(caption, dict) or not isinstance(unwanted, dict):
                invalid = True
                break
            centers = caption.get("centers") or []
            negative_centers = unwanted.get("centers") or []
            if (
                not isinstance(centers, list)
                or len(centers) > 1
                or not isinstance(negative_centers, list)
                or len(negative_centers) > 1
                or (centers and negative_centers and centers != negative_centers)
            ):
                invalid = True
                break
            center = (centers or negative_centers or [{"x": 0.5, "y": 0.5}])[0]
            prompt, negative_prompt = (
                caption.get("char_caption", ""),
                unwanted.get("char_caption", ""),
            )
            if (
                not isinstance(center, dict)
                or not isinstance(prompt, str)
                or not isinstance(negative_prompt, str)
                or any(
                    not isinstance(center.get(axis, 0.5), (int, float))
                    or isinstance(center.get(axis), bool)
                    or not 0 <= center.get(axis, 0.5) <= 1
                    for axis in ("x", "y")
                )
            ):
                invalid = True
                break
            characters.append(
                {
                    "prompt": prompt,
                    "negative_prompt": negative_prompt,
                    "x": center.get("x", 0.5),
                    "y": center.get("y", 0.5),
                }
            )
    if invalid:
        warnings.append(
            "NovelAI 多角色的正反向编号或位置不对应，无法安全配对；已保留原始角色结构，未自动回填。"
        )
        return {}, {"characters": captions}
    result: dict[str, Any] = {"characters": characters}
    prompt = parameters.get("v4_prompt")
    if isinstance(prompt, dict) and "use_coords" in prompt:
        result["use_coords"] = prompt["use_coords"]
    return result, {}


def _novelai_reference_settings(
    parameters: dict[str, Any], reference_mode: str, warnings: list[str]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    prefix: list[dict[str, Any]] = []
    if reference_mode == "inpaint":
        prefix = [{"type": "base"}, {"type": "mask"}]
        if not any(
            value
            for key, value in parameters.items()
            if key.startswith("director_reference_")
        ):
            return prefix, {}
        reference_mode = "precise"
    elif (
        parameters.get("image")
        or parameters.get("action") == "img2img"
        or parameters.get("request_type") == "ImageGenerateRequest"
    ):
        prefix = [{"type": "base"}]
    if reference_mode not in {"precise", "vibe"}:
        return [], {}
    if reference_mode == "precise":
        keys = {
            "type": "director_reference_descriptions",
            "strength": "director_reference_strength_values",
            "fidelity": "director_reference_secondary_strength_values",
            "information_extracted": "director_reference_information_extracted",
        }
        image_keys = ("director_reference_images",)
    else:
        keys = {
            "strength": "reference_strength_multiple",
            "information_extracted": "reference_information_extracted_multiple",
        }
        image_keys = ("reference_image_multiple", "reference_vibe_multiple")
    arrays = {
        name: parameters[key] for name, key in keys.items() if parameters.get(key)
    }
    image_lengths = [
        len(parameters[key])
        for key in image_keys
        if isinstance(parameters.get(key), list) and parameters[key]
    ]
    lengths = image_lengths + [
        len(value) for value in arrays.values() if isinstance(value, list)
    ]
    if not lengths and reference_mode == "vibe" and parameters.get("reference_image"):
        return prefix + [
            {
                "type": "vibe",
                "strength": parameters.get("reference_strength", 0.6),
                "information_extracted": parameters.get(
                    "reference_information_extracted", 1.0
                ),
            }
        ], {}
    if (
        not lengths
        or any(not isinstance(value, list) for value in arrays.values())
        or len(set(lengths)) != 1
    ):
        warnings.append(
            "NovelAI 参考图数量与逐图设置不一致，无法保证编号对应；未回填逐图设置，请按原始顺序重新选择并配置参考图。"
        )
        return [], {key: parameters[key] for key in keys.values() if key in parameters}
    settings = []
    for index in range(lengths[0]):
        item: dict[str, Any] = {
            "type": "vibe" if reference_mode == "vibe" else "character"
        }
        for name, values in arrays.items():
            value = values[index]
            if name == "type":
                caption = value.get("caption", {}) if isinstance(value, dict) else {}
                if not isinstance(caption, dict):
                    caption = {}
                value = {
                    "character": "character",
                    "style": "style",
                    "character&style": "character_style",
                }.get(caption.get("base_caption"))
                if value is None:
                    warnings.append(
                        f"NovelAI 第 {index + 1} 张精准参考的类型无法识别，未回填逐图设置。"
                    )
                    return [], {
                        key: parameters[key]
                        for key in keys.values()
                        if key in parameters
                    }
            else:
                if (
                    not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    or not 0 <= value <= 1
                ):
                    warnings.append(
                        f"NovelAI 第 {index + 1} 张参考图的 {keys[name]} 不是 0–1 的有效数值，未回填逐图设置。"
                    )
                    return [], {
                        key: parameters[key]
                        for key in keys.values()
                        if key in parameters
                    }
                if name == "fidelity":
                    value = round(1.0 - value, 10)
            item[name] = value
        settings.append(item)
    return prefix + settings, {}


def _novelai_projection(
    wire: dict[str, Any], normalized: dict[str, Any], warnings: list[str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    excluded = {
        "prompt",
        "uc",
        "negative_prompt",
        "model",
        "model_name",
        "action",
        "request_type",
        "width",
        "height",
        "v4_prompt",
        "v4_negative_prompt",
        *(_NOVELAI_IMAGE_FIELDS),
        "director_reference_descriptions",
        "director_reference_strength_values",
        "director_reference_secondary_strength_values",
        "director_reference_information_extracted",
    }
    result = {
        key: value
        for key, value in wire.items()
        if key not in excluded
        and key
        not in {
            "reference_strength_multiple",
            "reference_information_extracted_multiple",
            "reference_strength",
            "reference_information_extracted",
        }
    }
    if "n_samples" in result:
        result["count"] = result.pop("n_samples")
    if "skip_cfg_above_sigma" in result:
        threshold = result["skip_cfg_above_sigma"]
        expected_threshold = 58.0
        width, height = wire.get("width"), wire.get("height")
        if (
            isinstance(width, (int, float))
            and isinstance(height, (int, float))
            and min(width, height) >= 64
        ):
            expected_threshold *= math.sqrt(
                ((width // 8) * (height // 8)) / (104 * 152)
            )
        matches_boost = (
            isinstance(threshold, (int, float))
            and not isinstance(threshold, bool)
            and math.isclose(threshold, expected_threshold, rel_tol=1e-7, abs_tol=1e-7)
        )
        if threshold is None or matches_boost:
            result.setdefault("variety_boost", matches_boost)
            result.pop("skip_cfg_above_sigma")
        else:
            warnings.append(
                "skip_cfg_above_sigma 不符合官方 Variety Boost 基准阈值 58 按尺寸缩放的结果，已保留原值但未启用该开关。"
            )
    character_values, unmapped = _novelai_characters(wire, normalized, warnings)
    result.update(character_values)
    reference_mode = _novelai_reference_mode(
        wire, str(wire.get("model", wire.get("model_name", "")))
    )
    if reference_mode:
        result["reference_mode"] = reference_mode
        reference_settings, unrecognized = _novelai_reference_settings(
            wire, reference_mode, warnings
        )
        if reference_settings:
            result.setdefault("reference_settings", reference_settings)
        unmapped.update(unrecognized)
    has_vibe = any(
        wire.get(key)
        for key in (
            "reference_image",
            "reference_image_multiple",
            "reference_vibe_multiple",
            "reference_strength_multiple",
        )
    )
    if reference_mode in {"precise", "inpaint"} and has_vibe:
        warnings.append(
            "原始请求混用了 Vibe 与精准参考或局部重绘，当前官方接口组合不受支持；未回填参考设置，不能直接复现。"
        )
        result.pop("reference_settings", None)
        unmapped["reference_combination"] = {
            "reference_mode": reference_mode,
            "reference_strength_multiple": wire.get("reference_strength_multiple", []),
            "director_reference_descriptions": wire.get(
                "director_reference_descriptions", []
            ),
        }
    return result, unmapped


def request_snapshot(detail: dict[str, Any]) -> dict[str, Any]:
    """Project a saved request without credentials or invocation identity."""

    parameters = detail.get("parameters") or {}
    snapshot = {
        "mode": detail.get("mode", "unknown"),
        "provider_id": detail.get("provider_id", ""),
        "model_ref": (
            f"{detail['provider_id']}:{detail['model']}"
            if detail.get("provider_id") and detail.get("model")
            else ""
        ),
        "model": detail.get("model", ""),
        "prompt": detail.get("original_prompt", ""),
        "parameters": copy.deepcopy(parameters.get("parameters", {})),
    }
    for name in (
        "negative_prompt",
        "size",
        "count",
        "native_batch_size",
        "max_concurrent_requests",
    ):
        if name in parameters:
            snapshot[name] = parameters[name]
    return (
        compact_comfy_request(snapshot)
        if detail.get("provider_kind") == "comfyui"
        else snapshot
    )


def _restore_empty_comfy_inputs(
    source: dict[str, Any], config: dict[str, Any]
) -> dict[str, Any]:
    """Recover intentional empty node values omitted from the request summary.

    The executed graph remains authoritative; current refill/display policy is
    still applied by the normal draft resolver below. Unrecorded controls must
    not regain their values through this fallback.
    """
    result = dict(source)
    parameters = dict(source.get("parameters") or {})
    schema = config.get("parameters_schema") or {}
    graph = config.get("api_graph") or {}
    for key, binding in config.get("bindings", {}).items():
        kind = binding.get("source", "parameter")
        if kind not in {
            "parameter",
            "prompt",
            "negative_prompt",
            "width",
            "height",
            "seed",
        }:
            continue
        targets = binding.get("targets") or []
        values = [
            graph.get(target["node_id"], {}).get("inputs", {}).get(target["input_name"])
            for target in targets
        ]
        if (
            not values
            or has_request_value(values[0])
            or any(value != values[0] for value in values[1:])
        ):
            continue
        wire_key = kind if kind in {"prompt", "negative_prompt"} else key
        descriptor = next(
            (
                item
                for name, item in schema.items()
                if (item.get("request_key") or name) == wire_key
            ),
            {},
        )
        if not parameter_flag(descriptor, "record_in_history"):
            continue
        if kind in {"prompt", "negative_prompt"}:
            result.setdefault(wire_key, values[0])
        else:
            parameters.setdefault(wire_key, values[0])
    if parameters:
        result["parameters"] = parameters
    return result


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
    metadata_record = detail.get("source") in {"import", "external"}
    if metadata_record and image.get("supplemental"):
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
    if not metadata_record and isinstance(
        image.get("supplemental", {}).get("effective_request"), dict
    ):
        detail = {**detail, "parameters": image["supplemental"]["effective_request"]}
    request = request_snapshot(detail)
    if metadata_record:
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
            "metadata": metadata if metadata_record else {},
            "supplemental": {
                **detail.get("supplemental", {}),
                **(
                    {"comfyui": image["supplemental"]["comfyui"]}
                    if image.get("supplemental", {}).get("comfyui")
                    else {}
                ),
            },
            "has_request_snapshot": not metadata_record,
        }
    elif format_name == "nai":
        external_parameters = (detail.get("supplemental") or {}).get(
            "external_parameters"
        )
        if (
            detail.get("source") == "external"
            and external_parameters
            and detail.get("generation_engine") == "novelai"
        ):
            content = copy.deepcopy(external_parameters)
        elif not metadata_record and detail.get("provider_kind") == "nai_direct":
            content = {
                **request["parameters"],
                "tag": request["prompt"],
                "model": request["model"],
            }
            if "size" in request:
                content["size"] = request["size"]
            if "negative_prompt" in request:
                content["negative"] = request["negative_prompt"]
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
        snapshot = image.get("supplemental", {}).get("comfyui") or {}
        content = snapshot.get(
            "workflow" if format_name == "workflow" else "api_graph"
        ) or raw.get("workflow" if format_name == "workflow" else "prompt")
        if not content:
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
    content: str,
    settings: RuntimeSettings,
    model_ref: str = "",
    *,
    for_reproduction: bool = False,
) -> dict[str, Any]:
    """Resolve a copy format to a model draft, reporting every rejected value."""

    parsed = parse_parameter_text(content)
    normalized = parsed.get("normalized") or {}
    source_format = parsed.get("format", "unknown")
    source = copy.deepcopy(parsed.get("request") or {})
    # An Image Studio export carries exact request intent; imported exports retain provenance.
    envelope = parsed.get("envelope")
    exact_snapshot = (
        isinstance(envelope, dict) and envelope.get("has_request_snapshot") is True
    )
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
    metadata = (
        envelope.get("metadata", {})
        if isinstance(envelope, dict) and not exact_snapshot
        else parsed
    )
    wire = (
        _novelai_metadata_parameters(metadata)
        if source_format in {"novelai", "nai"} and not exact_snapshot
        else {}
    )
    reference_mode = (
        _novelai_reference_mode(
            {**wire, **(source.get("parameters") or {})},
            str(source.get("model") or normalized.get("model") or ""),
        )
        if source_format in {"novelai", "nai"}
        else ""
    )
    removed_images: list[str] = []
    if source_format in {"novelai", "nai"}:
        source = _without_image_payloads(source, removed_images)
        normalized = _without_image_payloads(normalized, removed_images)
        _without_image_payloads(wire, removed_images)
    mode_aliases = {
        "text2img": "text2img",
        "txt2img": "text2img",
        "t2i": "text2img",
        "generate": "text2img",
        "img2img": "img2img",
        "image2image": "img2img",
        "i2i": "img2img",
        "infill": "img2img",
        "inpaint": "img2img",
        "inpainting": "img2img",
    }
    explicit_mode = next(
        (
            mode_aliases[str(value).strip().lower()]
            for value in (
                source.get("mode"),
                normalized.get("mode"),
                source.get("action"),
                wire.get("action"),
            )
            if str(value).strip().lower() in mode_aliases
        ),
        "",
    )
    if reference_mode in {"precise", "vibe", "inpaint"}:
        explicit_mode = "img2img"
    mode = explicit_mode or "text2img"
    comfy_snapshot = (
        envelope.get("supplemental", {}).get("comfyui")
        if isinstance(envelope, dict)
        else None
    )
    if source_format == "comfyui" and comfy_snapshot:
        from ..providers.comfyui.workflows import migrate_fixed_outputs

        comfy_snapshot = migrate_fixed_outputs(
            comfy_snapshot, prefer_graph_values=True
        )["comfyui"]
        if exact_snapshot:
            source = _restore_empty_comfy_inputs(source, comfy_snapshot)
        mode = (
            "img2img"
            if any(
                item["source"] == "reference"
                for item in comfy_snapshot["bindings"].values()
            )
            else "text2img"
        )
    requested_model = str(source.get("model") or normalized.get("model") or "")
    requested_ref = str(source.get("model_ref") or "")
    if not requested_ref and source.get("provider_id") and requested_model:
        requested_ref = f"{source['provider_id']}:{requested_model}"
    if source_format in {"novelai", "nai"} and requested_model.endswith("-inpainting"):
        requested_model = requested_model.removesuffix("-inpainting")
        requested_ref = requested_ref.removesuffix("-inpainting")
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
                    if m["provider_kind"] in {"nai_direct", "novelai_official"}
                    and m["id"].startswith(prefix)
                ]
        if len(matches) == 1:
            selected = matches[0]
            selection_reason = "model"
    if not selected and not requested_model and not requested_ref:
        family = {
            "nai": {"nai_direct", "novelai_official"},
            "novelai": {"nai_direct", "novelai_official"},
            "openai_images": {"openai_images"},
            "gemini": {"gemini"},
        }.get(source_format, set())
        provider_id = str(source.get("provider_id") or "")
        matches = [
            item
            for item in candidates
            if family
            and item["provider_kind"] in family
            and (not provider_id or item["provider_id"] == provider_id)
        ]
        if len(matches) == 1:
            selected = matches[0]
            selection_reason = "source_unique"
    warnings = list(parsed.get("warnings") or [])
    if removed_images:
        warnings.append(
            "参数内嵌的参考图、蒙版或编码数据不会导入草稿，请重新选择原始图片；逐图设置按原始输入顺序保留。"
        )
    if source_format == "comfyui":
        warnings.append(
            "ComfyUI 仅自动识别证据明确的提示词；其余文本候选保留供人工采用，不会全部加入提示词。"
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
        sources = [
            source
            for field in ("prompt_sources", "negative_prompt_sources")
            for source in normalized.get(field, [])
        ]
        if any(source.get("kind") == "display_snapshot" for source in sources):
            warnings.append(
                "部分提示词由显示节点快照识别，来源已保留；快照未与本次执行独立校验。"
            )
        if any(source.get("kind") == "user_rule" for source in sources):
            warnings.append("部分提示词按用户声明的文本节点规则识别，来源已保留。")
    if not explicit_mode:
        warnings.append("元数据未确定生成模式，暂按文生图准备草稿，请核对。")
    if selection_reason == "source_unique":
        warnings.append("参数未指定型号，已选择当前来源唯一可用的模型，请核对。")
    if mode == "img2img":
        if reference_mode == "inpaint":
            warnings.append(
                "局部重绘需要重新选择原始底图和蒙版，顺序为第 1 张底图、第 2 张蒙版；生成结果图不能替代原始底图。"
            )
        elif reference_mode in {"precise", "vibe"}:
            label = "精准参考" if reference_mode == "precise" else "Vibe Transfer"
            warnings.append(
                f"{label} 需要原始参考图；请按原始编号顺序补充图片，以对应逐图强度等设置。"
            )
        else:
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
    if for_reproduction:
        draft["for_reproduction"] = True
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
    is_official = selected["provider_kind"] == "novelai_official"
    descriptors = selected.get("parameters") or {}
    values = {
        key: copy.deepcopy(desc["default"])
        for key, desc in descriptors.items()
        if "default" in desc
    }
    if not for_reproduction:
        _apply_preset_values(values, descriptors)
    supplied = copy.deepcopy(source.get("parameters") or {})
    novelai_unmapped: dict[str, Any] = {}
    if is_official and source_format in {"nai", "novelai"} and not exact_snapshot:
        projected, novelai_unmapped = _novelai_projection(wire, normalized, warnings)
        supplied = {**projected, **supplied}
        if reference_mode:
            supplied.setdefault("reference_mode", reference_mode)
    if is_official:
        supplied = _without_image_payloads(supplied, removed_images)
        capabilities = model_capabilities(selected["id"])
        selected_reference_mode = supplied.get("reference_mode")
        if selected_reference_mode and selected_reference_mode not in capabilities.get(
            "reference_modes", []
        ):
            novelai_unmapped["reference_mode"] = supplied.pop("reference_mode")
            if "reference_settings" in supplied:
                novelai_unmapped["reference_settings"] = supplied.pop(
                    "reference_settings"
                )
            warnings.append(
                f"所选模型 {selected['id']} 不支持 {selected_reference_mode} 参考模式，未回填该模式及逐图设置，不能直接复现。"
            )
        max_characters = capabilities.get(
            "inpainting_max_characters"
            if selected_reference_mode == "inpaint"
            else "max_characters",
            0,
        )
        if (
            isinstance(supplied.get("characters"), list)
            and len(supplied["characters"]) > max_characters
        ):
            novelai_unmapped["characters"] = supplied.pop("characters")
            warnings.append(
                f"角色数量超过所选模型在当前模式支持的 {max_characters} 个，未回填角色；请手动调整。"
            )
        for key, capability in (
            ("straight_alpha", "transparency"),
            ("tag_hint_transparent_background", "transparency"),
            ("variety_boost", "variety_boost"),
        ):
            if supplied.get(key) and not capabilities.get(capability):
                novelai_unmapped[key] = supplied.pop(key)
                warnings.append(
                    f"所选模型 {selected['id']} 不支持 {key}，已保留原值但未回填。"
                )
        reference_settings = supplied.get("reference_settings")
        if isinstance(reference_settings, list) and len(
            reference_settings
        ) > selected.get("max_reference_images", 1):
            novelai_unmapped["reference_settings"] = supplied.pop("reference_settings")
            warnings.append(
                f"原始参考图数量超过所选模型在插件中的 {selected.get('max_reference_images', 1)} 张上限，未截断或重排，请手动选择。"
            )
        elif isinstance(reference_settings, list) and any(
            isinstance(item, dict)
            and (
                (
                    item.get("type") in {"character", "style", "character_style"}
                    and not capabilities.get("precise_reference")
                )
                or (
                    item.get("type") == "vibe" and not capabilities.get("vibe_transfer")
                )
            )
            for item in reference_settings
        ):
            novelai_unmapped["reference_settings"] = supplied.pop("reference_settings")
            warnings.append(
                f"逐图设置包含所选模型 {selected['id']} 不支持的参考类型，未回填整组参考设置，不能直接复现。"
            )
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
        "scheduler": "noise_schedule" if is_nai or is_official else "scheduler",
        "guidance_scale": "scale" if is_nai or is_official else "cfg_scale",
        "cfg_rescale": "cfg" if is_nai else "cfg_rescale",
        "strength": "strength",
        "noise": "noise",
        "extra_noise_seed": "extra_noise_seed",
        "image_format": "image_format",
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
    if is_nai and source_format in {"nai", "novelai"} and not exact_snapshot:
        supplied.setdefault("artist", source.get("artist", ""))
        if "cfg_rescale" in supplied:
            supplied.setdefault("cfg", supplied.pop("cfg_rescale"))
    elif is_official and source_format in {"nai", "novelai"}:
        if "cfg" in supplied:
            supplied.setdefault("cfg_rescale", supplied.pop("cfg"))
        artist = supplied.pop("artist", "")
        if artist and isinstance(artist, str):
            draft["prompt"] = "\n".join([artist, draft.get("prompt", "")]).strip()
    elif not is_nai and source_format in {"nai", "novelai"}:
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
    if is_official and isinstance(supplied.get("size"), str):
        supplied["size"] = {
            "竖图": "832x1216",
            "横图": "1216x832",
            "方图": "1024x1024",
            "2K竖图": "1088x1600",
            "2K横图": "1600x1088",
            "2K方图": "1344x1344",
            "4K竖图": "1344x1984",
            "4K横图": "1984x1344",
            "4K方图": "1728x1728",
        }.get(supplied["size"], supplied["size"])
    if "negative_prompt" in source:
        supplied["negative_prompt"] = source["negative_prompt"]
    elif not exact_snapshot and draft["negative_prompt"]:
        supplied["negative_prompt"] = draft["negative_prompt"]
    if is_official and supplied.get("reference_mode") == "inpaint":
        # Official infill keeps its optional img2img influence nested; older
        # Image Studio snapshots used ordinary strength for this same control.
        # An explicit zero is meaningful, and the new control takes precedence
        # when a snapshot already contains it.
        nested_img2img = supplied.pop("img2img", None)
        if isinstance(nested_img2img, dict):
            if "strength" in nested_img2img:
                supplied.setdefault("inpaint_strength", nested_img2img.pop("strength"))
            if "color_correct" in nested_img2img:
                supplied.setdefault(
                    "color_correct", nested_img2img.pop("color_correct")
                )
        if nested_img2img is not None and nested_img2img != {}:
            novelai_unmapped["img2img"] = nested_img2img
            warnings.append(
                "局部重绘 img2img 中有无法回填的参数，已保留原值，请检查未映射参数。"
            )
        if "strength" in supplied:
            supplied.setdefault("inpaint_strength", supplied.pop("strength"))
    unmapped: dict[str, Any] = novelai_unmapped
    accepted: dict[str, Any] = {}
    for key, value in supplied.items():
        if key in MODEL_SCHEDULING_KEYS:
            continue
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
            if key == "negative_prompt":
                continue
            unmapped[key] = value
            continue
        descriptor = descriptors[name]
        if not parameter_flag(descriptor, "webui_visible") or (
            for_reproduction and not parameter_flag(descriptor, "refill_from_history")
        ):
            continue
        choices = descriptor.get("choices")
        valid = not isinstance(choices, list) or any(
            str(item.get("value") if isinstance(item, dict) else item) == str(value)
            for item in choices
        )
        kind = str(descriptor.get("type") or "text")
        if value is None and (
            descriptor.get("nullable") is True
            or ("default" in descriptor and descriptor["default"] is None)
        ):
            pass
        elif kind in {"number", "int", "integer", "float"}:
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
        accepted[name] = value
    if not for_reproduction:
        _apply_preset_values(accepted, descriptors, protected=set(accepted))
    values.update(accepted)
    for name, descriptor in descriptors.items():
        if not parameter_flag(descriptor, "webui_visible"):
            if "default" in descriptor:
                values[name] = copy.deepcopy(descriptor["default"])
            else:
                values.pop(name, None)
    _derive_presets(values, descriptors)
    negative_name = next(
        (
            name
            for name, descriptor in descriptors.items()
            if name == "negative_prompt"
            or descriptor.get("request_key") == "negative_prompt"
        ),
        None,
    )
    if negative_name:
        if negative_name in values:
            draft["negative_prompt"] = values[negative_name]
        else:
            draft.pop("negative_prompt", None)
    elif exact_snapshot and "negative_prompt" not in source:
        draft["negative_prompt"] = selected.get("negative_prompt_default", "")
    if not selected.get("supports_negative_prompt") and draft.get("negative_prompt"):
        unmapped["negative_prompt"] = draft["negative_prompt"]
        draft["negative_prompt"] = ""
    elif (
        not negative_name
        and "negative_prompt" not in source
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
        "prompt_sources",
        "negative_prompt_sources",
        "selected_output_node",
        "requires_output_selection",
        "characters",
        "character_prompts",
    ):
        if normalized.get(key) and not (
            is_official and key == "characters" and "characters" in supplied
        ):
            unmapped.setdefault(key, normalized[key])
    if (
        source_format in {"comfyui", "a1111"}
        and selected.get("provider_kind") != "comfyui"
    ):
        warnings.append(
            "当前插件不执行 ComfyUI 或 Stable Diffusion 工作流；这里只填写目标模型可接受的字段。"
        )
    if is_nai and source_format not in {"nai", "novelai", "image_studio"}:
        warnings.append("目标为 NAI 标签模型，请核对提示词语法和参数含义。")
    draft["parameters"] = values
    if comfy_snapshot and selected.get("provider_kind") == "comfyui":
        draft["comfyui"] = comfy_snapshot
    for name, value in values.items():
        key = descriptors.get(name, {}).get("request_key", name)
        if key in {"size", "count", "n"}:
            draft["count" if key == "n" else key] = value
    if for_reproduction:
        draft["native_batch_size"] = selected["native_batch_size"]
        draft["max_concurrent_requests"] = selected["max_concurrent_requests"]
    if unmapped:
        warnings.append("部分参数无法映射，原值保留在未填写参数中。")
    return {
        "draft": draft,
        "candidates": candidates,
        "requires_model_selection": False,
        "selection_reason": selection_reason,
        "warnings": list(dict.fromkeys(warnings)),
        "unmapped": _without_image_payloads(unmapped, removed_images)
        if is_official or source_format in {"novelai", "nai"}
        else unmapped,
    }


def _apply_preset_values(
    values: dict[str, Any],
    descriptors: dict[str, Any],
    *,
    protected: set[str] | None = None,
) -> None:
    for name, descriptor in descriptors.items():
        if descriptor.get("type") != "preset" or name not in values:
            continue
        target = descriptor.get("target")
        if not target or target in (protected or ()):
            continue
        if not parameter_flag(descriptors.get(target, {}), "webui_visible"):
            continue
        choice = next(
            (
                item
                for item in descriptor.get("choices", [])
                if isinstance(item, dict) and item.get("value") == values[name]
            ),
            None,
        )
        if choice and isinstance(choice.get("fill"), str):
            values[target] = choice["fill"]


def _derive_presets(values: dict[str, Any], descriptors: dict[str, Any]) -> None:
    for name, descriptor in descriptors.items():
        if descriptor.get("type") != "preset" or not parameter_flag(
            descriptor, "webui_visible"
        ):
            continue
        target = descriptor.get("target")
        if target not in values:
            continue
        choices = [
            item for item in descriptor.get("choices", []) if isinstance(item, dict)
        ]
        match = next(
            (
                item
                for item in choices
                if "fill" in item and item["fill"] == values[target]
            ),
            None,
        )
        custom = next((item for item in choices if item.get("value") == "custom"), None)
        if match or custom:
            values[name] = (match or custom)["value"]
