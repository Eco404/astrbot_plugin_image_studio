"""Mode contracts stay consistent across execution, discovery and reproduction."""

from __future__ import annotations

import asyncio
import copy
from dataclasses import replace

import pytest

from astrbot_plugin_image_studio.backend.gallery.store import GenerationStore
from astrbot_plugin_image_studio.backend.generation.service import (
    ImageGenerationService,
    _parameters_for_model,
)
from astrbot_plugin_image_studio.backend.models import (
    ImageProvider,
    ReferenceImage,
    resolve_mode_parameters,
)
from astrbot_plugin_image_studio.backend.parameters.exchange import resolve_parameters
from astrbot_plugin_image_studio.backend.tools.capabilities import query_capabilities
from astrbot_plugin_image_studio.tests.backend.config.test_parameter_policy import (
    CapturingExecutor,
    policy_provider,
    policy_settings,
    studio_content,
)


def dual_provider(parameters, tool=None):
    provider = policy_provider(parameters, tool=tool)
    return replace(provider, models=(replace(provider.models[0], img2img=True),))


def contract(provider, mode=""):
    return query_capabilities(
        policy_settings(provider),
        query_type="model",
        model_refs=["policy:paint"],
        mode=mode,
    )["models"][0]


@pytest.mark.parametrize("mode", ["text2img", "img2img"])
@pytest.mark.parametrize("source", ["webui", "command", "llm_tool"])
def test_disjoint_descriptors_with_same_wire_key_keep_current_default(
    tmp_path, mode, source
):
    provider = dual_provider(
        {
            "text_body": {
                "type": "json",
                "default": {"format": "text"},
                "request_key": "extra_body",
                "modes": ["text2img"],
            },
            "edit_body": {
                "type": "json",
                "default": {"format": "edit"},
                "request_key": "extra_body",
                "modes": ["img2img"],
            },
        }
    )

    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        executor = CapturingExecutor()
        service = ImageGenerationService(
            settings=policy_settings(provider), executor=executor, store=store
        )
        inactive = "edit_body" if mode == "text2img" else "text_body"
        await service.generate(
            mode=mode,
            provider_id="policy",
            model_ref="policy:paint",
            prompt="a mountain",
            source=source,
            parameters={inactive: "invalid for the other mode"},
            references=(
                ReferenceImage("ref", "ref.png", executor.image.data, "image/png"),
            )
            if mode == "img2img"
            else (),
        )
        assert executor.requests[0].parameters == {
            "extra_body": {"format": "text" if mode == "text2img" else "edit"}
        }

    asyncio.run(run())


def test_mode_overrides_replace_properties_without_mutating_shared_schema():
    schema = {
        "strength": {
            "type": "number",
            "default": 1,
            "modes": ["img2img"],
            "mode_overrides": {"text2img": {"modes": ["text2img"], "default": 2}},
        },
        "setting": {
            "type": "integer",
            "default": 3,
            "mode_overrides": {"img2img": {"type": "json", "default": {"weight": 0.5}}},
        },
        "optional": {
            "default": "inherited",
            "mode_overrides": {"img2img": {"default": None, "nullable": True}},
        },
    }
    original = copy.deepcopy(schema)
    text = resolve_mode_parameters(schema, "text2img")
    edit = resolve_mode_parameters(schema, "img2img")
    assert "strength" not in text
    assert text["setting"]["default"] == 3
    assert edit["optional"]["default"] is None
    edit["setting"]["default"]["weight"] = 999
    assert schema == original
    assert all("mode_overrides" not in item for item in text.values())
    model = dual_provider(schema).models[0]
    assert _parameters_for_model({"setting": {"weight": 1}}, model, mode="img2img")[
        "setting"
    ] == {"weight": 1}
    with pytest.raises(ValueError):
        _parameters_for_model({"setting": {"weight": 1}}, model, mode="text2img")


