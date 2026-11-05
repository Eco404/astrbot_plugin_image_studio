"""Effective mode contracts cannot overwrite explicit values or send bad defaults."""

from __future__ import annotations

import pytest

from astrbot_plugin_image_studio.backend.config import normalize_webui_settings
from astrbot_plugin_image_studio.backend.generation.service import _parameters_for_model
from astrbot_plugin_image_studio.backend.models import ImageProvider
from astrbot_plugin_image_studio.backend.parameters.exchange import resolve_parameters
from astrbot_plugin_image_studio.backend.tools.capabilities import query_capabilities
from astrbot_plugin_image_studio.tests.backend.config.test_parameter_policy import (
    policy_settings,
    studio_content,
)


def provider(parameters, *, tool=None, img2img=True):
    return ImageProvider.from_mapping(
        {
            "id": "policy",
            "name": "Validation test",
            "kind": "custom_json",
            "base_url": "https://example.invalid",
            "models": [
                {
                    "id": "paint",
                    "supports_img2img": img2img,
                    "parameters": parameters,
                    "tool": tool or {},
                }
            ],
        }
    )


def test_duplicate_active_aliases_reject_save_without_dropping_existing_provider():
    configured = provider(
        {
            "first": {"type": "integer", "default": 1, "request_key": "seed"},
            "second": {"type": "integer", "default": 2, "request_key": "seed"},
        }
    )
    normalized, errors = normalize_webui_settings(
        {"providers": [configured.public_dict()]}
    )
    assert any("相同 request_key：seed" in error for error in errors)
    assert (
        normalized["providers"][0]["models"][0]["parameters"]["first"]["default"] == 1
    )
    with pytest.raises(ValueError, match="相同 request_key"):
        _parameters_for_model({"first": 77}, configured.models[0], mode="text2img")


def test_mode_override_cannot_create_duplicate_wire_alias():
    configured = provider(
        {
            "first": {"default": "one", "request_key": "shared"},
            "second": {
                "default": "two",
                "request_key": "other",
                "mode_overrides": {"img2img": {"request_key": "shared"}},
            },
        }
    )
    assert _parameters_for_model({}, configured.models[0], mode="text2img") == {
        "shared": "one",
        "other": "two",
    }
    with pytest.raises(ValueError, match="图生图参数"):
        configured.models[0].validate_parameters()


def test_mode_disjoint_aliases_and_ui_only_presets_are_not_conflicts():
    configured = provider(
        {
            "text_value": {
                "default": "text",
                "request_key": "shared",
                "modes": ["text2img"],
            },
            "edit_value": {
                "default": "edit",
                "request_key": "shared",
                "modes": ["img2img"],
            },
            "decoration": {
                "default": "ignored",
                "request_key": "shared",
                "ui_only": True,
            },
        }
    )
    configured.models[0].validate_parameters()
    for mode, expected in (("text2img", "text"), ("img2img", "edit")):
        assert _parameters_for_model({}, configured.models[0], mode=mode) == {
            "shared": expected
        }


def test_request_key_cannot_be_mistaken_for_another_schema_field_name():
    configured = provider(
        {
            "first": {"type": "integer", "default": 1, "request_key": "second"},
            "second": {"type": "integer", "default": 2, "request_key": "other"},
        }
    )
    normalized, errors = normalize_webui_settings(
        {"providers": [configured.public_dict()]}
    )
    assert normalized["providers"]
    assert any("与另一个参数名冲突" in error for error in errors)
    with pytest.raises(ValueError, match="与另一个参数名冲突"):
        _parameters_for_model(
            {"second": 77, "other": 88}, configured.models[0], mode="text2img"
        )


@pytest.mark.parametrize(
    "descriptor,message",
    [
        (
            {
                "type": "integer",
                "default": 3,
                "mode_overrides": {"img2img": {"type": "json"}},
            },
            "JSON 对象或数组",
        ),
        (
            {
                "type": "select",
                "default": "a",
                "choices": ["a"],
                "mode_overrides": {"img2img": {"choices": ["b"]}},
            },
            "可选范围",
        ),
        (
            {
                "type": "integer",
                "default": 3,
                "mode_overrides": {"img2img": {"max": 2}},
            },
            "不能大于",
        ),
        ({"type": "boolean", "default": "true"}, "布尔值"),
        (
            {
                "type": "json",
                "default": {},
                "mode_overrides": {"img2img": {"type": "text"}},
            },
            "文本或数字",
        ),
    ],
)
def test_invalid_effective_default_is_reported_and_never_sent(descriptor, message):
    configured = provider({"option": descriptor})
    normalized, errors = normalize_webui_settings(
        {"providers": [configured.public_dict()]}
    )
    assert len(normalized["providers"]) == 1
    assert any(message in error for error in errors)
    with pytest.raises(ValueError, match=message):
        _parameters_for_model({}, configured.models[0], mode="img2img")


