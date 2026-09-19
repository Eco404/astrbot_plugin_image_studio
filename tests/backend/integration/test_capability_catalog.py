from __future__ import annotations

import json
from dataclasses import replace

import pytest
from astrbot_plugin_image_studio.backend.tools.capability_catalog import (
    search_catalog,
    select_capability_models,
)
from astrbot_plugin_image_studio.backend.config import HistorySettings, RuntimeSettings
from astrbot_plugin_image_studio.backend.models import ImageModel, ImageProvider


def model(
    identifier: str,
    name: str = "",
    *,
    description: str = "",
    visible: bool = True,
    text: bool = True,
    edit: bool = False,
) -> ImageModel:
    return ImageModel(
        id=identifier,
        name=name or identifier,
        text2img=text,
        img2img=edit,
        negative_prompt=False,
        max_reference_images=1 if edit else 0,
        tool={"enabled": visible, "selection_description": description},
        parameters={"private_input": {"default": "must-not-leak"}},
        comfyui={"prompt": {"1": {"inputs": {"secret": "must-not-leak"}}}},
    )


def provider(identifier: str, *models: ImageModel, **overrides) -> ImageProvider:
    base = ImageProvider.from_mapping(
        {
            "id": identifier,
            "name": identifier,
            "kind": "openai_images",
            "api_key": "must-not-leak",
            "base_url": "https://must-not-leak.example.test",
            "custom_headers": '{"Authorization":"must-not-leak"}',
        }
    )
    return replace(base, models=tuple(models), **overrides)


def settings(*providers: ImageProvider, **overrides) -> RuntimeSettings:
    return RuntimeSettings(
        enable_llm_tool=True,
        providers=tuple(providers),
        history=HistorySettings(False, 0, 0, False),
        revision=1,
        **overrides,
    )


def refs(selected) -> list[str]:
    return [f"{item[0].id}:{item[1].id}" for item in selected]


def test_search_ranks_ids_names_and_descriptions_with_stable_ties() -> None:
    config = settings(
        provider(
            "main",
            model("last", description="可使用 ANIMA 进行随机抽卡"),
            model("contains-first", name="Anima 第一组"),
            model("contains-second", name="Anima 第二组"),
            model("exact-name", name="Ａｎｉｍａ"),
            model("Anima", name="原始名字"),
        )
    )
    result = search_catalog(config, query_type="search", query="anima")
    assert [item["model_id"] for item in result["models"]] == [
        "Anima",
        "exact-name",
        "contains-first",
        "contains-second",
        "last",
    ]
    assert [item["matched_fields"] for item in result["models"]] == [
        ["model_id"],
        ["model_name"],
        ["model_name"],
        ["model_name"],
        ["selection_description"],
    ]
    assert result["total"] == 5
    assert not result["has_more"]
    assert "must-not-leak" not in json.dumps(result)
    assert "model_refs" in result["next_action"]


def test_search_filters_visibility_and_modes_without_mutating_config() -> None:
    config = settings(
        provider("disabled", model("hidden-by-provider"), enabled=False),
        provider(
            "comfy",
            model("first", edit=True),
            model("hidden", visible=False, edit=True),
            model("text-only"),
            model("no-mode", text=False),
            kind="comfyui",
        ),
        provider("other", model("edit", edit=True)),
    )
    result = search_catalog(
        config, query_type="search", provider_kind="comfyui", mode="img2img"
    )
    assert [item["model_ref"] for item in result["models"]] == ["comfy:first"]
    assert result["models"][0]["modes"] == ["text2img", "img2img"]
    assert result["models"][0]["max_reference_images"] == 1
    assert config.providers[1].models[0].parameters["private_input"]["default"] == (
        "must-not-leak"
    )
    assert (
        search_catalog(
            config, query_type="search", provider_id="comfy", provider_kind="gemini"
        )["total"]
        == 0
    )


