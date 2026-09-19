"""Build public capability payloads without touching AstrBot event state."""

from typing import Any

from ..config import RuntimeSettings
from ..providers.comfyui.workflows import FIXED_OUTPUT_POLICY
from .capability_catalog import search_catalog, select_capability_models

_CAPABILITY_REUSE_GUIDANCE = (
    "在处理当前这条用户消息期间，使用本次查询成功的 model_ref 和该项 query_modes 中的模式时，"
    "可按照本次返回的参数说明重复调用 image_studio_generate，无需重复查询。"
    "修改 prompt 或 parameters 中的生成参数值不需要重新查询。"
    "处理新的用户消息、使用尚未查询的模型或模式，或插件设置发生变化后，需要重新查询；"
    "工具提示重新查询时请按提示操作。"
)


def query_capabilities(
    settings: RuntimeSettings,
    *,
    query_type: str = "default",
    mode: str = "",
    model_refs: list[str] | None = None,
    query: str = "",
    provider_id: str = "",
    provider_kind: str = "",
    limit: int = 10,
    offset: int = 0,
) -> dict[str, Any]:
    """Return discovery summaries or full contracts; malformed requests raise ValueError."""
    if not settings.enable_llm_tool:
        raise ValueError("Image Studio 的 LLM 生图工具已关闭。")
    if not isinstance(mode, str):
        raise ValueError("mode 仅支持 text2img 或 img2img。")
    normalized_mode = mode.strip().lower()
    if normalized_mode not in {"", "text2img", "img2img"}:
        raise ValueError("mode 仅支持 text2img 或 img2img。")
    if not isinstance(query_type, str):
        raise ValueError("query_type 仅支持 all、default、model、providers 或 search。")
    normalized_query_type = query_type.strip().lower() or "default"
    if normalized_query_type not in {
        "all",
        "default",
        "model",
        "providers",
        "search",
    }:
        raise ValueError("query_type 仅支持 all、default、model、providers 或 search。")
    if model_refs is not None:
        if normalized_query_type == "default":
            normalized_query_type = "model"
        elif normalized_query_type != "model":
            raise ValueError("model_refs 仅用于 query_type=model 查询。")
    if normalized_query_type in {"providers", "search"}:
        return search_catalog(
            settings,
            query_type=normalized_query_type,
            mode=normalized_mode,
            query=query,
            provider_id=provider_id,
            provider_kind=provider_kind,
            limit=limit,
            offset=offset,
        )
    if query != "" or limit != 10 or offset != 0:
        raise ValueError("query、limit 和 offset 仅用于 search/providers 查询。")
    selected, capability_errors = select_capability_models(
        settings,
        query_type=normalized_query_type,
        mode=normalized_mode,
        model_refs=model_refs,
        provider_id=provider_id,
        provider_kind=provider_kind,
    )

    entries: list[dict[str, Any]] = []
    entries_by_ref: dict[str, dict[str, Any]] = {}
    for (
        provider,
        model,
        query_modes,
        default_for_modes,
        input_index,
        requested_ref,
    ) in selected:
        ref = f"{provider.id}:{model.id}"
        if normalized_query_type == "model" and ref in entries_by_ref:
            entries_by_ref[ref]["input_indices"].append(input_index)
            entries_by_ref[ref]["requested_refs"].append(requested_ref)
            continue
        modes = [
            candidate
            for candidate in ("text2img", "img2img")
            if model.supports(candidate)
        ]
        tool = model.tool
        exposed_parameters: dict[str, Any] = {}
        configured_parameters = tool.get("parameters")
        exposed_parameter_names = model.llm_exposed_parameter_names
        for name, descriptor in model.parameters.items():
            # This dedicated field uses tool policy, not model schema.
            if name == "negative_prompt":
                continue
            if name not in exposed_parameter_names:
                continue
            parameter_modes = descriptor.get("modes")
            if isinstance(parameter_modes, list) and not any(
                candidate in parameter_modes for candidate in query_modes
            ):
                continue
            request_key = str(descriptor.get("request_key") or name)
            if (
                provider.kind == "comfyui"
                and request_key in {"count", "n", "size"}
                and (
                    request_key == "size"
                    or model.comfyui.get("execution_policy") != FIXED_OUTPUT_POLICY
                )
            ):
                source = (
                    "count"
                    if str(descriptor.get("request_key") or name) in {"count", "n"}
                    else "width"
                )
                if not any(
                    item["source"] == source
                    for item in model.comfyui.get("bindings", {}).values()
                ):
                    continue
            policy = (
                configured_parameters.get(name)
                if isinstance(configured_parameters, dict)
                else None
            )
            policy = policy if isinstance(policy, dict) else {}
            visible = _llm_parameter_descriptor(descriptor, policy)
            missing = object()
            default = model.parameter_default(name, source="llm_tool", fallback=missing)
            if default is not missing:
                visible["default"] = default
            exposed_parameters[name] = visible
        if model.llm_negative_prompt_enabled:
            exposed_parameters["negative_prompt"] = _llm_parameter_descriptor(
                {
                    "type": "string",
                    "description": (
                        "专用反向提示词，只填写不希望出现在画面中的内容；"
                        "省略时使用此处的默认值。"
                    ),
                    "default": model.llm_negative_prompt_default,
                },
                model.llm_negative_prompt_policy,
            )
        prompt_profile = tool.get("prompt_profile", "natural_language")
        prompt_instructions = tool.get("prompt_instructions", "")
        entry = {
            "model_ref": ref,
            "provider_name": provider.name,
            "model_name": model.name,
            "modes": modes,
            "query_modes": query_modes,
            "default_for_modes": default_for_modes,
            "max_reference_images": model.llm_max_reference_images,
            "selection_description": tool.get("selection_description", ""),
            "prompt_contract": {
                "format": prompt_profile,
                "instruction": prompt_instructions,
                "negative_prompt": (
                    "仅在本次返回的 parameters 中列出 negative_prompt 时，才可填写 parameters.negative_prompt。"
                    if model.llm_negative_prompt_enabled
                    else "不要传入 negative_prompt。"
                ),
            },
            "parameters": exposed_parameters,
            **(
                {"novelai_capabilities": model.novelai_capabilities}
                if model.novelai_capabilities
                else {}
            ),
        }
        if provider.kind == "comfyui":
            entry["comfyui_capabilities"] = model.comfyui_capabilities
            entry["reference_slots"] = model.comfyui_reference_slots
            entry["prompt_contract"]["required"] = model.comfyui_capabilities[
                "prompt_required"
            ]
            if (
                not model.comfyui_capabilities["prompt_required"]
                and not prompt_instructions
            ):
                entry["prompt_contract"]["instruction"] = (
                    "此工作流未绑定 image_studio_generate 的 prompt 参数，可以省略该参数。"
                    "可修改的工作流参数见本次返回的 parameters。"
                )
        if normalized_query_type == "model":
            entry.update(
                input_index=input_index,
                requested_ref=requested_ref,
                input_indices=[input_index],
                requested_refs=[requested_ref],
            )
        entries.append(entry)
        entries_by_ref[ref] = entry
    if normalized_query_type == "default":
        next_action = (
            "已返回默认模型在 Image Studio 中允许使用的生成模式、参数和提示词要求。"
            "选择 default_for_modes 包含所需模式的模型，使用该项 model_ref 调用 image_studio_generate。"
        )
    elif normalized_query_type == "all":
        next_action = (
            "已返回各模型在 Image Studio 中允许使用的生成模式、参数和提示词要求。"
            "请从 models 中选择所需模型，按照该项 prompt_contract 编写提示词，并使用 parameters 中列出的参数。"
        )
    elif not entries:
        next_action = (
            "没有成功查询的模型；根据 errors 中的原因修正 model_refs，"
            '或设置 query_type="search" 查找可用模型。'
        )
    elif capability_errors:
        next_action = (
            "models 中的模型已成功查询，可按照各项 prompt_contract 和 parameters 调用 image_studio_generate。"
            "未成功查询的输入及原因见 errors；使用这些模型前需修正 model_refs 后重新查询。"
        )
    else:
        next_action = (
            "已返回所选模型在 Image Studio 中允许使用的生成模式、参数和提示词要求。"
            "按照所选模型的 prompt_contract 编写提示词，并仅使用 parameters 中列出的参数。"
        )
    if entries:
        next_action += _CAPABILITY_REUSE_GUIDANCE
    return {
        "query_type": normalized_query_type,
        "next_action": next_action,
        "asset_policy": {
            "return_mode": settings.llm_image_return_mode,
        },
        "default_model_refs": {
            "text2img": settings.default_model_ref("text2img", "llm_tool"),
            "img2img": settings.default_model_ref("img2img", "llm_tool"),
        },
        "models": entries,
        **({"errors": capability_errors} if normalized_query_type == "model" else {}),
        **(
            {
                "input_mapping": "同一模型只在 models 中返回一次。input_indices 与 requested_refs 一一对应，分别表示原始 model_refs 中的位置（从 0 开始）和输入字符串；input_index 和 requested_ref 对应首次成功输入。"
            }
            if normalized_query_type == "model"
            and any(len(entry["input_indices"]) > 1 for entry in entries)
            else {}
        ),
    }


