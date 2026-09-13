from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import mcp
import pytest
from astrbot.api.message_components import Image, Reply
from astrbot.core.provider.register import llm_tools
from astrbot_plugin_image_studio.config import HistorySettings, RuntimeSettings
from astrbot_plugin_image_studio.main import (
    AGENT_WORKFLOW_PROMPT_MARKER,
    CAPABILITY_QUERY_EXTRA_KEY,
    IMAGE_WORKFLOW_CONTINUATION_MARKER,
    IMAGE_WORKFLOW_STATE_EXTRA_KEY,
    ImageStudioPlugin,
    _invocation_source,
    _iter_event_images,
    _llm_parameter_descriptor,
    _parse_command,
    _provider_request_image_refs,
)
from astrbot_plugin_image_studio.models import (
    GeneratedImage,
    GenerationRequest,
    GenerationResult,
    ImageProvider,
    ReferenceImage,
    WorkflowImageAsset,
    WorkflowImageLoadResult,
)
from astrbot_plugin_image_studio.providers import ProviderError, ProviderExecutor
from astrbot_plugin_image_studio.service import (
    ImageGenerationService,
    _control_parameter_value,
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
        history=HistorySettings(False, 0, 0, False),
        revision=0,
        default_page_text2img_model_ref="test-provider:test-image",
        default_tool_text2img_model_ref="test-provider:test-image",
    )


class FakeExecutor:
    async def generate(self, provider, request):
        return (GeneratedImage(PNG, "image/png"),)


class FakeAgentAssetStore:
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

    async def load_workflow_image(self, asset_id, **_kwargs):
        if asset_id != f"{1:064x}":
            return None
        return (
            GeneratedImage(PNG, "image/png"),
            "/AstrBot/data/temp/image-studio-original-1.png",
        )

    async def load_workflow_image_detailed(self, asset_id, **kwargs):
        loaded = await self.load_workflow_image(asset_id, **kwargs)
        if loaded is None:
            return WorkflowImageLoadResult(asset_id, "not_found")
        image, internal_path = loaded
        return WorkflowImageLoadResult(
            asset_id,
            "ok",
            image=image,
            internal_path=internal_path,
        )


class ToolEvent:
    def __init__(self) -> None:
        self._extras = {}
        self.unified_msg_origin = "test:private:user-1"
        self.sent = []

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value) -> None:
        self._extras[key] = value

    def get_messages(self):
        return []

    async def send(self, message):
        self.sent.append(message)


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


def test_page_and_tool_defaults_use_each_models_parameter_defaults(tmp_path) -> None:
    async def run() -> None:
        provider = ImageProvider.from_mapping(
            {
                "id": "provider",
                "name": "Provider",
                "kind": "openai_images",
                "base_url": "https://example.test",
                "models": [
                    {
                        "id": "page-model",
                        "parameters": {
                            "size": {
                                "type": "select",
                                "default": "1024x1536",
                                "choices": ["1024x1536"],
                            },
                            "count": {"type": "number", "default": 2},
                        },
                    },
                    {
                        "id": "tool-model",
                        "parameters": {
                            "size": {
                                "type": "select",
                                "default": "1536x1024",
                                "choices": ["1536x1024"],
                            },
                            "count": {"type": "number", "default": 3},
                        },
                    },
                ],
            }
        )
        captured = []

        class CapturingExecutor:
            async def generate(self, _provider, request):
                captured.append(request)
                return (GeneratedImage(PNG, "image/png"),)

        store = GenerationStore(tmp_path)
        await store.initialize()
        service = ImageGenerationService(
            settings=RuntimeSettings(
                enable_llm_tool=True,
                providers=(provider,),
                history=HistorySettings(False, 0, 0, False),
                revision=0,
                default_page_text2img_model_ref="provider:page-model",
                default_tool_text2img_model_ref="provider:tool-model",
            ),
            executor=CapturingExecutor(),
            store=store,
        )

        await service.generate(
            mode="text2img", provider_id="", prompt="page", source="webui"
        )
        await service.generate(
            mode="text2img", provider_id="", prompt="tool", source="llm_tool"
        )

        assert [(item.model, item.size, item.count) for item in captured] == [
            ("page-model", "1024x1536", 1),
            ("page-model", "1024x1536", 1),
            ("tool-model", "1536x1024", 1),
            ("tool-model", "1536x1024", 1),
            ("tool-model", "1536x1024", 1),
        ]

    asyncio.run(run())


def test_llm_tool_returns_mcp_image_content() -> None:
    class FakeService:
        async def reference_from_safe_path(self, _path):
            return None

        async def generate(self, **kwargs):
            provider = settings().providers[0]
            request = SimpleNamespace(model=provider.model, mode="text2img")
            return GenerationResult(
                provider,
                request,
                (GeneratedImage(PNG, "image/png"),),
                5,
                generation_id="gallery-123",
            )

    plugin = object.__new__(ImageStudioPlugin)
    plugin._settings = settings()
    plugin._service = FakeService()
    plugin.store = FakeAgentAssetStore()
    event = ToolEvent()

    capability_result = asyncio.run(
        plugin.image_studio_get_capabilities(
            event, query_type="default", mode="text2img"
        )
    )
    assert not capability_result.isError

    result = asyncio.run(plugin.image_studio_generate(event, prompt="one tree"))

    assert isinstance(result, mcp.types.CallToolResult)
    assert any(isinstance(item, mcp.types.ImageContent) for item in result.content)
    text = next(
        item.text for item in result.content if isinstance(item, mcp.types.TextContent)
    )
    assert "generation_id=gallery-123" in text
    assert '"asset_id":"' in text
    assert "original_path" not in text


def test_llm_tool_reference_fallback_follows_explicit_mode() -> None:
    class FakeService:
        async def generate(self, **kwargs):
            provider = settings().providers[0]
            return GenerationResult(
                provider,
                SimpleNamespace(model=provider.model, mode=kwargs["mode"]),
                (GeneratedImage(PNG, "image/png"),),
                5,
            )

    plugin = object.__new__(ImageStudioPlugin)
    plugin._settings = settings()
    plugin._service = FakeService()
    plugin.store = FakeAgentAssetStore()
    calls = []

    async def capture_references(event, paths, *, include_event_references=True):
        calls.append((list(paths), include_event_references))
        return ()

    plugin._event_references = capture_references
    event = ToolEvent()

    async def generate(mode="text2img", references=...):
        await plugin.image_studio_get_capabilities(
            event, query_type="default", mode=mode
        )
        kwargs = {"prompt": "one tree", "mode": mode}
        if references is not ...:
            kwargs["references"] = references
        return await plugin.image_studio_generate(event, **kwargs)

    omitted = asyncio.run(generate())
    empty = asyncio.run(generate(references=[]))
    explicit = asyncio.run(generate(references=[{"path": "chosen.png"}]))

    assert not omitted.isError
    assert not empty.isError
    assert not explicit.isError
    assert calls == [
        ([], False),
        ([], False),
        ([{"path": "chosen.png"}], False),
    ]


