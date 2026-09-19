from __future__ import annotations

import asyncio
import copy
import io
import json
from dataclasses import replace

import httpx
import pytest
from astrbot_plugin_image_studio.backend.config import (
    HistorySettings,
    RuntimeSettings,
    normalize_webui_settings,
)
from astrbot_plugin_image_studio.backend.models import GeneratedImage, ImageProvider
from astrbot_plugin_image_studio.backend.parameters.exchange import (
    export_parameters,
    resolve_parameters,
)
from astrbot_plugin_image_studio.backend.providers.executor import (
    ProviderPartialResponseError,
)
from astrbot_plugin_image_studio.backend.generation.service import (
    ImageGenerationService,
)
from astrbot_plugin_image_studio.backend.gallery.store import GenerationStore
from PIL import Image, PngImagePlugin

MODEL = "nai-diffusion-4-5-full"


def provider(identifier="official", token="pst-offline-token", **model):
    return ImageProvider.from_mapping(
        {
            "id": identifier,
            "name": identifier,
            "kind": "novelai_official",
            "api_key": token,
            "max_concurrent_generations": 8,
            "models": [
                {
                    "id": MODEL,
                    "supports_img2img": True,
                    "max_reference_images": 8,
                    **model,
                }
            ],
        }
    )


def settings(*providers):
    return RuntimeSettings(True, providers, HistorySettings(True, 100, 100, True), 0)


def image(seed=77, **effective):
    data = io.BytesIO()
    info = PngImagePlugin.PngInfo()
    info.add_text("Software", "NovelAI")
    info.add_text(
        "Comment",
        json.dumps({"seed": seed, "steps": 28, "scale": 5, "prompt": "landscape"}),
    )
    Image.new("RGB", (64, 64), (seed % 255, 40, 90)).save(
        data, format="PNG", pnginfo=info
    )
    return GeneratedImage(data.getvalue(), "image/png", {"seed": seed, **effective}, 0)


class Executor:
    def __init__(self, outputs=None):
        self.calls = []
        self.active = 0
        self.peak = 0
        self.outputs = outputs

    async def generate(self, configured, request):
        self.calls.append((configured, request))
        number = len(self.calls)
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.01)
            return self.outputs or (
                image(number, size=request.size, count=request.count),
            )
        finally:
            self.active -= 1


async def generate(service, identifier="official", **kwargs):
    return await service.generate(
        mode="text2img", provider_id=identifier, prompt="landscape", **kwargs
    )


def test_official_config_defaults_and_img2img_capability_are_bounded():
    normalized, errors = normalize_webui_settings(
        {
            "providers": [
                {
                    "id": "official",
                    "name": "NovelAI",
                    "kind": "novelai_official",
                    "models": [{"id": MODEL}],
                }
            ]
        }
    )
    assert errors == []
    result = normalized["providers"][0]
    assert result["base_url"] == "https://image.novelai.net"
    assert result["generate_path"] == result["edit_path"] == "/ai/generate-image"
    assert result["max_concurrent_generations"] == 1
    model = result["models"][0]
    assert model["supports_img2img"] is True
    assert model["supports_negative_prompt"] is True
    assert model["native_batch_size"] == 1
    assert model["max_concurrent_requests"] == 8
    assert model["parameters"]["count"]["default"] == 1
    assert model["parameters"]["seed"]["default"] == -1
    assert model["parameters"]["strength"]["modes"] == ["img2img"]
    parsed_default = ImageProvider.from_mapping(
        {"id": "official", "kind": "novelai_official", "models": [{"id": MODEL}]}
    )
    assert parsed_default.models[0].img2img is True
    parsed_disabled = ImageProvider.from_mapping(
        {
            "id": "official",
            "kind": "novelai_official",
            "models": [{"id": MODEL, "supports_img2img": False}],
        }
    )
    assert parsed_disabled.models[0].img2img is False
    enabled = provider()
    assert enabled.models[0].img2img
    assert (
        enabled.models[0].max_reference_images
        == enabled.models[0].llm_max_reference_images
        == 8
    )


@pytest.mark.parametrize("source", ["webui", "command", "llm_tool"])
def test_generation_records_each_actual_seed_and_reproduces_selected_image(
    tmp_path, source
):
    async def run():
        p = provider()
        current = settings(p)
        store = GenerationStore(tmp_path)
        await store.initialize()
        executor = Executor()
        service = ImageGenerationService(
            settings=current, executor=executor, store=store
        )
        result = await generate(
            service, source=source, model_ref=f"official:{MODEL}", count=2
        )
        assert len(result.images) == 2
        assert all(
            "strength" not in request.parameters and "noise" not in request.parameters
            for _, request in executor.calls
        )
        detail = await store.generation_detail(result.generation_id)
        assert detail["generation_engine"] == "novelai"
        assert detail["provider_kind"] == "novelai_official"
        for index, item in enumerate(detail["images"], 1):
            exported = export_parameters(detail, item["id"])
            data = json.loads(exported["content"])["data"]
            assert data["parameters"]["seed"] == index
            draft = resolve_parameters(
                exported["content"], current, for_reproduction=True
            )["draft"]
            assert draft["model_ref"] == f"official:{MODEL}"
            assert draft["parameters"]["seed"] == index
            assert draft["count"] == 1
            assert draft["native_batch_size"] == 1
            assert draft["max_concurrent_requests"] == 8
        assert "pst-offline-token" not in json.dumps(detail)

    asyncio.run(run())