def _llm_parameter_descriptor(
    descriptor: dict[str, Any], policy: dict[str, Any]
) -> dict[str, Any]:
    visible = {
        key: descriptor[key]
        for key in ("type", "default", "min", "max", "step", "modes", "nullable")
        if key in descriptor
    }
    description = str(
        policy.get("description")
        or descriptor.get("description")
        or descriptor.get("label")
        or ""
    )
    # Settings show numeric ranges in tooltips; the LLM already receives min/max.
    # Remove only the identical generated suffix from this projection.
    lower, upper = descriptor.get("min"), descriptor.get("max")
    duplicate_range = (
        f"取值范围：[{lower}, {upper}]。"
        if lower is not None and upper is not None
        else f"取值范围：不小于 {lower}。"
        if lower is not None
        else f"取值范围：不大于 {upper}。"
        if upper is not None
        else ""
    )
    if duplicate_range and description.endswith(duplicate_range):
        description = description[: -len(duplicate_range)].rstrip()
    if description:
        visible["description"] = description
    choices = descriptor.get("choices")
    choice_descriptions = policy.get("choice_descriptions")
    if isinstance(choices, list):
        visible_choices: list[dict[str, Any]] = []
        for choice in choices:
            value = choice.get("value") if isinstance(choice, dict) else choice
            label = choice.get("label", value) if isinstance(choice, dict) else value
            choice_description = (
                choice_descriptions.get(str(value), "")
                if isinstance(choice_descriptions, dict)
                else ""
            )
            visible_choice = {"value": value}
            if label != value:
                visible_choice["label"] = label
            if choice_description:
                visible_choice["description"] = choice_description
            visible_choices.append(visible_choice)
        visible["choices"] = visible_choices
    return visible