def test_img2img_empty_references_use_event_images_only_as_fallback() -> None:
    provider = ImageProvider.from_mapping(
        {
            "id": "provider",
            "name": "Provider",
            "kind": "openai_images",
            "base_url": "https://example.test",
            "models": [
                {
                    "id": "image-model",
                    "supports_text2img": True,
                    "supports_img2img": True,
                    "max_reference_images": 2,
                    "tool": {"enabled": True, "max_reference_images": 2},
                }
            ],
        }
    )
    plugin = object.__new__(ImageStudioPlugin)
    plugin._settings = RuntimeSettings(
        enable_llm_tool=True,
        providers=(provider,),
        history=HistorySettings(False, 0, 0, False),
        revision=0,
        default_tool_text2img_model_ref="provider:image-model",
        default_tool_img2img_model_ref="provider:image-model",
    )
    captured = []

    class FakeService:
        async def generate(self, **kwargs):
            captured.append(kwargs)
            return GenerationResult(
                provider,
                SimpleNamespace(model="image-model", mode=kwargs["mode"]),
                (GeneratedImage(PNG, "image/png"),),
                5,
            )

    plugin._service = FakeService()
    plugin.store = FakeAgentAssetStore()

    async def fallback_references(_event, values, *, include_event_references=True):
        assert values == []
        assert include_event_references is True
        return (ReferenceImage("event", "event.png", PNG, "image/png"),)

    plugin._event_references = fallback_references
    event = ToolEvent()

    async def run():
        await plugin.image_studio_get_capabilities(
            event, query_type="default", mode="img2img"
        )
        return await plugin.image_studio_generate(
            event, prompt="redraw", mode="img2img", references=[]
        )

    result = asyncio.run(run())

    assert not result.isError
    assert captured[0]["mode"] == "img2img"
    assert len(captured[0]["references"]) == 1


def test_llm_tool_image_return_modes_preserve_original_asset_path() -> None:
    class FakeService:
        async def generate(self, **_kwargs):
            provider = settings().providers[0]
            return GenerationResult(
                provider,
                SimpleNamespace(model=provider.model, mode="text2img"),
                (GeneratedImage(PNG, "image/png"),),
                5,
            )

    async def generate_with_mode(mode: str):
        plugin = object.__new__(ImageStudioPlugin)
        plugin._settings = replace(settings(), llm_image_return_mode=mode)
        plugin._service = FakeService()
        plugin.store = FakeAgentAssetStore()
        event = ToolEvent()
        await plugin.image_studio_get_capabilities(
            event, query_type="default", mode="text2img"
        )
        return await plugin.image_studio_generate(event, prompt="one tree")

    preview_result = asyncio.run(generate_with_mode("preview"))
    asset_result = asyncio.run(generate_with_mode("asset"))
    original_result = asyncio.run(generate_with_mode("original"))

    preview_image = next(
        item
        for item in preview_result.content
        if isinstance(item, mcp.types.ImageContent)
    )
    original_image = next(
        item
        for item in original_result.content
        if isinstance(item, mcp.types.ImageContent)
    )
    assert base64.b64decode(preview_image.data) == b"preview"
    assert base64.b64decode(original_image.data) == PNG
    assert not any(
        isinstance(item, mcp.types.ImageContent) for item in asset_result.content
    )
    for result, mode in (
        (preview_result, "preview"),
        (asset_result, "asset"),
        (original_result, "original"),
    ):
        text = next(
            item.text
            for item in result.content
            if isinstance(item, mcp.types.TextContent)
        )
        assert f"return_mode={mode}" in text
        assert '"asset_id":"' in text
        assert "original_path" not in text


def test_llm_can_view_agent_asset_on_demand() -> None:
    plugin = object.__new__(ImageStudioPlugin)
    plugin._settings = settings()
    plugin.store = FakeAgentAssetStore()

    result = asyncio.run(
        plugin.image_studio_view_asset(ToolEvent(), asset_ids=[f"{1:064x}"])
    )
    missing = asyncio.run(
        plugin.image_studio_view_asset(ToolEvent(), asset_ids=[f"{2:064x}"])
    )

    assert isinstance(result.content[0], mcp.types.ImageContent)
    assert "asset_id" in result.content[1].text
    assert "original_path" not in result.content[1].text
    assert missing.isError
    payload = json.loads(missing.content[0].text)
    assert payload["failures"] == [
        {
            "index": 0,
            "asset_id": f"{2:064x}",
            "reason": "not_found",
            "message": "没有对应的资产记录",
            "retryable": False,
        }
    ]


def test_llm_can_view_multiple_assets_in_input_order() -> None:
    first_id = f"{1:064x}"
    second_id = f"{2:064x}"

    class BatchStore:
        async def load_workflow_image_detailed(self, asset_id, **_kwargs):
            images = {
                first_id: GeneratedImage(b"first", "image/png"),
                second_id: GeneratedImage(b"second", "image/webp"),
            }
            image = images.get(asset_id)
            if image is None:
                return WorkflowImageLoadResult(asset_id, "not_found")
            return WorkflowImageLoadResult(asset_id, "ok", image=image)

    plugin = object.__new__(ImageStudioPlugin)
    plugin._settings = settings()
    plugin.store = BatchStore()

    result = asyncio.run(
        plugin.image_studio_view_asset(
            ToolEvent(), asset_ids=[second_id, first_id], detail="original"
        )
    )

    assert not result.isError
    images = [
        base64.b64decode(item.data)
        for item in result.content
        if isinstance(item, mcp.types.ImageContent)
    ]
    assert images == [b"second", b"first"]
    payload = json.loads(result.content[-1].text.split("。", 1)[0])
    assert payload["assets"] == [
        {
            "index": 0,
            "asset_id": second_id,
            "mime_type": "image/webp",
            "size_bytes": 6,
        },
        {
            "index": 1,
            "asset_id": first_id,
            "mime_type": "image/png",
            "size_bytes": 5,
        },
    ]


def test_new_llm_task_can_view_surviving_asset_after_retention_ends(tmp_path) -> None:
    async def run() -> None:
        store = GenerationStore(tmp_path)
        await store.initialize()
        first_event = ToolEvent()
        assets = await store.lease_agent_images(
            (GeneratedImage(PNG, "image/png"),),
            scope_id=first_event.unified_msg_origin,
            create_preview=False,
            preview_max_edge=768,
            preview_quality=80,
        )
        with store._connect() as conn:
            conn.execute(
                "UPDATE agent_asset_leases SET expires_at = 0, hard_expires_at = 0"
            )
        plugin = object.__new__(ImageStudioPlugin)
        plugin._settings = settings()
        plugin.store = store
        result = await plugin.image_studio_view_asset(
            ToolEvent(), asset_ids=[assets[0].asset_id], detail="original"
        )
        assert not result.isError
        assert base64.b64decode(result.content[0].data) == PNG
        outsider = ToolEvent()
        outsider.unified_msg_origin = "test:private:user-2"
        denied = await plugin.image_studio_view_asset(
            outsider, asset_ids=[assets[0].asset_id], detail="original"
        )
        assert denied.isError
        assert (
            json.loads(denied.content[0].text)["failures"][0]["reason"]
            == "access_denied"
        )

    asyncio.run(run())


