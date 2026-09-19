"""Workflow prompt contracts stay unspecified until the user defines them."""

from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest
from astrbot_plugin_image_studio.backend.config import (
    HistorySettings,
    RuntimeSettings,
    load_studio_settings,
    save_studio_settings,
)
from astrbot_plugin_image_studio.main import ImageStudioPlugin
from astrbot_plugin_image_studio.backend.models import ImageProvider

LEGACY = {
    "selection_description": "适合一般自然语言生图需求。",
    "prompt_profile": "natural_language",
    "prompt_instructions": "使用清晰、连贯的自然语言描述，不要使用英文逗号分隔的 NAI tag 串。",
}
EMPTY = {name: "" for name in LEGACY}


def provider(*, kind="comfyui", tool=None):
    return ImageProvider.from_mapping(
        {
            "id": "provider",
            "name": "Provider",
            "kind": kind,
            "models": [{"id": "workflow", "tool": {} if tool is None else tool}],
        }
    )


def prompt_contract(value):
    return {name: value.models[0].tool[name] for name in EMPTY}


def test_new_comfy_workflow_has_no_assumed_language_or_usage():
    assert prompt_contract(provider()) == EMPTY


def test_explicit_empty_tool_contract_survives_save_load_and_model_roundtrip(tmp_path):
    configured = provider(tool=EMPTY)
    asyncio.run(
        save_studio_settings(tmp_path, {"providers": [configured.public_dict()]})
    )
    loaded, errors = load_studio_settings(tmp_path)
    assert not errors
    reloaded = ImageProvider.from_mapping(loaded["providers"][0])
    assert prompt_contract(reloaded) == EMPTY
    assert prompt_contract(ImageProvider.from_mapping(reloaded.public_dict())) == EMPTY


def test_only_complete_former_automatic_triplet_is_cleared():
    legacy = copy.deepcopy(LEGACY)
    assert prompt_contract(provider(tool=legacy)) == EMPTY
    assert legacy == LEGACY


@pytest.mark.parametrize(
    "change",
    [
        {"selection_description": "生成透明头像并自动放大。"},
        {"prompt_instructions": "使用英文标签，保留括号权重。"},
        {"prompt_profile": "nai_tags"},
        {"prompt_profile": ""},
    ],
)
def test_custom_text_or_explicit_format_does_not_trigger_legacy_cleanup(change):
    custom = {**LEGACY, **change}
    assert prompt_contract(provider(tool=custom)) == custom


def test_custom_contract_and_unspecified_fields_remain_independent():
    assert prompt_contract(provider(tool={"prompt_profile": "custom"})) == {
        **EMPTY,
        "prompt_profile": "custom",
    }
    assert prompt_contract(
        provider(tool={"selection_description": "随机抽卡工作流"})
    ) == {**EMPTY, "selection_description": "随机抽卡工作流"}


@pytest.mark.parametrize("kind", ["openai_images", "gemini", "custom_json"])
def test_other_natural_language_provider_defaults_are_unchanged(kind):
    assert prompt_contract(provider(kind=kind)) == LEGACY
    assert prompt_contract(provider(kind=kind, tool=EMPTY)) == LEGACY


@pytest.mark.parametrize(
    "kind,profile", [("nai_direct", "nai_tags"), ("novelai_official", "custom")]
)
def test_novelai_provider_guidance_is_retained(kind, profile):
    contract = prompt_contract(provider(kind=kind))
    assert contract["prompt_profile"] == profile
    assert contract["selection_description"] and contract["prompt_instructions"]
    assert prompt_contract(provider(kind=kind, tool=EMPTY)) == contract


def test_unspecified_format_reaches_llm_capabilities_without_fallback():
    configured = provider()
    plugin = object.__new__(ImageStudioPlugin)
    plugin._settings = RuntimeSettings(
        enable_llm_tool=True,
        providers=(configured,),
        history=HistorySettings(False, 0, 0, False),
        revision=0,
        default_tool_text2img_model_ref="provider:workflow",
    )
    result = asyncio.run(
        plugin.image_studio_get_capabilities(SimpleNamespace(), mode="text2img")
    )
    entry = json.loads(result.content[0].text)["models"][0]
    assert entry["selection_description"] == ""
    assert entry["prompt_contract"]["format"] == ""
    assert (
        "不要使用英文逗号分隔的 NAI tag 串"
        not in entry["prompt_contract"]["instruction"]
    )


@pytest.mark.parametrize("exposed", [True, False])
def test_fixed_total_count_is_exposed_without_node_binding_and_respects_tool_policy(
    exposed,
):
    from astrbot_plugin_image_studio.tests.support.comfy_runtime import workflow

    raw = provider().public_dict()
    raw["models"][0].update(
        comfyui=workflow(),
        parameters={"count": {"type": "integer", "default": 7, "max": 40}},
        tool={"enabled": True, "parameters": {"count": {"exposed": exposed}}},
    )
    configured = ImageProvider.from_mapping(raw)
    plugin = object.__new__(ImageStudioPlugin)
    plugin._settings = RuntimeSettings(
        enable_llm_tool=True,
        providers=(configured,),
        history=HistorySettings(False, 0, 0, False),
        revision=0,
        default_tool_text2img_model_ref="provider:workflow",
    )
    result = asyncio.run(
        plugin.image_studio_get_capabilities(SimpleNamespace(), mode="text2img")
    )
    entry = json.loads(result.content[0].text)["models"][0]
    assert ("count" in entry["parameters"]) is exposed
    assert "native_batch_size" not in entry["parameters"]
    if exposed:
        assert entry["parameters"]["count"]["default"] == 7
        assert entry["parameters"]["count"]["max"] == 40
        assert "不足" in entry["parameters"]["count"]["description"]
