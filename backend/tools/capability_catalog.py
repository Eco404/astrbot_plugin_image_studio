"""Configuration-only discovery and selection for the LLM capability tool.

Search results deliberately contain no executable parameter contracts. Reading
the catalog must not grant permission to skip a full capability query.
"""

from __future__ import annotations

import unicodedata
from typing import Any

from ..config import SUPPORTED_PROVIDER_KINDS, RuntimeSettings
from ..models import ImageModel, ImageProvider

MODES = ("text2img", "img2img")
MODEL_REFS_LIMIT = 20
CapabilitySelection = tuple[
    ImageProvider, ImageModel, list[str], list[str], int | None, str
]


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} 必须是字符串。")
    return value.strip()


def _mode(value: Any) -> str:
    value = _text(value, "mode").lower()
    if value not in {"", *MODES}:
        raise ValueError("mode 仅支持 text2img 或 img2img。")
    return value


def _modes(model: ImageModel) -> list[str]:
    return [mode for mode in MODES if model.supports(mode)]


def _default_modes_by_ref(
    settings: RuntimeSettings, mode: str = "", *, strict: bool = False
) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for target_mode in (mode,) if mode else MODES:
        requested = settings.default_model_ref(target_mode, "llm_tool")
        if not requested:
            continue
        resolved, error = _resolve_model(settings, requested, target_mode)
        if error:
            if strict and error["code"] == "ambiguous_model_id":
                raise ValueError(
                    f"默认模型 {requested} 的 ID 不唯一，"
                    "请在默认模型设置中使用 provider_id:model_id。"
                )
            continue
        provider, model = resolved
        result.setdefault(f"{provider.id}:{model.id}", []).append(target_mode)
    return result