def test_llm_asset_batch_reports_every_failure_without_partial_images() -> None:
    statuses = ["invalid_asset_id", "access_denied", "file_missing", "decode_failed"]

    class BatchStore:
        async def load_workflow_image_detailed(self, asset_id, **_kwargs):
            index = int(asset_id.rsplit("-", 1)[-1])
            return WorkflowImageLoadResult(asset_id, statuses[index])

    plugin = object.__new__(ImageStudioPlugin)
    plugin._settings = settings()
    plugin.store = BatchStore()
    result = asyncio.run(
        plugin.image_studio_view_asset(
            ToolEvent(), asset_ids=[f"asset-{index}" for index in range(4)]
        )
    )

    assert result.isError
    assert not any(isinstance(item, mcp.types.ImageContent) for item in result.content)
    payload = json.loads(result.content[0].text)
    assert [item["index"] for item in payload["failures"]] == [0, 1, 2, 3]
    assert [item["reason"] for item in payload["failures"]] == statuses
    assert all(item["retryable"] is False for item in payload["failures"])


def test_capability_query_activates_scoped_sender_and_hides_native_sender() -> None:
    plugin = object.__new__(ImageStudioPlugin)
    plugin._settings = settings()
    event = ToolEvent()

    class MutableToolSet:
        def __init__(self):
            self.names = {
                "image_studio_send_output",
                "image_studio_generate",
                "pc_send_current_media",
                "send_message_to_user",
            }

        def get_tool(self, name):
            return object() if name in self.names else None

        def remove_tool(self, name):
            self.names.discard(name)

    tool_set = MutableToolSet()
    event.set_extra(
        "provider_request",
        SimpleNamespace(func_tool=tool_set, extra_user_content_parts=[]),
    )

    result = asyncio.run(
        plugin.image_studio_get_capabilities(
            event, query_type="default", mode="text2img"
        )
    )

    assert not result.isError
    policy = json.loads(result.content[0].text)["asset_policy"]
    assert policy["temporary_retention_hours"] == 1
    assert "retention_hours" not in policy
    assert "send_message_to_user" not in tool_set.names
    assert "pc_send_current_media" in tool_set.names
    assert "image_studio_send_output" in tool_set.names
    assert event.get_extra(IMAGE_WORKFLOW_STATE_EXTRA_KEY)["active"] is True


def test_successful_generation_hides_competing_private_media_sender() -> None:
    class FakeService:
        async def generate(self, **kwargs):
            provider = settings().providers[0]
            return GenerationResult(
                provider,
                SimpleNamespace(model=provider.model, mode=kwargs["mode"]),
                (GeneratedImage(PNG, "image/png"),),
                5,
            )

    class MutableToolSet:
        def __init__(self):
            self.names = {
                "image_studio_send_output",
                "image_studio_generate",
                "pc_send_current_media",
                "send_message_to_user",
            }

        def get_tool(self, name):
            return object() if name in self.names else None

        def remove_tool(self, name):
            self.names.discard(name)

    plugin = object.__new__(ImageStudioPlugin)
    plugin._settings = settings()
    plugin._service = FakeService()
    plugin.store = FakeAgentAssetStore()
    event = ToolEvent()
    tool_set = MutableToolSet()
    event.set_extra(
        "provider_request",
        SimpleNamespace(func_tool=tool_set, extra_user_content_parts=[]),
    )

    async def run():
        await plugin.image_studio_get_capabilities(
            event, query_type="default", mode="text2img"
        )
        assert "pc_send_current_media" in tool_set.names
        return await plugin.image_studio_generate(
            event, prompt="one tree", mode="text2img"
        )

    result = asyncio.run(run())

    assert not result.isError
    assert "pc_send_current_media" not in tool_set.names
    assert "send_message_to_user" not in tool_set.names
    assert event.get_extra(IMAGE_WORKFLOW_STATE_EXTRA_KEY)["assets_protected"] is True


def test_asset_storage_failure_keeps_other_sender_and_returns_no_preview() -> None:
    class FakeService:
        async def generate(self, **kwargs):
            provider = settings().providers[0]
            return GenerationResult(
                provider,
                SimpleNamespace(model=provider.model, mode=kwargs["mode"]),
                (GeneratedImage(PNG, "image/png"),),
                5,
            )

    class FailingStore:
        async def lease_agent_images(self, _images, **_kwargs):
            raise OSError("disk unavailable")

    class MutableToolSet:
        def __init__(self):
            self.names = {
                "image_studio_send_output",
                "image_studio_generate",
                "pc_send_current_media",
            }

        def get_tool(self, name):
            return object() if name in self.names else None

        def remove_tool(self, name):
            self.names.discard(name)

    plugin = object.__new__(ImageStudioPlugin)
    plugin._settings = settings()
    plugin._service = FakeService()
    plugin.store = FailingStore()
    event = ToolEvent()
    tool_set = MutableToolSet()
    event.set_extra(
        "provider_request",
        SimpleNamespace(func_tool=tool_set, extra_user_content_parts=[]),
    )

    async def run():
        await plugin.image_studio_get_capabilities(
            event, query_type="default", mode="text2img"
        )
        return await plugin.image_studio_generate(
            event, prompt="one tree", mode="text2img"
        )

    result = asyncio.run(run())

    assert result.isError
    assert "原图资产保存失败" in result.content[0].text
    assert not any(isinstance(item, mcp.types.ImageContent) for item in result.content)
    assert "pc_send_current_media" in tool_set.names


def test_image_studio_sender_delivers_leased_asset_to_current_session(tmp_path) -> None:
    async def run() -> None:
        store = GenerationStore(tmp_path)
        await store.initialize()
        assets = await store.lease_agent_images(
            (GeneratedImage(PNG, "image/png"),),
            scope_id="test:private:user-1",
            create_preview=True,
            preview_max_edge=320,
            preview_quality=75,
        )
        plugin = object.__new__(ImageStudioPlugin)
        plugin._settings = settings()
        plugin.store = store
        with store._connect() as conn:
            conn.execute(
                "UPDATE agent_asset_leases SET expires_at = 0, hard_expires_at = 0"
            )
        plugin.context = SimpleNamespace(
            get_config=lambda **_kwargs: {
                "provider_settings": {"computer_use_runtime": "none"}
            }
        )
        event = ToolEvent()
        await plugin.image_studio_get_capabilities(event)

        result = await plugin.image_studio_send_output(
            event,
            destination="session",
            messages=[
                {"type": "plain", "text": "处理中"},
                {"type": "image", "asset_id": assets[0].asset_id},
            ],
        )

        assert not result.isError
        assert len(event.sent) == 1
        assert len(event.sent[0].chain) == 2
        assert isinstance(event.sent[0].chain[1], Image)
        assert "不会结束" in result.content[0].text

    asyncio.run(run())


