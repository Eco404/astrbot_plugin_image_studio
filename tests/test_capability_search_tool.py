from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
from astrbot.core.provider.register import llm_tools
from astrbot_plugin_image_studio.backend.config import HistorySettings, RuntimeSettings
from astrbot_plugin_image_studio.main import (
    CAPABILITY_QUERY_EXTRA_KEY,
    IMAGE_WORKFLOW_STATE_EXTRA_KEY,
    ImageStudioPlugin,
)
from astrbot_plugin_image_studio.backend.models import (
    GeneratedImage,
    GenerationResult,
    ImageProvider,
)
from astrbot_plugin_image_studio.tests.test_comfy_provider import config as workflow
from astrbot_plugin_image_studio.tests.test_service_and_tool import (
    PNG,
    FakeAgentAssetStore,
    ToolEvent,
)


def plugin_fixture():
    providers = (
        ImageProvider.from_mapping(
            {
                "id": "p1",
                "name": "Main Models",
                "kind": "custom_json",
                "api_key": "private-test-key",
                "base_url": "https://internal-provider.invalid",
                "models": [
                    {
                        "id": "alpha",
                        "name": "Alpha Portrait",
                        "parameters": {
                            "steps": {"type": "integer", "default": 30},
                            "private": {"type": "integer", "default": 7},
                        },
                        "tool": {
                            "selection_description": "人物摄影和肖像",
                            "parameters": {
                                "steps": {"exposed": True},
                                "private": {"exposed": False},
                            },
                        },
                    },
                    {"id": "shared", "name": "Shared A"},
                    {
                        "id": "editor",
                        "name": "Image Editor",
                        "supports_text2img": False,
                        "supports_img2img": True,
                        "max_reference_images": 2,
                    },
                    {"id": "hidden", "tool": {"enabled": False}},
                ],
            }
        ),
        ImageProvider.from_mapping(
            {
                "id": "p2",
                "name": "Other Models",
                "kind": "custom_json",
                "base_url": "https://other.invalid",
                "models": [{"id": "shared", "name": "Shared B"}],
            }
        ),
        ImageProvider.from_mapping(
            {
                "id": "comfy",
                "name": "Comfy Main",
                "kind": "comfyui",
                "base_url": "https://comfy.invalid",
                "models": [
                    {
                        "id": "raffle",
                        "name": "随机抽卡",
                        "comfyui": workflow(),
                        "tool": {"selection_description": "随机抽卡工作流"},
                    }
                ],
            }
        ),
        ImageProvider.from_mapping(
            {
                "id": "empty",
                "name": "Empty Comfy",
                "kind": "comfyui",
                "base_url": "https://empty.invalid",
                "models": [],
            }
        ),
        ImageProvider.from_mapping(
            {
                "id": "disabled",
                "name": "Disabled Provider",
                "kind": "custom_json",
                "base_url": "https://disabled.invalid",
                "enabled": False,
                "models": [{"id": "alpha"}],
            }
        ),
    )
    plugin = object.__new__(ImageStudioPlugin)
    plugin._settings = RuntimeSettings(
        enable_llm_tool=True,
        providers=providers,
        history=HistorySettings(False, 0, 0, False),
        revision=19,
        default_tool_text2img_model_ref="p1:alpha",
        default_tool_img2img_model_ref="p1:editor",
    )
    plugin.store = FakeAgentAssetStore()
    plugin._generation_calls = []

    async def generate(**kwargs):
        plugin._generation_calls.append(kwargs)
        provider_id, model_id = kwargs["model_ref"].split(":", 1)
        provider = next(p for p in providers if p.id == provider_id)
        return GenerationResult(
            provider,
            SimpleNamespace(model=model_id, mode=kwargs["mode"]),
            (GeneratedImage(PNG, "image/png"),),
            5,
        )

    plugin._service = SimpleNamespace(generate=generate)
    return plugin


def query(plugin, event=None, **kwargs):
    result = asyncio.run(
        plugin.image_studio_get_capabilities(event or ToolEvent(), **kwargs)
    )
    assert not result.isError, result.content[0].text
    return json.loads(result.content[0].text)


