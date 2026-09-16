from __future__ import annotations

import asyncio
import base64

import pytest

from astrbot_plugin_image_studio.backend.config import HistorySettings, RuntimeSettings
from astrbot_plugin_image_studio.backend.models import (
    GeneratedImage,
    ImageProvider,
    ReferenceImage,
)
from astrbot_plugin_image_studio.backend.generation.service import (
    ImageGenerationService,
)
from astrbot_plugin_image_studio.backend.gallery.store import GenerationStore

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9JZq4AAAAASUVORK5CYII="
)


def make_provider(model_fields, *, legacy=False, kind="custom_json"):
    fields = {
        "id": "provider",
        "name": "Provider",
        "kind": kind,
        "base_url": "https://example.test",
    }
    if legacy:
        fields.update({"model": "image-model", **model_fields})
    else:
        fields["models"] = [{"id": "image-model", **model_fields}]
    return ImageProvider.from_mapping(fields)


def make_settings(provider):
    return RuntimeSettings(
        enable_llm_tool=True,
        providers=(provider,),
        history=HistorySettings(False, 0, 0, False),
        revision=0,
        default_page_img2img_model_ref="provider:image-model",
        default_tool_img2img_model_ref="provider:image-model",
    )


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("supports", [False, True])
@pytest.mark.parametrize(
    ("model_limit", "tool_limit", "expected_model", "expected_tool"),
    [
        (None, None, 1, 1),
        (0, 0, 1, 1),
        (-3, -2, 1, 1),
        (4, None, 4, 4),
        (4, 0, 4, 1),
        (4, -1, 4, 1),
        (4, 2, 4, 2),
        (2, 4, 2, 2),
        (20, 20, 8, 8),
    ],
)
def test_reference_limits_are_positive_configuration_not_mode_switches(
    legacy, supports, model_limit, tool_limit, expected_model, expected_tool
):
    fields = {"supports_img2img": supports}
    if model_limit is not None:
        fields["max_reference_images"] = model_limit
    if tool_limit is not None:
        fields["tool"] = {"max_reference_images": tool_limit}
    provider = make_provider(fields, legacy=legacy)
    model = provider.models[0]

    assert model.max_reference_images == expected_model
    assert model.tool["max_reference_images"] == expected_tool
    assert model.llm_max_reference_images == (expected_tool if supports else 0)
    assert model.supports("img2img") is supports
    assert provider.capabilities.supports("img2img") is supports
    assert provider.capabilities.max_reference_images == (
        expected_model if supports else 0
    )
    assert bool(make_settings(provider).models_for_mode("img2img")) is supports


def test_reference_caps_survive_disabled_support_public_roundtrip():
    original = make_provider(
        {
            "supports_img2img": True,
            "max_reference_images": 7,
            "tool": {"max_reference_images": 3},
        }
    ).models[0]
    disabled_fields = {**original.public_dict(), "supports_img2img": False}
    disabled = make_provider(disabled_fields).models[0]

    assert disabled.max_reference_images == 7
    assert disabled.tool["max_reference_images"] == 3
    assert disabled.llm_max_reference_images == 0

    restored = make_provider(
        {**disabled.public_dict(), "supports_img2img": True}
    ).models[0]
    assert restored.max_reference_images == 7
    assert restored.llm_max_reference_images == 3
    assert restored.supports("img2img")


def test_unknown_model_capability_preserves_explicit_support_choice():
    enabled = make_provider(
        {"supports_img2img": True, "capability_source": "unknown"}
    ).models[0]
    unspecified = make_provider({"capability_source": "unknown"}).models[0]

    assert enabled.supports("img2img")
    assert enabled.max_reference_images == 1
    assert enabled.llm_max_reference_images == 1
    assert not unspecified.supports("img2img")
    assert unspecified.max_reference_images == 1


def test_nai_reference_limits_do_not_enable_unsupported_img2img():
    model = make_provider(
        {
            "supports_img2img": True,
            "max_reference_images": 7,
            "tool": {"max_reference_images": 3},
        },
        kind="nai_direct",
    ).models[0]

    assert model.max_reference_images == 7
    assert model.tool["max_reference_images"] == 3
    assert not model.supports("img2img")
    assert model.llm_max_reference_images == 0


def test_provider_reference_cap_ignores_disabled_models_preserved_limits():
    provider = ImageProvider.from_mapping(
        {
            "id": "provider",
            "name": "Provider",
            "kind": "custom_json",
            "base_url": "https://example.test",
            "models": [
                {
                    "id": "enabled",
                    "supports_img2img": True,
                    "max_reference_images": 2,
                },
                {
                    "id": "disabled",
                    "supports_img2img": False,
                    "max_reference_images": 8,
                },
            ],
        }
    )

    assert provider.get_model("disabled").max_reference_images == 8
    assert provider.capabilities.max_reference_images == 2
    assert provider.capabilities.supports("img2img")


@pytest.mark.parametrize("source", ["webui", "command", "llm_tool"])
def test_disabled_support_blocks_generation_despite_positive_caps(tmp_path, source):
    class RejectExecutor:
        async def generate(self, _provider, _request):
            pytest.fail("Disabled image-to-image model reached the provider")

    async def run():
        provider = make_provider(
            {
                "supports_img2img": False,
                "max_reference_images": 8,
                "tool": {"enabled": True, "max_reference_images": 8},
            }
        )
        store = GenerationStore(tmp_path)
        await store.initialize()
        service = ImageGenerationService(
            settings=make_settings(provider), executor=RejectExecutor(), store=store
        )
        with pytest.raises(ValueError, match="不支持当前生图模式"):
            await service.generate(
                mode="img2img",
                provider_id="provider",
                model_ref="provider:image-model",
                prompt="one tree",
                references=(ReferenceImage("ref", "ref.png", PNG, "image/png"),),
                source=source,
            )

    asyncio.run(run())


@pytest.mark.parametrize("source", ["webui", "command", "llm_tool"])
def test_enabled_legacy_zero_limits_allow_img2img_and_truncate_to_one(tmp_path, source):
    captured = []

    class CaptureExecutor:
        async def generate(self, _provider, request):
            captured.append(request)
            return (GeneratedImage(PNG, "image/png"),)

    async def run():
        provider = make_provider(
            {
                "supports_img2img": True,
                "max_reference_images": 0,
                "tool": {"enabled": True, "max_reference_images": 0},
            }
        )
        store = GenerationStore(tmp_path)
        await store.initialize()
        service = ImageGenerationService(
            settings=make_settings(provider), executor=CaptureExecutor(), store=store
        )
        references = tuple(
            ReferenceImage(f"ref-{index}", f"ref-{index}.png", PNG, "image/png")
            for index in range(3)
        )
        result = await service.generate(
            mode="img2img",
            provider_id="provider",
            model_ref="provider:image-model",
            prompt="one tree",
            references=references,
            source=source,
        )
        assert result.images[0].data == PNG
        assert captured[0].references == references[:1]

    asyncio.run(run())