def test_image_studio_sender_materializes_asset_to_local_workspace(
    tmp_path, monkeypatch
) -> None:
    async def run() -> None:
        store = GenerationStore(tmp_path / "plugin-data")
        await store.initialize()
        assets = await store.lease_agent_images(
            (GeneratedImage(PNG, "image/png"),),
            scope_id="test:private:user-1",
            create_preview=False,
            preview_max_edge=320,
            preview_quality=75,
        )
        workspace = tmp_path / "workspace"
        with store._connect() as conn:
            conn.execute(
                "UPDATE agent_asset_leases SET expires_at = 0, hard_expires_at = 0"
            )

        async def fake_workspace(_event, _context):
            return workspace

        monkeypatch.setattr(
            "astrbot_plugin_image_studio.main._event_workspace_root", fake_workspace
        )
        plugin = object.__new__(ImageStudioPlugin)
        plugin._settings = settings()
        plugin.store = store
        plugin.context = SimpleNamespace(
            get_config=lambda **_kwargs: {
                "provider_settings": {"computer_use_runtime": "local"}
            }
        )
        event = ToolEvent()
        event.set_extra(IMAGE_WORKFLOW_STATE_EXTRA_KEY, {"active": True})

        result = await plugin.image_studio_send_output(
            event,
            destination="workspace",
            messages=[
                {
                    "type": "image",
                    "asset_id": assets[0].asset_id,
                    "name": "panel.png",
                }
            ],
        )

        assert not result.isError
        payload = json.loads(result.content[0].text)
        assert payload["workspace_paths"] == ["image-studio-assets/panel.png"]
        assert (workspace / payload["workspace_paths"][0]).read_bytes() == PNG
        assert event.sent == []

    asyncio.run(run())


def test_llm_tool_rejects_generation_before_capability_query() -> None:
    plugin = object.__new__(ImageStudioPlugin)
    plugin._settings = settings()
    plugin._service = SimpleNamespace()

    result = asyncio.run(plugin.image_studio_generate(ToolEvent(), prompt="one tree"))

    assert result.isError
    assert "必须先调用 image_studio_get_capabilities" in result.content[0].text


def test_capability_query_is_consumed_by_one_generation() -> None:
    plugin = object.__new__(ImageStudioPlugin)
    plugin._settings = settings()

    class FakeService:
        async def generate(self, **_kwargs):
            provider = settings().providers[0]
            return GenerationResult(
                provider,
                SimpleNamespace(model=provider.model, mode="text2img"),
                (GeneratedImage(PNG, "image/png"),),
                5,
            )

    plugin._service = FakeService()
    plugin.store = FakeAgentAssetStore()
    event = ToolEvent()
    asyncio.run(
        plugin.image_studio_get_capabilities(
            event, query_type="default", mode="text2img"
        )
    )

    first = asyncio.run(plugin.image_studio_generate(event, prompt="one tree"))
    second = asyncio.run(plugin.image_studio_generate(event, prompt="another tree"))

    assert not first.isError
    assert second.isError
    assert "必须先调用 image_studio_get_capabilities" in second.content[0].text


def test_capability_query_supports_all_default_and_model() -> None:
    provider = ImageProvider.from_mapping(
        {
            "id": "provider",
            "name": "Provider",
            "kind": "custom_json",
            "base_url": "https://example.test",
            "models": [
                {"id": "text-model"},
                {
                    "id": "edit-model",
                    "supports_text2img": False,
                    "supports_img2img": True,
                    "max_reference_images": 1,
                },
            ],
        }
    )
    plugin = object.__new__(ImageStudioPlugin)
    plugin._settings = RuntimeSettings(
        enable_llm_tool=True,
        providers=(provider,),
        history=HistorySettings(False, 0, 0, False),
        revision=7,
        default_tool_text2img_model_ref="provider:text-model",
        default_tool_img2img_model_ref="provider:edit-model",
    )
    event = ToolEvent()

    combined_default_result = asyncio.run(
        plugin.image_studio_get_capabilities(event, query_type="default")
    )
    combined_default_payload = json.loads(combined_default_result.content[0].text)
    assert [item["model_ref"] for item in combined_default_payload["models"]] == [
        "provider:text-model",
        "provider:edit-model",
    ]
    assert combined_default_payload["models"][0]["default_for_modes"] == ["text2img"]
    assert combined_default_payload["models"][1]["default_for_modes"] == ["img2img"]
    assert event.get_extra(CAPABILITY_QUERY_EXTRA_KEY)["models"] == {
        "provider:text-model": ["text2img"],
        "provider:edit-model": ["img2img"],
    }

    all_result = asyncio.run(
        plugin.image_studio_get_capabilities(event, query_type="all")
    )
    default_result = asyncio.run(
        plugin.image_studio_get_capabilities(
            event, query_type="default", mode="text2img"
        )
    )
    model_result = asyncio.run(
        plugin.image_studio_get_capabilities(
            event, query_type="model", model_ref="provider:edit-model"
        )
    )

    all_payload = json.loads(all_result.content[0].text)
    default_payload = json.loads(default_result.content[0].text)
    model_payload = json.loads(model_result.content[0].text)

    assert [item["model_ref"] for item in all_payload["models"]] == [
        "provider:text-model",
        "provider:edit-model",
    ]
    assert [item["model_ref"] for item in default_payload["models"]] == [
        "provider:text-model"
    ]
    assert [item["model_ref"] for item in model_payload["models"]] == [
        "provider:edit-model"
    ]
    assert "普通画面描述不是能力缺口" in default_payload["next_action"]
    assert "无需再次 model 查询" in all_payload["next_action"]


def test_default_query_merges_shared_text_and_image_model() -> None:
    provider = ImageProvider.from_mapping(
        {
            "id": "provider",
            "name": "Provider",
            "kind": "custom_json",
            "base_url": "https://example.test",
            "models": [
                {
                    "id": "shared",
                    "supports_text2img": True,
                    "supports_img2img": True,
                    "max_reference_images": 2,
                }
            ],
        }
    )
    plugin = object.__new__(ImageStudioPlugin)
    plugin._settings = RuntimeSettings(
        enable_llm_tool=True,
        providers=(provider,),
        history=HistorySettings(False, 0, 0, False),
        revision=8,
        default_tool_text2img_model_ref="provider:shared",
        default_tool_img2img_model_ref="provider:shared",
    )
    event = ToolEvent()

    result = asyncio.run(plugin.image_studio_get_capabilities(event))
    payload = json.loads(result.content[0].text)

    assert not result.isError
    assert len(payload["models"]) == 1
    assert payload["models"][0]["default_for_modes"] == ["text2img", "img2img"]
    assert payload["models"][0]["query_modes"] == ["text2img", "img2img"]
    assert event.get_extra(CAPABILITY_QUERY_EXTRA_KEY)["models"] == {
        "provider:shared": ["img2img", "text2img"]
    }