def test_model_query_returns_each_success_and_failure_in_input_order():
    plugin = plugin_fixture()
    event = ToolEvent()
    refs = ["p2:shared", "missing:nope", "p1:alpha", "p1:hidden", "p1:alpha"]

    payload = query(plugin, event, query_type="model", model_refs=refs)

    assert [entry["model_ref"] for entry in payload["models"]] == [
        "p2:shared",
        "p1:alpha",
        "p1:alpha",
    ]
    assert [entry["input_index"] for entry in payload["models"]] == [0, 2, 4]
    assert [entry["requested_ref"] for entry in payload["models"]] == [
        refs[0],
        refs[2],
        refs[4],
    ]
    assert [entry["model_ref"] for entry in payload["errors"]] == [refs[1], refs[3]]
    assert [entry["input_index"] for entry in payload["errors"]] == [1, 3]
    assert all(entry["code"] and entry["message"] for entry in payload["errors"])
    assert event.get_extra(CAPABILITY_QUERY_EXTRA_KEY)["models"] == {
        "p2:shared": ["text2img"],
        "p1:alpha": ["text2img"],
    }
    assert "steps" in payload["models"][1]["parameters"]
    assert "private" not in payload["models"][1]["parameters"]


def test_ambiguous_bare_id_does_not_remove_explicit_reference_success():
    plugin = plugin_fixture()
    payload = query(
        plugin, query_type="model", model_refs=["shared", "p1:shared", "alpha"]
    )

    assert [entry["model_ref"] for entry in payload["models"]] == [
        "p1:shared",
        "p1:alpha",
    ]
    assert payload["models"][1]["requested_ref"] == "alpha"
    assert payload["errors"][0]["model_ref"] == "shared"
    assert payload["errors"][0]["code"] == "ambiguous_model_id"
    assert "provider_id:model_id" in payload["errors"][0]["message"]


def test_all_invalid_models_return_structured_result_without_authorizing_generation():
    plugin = plugin_fixture()
    event = ToolEvent()
    refs = ["missing:nope", "disabled:alpha", "p1:hidden", "p1:editor"]
    payload = query(plugin, event, query_type="model", mode="text2img", model_refs=refs)

    assert payload["models"] == []
    assert [entry["model_ref"] for entry in payload["errors"]] == refs
    assert [entry["code"] for entry in payload["errors"]] == [
        "provider_not_found",
        "provider_disabled",
        "model_not_exposed",
        "unsupported_mode",
    ]
    assert event.get_extra(CAPABILITY_QUERY_EXTRA_KEY) is None
    assert event.get_extra(IMAGE_WORKFLOW_STATE_EXTRA_KEY) is None


@pytest.mark.parametrize(
    "refs",
    [
        None,
        [],
        "p1:alpha",
        {"model_ref": "p1:alpha"},
        [None],
        [1],
        [""],
        [" "],
        ["p1:alpha", 2],
    ],
)
def test_invalid_model_refs_format_rejects_whole_query(refs):
    plugin = plugin_fixture()
    event = ToolEvent()

    result = asyncio.run(
        plugin.image_studio_get_capabilities(event, query_type="model", model_refs=refs)
    )

    assert result.isError
    assert "model_refs" in result.content[0].text
    assert event.get_extra(CAPABILITY_QUERY_EXTRA_KEY) is None


def test_supplied_model_refs_infer_model_query_and_empty_error_list():
    payload = query(plugin_fixture(), model_refs=["p1:alpha"])
    assert payload["query_type"] == "model"
    assert payload["errors"] == []


@pytest.mark.parametrize("query_type", ["all", "search", "providers"])
def test_model_refs_cannot_silently_override_other_explicit_query_types(query_type):
    result = asyncio.run(
        plugin_fixture().image_studio_get_capabilities(
            ToolEvent(), query_type=query_type, model_refs=["p1:alpha"]
        )
    )
    assert result.isError


