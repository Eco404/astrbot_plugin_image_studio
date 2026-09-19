"""Discovery contracts agree with the normalized execution configuration."""

from __future__ import annotations

import asyncio
import copy
from dataclasses import replace

import pytest

from astrbot_plugin_image_studio.backend.config import normalize_webui_settings
from astrbot_plugin_image_studio.backend.generation.service import (
    ImageGenerationService,
    _control_parameter_value,
    _parameters_for_mode,
    _parameters_for_model,
)
from astrbot_plugin_image_studio.backend.gallery.store import GenerationStore
from astrbot_plugin_image_studio.backend.models import ImageProvider
from astrbot_plugin_image_studio.backend.tools.capabilities import query_capabilities
from astrbot_plugin_image_studio.tests.backend.config.test_parameter_policy import (
    CapturingExecutor,
    policy_provider,
    policy_settings,
)


def contract(provider, mode=""):
    return query_capabilities(
        policy_settings(provider),
        query_type="model",
        model_refs=[f"{provider.id}:{provider.models[0].id}"],
        mode=mode,
    )["models"][0]


@pytest.mark.parametrize(
    "policy,expected",
    [({"default": 7}, 7), ({"default_override": 9, "default": 7}, 9), ({}, 20)],
)
def test_normalized_tool_defaults_match_discovery_and_execution(policy, expected):
    original = policy_provider(
        {"steps": {"type": "integer", "default": 20}},
        tool={"parameters": {"steps": {"exposed": True, **policy}}},
    )
    normalized, errors = normalize_webui_settings(
        {"providers": [original.public_dict()]}
    )
    assert not errors
    provider = ImageProvider.from_mapping(normalized["providers"][0])
    before = copy.deepcopy(provider.public_dict())
    assert contract(provider)["parameters"]["steps"]["default"] == expected
    assert (
        _parameters_for_model({}, provider.models[0], source="llm_tool")["steps"]
        == expected
    )
    assert _parameters_for_model({}, provider.models[0], source="webui")["steps"] == 20
    assert provider.public_dict() == before


def test_control_defaults_share_legacy_precedence_and_preserve_explicit_zero():
    provider = policy_provider(
        {"count": {"type": "integer", "default": 1}},
        tool={"parameters": {"count": {"default": 5, "exposed": True}}},
    )
    assert contract(provider)["parameters"]["count"]["default"] == 5
    assert (
        _control_parameter_value({}, provider.models[0], "count", source="llm_tool")
        == 5
    )
    assert (
        _control_parameter_value(
            {"count": 0}, provider.models[0], "count", source="llm_tool"
        )
        == 0
    )


def test_model_query_filters_mode_specific_parameters_and_marks_joint_contract():
    provider = ImageProvider.from_mapping(
        {
            "id": "nai",
            "kind": "novelai_official",
            "models": [{"id": "nai-diffusion-4-5-full"}],
        }
    )
    text = contract(provider, "text2img")["parameters"]
    edit = contract(provider, "img2img")["parameters"]
    both = contract(provider)["parameters"]
    for name in ("strength", "noise", "reference_mode", "reference_settings"):
        assert name not in text
        assert edit[name]["modes"] == both[name]["modes"] == ["img2img"]
    wire = _parameters_for_mode(
        _parameters_for_model({"strength": 0.9}, provider.models[0], source="llm_tool"),
        provider.models[0],
        "text2img",
    )
    assert "strength" not in wire


def test_default_contract_contains_only_parameters_for_its_queried_modes():
    provider = policy_provider(
        {"edit_only": {"type": "number", "default": 0.5, "modes": ["img2img"]}}
    )
    provider = replace(provider, models=(replace(provider.models[0], img2img=True),))
    payload = query_capabilities(policy_settings(provider))
    assert payload["models"][0]["query_modes"] == ["text2img"]
    assert "edit_only" not in payload["models"][0]["parameters"]


def test_declared_unicode_parameter_reaches_generation_but_hidden_and_unknown_do_not(
    tmp_path,
):
    raw = policy_provider(
        {
            "重绘": {"type": "number", "default": 0.2, "request_key": "denoise"},
            "隐藏": {"type": "integer", "default": 3, "request_key": "private"},
        },
        tool={"parameters": {"重绘": {"exposed": True}, "隐藏": {"exposed": False}}},
    )
    normalized, errors = normalize_webui_settings({"providers": [raw.public_dict()]})
    assert not errors
    provider = ImageProvider.from_mapping(normalized["providers"][0])
    assert "重绘" in contract(provider)["parameters"]
    assert "隐藏" not in contract(provider)["parameters"]

    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        executor = CapturingExecutor()
        service = ImageGenerationService(
            settings=policy_settings(provider), executor=executor, store=store
        )
        await service.generate(
            mode="text2img",
            provider_id=provider.id,
            prompt="mountain",
            source="llm_tool",
            parameters={"重绘": 0.9, "隐藏": 99, "未定义": 88, "unknown": 77},
        )
        actual = executor.requests[0].parameters
        assert actual["denoise"] == 0.9
        assert actual["private"] == 3
        assert "未定义" not in actual and "unknown" not in actual

    asyncio.run(run())