def test_negative_prompt_requires_model_tool_exposure() -> None:
    provider = ImageProvider.from_mapping(
        {
            "id": "provider",
            "name": "Provider",
            "kind": "custom_json",
            "base_url": "https://example.test",
            "models": [
                {
                    "id": "hidden-negative",
                    "supports_negative_prompt": True,
                    "negative_prompt_default": "low quality",
                    "tool": {
                        "negative_prompt_exposed": False,
                        "parameters": {"count": {"exposed": False}},
                    },
                }
            ],
        }
    )
    configured = RuntimeSettings(
        enable_llm_tool=True,
        providers=(provider,),
        history=HistorySettings(False, 0, 0, False),
        revision=3,
        default_tool_text2img_model_ref="provider:hidden-negative",
    )
    captured = []

    class CapturingService:
        async def generate(self, **kwargs):
            captured.append(kwargs)
            return GenerationResult(
                provider,
                SimpleNamespace(model="hidden-negative", mode="text2img"),
                (GeneratedImage(PNG, "image/png"),),
                5,
            )

    plugin = object.__new__(ImageStudioPlugin)
    plugin._settings = configured
    plugin._service = CapturingService()
    plugin.store = FakeAgentAssetStore()
    event = ToolEvent()
    capability_result = asyncio.run(
        plugin.image_studio_get_capabilities(
            event, query_type="model", model_ref="provider:hidden-negative"
        )
    )
    capability = json.loads(capability_result.content[0].text)["models"][0]

    success = asyncio.run(plugin.image_studio_generate(event, prompt="one tree"))
    asyncio.run(
        plugin.image_studio_get_capabilities(
            event, query_type="model", model_ref="provider:hidden-negative"
        )
    )
    ignored = asyncio.run(
        plugin.image_studio_generate(
            event,
            prompt="one tree",
            parameters={
                "negative_prompt": "fog",
                "count": 4,
                "unknown": "discard me",
            },
        )
    )

    assert "negative_prompt" not in capability["parameters"]
    assert (
        "不要传入 negative_prompt" in capability["prompt_contract"]["negative_prompt"]
    )
    assert not success.isError
    assert captured[0]["negative_prompt"] == "low quality"
    assert not ignored.isError
    assert captured[1]["negative_prompt"] == "low quality"
    assert captured[1]["parameters"] == {}


def test_exposed_negative_prompt_is_disclosed_and_forwarded() -> None:
    provider = ImageProvider.from_mapping(
        {
            "id": "provider",
            "name": "Provider",
            "kind": "custom_json",
            "base_url": "https://example.test",
            "models": [
                {
                    "id": "negative-model",
                    "supports_negative_prompt": True,
                    "parameters": {
                        "negative_prompt": {
                            "type": "string",
                            "description": "schema description must not override tool policy",
                        }
                    },
                    "tool": {"negative_prompt_exposed": True},
                }
            ],
        }
    )
    configured = RuntimeSettings(
        enable_llm_tool=True,
        providers=(provider,),
        history=HistorySettings(False, 0, 0, False),
        revision=4,
        default_tool_text2img_model_ref="provider:negative-model",
    )
    captured = {}

    class CapturingService:
        async def generate(self, **kwargs):
            captured.update(kwargs)
            return GenerationResult(
                provider,
                SimpleNamespace(model="negative-model", mode="text2img"),
                (GeneratedImage(PNG, "image/png"),),
                5,
            )

    plugin = object.__new__(ImageStudioPlugin)
    plugin._settings = configured
    plugin._service = CapturingService()
    plugin.store = FakeAgentAssetStore()
    event = ToolEvent()
    capability_result = asyncio.run(
        plugin.image_studio_get_capabilities(
            event, query_type="default", mode="text2img"
        )
    )
    capability = json.loads(capability_result.content[0].text)["models"][0]
    result = asyncio.run(
        plugin.image_studio_generate(
            event,
            prompt="one tree",
            parameters={"negative_prompt": "fog"},
        )
    )

    assert capability["parameters"]["negative_prompt"]["type"] == "string"
    assert "negative_prompt_exposed" not in capability
    assert not result.isError
    assert captured["negative_prompt"] == "fog"
    assert "negative_prompt" not in captured["parameters"]


def test_unsupported_model_ignores_dynamic_negative_prompt() -> None:
    plugin = object.__new__(ImageStudioPlugin)
    plugin._settings = settings()
    captured = {}

    class CapturingService:
        async def generate(self, **kwargs):
            captured.update(kwargs)
            provider = settings().providers[0]
            return GenerationResult(
                provider,
                SimpleNamespace(model="test-image", mode="text2img"),
                (GeneratedImage(PNG, "image/png"),),
                5,
            )

    plugin._service = CapturingService()
    plugin.store = FakeAgentAssetStore()
    event = ToolEvent()
    capability_result = asyncio.run(
        plugin.image_studio_get_capabilities(
            event, query_type="default", mode="text2img"
        )
    )
    capability = json.loads(capability_result.content[0].text)["models"][0]

    result = asyncio.run(
        plugin.image_studio_generate(
            event,
            prompt="one tree",
            parameters={"negative_prompt": "fog"},
        )
    )

    assert capability["prompt_contract"]["format"] == "natural_language"
    assert (
        "不要使用英文逗号分隔的 NAI tag 串"
        in capability["prompt_contract"]["instruction"]
    )
    assert "negative_prompt" not in capability["parameters"]
    assert not result.isError
    assert captured["negative_prompt"] == ""
    assert "negative_prompt" not in captured["parameters"]


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
        history=HistorySettings(False, 0, 0, False),
        revision=0,
        default_tool_text2img_model_ref="provider:visible",
    )

    result = asyncio.run(
        plugin.image_studio_get_capabilities(SimpleNamespace(), mode="text2img")
    )
    payload = json.loads(result.content[0].text)

    assert "\n" not in result.content[0].text
    assert payload["query_type"] == "default"
    assert [item["model_ref"] for item in payload["models"]] == ["provider:visible"]
    assert "prompt_profile" not in payload["models"][0]
    assert "prompt_instructions" not in payload["models"][0]
    assert "supports_negative_prompt" not in payload["models"][0]
    assert payload["default_model_refs"] == {
        "text2img": "provider:visible",
        "img2img": "",
    }
    assert payload["asset_policy"]["return_mode"] == "preview"
    assert payload["asset_policy"]["private_asset_handle"] == "asset_id"
    assert payload["asset_policy"]["delivery_tool"] == "image_studio_send_output"
    assert "tool_images" in payload["asset_policy"]["temporary_preview_path"]