@pytest.mark.parametrize("query_type", ["search", "providers"])
def test_discovery_does_not_activate_image_workflow_or_disclose_secrets(query_type):
    plugin = plugin_fixture()
    event = ToolEvent()
    payload = query(plugin, event, query_type=query_type)

    serialized = json.dumps(payload)
    assert "private-test-key" not in serialized
    assert "internal-provider.invalid" not in serialized
    assert "api_graph" not in serialized
    assert '"parameters"' not in serialized
    assert '"prompt_contract"' not in serialized
    assert event.get_extra(CAPABILITY_QUERY_EXTRA_KEY) is None
    assert event.get_extra(IMAGE_WORKFLOW_STATE_EXTRA_KEY) is None
    result = asyncio.run(
        plugin.image_studio_generate(
            event, mode="text2img", prompt="tree", model_ref="p1:alpha"
        )
    )
    assert result.isError
    assert "必须先调用 image_studio_get_capabilities" in result.content[0].text
    assert not plugin._generation_calls


def test_search_supports_name_description_provider_and_mode_filters():
    plugin = plugin_fixture()
    result = query(plugin, query_type="search", query="ALPHA", provider_id="p1")
    assert [item["model_ref"] for item in result["models"]] == ["p1:alpha"]
    assert result["models"][0]["matched_fields"]
    result = query(plugin, query_type="search", query="人物摄影")
    assert [item["model_ref"] for item in result["models"]] == ["p1:alpha"]
    assert "selection_description" in result["models"][0]["matched_fields"]
    result = query(plugin, query_type="search", query="抽卡", provider_kind="comfyui")
    assert [item["model_ref"] for item in result["models"]] == ["comfy:raffle"]
    result = query(plugin, query_type="search", mode="img2img")
    assert [item["model_ref"] for item in result["models"]] == ["p1:editor"]
    result = query(plugin, query_type="search", query="no matching model")
    assert result["models"] == []
    assert result["total"] == 0


def test_search_pages_visible_models_in_configuration_order():
    plugin = plugin_fixture()
    first = query(plugin, query_type="search", provider_id="p1", limit=2)
    second = query(plugin, query_type="search", provider_id="p1", limit=2, offset=2)
    assert [item["model_ref"] for item in first["models"]] == ["p1:alpha", "p1:shared"]
    assert [item["model_ref"] for item in second["models"]] == ["p1:editor"]
    assert first["total"] == second["total"] == 3
    assert first["limit"] == second["limit"] == 2
    assert first["offset"] == 0 and second["offset"] == 2
    assert first["has_more"] is True and second["has_more"] is False
    beyond_end = query(plugin, query_type="search", offset=100)
    assert beyond_end["models"] == []
    assert beyond_end["has_more"] is False


def test_provider_discovery_includes_empty_comfy_and_excludes_disabled():
    plugin = plugin_fixture()
    result = query(plugin, query_type="providers")
    assert [item["provider_id"] for item in result["providers"]] == [
        "p1",
        "p2",
        "comfy",
        "empty",
    ]
    assert [item["model_count"] for item in result["providers"]] == [3, 1, 1, 0]
    result = query(
        plugin, query_type="providers", provider_kind="comfyui", query="EMPTY"
    )
    assert [item["provider_id"] for item in result["providers"]] == ["empty"]
    result = query(plugin, query_type="search", provider_id="empty")
    assert result["models"] == [] and result["total"] == 0


@pytest.mark.parametrize("query_type", ["search", "providers"])
@pytest.mark.parametrize("provider_id", ["unknown", "disabled"])
def test_discovery_reports_invalid_explicit_provider_instead_of_fallback(
    query_type, provider_id
):
    result = asyncio.run(
        plugin_fixture().image_studio_get_capabilities(
            ToolEvent(), query_type=query_type, provider_id=provider_id
        )
    )
    assert result.isError
    assert provider_id in result.content[0].text


@pytest.mark.parametrize(
    "arguments",
    [
        {"limit": 0},
        {"limit": 51},
        {"limit": True},
        {"limit": 1.5},
        {"offset": -1},
        {"offset": False},
        {"offset": 0.5},
        {"provider_kind": "unsupported-kind"},
    ],
)
def test_discovery_rejects_invalid_filters_and_page_bounds(arguments):
    result = asyncio.run(
        plugin_fixture().image_studio_get_capabilities(
            ToolEvent(), query_type="search", **arguments
        )
    )
    assert result.isError


