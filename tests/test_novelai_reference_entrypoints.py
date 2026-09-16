"""Reference order and all-or-nothing limits at actual generation entrypoints."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from astrbot.api.message_components import Image
from astrbot_plugin_image_studio import main as plugin_main
from astrbot_plugin_image_studio.backend.config import HistorySettings, RuntimeSettings
from astrbot_plugin_image_studio.main import ImageStudioPlugin
from astrbot_plugin_image_studio.backend.models import GeneratedImage, ImageProvider
from astrbot_plugin_image_studio.backend.providers.novelai.protocol import (
    prepare_generation_payload,
)
from astrbot_plugin_image_studio.backend.generation.service import (
    ImageGenerationService,
)
from astrbot_plugin_image_studio.backend.gallery.store import GenerationStore
from astrbot_plugin_image_studio.tests.test_novelai_advanced import reference
from astrbot_plugin_image_studio.tests.test_service_and_tool import ToolEvent

V45 = "nai-diffusion-4-5-full"
V5 = "nai-diffusion-5-full"


class PayloadExecutor:
    def __init__(self):
        self.requests = []
        self.payloads = []

    async def generate(self, provider, request):
        self.requests.append(request)
        if provider.kind == "novelai_official":
            payload, _, _ = prepare_generation_payload(request, request.model)
            self.payloads.append(payload)
        return (GeneratedImage(reference().data, "image/png"),)


async def subject(tmp_path, *, model=V45, kind="novelai_official", tool_limit=None):
    configured = ImageProvider.from_mapping(
        {
            "id": "provider",
            "kind": kind,
            "base_url": "https://offline.invalid",
            "models": [
                {
                    "id": model,
                    "supports_img2img": True,
                    "max_reference_images": 8 if model == V45 else 2,
                    "tool": {
                        "enabled": True,
                        **({"max_reference_images": tool_limit} if tool_limit else {}),
                    },
                }
            ],
        }
    )
    settings = RuntimeSettings(
        True,
        (configured,),
        HistorySettings(False, 0, 0, False),
        0,
        default_tool_img2img_model_ref=f"provider:{model}",
        default_tool_text2img_model_ref=f"provider:{model}",
        default_page_img2img_model_ref=f"provider:{model}",
    )
    store = GenerationStore(tmp_path)
    await store.initialize()
    executor = PayloadExecutor()
    service = ImageGenerationService(settings=settings, executor=executor, store=store)
    plugin = object.__new__(ImageStudioPlugin)
    plugin._settings, plugin._service, plugin.store = settings, service, store
    return plugin, executor


@pytest.fixture
def local_references(monkeypatch):
    async def workspace(_event, _context):
        return None

    async def quoted(_event):
        return []

    async def convert(image):
        return image.file

    monkeypatch.setattr(plugin_main, "_event_workspace_root", workspace)
    monkeypatch.setattr(plugin_main, "extract_quoted_message_images", quoted)
    monkeypatch.setattr(Image, "convert_to_file_path", convert)


@pytest.mark.parametrize("kind", ["path", "asset_id"])
def test_tool_same_image_can_be_base_and_character(tmp_path, local_references, kind):
    async def run():
        plugin, executor = await subject(tmp_path)
        shared = reference()
        reads = []

        async def read(path, **_kwargs):
            reads.append(path)
            return shared

        plugin._service.reference_from_media_ref = read
        if kind == "asset_id":
            assets = await plugin.store.lease_agent_images(
                (GeneratedImage(shared.data, shared.mime_type),),
                scope_id=ToolEvent().unified_msg_origin,
                create_preview=False,
                preview_max_edge=768,
                preview_quality=80,
            )
            input_reference = {"asset_id": assets[0].asset_id}
        else:
            input_reference = {"path": "same.png"}
        event = ToolEvent()
        await plugin.image_studio_get_capabilities(event, mode="img2img")
        result = await plugin.image_studio_generate(
            event,
            prompt="one tree",
            mode="img2img",
            parameters={
                "reference_settings": [{"type": "base"}, {"type": "character"}],
            },
            references=[input_reference, dict(input_reference)],
        )
        assert not result.isError, result.content
        assert len(executor.requests) == 1
        assert [item.data for item in executor.requests[0].references] == [
            shared.data,
            shared.data,
        ]
        wire = executor.payloads[0]
        assert wire["action"] == "img2img"
        assert wire["parameters"]["image"]
        assert len(wire["parameters"]["director_reference_images"]) == 1
        assert len(reads) == (2 if kind == "path" else 0)

    asyncio.run(run())


@pytest.mark.parametrize("model,limit", [(V45, 8), (V5, 2)])
@pytest.mark.parametrize("mode", ["img2img", ""])
def test_tool_excess_explicit_references_fail_before_any_read(
    tmp_path, local_references, model, limit, mode
):
    async def run():
        plugin, executor = await subject(tmp_path, model=model)

        async def reject_read(*_args, **_kwargs):
            pytest.fail("Over-limit references were materialized")

        plugin._service.reference_from_media_ref = reject_read
        event = ToolEvent()
        await plugin.image_studio_get_capabilities(event, mode="img2img")
        result = await plugin.image_studio_generate(
            event,
            prompt="one tree",
            mode=mode,
            references=[{"path": "same.png"}] * (limit + 1),
        )
        assert result.isError
        assert f"最多允许 {limit}" in result.content[0].text
        assert not executor.requests

    asyncio.run(run())


def test_tool_reference_limit_uses_tool_cap_before_read(tmp_path, local_references):
    async def run():
        plugin, executor = await subject(tmp_path, tool_limit=1)
        event = ToolEvent()
        result = await plugin.image_studio_generate(
            event,
            prompt="one tree",
            mode="img2img",
            references=[{"path": "missing.png"}] * 2,
        )
        assert result.isError
        assert "最多允许 1" in result.content[0].text
        assert not executor.requests

    asyncio.run(run())


def test_auto_reference_discovery_deduplicates_but_detects_excess(
    tmp_path, local_references
):
    async def run():
        plugin, executor = await subject(tmp_path, tool_limit=1)
        images = {"one.png": reference("red"), "alias.png": reference("red")}

        async def read(path, **_kwargs):
            return images[path]

        plugin._service.reference_from_media_ref = read
        event = ToolEvent()
        event.get_messages = lambda: [Image(file=path) for path in images]
        resolved = await plugin._event_references(
            event, max_images=1, reject_excess=True
        )
        assert len(resolved) == 1
        images["two.png"] = reference("blue")
        result = await plugin.image_studio_generate(
            event, prompt="one tree", mode="img2img"
        )
        assert result.isError
        assert "最多允许 1" in result.content[0].text
        assert not executor.requests

    asyncio.run(run())


@pytest.mark.parametrize("model,limit", [(V45, 8), (V5, 2)])
def test_webui_excess_ids_are_rejected_before_loading_staging(
    tmp_path, monkeypatch, model, limit
):
    async def run():
        plugin, executor = await subject(tmp_path, model=model)

        def reject_read(_ids):
            pytest.fail("Over-limit staging IDs reached disk")

        plugin.store._load_staged_references_sync = reject_read

        async def body(**_kwargs):
            return {
                "mode": "img2img",
                "provider_id": "provider",
                "model_ref": f"provider:{model}",
                "prompt": "one tree",
                "reference_ids": ["missing"] * (limit + 1),
            }

        monkeypatch.setattr(plugin_main, "web_request", SimpleNamespace(json=body))
        result = await plugin._api_generate()
        assert result.status_code == 400
        assert f"最多允许 {limit}" in json.loads(result.body)["message"]
        assert not executor.requests

    asyncio.run(run())


def test_webui_keeps_duplicate_ids_and_missing_reference_is_fatal(tmp_path):
    async def run():
        plugin, executor = await subject(tmp_path)
        shared = reference()
        staged = await plugin.store.stage_reference(
            filename="same.png", content=shared.data, mime_type=shared.mime_type
        )
        kwargs = {"mode": "img2img", "model_ref": f"provider:{V45}"}
        loaded = await plugin._service.staged_references([staged["id"]] * 2, **kwargs)
        assert len(loaded) == 2
        assert loaded[0] == loaded[1]
        with pytest.raises(ValueError, match="已不存在"):
            await plugin._service.staged_references([staged["id"], "f" * 32], **kwargs)
        assert not executor.requests

    asyncio.run(run())


def test_other_providers_keep_existing_eight_reference_staging_cap(tmp_path):
    async def run():
        plugin, _executor = await subject(tmp_path, kind="custom_json")
        shared = reference()
        staged = await plugin.store.stage_reference(
            filename="same.png", content=shared.data, mime_type=shared.mime_type
        )
        loaded = await plugin._service.staged_references(
            [staged["id"]] * 8 + ["missing"],
            mode="img2img",
            model_ref=f"provider:{V45}",
        )
        assert len(loaded) == 8

    asyncio.run(run())
