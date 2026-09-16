from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace

import pytest

from astrbot_plugin_image_studio.backend.config import HistorySettings, RuntimeSettings
from astrbot_plugin_image_studio.main import ImageStudioPlugin
from astrbot_plugin_image_studio.backend.models import (
    GeneratedImage,
    GenerationResult,
    ImageProvider,
    WorkflowImageAsset,
)
from astrbot_plugin_image_studio.backend.generation.service import (
    ImageGenerationService,
)
from astrbot_plugin_image_studio.backend.gallery.store import GenerationStore


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9JZq4AAAAASUVORK5CYII="
)


def provider_with_tool(tool=None, *, supported=True):
    return ImageProvider.from_mapping(
        {
            "id": "negative-policy",
            "name": "Negative policy provider",
            "kind": "custom_json",
            "base_url": "https://example.invalid",
            "models": [
                {
                    "id": "paint",
                    "supports_text2img": True,
                    "supports_negative_prompt": supported,
                    "negative_prompt_default": "model negative",
                    "parameters": {
                        "quality": {"type": "text", "default": "high"},
                        "cfg": {"type": "number", "default": 0.3},
                    },
                    "tool": tool or {},
                }
            ],
        }
    )


def settings_for(provider):
    return RuntimeSettings(
        enable_llm_tool=True,
        providers=(provider,),
        history=HistorySettings(False, 0, 0, False),
        revision=1,
        default_page_text2img_model_ref="negative-policy:paint",
        default_tool_text2img_model_ref="negative-policy:paint",
    )


class ToolEvent:
    unified_msg_origin = "test:private:negative-policy-user"

    def __init__(self):
        self.extras = {}

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_messages(self):
        return []


class AssetStore:
    async def lease_agent_images(self, images, **_kwargs):
        return tuple(
            WorkflowImageAsset(
                asset_id=f"{index + 1:064x}",
                mime_type=image.mime_type,
                size_bytes=len(image.data),
                preview=GeneratedImage(b"preview", "image/webp"),
            )
            for index, image in enumerate(images)
        )


def invoke_tool(provider, parameters=None):
    captured = {}

    class CapturingService:
        async def generate(self, **kwargs):
            captured.update(kwargs)
            return GenerationResult(
                provider,
                SimpleNamespace(model="paint", mode="text2img"),
                (GeneratedImage(PNG, "image/png"),),
                5,
            )

    async def run():
        plugin = object.__new__(ImageStudioPlugin)
        plugin._settings = settings_for(provider)
        plugin._service = CapturingService()
        plugin.store = AssetStore()
        event = ToolEvent()
        capability_result = await plugin.image_studio_get_capabilities(
            event, query_type="default", mode="text2img"
        )
        assert not capability_result.isError
        capability = json.loads(capability_result.content[0].text)["models"][0]
        result = await plugin.image_studio_generate(
            event, prompt="one mountain", parameters=parameters
        )
        assert not result.isError
        return capability, captured

    return asyncio.run(run())


@pytest.mark.parametrize("legacy_exposed", [True, False])
@pytest.mark.parametrize("structured_exposed", [True, False])
def test_structured_negative_exposure_overrides_legacy_setting(
    legacy_exposed, structured_exposed
):
    provider = provider_with_tool(
        {
            "negative_prompt_exposed": legacy_exposed,
            "parameters": {"negative_prompt": {"exposed": structured_exposed}},
        }
    )
    model = provider.models[0]
    capability, captured = invoke_tool(provider, {"negative_prompt": "user negative"})

    assert model.llm_negative_prompt_enabled is structured_exposed
    assert (
        "negative_prompt" in model.llm_exposed_parameter_names
    ) is structured_exposed
    assert ("negative_prompt" in capability["parameters"]) is structured_exposed
    assert captured["negative_prompt"] == (
        "user negative" if structured_exposed else "model negative"
    )


@pytest.mark.parametrize("legacy_exposed", [True, False])
@pytest.mark.parametrize("policy", [None, {}, {"description": "custom negative"}])
def test_missing_structured_exposure_keeps_legacy_setting(legacy_exposed, policy):
    tool = {"negative_prompt_exposed": legacy_exposed}
    if policy is not None:
        tool["parameters"] = {"negative_prompt": policy}
    model = provider_with_tool(tool).models[0]

    assert model.llm_negative_prompt_enabled is legacy_exposed


@pytest.mark.parametrize("policy", [{}, {"exposed": True}, {"exposed": False}])
def test_only_negative_policy_does_not_hide_other_default_schema_fields(policy):
    provider = provider_with_tool({"parameters": {"negative_prompt": policy}})
    capability, _captured = invoke_tool(provider)

    assert {"quality", "cfg", "count"}.issubset(
        provider.models[0].llm_exposed_parameter_names
    )
    assert {"quality", "cfg", "count"}.issubset(capability["parameters"])


