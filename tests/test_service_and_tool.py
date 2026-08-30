from __future__ import annotations

import asyncio
import base64
import json
import time
from types import SimpleNamespace

import mcp
from astrbot.api.message_components import Image, Reply
from astrbot_plugin_image_studio.config import HistorySettings, RuntimeSettings
from astrbot_plugin_image_studio.main import (
    ImageStudioPlugin,
    _iter_event_images,
    _parse_command,
)
from astrbot_plugin_image_studio.models import (
    GeneratedImage,
    GenerationRequest,
    GenerationResult,
    ImageProvider,
    ReferenceImage,
)
from astrbot_plugin_image_studio.providers import ProviderExecutor
from astrbot_plugin_image_studio.service import (
    ImageGenerationService,
    _parameters_for_model,
    _size,
)
from astrbot_plugin_image_studio.storage import GenerationStore
from fastapi.responses import FileResponse

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9JZq4AAAAASUVORK5CYII="
)


def settings() -> RuntimeSettings:
    provider = ImageProvider.from_mapping(
        {
            "id": "test-provider",
            "name": "Test Provider",
            "kind": "openai_images",
            "base_url": "https://example.test",
            "model": "test-image",
        }
    )
    return RuntimeSettings(
        enable_llm_tool=True,
        providers=(provider,),
        default_provider_id="test-provider",
        default_size="1024x1024",
        default_count=1,
        history=HistorySettings(False, 0, 0, False),
        revision=0,
        default_text2img_model_ref="test-provider:test-image",
    )


class FakeExecutor:
    async def generate(self, provider, request):
        return (GeneratedImage(PNG, "image/png"),)


def test_service_generates_without_history(tmp_path) -> None:
    async def run() -> None:
        store = GenerationStore(tmp_path)
        await store.initialize()
        service = ImageGenerationService(
            settings=settings(), executor=FakeExecutor(), store=store
        )
        result = await service.generate(
            mode="text2img", provider_id="", prompt="one tree"
        )
        assert result.generation_id == ""
        assert result.images[0].data == PNG

    asyncio.run(run())


def test_llm_tool_returns_mcp_image_content() -> None:
    class FakeService:
        async def reference_from_safe_path(self, _path):
            return None

        async def generate(self, **kwargs):
            provider = settings().providers[0]
            request = SimpleNamespace(model=provider.model, mode="text2img")
            return GenerationResult(
                provider, request, (GeneratedImage(PNG, "image/png"),), 5
            )

    plugin = object.__new__(ImageStudioPlugin)
    plugin._settings = settings()
    plugin._service = FakeService()

    result = asyncio.run(
        plugin.image_gen_generate(SimpleNamespace(), prompt="one tree")
    )

    assert isinstance(result, mcp.types.CallToolResult)
    assert any(isinstance(item, mcp.types.ImageContent) for item in result.content)


def test_capabilities_only_lists_llm_enabled_models() -> None:
    provider = ImageProvider.from_mapping(
        {
            "id": "provider",
            "name": "Provider",
            "kind": "openai_images",
            "base_url": "https://example.test",
            "models": [
                {"id": "visible", "tool": {"enabled": True}},
                {"id": "hidden", "tool": {"enabled": False}},
            ],
        }
    )
    plugin = object.__new__(ImageStudioPlugin)
    plugin._settings = RuntimeSettings(
        enable_llm_tool=True,
        providers=(provider,),
        default_provider_id="provider",
        default_size="1024x1024",
        default_count=1,
        history=HistorySettings(False, 0, 0, False),
        revision=0,
    )

    result = asyncio.run(
        plugin.image_gen_get_capabilities(SimpleNamespace(), mode="text2img")
    )
    payload = json.loads(result.content[0].text)

    assert [item["model_ref"] for item in payload["models"]] == ["provider:visible"]


def test_llm_tool_guide_is_injected_once() -> None:
    plugin = object.__new__(ImageStudioPlugin)
    plugin._settings = settings()
    request = SimpleNamespace(system_prompt="base")

    asyncio.run(plugin.inject_image_tool_guide(SimpleNamespace(), request))
    first = request.system_prompt
    asyncio.run(plugin.inject_image_tool_guide(SimpleNamespace(), request))

    assert "image_gen_get_capabilities" in first
    assert request.system_prompt == first