@pytest.mark.parametrize(
    "value", [True, False, 1.5, "1.5", float("nan"), float("inf"), "NaN", "-Infinity"]
)
def test_integer_parameters_reject_fractional_boolean_and_nonfinite_values(value):
    model = policy_provider({"steps": {"type": "integer", "min": 0, "max": 50}}).models[
        0
    ]
    with pytest.raises(ValueError, match="参数 steps"):
        _parameters_for_model({"steps": value}, model, source="llm_tool")


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), "NaN", "Infinity"])
def test_number_parameters_require_finite_values(value):
    model = policy_provider({"scale": {"type": "number"}}).models[0]
    with pytest.raises(ValueError, match="有限数字"):
        _parameters_for_model({"scale": value}, model, source="llm_tool")


def test_uint64_integer_bounds_are_checked_without_float_rounding():
    maximum = 2**64 - 1
    model = policy_provider(
        {"seed": {"type": "integer", "min": -1, "max": str(maximum)}}
    ).models[0]
    assert _parameters_for_model({"seed": str(maximum)}, model)["seed"] == str(maximum)
    with pytest.raises(ValueError, match="不能大于"):
        _parameters_for_model({"seed": str(maximum + 1)}, model)


def test_comfy_contract_preserves_custom_instructions_and_describes_reference_slots():
    graph = {
        "1": {"class_type": "LoadImage", "inputs": {"image": "base.png"}},
        "2": {"class_type": "LoadImage", "inputs": {"image": "mask.png"}},
        "3": {
            "class_type": "SaveImage",
            "inputs": {"images": ["1", 0], "filename_prefix": "test"},
        },
    }
    provider = ImageProvider.from_mapping(
        {
            "id": "comfy",
            "kind": "comfyui",
            "models": [
                {
                    "id": "workflow",
                    "tool": {"prompt_instructions": "上传底图后上传蒙版。"},
                    "comfyui": {
                        "api_graph": graph,
                        "outputs": ["3"],
                        "execution_policy": "fixed_outputs_v1",
                        "bindings": {
                            "base": {
                                "source": "reference",
                                "type": "image",
                                "label": "底图",
                                "required": True,
                                "reference_index": 0,
                                "targets": [{"node_id": "1", "input_name": "image"}],
                            },
                            "mask": {
                                "source": "reference",
                                "type": "mask",
                                "label": "蒙版",
                                "required": False,
                                "reference_index": 1,
                                "targets": [{"node_id": "2", "input_name": "image"}],
                            },
                        },
                    },
                }
            ],
        }
    )
    result = contract(provider)
    assert result["prompt_contract"]["instruction"] == "上传底图后上传蒙版。"
    assert result["prompt_contract"]["required"] is False
    assert result["reference_slots"] == [
        {"name": "底图", "index": 0, "type": "image", "required": True},
        {"name": "蒙版", "index": 1, "type": "mask", "required": False},
    ]
    assert "api_graph" not in result and "bindings" not in result


def test_capability_asset_policy_does_not_repeat_global_tool_instructions():
    payload = query_capabilities(policy_settings(policy_provider()))
    assert payload["asset_policy"] == {"return_mode": "preview"}


def test_numeric_range_is_not_repeated_in_llm_description_or_removed_from_settings():
    description = "控制精细程度。取值范围：[1, 50]。"
    provider = policy_provider(
        {"steps": {"type": "integer", "default": 20, "min": 1, "max": 50}},
        tool={"parameters": {"steps": {"description": description}}},
    )
    descriptor = contract(provider)["parameters"]["steps"]
    assert descriptor["description"] == "控制精细程度。"
    assert (descriptor["min"], descriptor["max"]) == (1, 50)
    assert provider.models[0].tool["parameters"]["steps"]["description"] == description


@pytest.mark.parametrize(
    "kind,model_id,known_natural_language",
    [
        ("nai_direct", "nai-diffusion-4-5-full", False),
        ("novelai_official", "nai-diffusion-4-5-full", True),
        ("novelai_official", "nai-diffusion-5-curated", True),
        ("novelai_official", "nai-diffusion-3", False),
        ("novelai_official", "custom-model", False),
    ],
)
def test_novelai_defaults_describe_format_without_overriding_model_selection(
    kind, model_id, known_natural_language
):
    provider = ImageProvider.from_mapping(
        {"id": "nai", "kind": kind, "models": [{"id": model_id}]}
    )
    result = contract(provider)
    assert "仅在用户明确要求" not in result["selection_description"]
    instruction = result["prompt_contract"]["instruction"]
    assert "英文逗号分隔标签" in instruction
    assert ("可结合自然语言" in instruction) is known_natural_language


@pytest.mark.parametrize("kind", ["nai_direct", "novelai_official"])
def test_novelai_custom_tool_guidance_survives_normalization_and_discovery(kind):
    custom = {
        "selection_description": "仅用于团队约定的画风。",
        "prompt_profile": "custom",
        "prompt_instructions": "按用户的自定义格式输入，不自动添加内容。",
    }
    normalized, errors = normalize_webui_settings(
        {
            "providers": [
                {
                    "id": "nai",
                    "name": "NAI",
                    "kind": kind,
                    "base_url": "https://example.invalid",
                    "models": [{"id": "nai-diffusion-4-5-full", "tool": custom}],
                }
            ]
        }
    )
    assert not errors
    provider = ImageProvider.from_mapping(normalized["providers"][0])
    result = contract(provider)
    assert result["selection_description"] == custom["selection_description"]
    assert result["prompt_contract"]["format"] == custom["prompt_profile"]
    assert result["prompt_contract"]["instruction"] == custom["prompt_instructions"]