def test_capabilities_uses_img2img_support_not_zero_reference_limit() -> None:
    provider = ImageProvider.from_mapping(
        {
            "id": "provider",
            "name": "Provider",
            "kind": "custom_json",
            "base_url": "https://example.test",
            "models": [
                {
                    "id": "zero-limit",
                    "supports_img2img": True,
                    "max_reference_images": 0,
                },
                {
                    "id": "positive-limit",
                    "supports_img2img": True,
                    "max_reference_images": 2,
                },
                {
                    "id": "disabled-support",
                    "supports_img2img": False,
                    "max_reference_images": 8,
                    "tool": {"enabled": True, "max_reference_images": 8},
                },
            ],
        }
    )
    plugin = object.__new__(ImageStudioPlugin)
    plugin._settings = RuntimeSettings(
        enable_llm_tool=True,
        providers=(provider,),
        history=HistorySettings(False, 0, 0, False),
        revision=0,
    )

    result = asyncio.run(
        plugin.image_studio_get_capabilities(
            SimpleNamespace(), query_type="all", mode="img2img"
        )
    )
    payload = json.loads(result.content[0].text)

    assert [item["model_ref"] for item in payload["models"]] == [
        "provider:zero-limit",
        "provider:positive-limit",
    ]


def test_registered_image_tool_descriptions_contain_routing_contract() -> None:
    capabilities = llm_tools.get_func("image_studio_get_capabilities")
    generate = llm_tools.get_func("image_studio_generate")
    view_asset = llm_tools.get_func("image_studio_view_asset")
    send_output = llm_tools.get_func("image_studio_send_output")
    light_tools = llm_tools.get_full_tool_set().get_light_tool_set()

    assert llm_tools.get_func("image_gen_get_capabilities") is None
    assert llm_tools.get_func("image_gen_generate") is None
    assert capabilities is not None
    assert generate is not None
    assert view_asset is not None
    assert send_output is not None
    assert "必须先调用本工具" in capabilities.description
    assert "query_type" in capabilities.parameters["properties"]
    assert (
        "default(查询默认模型参数)"
        in capabilities.parameters["properties"]["query_type"]["description"]
    )
    assert "生成图片" in generate.description
    reference_description = generate.parameters["properties"]["references"][
        "description"
    ]
    assert "asset_id" in reference_description
    assert "negative_prompt" not in generate.parameters["properties"]
    assert set(generate.parameters["properties"]) == {
        "prompt",
        "mode",
        "model_ref",
        "parameters",
        "references",
    }
    assert "required" not in generate.parameters
    assert "当前会话" in view_asset.description
    assert "ImageContent" not in view_asset.description
    assert set(view_asset.parameters["properties"]) == {"asset_ids", "detail"}
    assert "1 至 8" in view_asset.parameters["properties"]["asset_ids"]["description"]
    assert "preview" in view_asset.parameters["properties"]["detail"]["description"]
    assert "required" not in view_asset.parameters
    assert (
        light_tools.get_tool("image_studio_get_capabilities").description
        == capabilities.description
    )
    assert (
        light_tools.get_tool("image_studio_generate").description
        == generate.description
    )
    assert (
        light_tools.get_tool("image_studio_view_asset").description
        == view_asset.description
    )
    assert (
        light_tools.get_tool("image_studio_send_output").description
        == send_output.description
    )
    assert "session" not in send_output.parameters["properties"]
    assert (
        "必须明确填写"
        in send_output.parameters["properties"]["destination"]["description"]
    )


def test_agent_workflow_prompt_is_scoped_and_idempotent() -> None:
    plugin = object.__new__(ImageStudioPlugin)
    plugin._settings = settings()

    class ToolSet:
        @staticmethod
        def get_tool(name):
            return object() if name == "image_studio_generate" else None

    request = SimpleNamespace(func_tool=ToolSet(), system_prompt="原有人格")
    asyncio.run(plugin.inject_agent_workflow_prompt(object(), request))
    asyncio.run(plugin.inject_agent_workflow_prompt(object(), request))

    assert request.system_prompt.startswith("原有人格")
    assert request.system_prompt.count(AGENT_WORKFLOW_PROMPT_MARKER) == 1
    assert "先调用 image_studio_get_capabilities" in request.system_prompt
    assert "asset_id" in request.system_prompt
    assert "original_path" not in request.system_prompt
    assert "data/temp/tool_images" in request.system_prompt
    assert "发送、复制、编辑" in request.system_prompt
    assert "ImageContent" not in request.system_prompt
    assert "不代表本轮结束" in request.system_prompt
    assert "普通 assistant 文本回复" in request.system_prompt
    assert "llm.response" not in request.system_prompt
    assert "pc_send_current_media" not in request.system_prompt

    request_without_tool = SimpleNamespace(
        func_tool=SimpleNamespace(get_tool=lambda _name: None),
        system_prompt="保持不变",
    )
    asyncio.run(plugin.inject_agent_workflow_prompt(object(), request_without_tool))
    assert request_without_tool.system_prompt == "保持不变"


def test_agent_tool_send_adds_one_shot_continuation_prompt() -> None:
    plugin = object.__new__(ImageStudioPlugin)
    plugin._settings = settings()
    request = SimpleNamespace(extra_user_content_parts=[])
    event = ToolEvent()
    event.set_extra("provider_request", request)
    generated = mcp.types.CallToolResult(
        content=[mcp.types.TextContent(type="text", text="asset")]
    )
    send_result = mcp.types.CallToolResult(
        content=[mcp.types.TextContent(type="text", text="Message sent")]
    )

    asyncio.run(
        plugin.observe_agent_tool_result(
            event,
            SimpleNamespace(name="image_studio_generate"),
            {},
            generated,
        )
    )
    assert event.get_extra(IMAGE_WORKFLOW_STATE_EXTRA_KEY) == {
        "active": True,
        "reminder_added": False,
    }

    asyncio.run(
        plugin.observe_agent_tool_result(
            event,
            SimpleNamespace(name="image_studio_send_output"),
            {"destination": "session", "messages": [{"type": "image"}]},
            send_result,
        )
    )
    assert len(request.extra_user_content_parts) == 1
    reminder = request.extra_user_content_parts[0].text
    assert IMAGE_WORKFLOW_CONTINUATION_MARKER in reminder
    assert "普通 assistant 文本回复" in reminder
    assert "llm.response" not in reminder
    assert "pc_send_current_media" not in reminder
    assert event.get_extra(IMAGE_WORKFLOW_STATE_EXTRA_KEY)["reminder_added"] is True

    asyncio.run(
        plugin.observe_agent_tool_result(
            event,
            SimpleNamespace(name="image_studio_send_output"),
            {"destination": "session", "messages": [{"type": "image"}]},
            send_result,
        )
    )
    assert len(request.extra_user_content_parts) == 1