def test_reproduction_keeps_available_parameters_without_reference(tmp_path) -> None:
    async def run() -> None:
        store = GenerationStore(tmp_path)
        await store.initialize()
        provider = settings().providers[0]
        generation_id = await store.record_success(
            provider=provider,
            request=GenerationRequest(
                mode="img2img",
                provider_id=provider.id,
                prompt="repaint the lake",
                size="1024x1024",
                references=(ReferenceImage("source", "source.png", PNG, "image/png"),),
            ),
            images=(GeneratedImage(PNG, "image/png"),),
            elapsed_ms=20,
            history=HistorySettings(True, 10, 10, False),
        )
        service = ImageGenerationService(
            settings=settings(), executor=FakeExecutor(), store=store
        )

        plan = await service.reproduction_plan(generation_id)

        assert plan["prompt"] == "repaint the lake"
        assert plan["mode"] == "img2img"
        assert plan["reference_available"] is False
        assert "历史参考图未保留" in plan["notice"]

    asyncio.run(run())


def test_service_drops_negative_prompt_for_unsupported_provider(tmp_path) -> None:
    async def run() -> None:
        store = GenerationStore(tmp_path)
        await store.initialize()
        captured = {}

        class CapturingExecutor:
            async def generate(self, _provider, request):
                captured["request"] = request
                return (GeneratedImage(PNG, "image/png"),)

        service = ImageGenerationService(
            settings=settings(), executor=CapturingExecutor(), store=store
        )
        await service.generate(
            mode="text2img", provider_id="", prompt="one tree", negative_prompt="no fog"
        )
        assert captured["request"].negative_prompt == ""

    asyncio.run(run())


def test_service_limits_concurrency_per_provider(tmp_path) -> None:
    async def run() -> None:
        providers = tuple(
            ImageProvider.from_mapping(
                {
                    "id": provider_id,
                    "name": provider_id,
                    "kind": "openai_images",
                    "base_url": "https://example.test",
                    "max_concurrent_generations": 1,
                    "models": [{"id": "image-model"}],
                }
            )
            for provider_id in ("one", "two")
        )
        active: dict[str, int] = {"one": 0, "two": 0}
        maximum: dict[str, int] = {"one": 0, "two": 0}
        total_active = 0
        maximum_total = 0

        class CapturingExecutor:
            async def generate(self, provider, _request):
                nonlocal total_active, maximum_total
                active[provider.id] += 1
                total_active += 1
                maximum[provider.id] = max(maximum[provider.id], active[provider.id])
                maximum_total = max(maximum_total, total_active)
                await asyncio.sleep(0.02)
                active[provider.id] -= 1
                total_active -= 1
                return (GeneratedImage(PNG, "image/png"),)

        store = GenerationStore(tmp_path)
        await store.initialize()
        service = ImageGenerationService(
            settings=RuntimeSettings(
                enable_llm_tool=True,
                providers=providers,
                default_provider_id="",
                default_size="1024x1024",
                default_count=1,
                history=HistorySettings(False, 0, 0, False),
                revision=0,
            ),
            executor=CapturingExecutor(),
            store=store,
        )
        await asyncio.gather(
            *(
                service.generate(
                    mode="text2img",
                    provider_id=provider_id,
                    model_ref=f"{provider_id}:image-model",
                    prompt=f"prompt {index}",
                )
                for provider_id in ("one", "one", "two", "two")
                for index in (1,)
            )
        )

        assert maximum == {"one": 1, "two": 1}
        assert maximum_total == 2

    asyncio.run(run())


def test_service_maps_model_schema_parameter_names(tmp_path) -> None:
    async def run() -> None:
        store = GenerationStore(tmp_path)
        await store.initialize()
        provider = ImageProvider.from_mapping(
            {
                "id": "custom",
                "name": "Custom",
                "kind": "custom_json",
                "base_url": "https://example.test",
                "models": [
                    {
                        "id": "draw-v2",
                        "parameters": {
                            "style": {
                                "type": "preset",
                                "default": "custom",
                                "ui_only": True,
                            },
                            "guidance": {
                                "type": "number",
                                "request_key": "cfg_scale",
                            },
                        },
                    }
                ],
            }
        )
        captured = {}

        class CapturingExecutor:
            async def generate(self, _provider, request):
                captured["request"] = request
                return (GeneratedImage(PNG, "image/png"),)

        service = ImageGenerationService(
            settings=RuntimeSettings(
                enable_llm_tool=True,
                providers=(provider,),
                default_provider_id="custom",
                default_size="1024x1024",
                default_count=1,
                history=HistorySettings(False, 0, 0, False),
                revision=0,
            ),
            executor=CapturingExecutor(),
            store=store,
        )
        await service.generate(
            mode="text2img",
            provider_id="custom",
            model_ref="custom:draw-v2",
            prompt="a lake",
            parameters={"style": "custom", "guidance": 6},
        )
        assert captured["request"].parameters["cfg_scale"] == 6
        assert "style" not in captured["request"].parameters

    asyncio.run(run())