def test_providers_include_empty_and_count_only_callable_models_for_mode() -> None:
    config = settings(
        provider("empty", kind="comfyui"),
        provider(
            "main",
            model("one", edit=True),
            model("two"),
            model("hidden", visible=False, edit=True),
            model("none", text=False),
        ),
        provider("off", model("one"), enabled=False),
    )
    result = search_catalog(config, query_type="providers", mode="img2img")
    assert [
        (item["provider_id"], item["model_count"]) for item in result["providers"]
    ] == [
        ("empty", 0),
        ("main", 1),
    ]
    assert "must-not-leak" not in json.dumps(result)
    assert search_catalog(config, query_type="providers", query="main")["total"] == 1


def test_pagination_uses_filtered_total_and_stable_boundaries() -> None:
    config = settings(provider("main", *(model(str(index)) for index in range(5))))
    first = search_catalog(config, query_type="search", limit=2)
    second = search_catalog(config, query_type="search", limit=2, offset=2)
    last = search_catalog(config, query_type="search", limit=2, offset=4)
    empty = search_catalog(config, query_type="search", limit=2, offset=6)
    assert [item["model_id"] for item in first["models"]] == ["0", "1"]
    assert [item["model_id"] for item in second["models"]] == ["2", "3"]
    assert [item["model_id"] for item in last["models"]] == ["4"]
    assert empty["models"] == []
    assert all(item["total"] == 5 for item in (first, second, last, empty))
    assert [item["has_more"] for item in (first, second, last, empty)] == [
        True,
        True,
        False,
        False,
    ]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"limit": True},
        {"limit": "10"},
        {"limit": 0},
        {"limit": 51},
        {"limit": 2.0},
        {"offset": False},
        {"offset": -1},
        {"offset": "0"},
        {"query": []},
        {"query": None},
        {"mode": "bad"},
        {"provider_id": []},
        {"provider_kind": "unknown"},
        {"provider_kind": None},
        {"provider_id": "missing"},
        {"provider_id": "off"},
    ],
)
def test_search_rejects_malformed_filters_and_unknown_providers(kwargs) -> None:
    config = settings(provider("off", enabled=False))
    with pytest.raises(ValueError):
        search_catalog(config, query_type="search", **kwargs)


def test_batch_preserves_partial_success_errors_and_duplicate_input_order() -> None:
    config = settings(
        provider("main", model("shared"), model("unique", edit=True)),
        provider("second", model("shared")),
        provider("off", model("one"), enabled=False),
        provider("hidden", model("one", visible=False)),
    )
    selected, errors = select_capability_models(
        config,
        query_type="model",
        model_refs=[
            " main:shared ",
            "shared",
            "second:shared",
            "unique",
            "off:one",
            "hidden:one",
            "missing:one",
            "main:absent",
            "main:shared",
        ],
    )
    assert refs(selected) == [
        "main:shared",
        "second:shared",
        "main:unique",
        "main:shared",
    ]
    assert [item[4:] for item in selected] == [
        (0, "main:shared"),
        (2, "second:shared"),
        (3, "unique"),
        (8, "main:shared"),
    ]
    assert [(item["input_index"], item["code"]) for item in errors] == [
        (1, "ambiguous_model_id"),
        (4, "provider_disabled"),
        (5, "model_not_exposed"),
        (6, "provider_not_found"),
        (7, "model_not_found"),
    ]
    assert errors[0]["candidates"] == ["main:shared", "second:shared"]


def test_bare_model_ambiguity_only_includes_visible_supported_candidates() -> None:
    config = settings(
        provider("off", model("same", edit=True), enabled=False),
        provider("hidden", model("same", edit=True, visible=False)),
        provider("text", model("same")),
        provider("edit", model("same", edit=True)),
    )
    selected, errors = select_capability_models(
        config, query_type="model", mode="img2img", model_refs=["same", "text:same"]
    )
    assert refs(selected) == ["edit:same"]
    assert selected[0][2] == ["img2img"]
    assert errors[0]["code"] == "unsupported_mode"


def test_all_invalid_ids_are_data_errors_and_do_not_raise() -> None:
    selected, errors = select_capability_models(
        settings(provider("main", model("one"))),
        query_type="model",
        model_refs=["missing", "main:", ":one"],
    )
    assert selected == []
    assert [item["code"] for item in errors] == [
        "model_not_found",
        "invalid_model_ref",
        "invalid_model_ref",
    ]


