"""Build public capability payloads without touching AstrBot event state."""

from typing import Any

from ..config import RuntimeSettings
from ..providers.comfyui.workflows import FIXED_OUTPUT_POLICY
from .capability_catalog import (
    MODEL_SELECTION_GUIDANCE,
    search_catalog,
    select_capability_models,
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
                    "仅在 parameters 返回该字段时使用。"
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
                    "此工作流没有主提示词入口，可省略 prompt；仅通过已开放 parameters 调整工作流输入。"
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
            MODEL_SELECTION_GUIDANCE
            + "满足要求即可生成；有能力缺口时使用相同 mode 的 search，再用 model_refs 查询完整参数。"
        )
    elif normalized_query_type == "all":
        next_action = MODEL_SELECTION_GUIDANCE + "已返回完整能力，无需再次 model 查询。"
    elif not entries:
        next_action = "没有成功查询的模型；根据 errors 修正 model_refs，或使用 search 查找可用模型后重新查询。"
    elif capability_errors:
        next_action = (
            MODEL_SELECTION_GUIDANCE
            + "models 成功项可直接生成；errors 项修正后重新查询。"
        )
    else:
        next_action = (
            MODEL_SELECTION_GUIDANCE + "按 prompt_contract 和 parameters 直接生成。"
        )
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
                "input_mapping": "重复模型共用一份契约；input_indices 与 requested_refs 按顺序对应各次成功输入，input_index/requested_ref 为首次输入。"
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
