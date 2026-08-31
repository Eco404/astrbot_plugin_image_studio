from __future__ import annotations

import asyncio
import base64
import json
import time
from pathlib import Path
from types import SimpleNamespace

import mcp
import pytest
from astrbot.api.message_components import Image, Reply
from astrbot.core.provider.register import llm_tools
from astrbot_plugin_image_studio.config import HistorySettings, RuntimeSettings
from astrbot_plugin_image_studio.main import (
    ImageStudioPlugin,
    _invocation_source,
    _iter_event_images,
    _parse_command,
    _provider_request_image_refs,
)
from astrbot_plugin_image_studio.models import (
    GeneratedImage,
    GenerationRequest,
    GenerationResult,
    ImageProvider,
    ReferenceImage,
)
from astrbot_plugin_image_studio.providers import ProviderError, ProviderExecutor
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
        history=HistorySettings(False, 0, 0, False),
        revision=0,
        default_page_text2img_model_ref="test-provider:test-image",
        default_tool_text2img_model_ref="test-provider:test-image",
    )


class FakeExecutor:
    async def generate(self, provider, request):
        return (GeneratedImage(PNG, "image/png"),)


class ToolEvent:
    def __init__(self) -> None:
        self._extras = {}
        self.unified_msg_origin = ""

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value) -> None:
        self._extras[key] = value

    def get_messages(self):
        return []


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
            ("page-model", "1024x1536", 2),
            ("tool-model", "1536x1024", 3),
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
    assert "不会结束本轮 Agent" in text
    assert "最终回复不得复述" in text


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
    assert "不得查询 all" in default_payload["routing_contract"]["next_action"]
    assert "无需再使用 model 查询" in all_payload["routing_contract"]["next_action"]


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
                    "tool": {"negative_prompt_exposed": False},
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
    rejected = asyncio.run(
        plugin.image_studio_generate(
            event,
            prompt="one tree",
            parameters={"negative_prompt": "fog"},
        )
    )

    assert "negative_prompt" not in capability["parameters"]
    assert capability["negative_prompt_exposed"] is False
    assert (
        "不要传入 negative_prompt" in capability["prompt_contract"]["negative_prompt"]
    )
    assert not success.isError
    assert captured[0]["negative_prompt"] == "low quality"
    assert rejected.isError
    assert "未在当前模型的工具配置中向 LLM 开放" in rejected.content[0].text


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
    assert capability["negative_prompt_exposed"] is True
    assert not result.isError
    assert captured["negative_prompt"] == "fog"
    assert "negative_prompt" not in captured["parameters"]


def test_unsupported_model_rejects_dynamic_negative_prompt() -> None:
    plugin = object.__new__(ImageStudioPlugin)
    plugin._settings = settings()
    plugin._service = SimpleNamespace()
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
    assert result.isError
    assert "当前模型不支持专用反向提示词" in result.content[0].text


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

    assert payload["query_type"] == "default"
    assert [item["model_ref"] for item in payload["models"]] == ["provider:visible"]
    assert payload["default_model_refs"] == {
        "text2img": "provider:visible",
        "img2img": "",
    }
    assert "不会结束本轮 Agent" in payload["workflow_contract"]["delivery_tool"]
    assert "最终回复不得复述" in payload["workflow_contract"]["delivery_tool"]


def test_capabilities_excludes_zero_limit_model_from_img2img() -> None:
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
        "provider:positive-limit"
    ]


def test_registered_image_tool_descriptions_contain_routing_contract() -> None:
    capabilities = llm_tools.get_func("image_studio_get_capabilities")
    generate = llm_tools.get_func("image_studio_generate")
    light_tools = llm_tools.get_full_tool_set().get_light_tool_set()

    assert llm_tools.get_func("image_gen_get_capabilities") is None
    assert llm_tools.get_func("image_gen_generate") is None
    assert capabilities is not None
    assert generate is not None
    assert "每次调用 image_studio_generate 前" in capabilities.description
    assert "常规生图必须首先使用" in capabilities.description
    assert "不得继续查询 all" in capabilities.description
    assert "query_type" in capabilities.parameters["properties"]
    assert "image_studio_generate" in capabilities.description
    assert "每次生成前都必须先调用" in generate.description
    assert "常规" in generate.description
    assert "不得查询" in generate.description
    assert "image_studio_get_capabilities" in generate.description
    assert "不要编造" in generate.description
    assert "不会结束本轮 Agent" in generate.description
    assert "最终回复不得复述" in generate.description
    assert "negative_prompt" not in generate.parameters["properties"]
    assert (
        light_tools.get_tool("image_studio_get_capabilities").description
        == capabilities.description
    )
    assert (
        light_tools.get_tool("image_studio_generate").description
        == generate.description
    )


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