@pytest.mark.parametrize(
    "model_refs",
    [None, [], "main:one", ("main:one",), [""], [" "], ["one", 1], [False]],
)
def test_batch_rejects_invalid_shape_before_returning_partial_results(
    model_refs,
) -> None:
    with pytest.raises(ValueError, match="model_refs"):
        select_capability_models(
            settings(provider("main", model("one"))),
            query_type="model",
            model_refs=model_refs,
        )


def test_all_and_default_keep_config_order_and_mode_contracts() -> None:
    config = settings(
        provider("first", model("edit", text=False, edit=True)),
        provider("second", model("text"), model("other")),
        default_tool_text2img_model_ref="second:text",
        default_tool_img2img_model_ref="first:edit",
    )
    selected, errors = select_capability_models(config, query_type="default")
    assert not errors
    assert refs(selected) == ["first:edit", "second:text"]
    assert [item[2:4] for item in selected] == [
        (["img2img"], ["img2img"]),
        (["text2img"], ["text2img"]),
    ]
    selected, _ = select_capability_models(config, query_type="all")
    assert refs(selected) == ["first:edit", "second:text", "second:other"]
    assert [item[3] for item in selected] == [["img2img"], ["text2img"], []]
    assert all(item[4] is None for item in selected)


def test_search_and_model_queries_identify_mode_defaults_without_changing_ranking():
    config = settings(
        provider("main", model("ordinary"), model("chosen", edit=True)),
        default_tool_text2img_model_ref="chosen",
        default_tool_img2img_model_ref="main:chosen",
    )
    found = search_catalog(config, query_type="search")
    assert [item["model_ref"] for item in found["models"]] == [
        "main:ordinary",
        "main:chosen",
    ]
    assert [item["default_for_modes"] for item in found["models"]] == [
        [],
        ["text2img", "img2img"],
    ]
    selected, _ = select_capability_models(
        config, query_type="model", mode="img2img", model_refs=["main:chosen"]
    )
    assert selected[0][3] == ["img2img"]


def test_ambiguous_default_is_not_falsely_marked_in_discovery():
    config = settings(
        provider("first", model("same")),
        provider("second", model("same")),
        default_tool_text2img_model_ref="same",
    )
    assert all(
        not item["default_for_modes"]
        for item in search_catalog(config, query_type="search")["models"]
    )


def test_shared_default_merges_both_modes() -> None:
    config = settings(
        provider("main", model("one", edit=True)),
        default_tool_text2img_model_ref="main:one",
        default_tool_img2img_model_ref="main:one",
    )
    selected, _ = select_capability_models(config, query_type="default")
    assert len(selected) == 1
    assert selected[0][2] == selected[0][3] == ["text2img", "img2img"]


def test_missing_or_unavailable_defaults_remain_errors() -> None:
    with pytest.raises(ValueError, match="尚未设置"):
        select_capability_models(settings(), query_type="default")
    with pytest.raises(ValueError, match="没有符合条件"):
        select_capability_models(
            settings(default_tool_text2img_model_ref="missing:one"),
            query_type="default",
        )
    with pytest.raises(ValueError, match="没有符合条件"):
        select_capability_models(settings(), query_type="all")


def test_ambiguous_bare_default_does_not_grant_multiple_models() -> None:
    config = settings(
        provider("first", model("same")),
        provider("second", model("same")),
        default_tool_text2img_model_ref="same",
    )
    with pytest.raises(ValueError, match="默认模型 same 的 ID 不唯一"):
        select_capability_models(config, query_type="default")


def test_bare_defaults_in_different_modes_resolve_independently() -> None:
    config = settings(
        provider("first", model("same")),
        provider("second", model("same", text=False, edit=True)),
        default_tool_text2img_model_ref="same",
        default_tool_img2img_model_ref="same",
    )
    selected, _ = select_capability_models(config, query_type="default")
    assert refs(selected) == ["first:same", "second:same"]
    assert [item[2] for item in selected] == [["text2img"], ["img2img"]]


def test_full_queries_reject_search_only_filters() -> None:
    with pytest.raises(ValueError, match="筛选仅用于"):
        select_capability_models(
            settings(provider("main", model("one"))),
            query_type="model",
            model_refs=["main:one"],
            provider_id="main",
        )