def test_llm_parameter_descriptor_omits_redundant_choice_fields() -> None:
    descriptor = _llm_parameter_descriptor(
        {
            "type": "select",
            "choices": [
                "auto",
                {"value": "hd", "label": "高清"},
            ],
        },
        {"choice_descriptions": {"hd": "更精细"}},
    )

    assert "description" not in descriptor
    assert descriptor["choices"] == [
        {"value": "auto"},
        {"value": "hd", "label": "高清", "description": "更精细"},
    ]


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


def test_llm_parameters_apply_defaults_expand_presets_and_ignore_hidden() -> None:
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
                        "count": {
                            "type": "number",
                            "default": 1,
                            "min": 1,
                            "max": 4,
                        },
                    },
                    "tool": {
                        "parameters": {
                            "style": {"exposed": True},
                            "steps": {"exposed": False},
                            "count": {"exposed": False},
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
    filtered = _parameters_for_model(
        {"steps": 25, "unknown": "discard me"}, model, source="llm_tool"
    )
    assert filtered["steps"] == 24
    assert "unknown" not in filtered
    assert (
        _control_parameter_value({"count": 4}, model, "count", source="llm_tool") == 1
    )
    assert _control_parameter_value({"count": 4}, model, "count", source="webui") == 4


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


def test_invocation_source_captures_group_and_display_names() -> None:
    event = SimpleNamespace(
        get_platform_name=lambda: "aiocqhttp",
        get_platform_id=lambda: "bot-1",
        get_group_id=lambda: "group-42",
        get_sender_id=lambda: "user-7",
        get_sender_name=lambda: "空雨",
        message_obj=SimpleNamespace(group=SimpleNamespace(group_name="绘图讨论组")),
    )

    assert _invocation_source(event).public_dict() == {
        "context_type": "group",
        "platform_name": "aiocqhttp",
        "platform_id": "bot-1",
        "group_id": "group-42",
        "group_name": "绘图讨论组",
        "user_id": "user-7",
        "user_name": "空雨",
    }


def test_invocation_source_keeps_private_user_without_group_name() -> None:
    event = SimpleNamespace(
        get_platform_name=lambda: "qq_official",
        get_platform_id=lambda: "bot-1",
        get_group_id=lambda: "",
        get_sender_id=lambda: "user-7",
        get_sender_name=lambda: "user-7",
        message_obj=SimpleNamespace(group=None),
    )

    assert _invocation_source(event).public_dict() == {
        "context_type": "private",
        "platform_name": "qq_official",
        "platform_id": "bot-1",
        "group_id": "",
        "group_name": "",
        "user_id": "user-7",
        "user_name": "",
    }


def test_safe_reference_path_resolves_relative_agent_workspace(tmp_path) -> None:
    async def run() -> None:
        data_dir = tmp_path / "plugin-data"
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "img1.png").write_bytes(PNG)
        store = GenerationStore(data_dir)
        await store.initialize()
        service = ImageGenerationService(
            settings=settings(), executor=FakeExecutor(), store=store
        )

        reference = await service.reference_from_safe_path(
            "img1.png",
            workspace_root=workspace,
        )

        assert reference is not None
        assert reference.filename == "img1.png"
        assert reference.data == PNG

    asyncio.run(run())


def test_leased_asset_id_can_be_reused_as_image_reference(tmp_path) -> None:
    async def run() -> None:
        store = GenerationStore(tmp_path / "plugin-data")
        await store.initialize()
        service = ImageGenerationService(
            settings=settings(), executor=FakeExecutor(), store=store
        )
        assets = await store.lease_agent_images(
            (GeneratedImage(PNG, "image/png"),),
            scope_id="test:private:user-1",
            create_preview=True,
            preview_max_edge=768,
            preview_quality=80,
        )

        plugin = object.__new__(ImageStudioPlugin)
        plugin._settings = settings()
        plugin._service = service
        plugin.store = store
        with store._connect() as conn:
            conn.execute(
                "UPDATE agent_asset_leases SET expires_at = 0, hard_expires_at = 0"
            )
        references = await plugin._event_references(
            ToolEvent(),
            [{"asset_id": assets[0].asset_id}],
            include_event_references=False,
        )

        assert len(references) == 1
        assert references[0].data == PNG
        assert references[0].mime_type == "image/png"

    asyncio.run(run())


def test_safe_reference_path_rejects_workspace_escape(tmp_path) -> None:
    async def run() -> None:
        data_dir = tmp_path / "plugin-data"
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (tmp_path / "outside.png").write_bytes(PNG)
        store = GenerationStore(data_dir)
        await store.initialize()
        service = ImageGenerationService(
            settings=settings(), executor=FakeExecutor(), store=store
        )

        with pytest.raises(ValueError, match="不在当前 Agent 工作区"):
            await service.reference_from_safe_path(
                "../outside.png",
                workspace_root=workspace,
            )

    asyncio.run(run())


def test_safe_reference_path_rejects_astrbot_tool_image_cache(
    tmp_path, monkeypatch
) -> None:
    async def run() -> None:
        astrbot_temp = tmp_path / "astrbot-temp"
        tool_images = astrbot_temp / "tool_images"
        tool_images.mkdir(parents=True)
        cached_preview = tool_images / "call_preview.png"
        cached_preview.write_bytes(PNG)
        store = GenerationStore(tmp_path / "plugin-data")
        await store.initialize()
        service = ImageGenerationService(
            settings=settings(), executor=FakeExecutor(), store=store
        )
        monkeypatch.setattr(
            "astrbot_plugin_image_studio.service.get_astrbot_temp_path",
            lambda: str(astrbot_temp),
        )

        with pytest.raises(ValueError, match="仅用于视觉预览"):
            await service.reference_from_safe_path(str(cached_preview))

    asyncio.run(run())


def test_llm_tool_reads_multiple_relative_agent_workspace_images(
    tmp_path, monkeypatch
) -> None:
    async def run() -> None:
        data_dir = tmp_path / "plugin-data"
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "img1.png").write_bytes(PNG)
        (workspace / "img2.jpg").write_bytes(PNG + b"second")
        store = GenerationStore(data_dir)
        await store.initialize()
        service = ImageGenerationService(
            settings=settings(), executor=FakeExecutor(), store=store
        )
        plugin = object.__new__(ImageStudioPlugin)
        plugin._service = service
        plugin.context = SimpleNamespace(_db=None)

        async def fake_workspace_root(_umo, _db):
            return workspace

        async def no_quoted_images(_event):
            return []

        monkeypatch.setattr(
            "astrbot_plugin_image_studio.main.resolve_workspace_root_for_umo",
            fake_workspace_root,
        )
        monkeypatch.setattr(
            "astrbot_plugin_image_studio.main.extract_quoted_message_images",
            no_quoted_images,
        )
        event = SimpleNamespace(
            unified_msg_origin="qq_official:GroupMessage:test",
            get_messages=list,
            get_extra=lambda _key: None,
        )

        references = await plugin._event_references(
            event,
            ["img1.png", "img2.jpg"],
        )

        assert [item.filename for item in references] == ["img1.png", "img2.jpg"]
        assert [item.data for item in references] == [PNG, PNG + b"second"]

    asyncio.run(run())