def test_model_and_tool_mode_defaults_share_discovery_execution_contract():
    provider = dual_provider(
        {
            "quality": {
                "type": "select",
                "default": "standard",
                "choices": ["standard", "high"],
                "mode_overrides": {
                    "img2img": {"default": "precise", "choices": ["precise", "fast"]}
                },
            },
            "steps": {
                "type": "integer",
                "default": 12,
                "mode_overrides": {"img2img": {"default": 30}},
            },
        },
        tool={
            "parameters": {
                "quality": {"exposed": True},
                "steps": {
                    "exposed": True,
                    "default_override": 8,
                    "mode_overrides": {"img2img": {"default_override": 18}},
                },
            }
        },
    )
    for mode, quality, steps in [
        ("text2img", "standard", 8),
        ("img2img", "precise", 18),
    ]:
        parameters = contract(provider, mode)["parameters"]
        resolved = _parameters_for_model(
            {}, provider.models[0], source="llm_tool", mode=mode
        )
        assert parameters["quality"]["default"] == resolved["quality"] == quality
        assert parameters["steps"]["default"] == resolved["steps"] == steps
        assert "mode_overrides" not in parameters["quality"]
    combined = contract(provider)
    quality = combined["parameters"]["quality"]
    assert "default" not in quality and "choices" not in quality
    assert quality["type"] == "select"
    assert quality["mode_overrides"]["img2img"]["default"] == "precise"
    assert quality["mode_overrides"]["text2img"]["choices"] == [
        {"value": "standard"},
        {"value": "high"},
    ]
    assert "parameter_mode_guidance" in combined


def test_reproduction_uses_current_mode_defaults_and_validation():
    provider = dual_provider(
        {
            "quality": {
                "type": "select",
                "default": "standard",
                "choices": ["standard"],
                "mode_overrides": {
                    "img2img": {"default": "precise", "choices": ["precise", "fast"]}
                },
            },
            "text_only": {"default": "unused", "modes": ["text2img"]},
            "count": {"default": 1, "mode_overrides": {"img2img": {"default": 2}}},
        }
    )
    result = resolve_parameters(
        studio_content({"quality": "fast"}, mode="img2img"),
        policy_settings(provider),
        "policy:paint",
        for_reproduction=True,
    )
    assert result["draft"]["parameters"]["quality"] == "fast"
    assert result["draft"]["count"] == 2
    assert "text_only" not in result["draft"]["parameters"]
    assert "quality" not in result["unmapped"]


def test_request_templates_persist_and_fall_back_to_legacy_template():
    provider = ImageProvider.from_mapping(
        {
            "kind": "custom_json",
            "request_template": '{"shared":true}',
            "request_templates": {
                "text2img": "",
                "img2img": '{"edit":true}',
                "unknown": "ignored",
            },
        }
    )
    loaded = ImageProvider.from_mapping(provider.public_dict())
    assert loaded.request_template_for_mode("text2img") == '{"shared":true}'
    assert loaded.request_template_for_mode("img2img") == '{"edit":true}'
    assert "unknown" not in loaded.request_templates


def test_mode_specific_history_policy_does_not_hide_other_modes_wire_value(tmp_path):
    provider = dual_provider(
        {
            "text_seed": {
                "type": "integer",
                "default": 12,
                "request_key": "seed",
                "modes": ["text2img"],
                "record_in_history": True,
            },
            "edit_seed": {
                "type": "integer",
                "default": 42,
                "request_key": "seed",
                "modes": ["img2img"],
                "record_in_history": False,
            },
        }
    )

    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        executor = CapturingExecutor()
        service = ImageGenerationService(
            settings=policy_settings(provider), executor=executor, store=store
        )
        result = await service.generate(
            mode="text2img", provider_id="policy", prompt="a mountain"
        )
        detail = await store.generation_detail(
            result.generation_id, include_assets=False
        )
        assert detail["parameters"]["parameters"]["seed"] == 12

    asyncio.run(run())