@pytest.mark.parametrize(
    "kind,default,supplied,message",
    [
        ("json", {}, "not json", "JSON 对象或数组"),
        ("object", [], 12, "JSON 对象或数组"),
        ("boolean", True, "false", "布尔值"),
        ("bool", False, 0, "布尔值"),
        ("string", "", {}, "文本或数字"),
        ("textarea", "", [], "文本或数字"),
    ],
)
def test_explicit_structured_values_follow_same_contract(
    kind, default, supplied, message
):
    configured = provider({"option": {"type": kind, "default": default}})
    with pytest.raises(ValueError, match=message):
        _parameters_for_model(
            {"option": supplied}, configured.models[0], mode="img2img"
        )


def test_invalid_tool_default_is_reported_and_reset_can_use_mode_default():
    configured = provider(
        {
            "quality": {
                "type": "select",
                "default": "a",
                "choices": ["a"],
                "mode_overrides": {"img2img": {"default": "b", "choices": ["b"]}},
            }
        },
        tool={
            "parameters": {
                "quality": {
                    "exposed": True,
                    "default_override": "a",
                    "mode_overrides": {"img2img": {"use_model_default": True}},
                }
            }
        },
    )
    configured.models[0].validate_parameters()
    actual = _parameters_for_model(
        {}, configured.models[0], source="llm_tool", mode="img2img"
    )
    contract = query_capabilities(
        policy_settings(configured),
        query_type="model",
        model_refs=["policy:paint"],
        mode="img2img",
    )["models"][0]["parameters"]
    assert actual["quality"] == contract["quality"]["default"] == "b"
    raw = configured.public_dict()
    raw["models"][0]["tool"]["parameters"]["quality"]["mode_overrides"] = {}
    normalized, errors = normalize_webui_settings({"providers": [raw]})
    assert normalized["providers"]
    assert any("可选范围" in error for error in errors)


@pytest.mark.parametrize("kind", ["integer", "boolean", "json", "select"])
def test_explicit_null_defaults_and_nullable_choices_survive_execution_and_refill(kind):
    configured = provider(
        {
            "option": {
                "type": kind,
                "default": None,
                "nullable": True,
                **({"choices": ["a", "b"]} if kind == "select" else {}),
            }
        }
    )
    configured.models[0].validate_parameters()
    assert (
        _parameters_for_model({}, configured.models[0], mode="img2img")["option"]
        is None
    )
    refilled = resolve_parameters(
        studio_content({"option": None}, mode="img2img"),
        policy_settings(configured),
        "policy:paint",
        for_reproduction=True,
    )
    assert refilled["draft"]["parameters"]["option"] is None
    assert "option" not in refilled["unmapped"]


def test_optional_absence_and_count_clamping_configuration_remain_compatible():
    configured = provider({"optional": {"type": "json"}, "count": {"default": 0}})
    configured.models[0].validate_parameters()
    assert _parameters_for_model({}, configured.models[0], mode="text2img") == {}


def test_untyped_legacy_structured_defaults_infer_json():
    configured = provider({"option": {"default": {"nested": [1, 2]}}})
    configured.models[0].validate_parameters()
    assert configured.models[0].parameters["option"]["type"] == "json"
    assert _parameters_for_model({}, configured.models[0], mode="text2img") == {
        "option": {"nested": [1, 2]}
    }


@pytest.mark.parametrize("bound", ["min", "max"])
@pytest.mark.parametrize("value", ["bad", "NaN", "Infinity", True, {}])
def test_invalid_numeric_schema_bound_is_a_configuration_error(bound, value):
    configured = provider({"option": {"type": "number", "default": 1, bound: value}})
    normalized, errors = normalize_webui_settings(
        {"providers": [configured.public_dict()]}
    )
    assert normalized["providers"]
    assert any(f"{bound} 必须是有限数字" in error for error in errors)


def test_unsupported_mode_is_not_a_configuration_error():
    configured = provider(
        {
            "option": {
                "type": "integer",
                "default": 1,
                "mode_overrides": {"img2img": {"default": "bad"}},
            },
        },
        img2img=False,
    )
    configured.models[0].validate_parameters()
