from __future__ import annotations

import asyncio
import base64
import time
from types import SimpleNamespace

import mcp
from fastapi.responses import FileResponse

from astrbot_plugin_image_gen.config import HistorySettings, RuntimeSettings
from astrbot_plugin_image_gen.main import ImageStudioPlugin
from astrbot_plugin_image_gen.models import (
    GeneratedImage,
    GenerationRequest,
    GenerationResult,
    ImageProvider,
    ReferenceImage,
)
from astrbot_plugin_image_gen.service import ImageGenerationService
from astrbot_plugin_image_gen.storage import GenerationStore


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
        True,
        True,
        1,
        (provider,),
        "test-provider",
        "1024x1024",
        1,
        HistorySettings(False, 0, 0, False),
        0,
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
                            "guidance": {
                                "type": "number",
                                "request_key": "cfg_scale",
                            }
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
                True,
                True,
                1,
                (provider,),
                "custom",
                "1024x1024",
                1,
                HistorySettings(False, 0, 0, False),
                0,
            ),
            executor=CapturingExecutor(),
            store=store,
        )
        await service.generate(
            mode="text2img",
            provider_id="custom",
            model_ref="custom:draw-v2",
            prompt="a lake",
            parameters={"guidance": 6},
        )
        assert captured["request"].parameters["cfg_scale"] == 6

    asyncio.run(run())


def test_export_download_uses_standard_file_response(tmp_path) -> None:
    archive_path = tmp_path / "image-studio.zip"
    archive_path.write_bytes(b"PK\x03\x04test")
    plugin = object.__new__(ImageStudioPlugin)
    plugin._exports = {"export-1": (archive_path, time.time())}

    response = asyncio.run(plugin._api_download_export("export-1"))

    assert isinstance(response, FileResponse)
    assert response.media_type == "application/zip"