def _fold(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _match(
    query: str,
    identifiers: tuple[tuple[str, str], ...],
    name: str,
    description: str = "",
) -> tuple[int, list[str]] | None:
    if not query:
        return (0, [])
    identifier_matches = [(field, _fold(value)) for field, value in identifiers]
    exact_ids = [field for field, value in identifier_matches if query == value]
    if exact_ids:
        return (0, exact_ids)
    folded_name = _fold(name)
    if query == folded_name:
        return (1, ["name"])
    contained = [field for field, value in identifier_matches if query in value]
    if query in folded_name:
        contained.append("name")
    if contained:
        return (2, contained)
    if description and query in _fold(description):
        return (3, ["selection_description"])
    return None


def search_catalog(
    settings: RuntimeSettings,
    *,
    query_type: str,
    mode: str = "",
    query: str = "",
    provider_id: str = "",
    provider_kind: str = "",
    limit: int = 10,
    offset: int = 0,
) -> dict[str, Any]:
    """Return paginated public summaries, without network or state changes."""

    query_type = _text(query_type, "query_type").lower()
    if query_type not in {"providers", "search"}:
        raise ValueError("目录查询 query_type 仅支持 providers 或 search。")
    mode = _mode(mode)
    query = _text(query, "query")
    provider_id = _text(provider_id, "provider_id")
    provider_kind = _text(provider_kind, "provider_kind").lower()
    if provider_kind and provider_kind not in SUPPORTED_PROVIDER_KINDS:
        raise ValueError(f"不支持的 provider_kind：{provider_kind}。")
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("limit 必须是 1 至 50 之间的整数。")
    if type(offset) is not int or offset < 0:
        raise ValueError("offset 必须是大于或等于 0 的整数。")
    if provider_id:
        requested_provider = next(
            (item for item in settings.providers if item.id == provider_id), None
        )
        if requested_provider is None:
            raise ValueError(f"服务商不存在：{provider_id}。")
        if not requested_provider.enabled:
            raise ValueError(f"服务商已停用：{provider_id}。")

    ranked: list[tuple[int, dict[str, Any]]] = []
    default_modes = _default_modes_by_ref(settings, mode)
    folded_query = _fold(query)
    for provider in settings.providers:
        if (
            not provider.enabled
            or (provider_id and provider.id != provider_id)
            or (provider_kind and provider.kind != provider_kind)
        ):
            continue
        visible_models = [
            model
            for model in provider.models
            if model.llm_enabled
            and _modes(model)
            and (not mode or model.supports(mode))
        ]
        if query_type == "providers":
            match = _match(folded_query, (("provider_id", provider.id),), provider.name)
            if match is not None:
                ranked.append(
                    (
                        match[0],
                        {
                            "provider_id": provider.id,
                            "provider_name": provider.name,
                            "provider_kind": provider.kind,
                            "model_count": len(visible_models),
                            "matched_fields": [
                                "provider_name" if field == "name" else field
                                for field in match[1]
                            ],
                        },
                    )
                )
            continue
        for model in visible_models:
            ref = f"{provider.id}:{model.id}"
            description = model.tool.get("selection_description", "")
            match = _match(
                folded_query,
                (("model_ref", ref), ("model_id", model.id)),
                model.name,
                description,
            )
            if match is None:
                continue
            ranked.append(
                (
                    match[0],
                    {
                        "model_ref": ref,
                        "model_id": model.id,
                        "model_name": model.name,
                        "provider_id": provider.id,
                        "provider_name": provider.name,
                        "provider_kind": provider.kind,
                        "modes": _modes(model),
                        "max_reference_images": model.llm_max_reference_images,
                        "default_for_modes": default_modes.get(ref, []),
                        "selection_description": description,
                        "matched_fields": [
                            "model_name" if field == "name" else field
                            for field in match[1]
                        ],
                    },
                )
            )
    # Python's stable sort retains persisted provider/model ordering within rank.
    ranked.sort(key=lambda item: item[0])
    page = ranked[offset : offset + limit]
    if not ranked:
        next_action = "没有匹配项，请调整 query 关键词或筛选条件后重试。"
    elif not page:
        next_action = "当前页没有结果，请根据 total 调整 offset 后重试。"
    elif query_type == "providers":
        next_action = '设置 query_type="search"，将所选服务商的 ID 填入 provider_id，查询其模型或工作流。'
    else:
        next_action = '设置 query_type="model"，将所选模型的 model_ref 放入 model_refs 数组，查询它在 Image Studio 中可用的生成模式、参数和提示词要求。'
    return {
        "query_type": query_type,
        "query": query,
        "mode": mode,
        "provider_id": provider_id,
        "provider_kind": provider_kind,
        "total": len(ranked),
        "limit": limit,
        "offset": offset,
        "has_more": offset + limit < len(ranked),
        "providers" if query_type == "providers" else "models": [
            item[1] for item in page
        ],
        "next_action": next_action,
    }


def _resolve_model(
    settings: RuntimeSettings, requested_ref: str, mode: str
) -> tuple[tuple[ImageProvider, ImageModel] | None, dict[str, Any] | None]:
    def error(code: str, message: str, **details: Any):
        return None, {"code": code, "message": message, **details}

    if ":" in requested_ref:
        provider_id, model_id = requested_ref.split(":", 1)
        if not provider_id or not model_id:
            return error(
                "invalid_model_ref", "模型引用不完整，请使用 provider_id:model_id。"
            )
        provider = next(
            (item for item in settings.providers if item.id == provider_id), None
        )
        if provider is None:
            return error("provider_not_found", f"服务商不存在：{provider_id}。")
        if not provider.enabled:
            return error("provider_disabled", f"服务商已停用：{provider_id}。")
        candidates = [
            (provider, item) for item in provider.models if item.id == model_id
        ]
    else:
        candidates = [
            (provider, model)
            for provider in settings.providers
            for model in provider.models
            if model.id == requested_ref
        ]
    if not candidates:
        return error("model_not_found", f"模型或工作流不存在：{requested_ref}。")
    enabled = [item for item in candidates if item[0].enabled]
    if not enabled:
        return error("provider_disabled", "该模型或工作流所属的服务商已停用。")
    visible = [item for item in enabled if item[1].llm_enabled]
    if not visible:
        return error("model_not_exposed", "该模型或工作流未向 LLM 工具开放。")
    supported = [
        item
        for item in visible
        if _modes(item[1]) and (not mode or item[1].supports(mode))
    ]
    if not supported:
        return error(
            "unsupported_mode",
            f"该模型或工作流不支持 {mode} 模式。"
            if mode
            else "该模型或工作流没有支持的生图模式。",
        )
    if len(supported) > 1:
        return error(
            "ambiguous_model_id",
            "模型 ID 不唯一，请使用 provider_id:model_id。",
            candidates=[f"{provider.id}:{model.id}" for provider, model in supported],
        )
    return supported[0], None


def select_capability_models(
    settings: RuntimeSettings,
    *,
    query_type: str,
    mode: str = "",
    model_refs: list[str] | None = None,
    provider_id: str = "",
    provider_kind: str = "",
) -> tuple[list[CapabilitySelection], list[dict[str, Any]]]:
    """Select full contracts, retaining per-input errors for batched lookups.

    Each selected tuple contains provider, model, query modes, default modes,
    zero-based input index (only for model queries), and the requested reference.
    Duplicate inputs remain independent and in order.
    """

    query_type = _text(query_type, "query_type").lower()
    mode = _mode(mode)
    if query_type not in {"default", "all", "model"}:
        raise ValueError(
            '查询模型的生成模式、参数和提示词要求时，query_type 仅支持 "default"、"all" 或 "model"。'
        )
    if _text(provider_id, "provider_id") or _text(provider_kind, "provider_kind"):
        raise ValueError(
            "provider_id 和 provider_kind 筛选仅用于 providers 或 search 查询。"
        )
    requested_refs: list[str] = []
    if model_refs is not None:
        if (
            not isinstance(model_refs, list)
            or not model_refs
            or not all(isinstance(item, str) and item.strip() for item in model_refs)
        ):
            raise ValueError("model_refs 必须是至少包含一个非空字符串的数组。")
        if len(model_refs) > MODEL_REFS_LIMIT:
            raise ValueError(
                f"model_refs 一次最多查询 {MODEL_REFS_LIMIT} 项，请分批查询。"
            )
        requested_refs = [item.strip() for item in model_refs]
    selected: list[CapabilitySelection] = []
    errors: list[dict[str, Any]] = []
    default_modes_by_ref = _default_modes_by_ref(
        settings, mode, strict=query_type == "default"
    )
    if query_type == "model":
        if not requested_refs:
            raise ValueError("查询指定模型时必须传入 model_refs。")
        for index, requested_ref in enumerate(requested_refs):
            resolved, error = _resolve_model(settings, requested_ref, mode)
            if error is not None:
                errors.append(
                    {"input_index": index, "model_ref": requested_ref, **error}
                )
                continue
            provider, model = resolved
            selected.append(
                (
                    provider,
                    model,
                    [mode] if mode else _modes(model),
                    default_modes_by_ref.get(f"{provider.id}:{model.id}", []),
                    index,
                    requested_ref,
                )
            )
        return selected, errors
    if requested_refs:
        raise ValueError("model_refs 仅用于 query_type=model 查询。")

    if query_type == "default":
        if not any(
            settings.default_model_ref(target_mode, "llm_tool")
            for target_mode in ((mode,) if mode else MODES)
        ):
            raise ValueError(
                "当前模式尚未设置默认 LLM 生图模型。"
                if mode
                else "尚未设置可用的文生图或图生图默认 LLM 模型。"
            )
    for provider in settings.providers:
        if not provider.enabled:
            continue
        for model in provider.models:
            modes = _modes(model)
            if not model.llm_enabled or not modes or (mode and mode not in modes):
                continue
            ref = f"{provider.id}:{model.id}"
            default_modes = default_modes_by_ref.get(ref, [])
            if query_type == "default" and not default_modes:
                continue
            selected.append(
                (
                    provider,
                    model,
                    default_modes
                    if query_type == "default"
                    else [mode]
                    if mode
                    else modes,
                    default_modes,
                    None,
                    ref,
                )
            )
    if not selected:
        raise ValueError(
            "没有符合条件的模型；模型可能不存在、已停用、不支持该模式或未向 LLM 工具开放。"
        )
    return selected, errors