def test_llm_parameters_apply_defaults_expand_presets_and_reject_hidden() -> None:
    model = ImageProvider.from_mapping(
        {
            "id": "nai",
            "name": "NAI",
            "kind": "nai_direct",
            "models": [
                {
                    "id": "nai-diffusion-5-full",
                    "parameters": {
                        "style": {
                            "type": "preset",
                            "default": "anime",
                            "target": "artist",
                            "ui_only": True,
                            "choices": [
                                {"value": "anime", "fill": "artist:anime"},
                                {"value": "custom", "fill": ""},
                            ],
                        },
                        "artist": {
                            "type": "textarea",
                            "default": "",
                            "request_key": "artist",
                        },
                        "steps": {
                            "type": "number",
                            "default": 24,
                            "min": 1,
                            "max": 28,
                        },
                    },
                    "tool": {
                        "parameters": {
                            "style": {"exposed": True},
                            "steps": {"exposed": False},
                        }
                    },
                }
            ],
        }
    ).models[0]

    assert (
        _parameters_for_model({}, model, source="llm_tool")["artist"] == "artist:anime"
    )
    assert (
        _parameters_for_model({"style": "custom"}, model, source="llm_tool")["artist"]
        == ""
    )
    try:
        _parameters_for_model({"steps": 25}, model, source="llm_tool")
    except ValueError as exc:
        assert "未向 LLM 工具开放" in str(exc)
    else:
        raise AssertionError("hidden tool parameter should be rejected")


def test_command_parser_accepts_multiple_references_and_tracks_explicit_mode() -> None:
    parsed = _parse_command(
        "/img repaint --ref /tmp/one.png --ref=/tmp/two.png --mode img2img"
    )

    assert parsed["reference_paths"] == ["/tmp/one.png", "/tmp/two.png"]
    assert parsed["mode"] == "img2img"
    assert parsed["mode_explicit"] is True


def test_event_image_iterator_reads_current_and_quoted_images() -> None:
    current = Image(file="current.png")
    quoted = Image(file="quoted.png")

    images = list(_iter_event_images([current, Reply(id="1", chain=[quoted])]))

    assert images == [current, quoted]


def test_nai_model_discovery_uses_builtin_capabilities() -> None:
    provider = ImageProvider.from_mapping(
        {
            "id": "nai",
            "name": "NAI",
            "kind": "nai_direct",
            "base_url": "https://nai.sta1n.cn",
        }
    )

    models = asyncio.run(ProviderExecutor(None).discover_models(provider))

    assert [item["id"] for item in models] == [
        "nai-diffusion-4-5-full",
        "nai-diffusion-5-full",
    ]
    assert all(item["capability_source"] == "builtin" for item in models)
    assert all(item["supports_img2img"] is False for item in models)


def test_provider_test_uses_configured_nai_model_defaults() -> None:
    provider = ImageProvider.from_mapping(
        {
            "id": "nai",
            "name": "NAI third party",
            "kind": "nai_direct",
            "api_key": "to-user-id",
            "models": [
                {
                    "id": "nai-diffusion-4-5-full",
                    "parameters": {
                        "style": {
                            "type": "preset",
                            "default": "vertical",
                            "ui_only": True,
                        },
                        "artist": {
                            "type": "text",
                            "default": "artist:test",
                            "request_key": "artist",
                        },
                        "size": {
                            "type": "select",
                            "default": "竖图",
                            "request_key": "size",
                        },
                        "steps": {
                            "type": "number",
                            "default": 24,
                            "request_key": "steps",
                        },
                    },
                }
            ],
        }
    )
    plugin = object.__new__(ImageStudioPlugin)

    request = plugin._test_request(provider, "nai-diffusion-4-5-full")

    assert request.size == "竖图"
    assert "bad anatomy" in request.negative_prompt
    assert request.parameters == {"artist": "artist:test", "steps": 24}
    assert _size("4K横图", "nai_direct") == "4K横图"


def test_export_download_uses_standard_file_response(tmp_path) -> None:
    archive_path = tmp_path / "image-studio.zip"
    archive_path.write_bytes(b"PK\x03\x04test")
    plugin = object.__new__(ImageStudioPlugin)
    plugin._exports = {"export-1": (archive_path, time.time())}

    response = asyncio.run(plugin._api_download_export("export-1"))

    assert isinstance(response, FileResponse)
    assert response.media_type == "application/zip"
    assert "export-1" not in plugin._exports
    assert archive_path.is_file()
    assert response.background is not None
    asyncio.run(response.background())
    assert not archive_path.exists()