def test_actual_parameters_cannot_bypass_recording_or_current_refill_policy(tmp_path):
    async def run():
        p = provider()
        schema = copy.deepcopy(p.models[0].parameters)
        schema["seed"]["record_in_history"] = False
        schema["scale"]["record_in_history"] = False
        schema["scale"]["default"] = 7
        p = replace(p, models=(replace(p.models[0], parameters=schema),))
        store = GenerationStore(tmp_path)
        await store.initialize()
        service = ImageGenerationService(
            settings=settings(p),
            executor=Executor((image(77, scale=11, steps=36),)),
            store=store,
        )
        result = await generate(service)
        detail = await store.generation_detail(result.generation_id)
        effective = detail["images"][0]["supplemental"]["effective_request"][
            "parameters"
        ]
        assert "seed" not in effective and "scale" not in effective
        exported = export_parameters(detail)
        assert "seed" not in json.loads(exported["content"])["data"]["parameters"]
        schema["steps"]["webui_visible"] = False
        p = replace(p, models=(replace(p.models[0], parameters=schema),))
        draft = resolve_parameters(
            exported["content"], settings(p), for_reproduction=True
        )["draft"]
        assert draft["parameters"]["seed"] == -1
        assert draft["parameters"]["scale"] == 7
        assert draft["parameters"]["steps"] == 23

    asyncio.run(run())


@pytest.mark.parametrize("same_token,expected_peak", [(True, 1), (False, 2)])
def test_account_limit_shared_across_provider_aliases(
    tmp_path, same_token, expected_peak
):
    async def run():
        providers = (
            provider("a"),
            provider("b", "pst-offline-token" if same_token else "pst-another-token"),
        )
        store = GenerationStore(tmp_path)
        await store.initialize()
        executor = Executor()
        service = ImageGenerationService(
            settings=settings(*providers), executor=executor, store=store
        )
        await asyncio.gather(
            generate(service, "a", count=2), generate(service, "b", count=2)
        )
        assert len(executor.calls) == 4
        assert executor.peak == expected_peak

    asyncio.run(run())


def test_partial_response_keeps_successful_images_and_precise_warning(tmp_path):
    class PartialExecutor:
        async def generate(self, configured, request):
            raise ProviderPartialResponseError((image(91),), ((2, "图片Base64无效"),))

    async def run():
        p = provider(native_batch_size=2)
        store = GenerationStore(tmp_path)
        await store.initialize()
        service = ImageGenerationService(
            settings=settings(p), executor=PartialExecutor(), store=store
        )
        result = await generate(service, count=2)
        assert len(result.images) == 1
        assert "2" in result.warning and "Base64" in result.warning
        assert "涉及目标图片 1 张" in result.warning
        detail = await store.generation_detail(result.generation_id)
        assert len(detail["images"]) == 1
        assert (
            detail["images"][0]["supplemental"]["effective_request"]["parameters"][
                "seed"
            ]
            == 91
        )

    asyncio.run(run())


def test_paste_novelai_and_third_party_parameters_matches_official_model():
    current = settings(provider())
    raw = {
        "model": MODEL,
        "prompt": "mountain",
        "uc": "text",
        "steps": 24,
        "scale": 6,
        "cfg_rescale": 0.3,
        "seed": 33,
        "noise_schedule": "karras",
        "width": 832,
        "height": 1216,
    }
    result = resolve_parameters(json.dumps(raw), current)
    assert result["draft"]["model_ref"] == f"official:{MODEL}"
    assert result["draft"]["size"] == "832x1216"
    assert result["draft"]["parameters"]["scale"] == 6
    assert result["draft"]["parameters"]["cfg_rescale"] == 0.3
    proxy = resolve_parameters(
        json.dumps(
            {
                "tag": "mountain",
                "artist": "artist:example",
                "model": MODEL,
                "cfg": 0.2,
                "scale": 4,
                "size": "竖图",
            }
        ),
        current,
    )
    assert proxy["draft"]["prompt"] == "artist:example\nmountain"
    assert proxy["draft"]["parameters"]["cfg_rescale"] == 0.2
    assert proxy["draft"]["parameters"]["scale"] == 4
    assert proxy["draft"]["size"] == "832x1216"
    official_wire = resolve_parameters(
        json.dumps(
            {
                "input": "mountain",
                "model": MODEL,
                "action": "generate",
                "parameters": {
                    "seed": 44,
                    "steps": 20,
                    "scale": 6,
                    "cfg_rescale": 0.2,
                    "width": 832,
                    "height": 1216,
                    "image_format": "webp",
                },
            }
        ),
        current,
    )
    assert official_wire["draft"]["model_ref"] == f"official:{MODEL}"
    assert official_wire["draft"]["parameters"]["seed"] == 44
    assert official_wire["draft"]["parameters"]["image_format"] == "webp"


@pytest.mark.parametrize("subscribed", [True, False])
def test_official_quota_api_uses_saved_provider_and_returns_no_credentials(
    tmp_path, subscribed
):
    from astrbot_plugin_image_studio.tests.support.webui_harness import create_app

    async def run():
        app = await create_app(tmp_path, seed=False)
        plugin = app.state.plugin
        plugin._settings = settings(provider())
        observed = []

        async def quota(configured):
            observed.append(configured)
            return {
                "kind": "novelai_official",
                "subscription_active": subscribed,
                "tier": 3,
                "remaining": 123,
                "usage": None,
            }

        plugin._service.executor.fetch_quota = quota
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            result = await client.get(
                "/astrbot_plugin_image_studio/studio/provider-quota",
                params={"provider_id": "official", "api_key": "ignored"},
            )
        assert result.status_code == 200
        assert result.json()["remaining"] == 123
        assert result.json()["subscription_active"] is subscribed
        assert "enabled" not in result.json()
        assert result.headers["cache-control"] == "no-store"
        assert observed[0].api_key == "pst-offline-token"
        assert "pst-offline-token" not in result.text

    asyncio.run(run())