def test_other_parameter_policy_still_limits_exposure_when_negative_policy_present():
    provider = provider_with_tool(
        {
            "parameters": {
                "negative_prompt": {"exposed": True},
                "quality": {"exposed": True},
                "cfg": {"exposed": False},
            }
        }
    )
    capability, _captured = invoke_tool(provider)

    assert set(capability["parameters"]) == {"negative_prompt", "quality"}


@pytest.mark.parametrize("default_override", ["tool negative", ""])
def test_negative_description_and_override_are_disclosed_and_used(default_override):
    provider = provider_with_tool(
        {
            "parameters": {
                "negative_prompt": {
                    "exposed": True,
                    "description": "Only describe elements to exclude.",
                    "default_override": default_override,
                }
            }
        }
    )
    capability, captured = invoke_tool(provider)
    descriptor = capability["parameters"]["negative_prompt"]

    assert descriptor["type"] == "string"
    assert descriptor["description"] == "Only describe elements to exclude."
    assert descriptor["default"] == default_override
    assert captured["negative_prompt"] == default_override
    assert "negative_prompt" not in captured["parameters"]


def test_missing_negative_override_uses_model_default():
    capability, captured = invoke_tool(
        provider_with_tool({"parameters": {"negative_prompt": {"exposed": True}}})
    )

    assert capability["parameters"]["negative_prompt"]["default"] == "model negative"
    assert captured["negative_prompt"] == "model negative"


@pytest.mark.parametrize("user_value", ["user negative", ""])
def test_explicit_negative_input_takes_precedence_over_tool_default(user_value):
    provider = provider_with_tool(
        {
            "parameters": {
                "negative_prompt": {
                    "exposed": True,
                    "default_override": "tool negative",
                }
            }
        }
    )
    _capability, captured = invoke_tool(provider, {"negative_prompt": user_value})

    assert captured["negative_prompt"] == user_value


@pytest.mark.parametrize("default_override", ["tool negative", ""])
@pytest.mark.parametrize("user_value", ["discard this", {"not": "a string"}])
def test_unexposed_negative_input_is_discarded_but_trusted_override_is_used(
    default_override, user_value
):
    provider = provider_with_tool(
        {
            "parameters": {
                "negative_prompt": {
                    "exposed": False,
                    "default_override": default_override,
                }
            }
        }
    )
    capability, captured = invoke_tool(provider, {"negative_prompt": user_value})

    assert "negative_prompt" not in capability["parameters"]
    assert captured["negative_prompt"] == default_override
    assert "negative_prompt" not in captured["parameters"]


def test_unsupported_model_never_exposes_or_forwards_negative_prompt():
    provider = provider_with_tool(
        {
            "negative_prompt_exposed": True,
            "parameters": {
                "negative_prompt": {
                    "exposed": True,
                    "description": "Must not appear.",
                    "default_override": "must not be sent",
                }
            },
        },
        supported=False,
    )
    capability, captured = invoke_tool(provider, {"negative_prompt": "discard this"})

    assert not provider.models[0].llm_negative_prompt_enabled
    assert "negative_prompt" not in capability["parameters"]
    assert captured["negative_prompt"] == ""
    assert "negative_prompt" not in captured["parameters"]


@pytest.mark.parametrize("source", ["webui", "command"])
@pytest.mark.parametrize("negative_prompt", [None, "", "page input"])
def test_tool_negative_defaults_do_not_change_page_or_command_defaults(
    tmp_path, source, negative_prompt
):
    provider = provider_with_tool(
        {
            "parameters": {
                "negative_prompt": {
                    "exposed": False,
                    "default_override": "tool negative",
                }
            }
        }
    )
    requests = []

    class CapturingExecutor:
        async def generate(self, _provider, request):
            requests.append(request)
            return (GeneratedImage(PNG, "image/png"),)

    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        service = ImageGenerationService(
            settings=settings_for(provider), executor=CapturingExecutor(), store=store
        )
        await service.generate(
            mode="text2img",
            provider_id="",
            prompt="one mountain",
            negative_prompt=negative_prompt,
            source=source,
        )

    asyncio.run(run())

    expected = negative_prompt
    if expected is None:
        # The WebUI submits its displayed default; only commands fill an omitted value.
        expected = "model negative" if source == "command" else ""
    assert requests[0].negative_prompt == expected


@pytest.mark.parametrize("default_override", ["tool negative", ""])
def test_negative_parameter_policy_survives_public_configuration_roundtrip(
    default_override,
):
    provider = provider_with_tool(
        {
            "negative_prompt_exposed": False,
            "parameters": {
                "negative_prompt": {
                    "exposed": True,
                    "description": "Custom negative prompt guidance",
                    "default_override": default_override,
                }
            },
        }
    )
    reloaded = ImageProvider.from_mapping(provider.public_dict())
    model = reloaded.models[0]

    assert model.llm_negative_prompt_enabled
    assert model.tool["parameters"]["negative_prompt"] == {
        "exposed": True,
        "description": "Custom negative prompt guidance",
        "default_override": default_override,
    }
    assert model.negative_prompt_default == "model negative"