def test_search_preserves_prior_full_contract_but_does_not_refresh_revision():
    plugin = plugin_fixture()
    event = ToolEvent()
    query(plugin, event, query_type="model", model_refs=["p1:alpha"])
    remembered = copy.deepcopy(event.get_extra(CAPABILITY_QUERY_EXTRA_KEY))

    query(plugin, event, query_type="search", query="alpha")
    assert event.get_extra(CAPABILITY_QUERY_EXTRA_KEY) == remembered
    plugin._settings = replace(plugin._settings, revision=20)
    query(plugin, event, query_type="providers")
    assert event.get_extra(CAPABILITY_QUERY_EXTRA_KEY) == remembered
    generated = asyncio.run(
        plugin.image_studio_generate(
            event, mode="text2img", prompt="tree", model_ref="p1:alpha"
        )
    )
    assert generated.isError
    assert not plugin._generation_calls


def test_batch_contracts_are_consumed_independently():
    plugin = plugin_fixture()
    event = ToolEvent()
    query(
        plugin,
        event,
        query_type="model",
        model_refs=["p1:alpha", "p2:shared", "p1:nope"],
    )

    for model_ref in ["p2:shared", "p1:alpha"]:
        result = asyncio.run(
            plugin.image_studio_generate(
                event, mode="text2img", prompt="tree", model_ref=model_ref
            )
        )
        assert not result.isError, result.content[0].text
    result = asyncio.run(
        plugin.image_studio_generate(
            event, mode="text2img", prompt="tree", model_ref="p1:alpha"
        )
    )
    assert result.isError
    assert len(plugin._generation_calls) == 2


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        ["p1:alpha", "p2:shared"],
        [""],
        [" "],
        [1],
        [["p1:alpha"]],
        {},
        1,
        False,
    ],
)
def test_generate_rejects_invalid_reference_with_only_official_format(value):
    plugin = plugin_fixture()
    event = ToolEvent()
    query(plugin, event, query_type="model", model_refs=["p1:alpha"])
    original_contract = copy.deepcopy(event.get_extra(CAPABILITY_QUERY_EXTRA_KEY))

    async def references_must_not_load(*_args, **_kwargs):
        pytest.fail("invalid model_ref must fail before resolving reference images")

    plugin._event_references = references_must_not_load
    result = asyncio.run(plugin.image_studio_generate(event, model_ref=value))

    assert result.isError
    message = result.content[0].text
    assert "model_ref 必须是字符串，例如 provider_id:model_id" in message
    assert not any(word in message for word in ("list", "array", "数组", "列表"))
    assert event.get_extra(CAPABILITY_QUERY_EXTRA_KEY) == original_contract
    assert not plugin._generation_calls


def test_host_executor_accepts_single_reference_fallback_without_widening_schema():
    plugin = plugin_fixture()
    event = ToolEvent()
    query(plugin, event, query_type="model", model_refs=["p1:alpha"])
    tool = copy.copy(llm_tools.get_func("image_studio_generate"))
    tool.handler = plugin.image_studio_generate
    assert tool.parameters["properties"]["model_ref"]["type"] == "string"
    context = SimpleNamespace(
        context=SimpleNamespace(event=event), tool_call_timeout=10
    )

    async def run():
        return [
            item
            async for item in FunctionToolExecutor.execute(
                tool, context, mode="text2img", prompt="tree", model_ref=["p1:alpha"]
            )
        ]

    results = asyncio.run(run())
    assert len(results) == 1
    assert not results[0].isError, results[0].content[0].text
    assert plugin._generation_calls[0]["model_ref"] == "p1:alpha"


def test_host_executor_passes_invalid_reference_to_official_format_error():
    plugin = plugin_fixture()
    tool = copy.copy(llm_tools.get_func("image_studio_generate"))
    tool.handler = plugin.image_studio_generate
    context = SimpleNamespace(
        context=SimpleNamespace(event=ToolEvent()), tool_call_timeout=10
    )

    async def run():
        return [
            item
            async for item in FunctionToolExecutor.execute(
                tool, context, model_ref=["p1:alpha", "p2:shared"]
            )
        ]

    results = asyncio.run(run())
    assert len(results) == 1
    assert results[0].isError
    assert (
        "model_ref 必须是字符串，例如 provider_id:model_id"
        in results[0].content[0].text
    )