def test_provider_request_refs_include_serialized_quoted_message_images() -> None:
    first = "https://multimedia.nt.qq.com.cn/download?fileid=first&spec=0"
    second = "https://multimedia.nt.qq.com.cn/download?fileid=second&spec=0"
    quoted = SimpleNamespace(
        text=(
            "<Quoted Message>\n"
            f"[附件1] 类型:图片 文件名:first.png URL:{first}\n"
            f"[附件1] 类型:图片 文件名:second.jpg URL:{second}\n"
            "</Quoted Message>"
        )
    )
    request = SimpleNamespace(
        image_urls=[first],
        extra_user_content_parts=[quoted],
    )
    event = SimpleNamespace(
        get_extra=lambda key: request if key == "provider_request" else None
    )

    assert _provider_request_image_refs(event) == [first, second]


def test_event_references_explicit_paths_can_disable_automatic_discovery() -> None:
    async def run() -> None:
        data_by_ref = {
            "explicit.png": PNG,
            "automatic.png": PNG + b"automatic",
        }

        class FakeService:
            async def reference_from_media_ref(self, raw_ref, *, workspace_root=None):
                return ReferenceImage(
                    raw_ref,
                    raw_ref,
                    data_by_ref[raw_ref],
                    "image/png",
                )

        request = SimpleNamespace(
            image_urls=["automatic.png"], extra_user_content_parts=[]
        )
        event = SimpleNamespace(
            unified_msg_origin="qq_official:GroupMessage:test",
            get_messages=lambda: [],
            get_extra=lambda key: request if key == "provider_request" else None,
        )
        plugin = object.__new__(ImageStudioPlugin)
        plugin._service = FakeService()
        plugin.context = SimpleNamespace(_db=None)

        explicit_only = await plugin._event_references(
            event,
            ["explicit.png"],
            include_event_references=False,
        )
        automatic = await plugin._event_references(event)

        assert [item.filename for item in explicit_only] == ["explicit.png"]
        assert [item.filename for item in automatic] == ["automatic.png"]

    asyncio.run(run())


def test_event_references_materializes_serialized_quoted_message_images(
    monkeypatch,
) -> None:
    async def run() -> None:
        first = "https://multimedia.nt.qq.com.cn/download?fileid=first&spec=0"
        second = "https://multimedia.nt.qq.com.cn/download?fileid=second&spec=0"
        data_by_ref = {first: PNG, second: PNG + b"second"}

        class FakeService:
            async def reference_from_media_ref(self, raw_ref, *, workspace_root=None):
                assert workspace_root is not None
                data = data_by_ref[raw_ref]
                return ReferenceImage(raw_ref, "reference.png", data, "image/png")

        async def fake_workspace_root(_umo, _db):
            return Path("/tmp/image-studio-test-workspace")

        async def no_quoted_images(_event):
            return []

        monkeypatch.setattr(
            "astrbot_plugin_image_studio.main.resolve_workspace_root_for_umo",
            fake_workspace_root,
        )
        monkeypatch.setattr(
            "astrbot_plugin_image_studio.main.extract_quoted_message_images",
            no_quoted_images,
        )
        quoted = SimpleNamespace(
            text=(
                "<Quoted Message>\n"
                f"[附件1] 类型:图片 文件名:first.png URL:{first}\n"
                f"[附件1] 类型:图片 文件名:second.jpg URL:{second}\n"
                "</Quoted Message>"
            )
        )
        request = SimpleNamespace(
            image_urls=[],
            extra_user_content_parts=[quoted],
        )
        event = SimpleNamespace(
            unified_msg_origin="qq_official:GroupMessage:test",
            get_messages=list,
            get_extra=lambda key: request if key == "provider_request" else None,
        )
        plugin = object.__new__(ImageStudioPlugin)
        plugin._service = FakeService()
        plugin.context = SimpleNamespace(_db=None)

        references = await plugin._event_references(event)

        assert [item.data for item in references] == [PNG, PNG + b"second"]

    asyncio.run(run())


def test_img2img_missing_references_reports_read_failure(tmp_path) -> None:
    async def run() -> None:
        provider = ImageProvider.from_mapping(
            {
                "id": "provider",
                "name": "Provider",
                "kind": "openai_images",
                "base_url": "https://example.test",
                "models": [
                    {
                        "id": "image-model",
                        "supports_img2img": True,
                        "max_reference_images": 8,
                        "tool": {"enabled": True, "max_reference_images": 8},
                    }
                ],
            }
        )
        store = GenerationStore(tmp_path)
        await store.initialize()
        service = ImageGenerationService(
            settings=RuntimeSettings(
                enable_llm_tool=True,
                providers=(provider,),
                history=HistorySettings(False, 0, 0, False),
                revision=0,
                default_tool_img2img_model_ref="provider:image-model",
            ),
            executor=FakeExecutor(),
            store=store,
        )

        with pytest.raises(ValueError, match="未读取到可用参考图"):
            await service.generate(
                mode="img2img",
                provider_id="",
                prompt="transfer style",
                source="llm_tool",
            )

    asyncio.run(run())


def test_openai_multipart_uses_array_field_for_multiple_references() -> None:
    async def run() -> None:
        class FakeResponse:
            status = 200

            def __init__(self):
                self.headers = {"Content-Type": "image/png"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def read(self):
                return PNG

        class FakeSession:
            data = None

            def post(self, _endpoint, **kwargs):
                self.data = kwargs.get("data")
                return FakeResponse()

        provider = ImageProvider.from_mapping(
            {
                "id": "openai",
                "name": "OpenAI",
                "kind": "openai_images",
                "base_url": "https://example.test/v1",
                "models": [
                    {
                        "id": "image-model",
                        "supports_img2img": True,
                        "max_reference_images": 2,
                    }
                ],
            }
        )
        request = GenerationRequest(
            mode="img2img",
            provider_id=provider.id,
            prompt="transfer style",
            model="image-model",
            references=(
                ReferenceImage("one", "one.png", PNG, "image/png"),
                ReferenceImage("two", "two.png", PNG + b"two", "image/png"),
            ),
        )
        session = FakeSession()

        images = await ProviderExecutor(session).generate(provider, request)

        assert images[0].data == PNG
        field_names = [field[0]["name"] for field in session.data._fields]
        assert field_names.count("image[]") == 2
        assert "image" not in field_names

    asyncio.run(run())


def test_nai_model_discovery_is_not_supported() -> None:
    provider = ImageProvider.from_mapping(
        {
            "id": "nai",
            "name": "NAI",
            "kind": "nai_direct",
            "base_url": "https://nai.sta1n.cn",
        }
    )

    with pytest.raises(ProviderError, match="不支持获取模型列表"):
        asyncio.run(ProviderExecutor(None).discover_models(provider))


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
