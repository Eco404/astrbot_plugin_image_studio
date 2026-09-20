"""AstrBot Image Studio plugin entry point."""

import asyncio
import base64
import copy
import hashlib
import json
import math
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

import aiohttp
import mcp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import File, Image, Plain, Record, Reply, Video
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.agent.tool import ToolSet
from astrbot.core.computer.computer_client import get_booter
from astrbot.core.utils.quoted_message import extract_quoted_message_images
from astrbot.core.workspace import (
    default_workspace_root,
    resolve_workspace_root_for_umo,
)

from .backend.config import (
    load_studio_settings,
    runtime_settings,
)
from .backend.tools.capabilities import query_capabilities
from .backend.tools.image_context import ImageContextAdapter, track_image_result
from .backend.commands.handler import run_image_command
from .backend.api.comfyui import ComfyAPI
from .backend.api.settings import SettingsAPI
from .backend.api.gallery import GalleryAPI
from .backend.api.imports import ImportsAPI
from .backend.api.generation import GenerationAPI
from .backend.api import preferences
from .backend.api.routes import register_web_apis
from .backend.gallery.external import ExternalGalleryManager
from .backend.models import (
    ImageProvider,
    InvocationSource,
    ReferenceImage,
)
from .backend.providers.executor import ProviderError, ProviderExecutor
from .backend.generation.comfyui_runtime import ComfyRuntime
from .backend.providers.comfyui.job_types import RECOVERY_SECONDS
from .backend.generation.service import ImageGenerationService
from .backend.gallery.store import GenerationStore
from .backend.media.images import export_image_filename, image_data_url

PLUGIN_NAME = "astrbot_plugin_image_studio"
PAGE_PREFIX = f"/{PLUGIN_NAME}"
LOG_TAG = "[ImageStudio]"
CAPABILITY_QUERY_EXTRA_KEY = "_image_studio_capability_queries"
AGENT_RESULT_RECOVERY_EXTRA_KEY = "_image_studio_result_recovery"
AGENT_DELIVERY_TOOLS_EXTRA_KEY = "_image_studio_delivery_tools"


class _AgentDeliveryToolSet(ToolSet):
    """Own the request's full, light and parameter-only tool list views."""

    def __init__(self, tools):
        super().__init__(list(tools))
        self.views = [self]

    def get_light_tool_set(self):
        view = super().get_light_tool_set()
        self.views.append(view)
        return view

    def get_param_only_tool_set(self):
        view = super().get_param_only_tool_set()
        self.views.append(view)
        return view


@register(
    PLUGIN_NAME,
    "econeco",
    "多服务商 AI 生图与图库工作台，支持 OpenAI、Gemini、ComfyUI 工作流、NovelAI 官方、NAI 第三方及自定义接口。支持对话生图改图、并发批量生成、图片参数导入与工作流复现，可扫描 nai-image 插件图库及自定义目录，统一浏览和管理图片。WebUI 适配桌面与手机。",
    "1.4.2",
)
class ImageStudioPlugin(Star):
    """Own Image Studio configuration, generation, gallery, and tool APIs."""

    def __init__(
        self, context: Context, config: AstrBotConfig | dict[str, Any]
    ) -> None:
        super().__init__(context, config)
        self.config = config
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.store = GenerationStore(Path(self.data_dir))
        self._session: aiohttp.ClientSession | None = None
        self._service: ImageGenerationService | None = None
        self._comfy: ComfyRuntime | None = None
        self._settings_lock = asyncio.Lock()
        self._storage_maintenance_lock = asyncio.Lock()
        self._maintenance_task: asyncio.Task[None] | None = None
        self._studio_settings, studio_errors = load_studio_settings(Path(self.data_dir))
        self._settings, runtime_errors = runtime_settings(config, self._studio_settings)
        self._settings_errors = [*studio_errors, *runtime_errors]
        self._external_gallery = ExternalGalleryManager(self.store, Path(self.data_dir))
        self._image_context_adapter = ImageContextAdapter(logger)

    async def initialize(self) -> None:
        """Initialize storage, HTTP resources, and Page API routes."""

        await self.store.initialize()
        await self._configure_external_gallery()
        self._session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=12, limit_per_host=6),
            timeout=aiohttp.ClientTimeout(total=180),
        )
        self._service = ImageGenerationService(
            settings=self._settings,
            executor=ProviderExecutor(self._session),
            store=self.store,
        )
        self._comfy = ComfyRuntime(self._service)
        self._service.comfy_runtime = self._comfy
        # The gallery has initialized the shared schema. Clear expired terminal
        # resources before queue recovery; pending/unknown jobs remain protected.
        await self._run_storage_maintenance()
        await self._comfy.start()
        self._maintenance_task = asyncio.create_task(
            self._maintenance_loop(), name="image-studio-maintenance"
        )
        self._register_web_apis()
        await self._external_gallery.start()
        self._image_context_adapter.install()
        if self._settings_errors:
            logger.warning(
                "%s 配置存在问题: %s", LOG_TAG, "; ".join(self._settings_errors)
            )
        logger.info(
            "%s 已初始化 providers=%s history=%s",
            LOG_TAG,
            len(self._settings.providers),
            self._settings.history.enabled,
        )

    async def terminate(self) -> None:
        """Close plugin-owned HTTP resources during reload or shutdown."""

        if getattr(self, "_image_context_adapter", None) is not None:
            self._image_context_adapter.restore()
        await self._external_gallery.close()
        if self._comfy is not None:
            await self._comfy.close()
            self._comfy = None
        if self._maintenance_task is not None:
            self._maintenance_task.cancel()
            try:
                await self._maintenance_task
            except asyncio.CancelledError:
                pass
        self._maintenance_task = None
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
        self._service = None
        if getattr(self, "_imports_controller", None) is not None:
            self._imports_controller.clear()
        await self.store.close()

    async def _configure_external_gallery(self) -> None:
        await self._external_gallery.configure(
            self._studio_settings.get("external_sources", {}),
            preview_max_edge=self._settings.asset_preview_max_edge,
            preview_quality=self._settings.asset_preview_quality,
        )

    async def _maintenance_loop(self) -> None:
        while True:
            await asyncio.sleep(3600)
            try:
                await self._run_storage_maintenance()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("%s 定时存储维护失败: %s", LOG_TAG, type(exc).__name__)

    async def _run_storage_maintenance(self, *, deep: bool = False) -> dict[str, Any]:
        """One cleanup order for startup, scheduled runs and the settings action."""

        async with self._storage_maintenance_lock:
            removed = 0
            if getattr(self, "_comfy", None) is not None:
                removed = await self._comfy.store.cleanup_terminal_files(
                    terminal_before=time.time() - RECOVERY_SECONDS
                )
            await self._expire_import_groups()
            # Release task dependencies first; the gallery then expires imports,
            # applies quotas and sweeps shared payloads only after owners vanish.
            return await self.store.run_maintenance(
                self._settings.history,
                preview_max_edge=self._settings.asset_preview_max_edge,
                preview_quality=self._settings.asset_preview_quality,
                deep=deep,
                extra_repaired={"comfy_files": removed},
            )

    def _register_web_apis(self) -> None:
        register_web_apis(self, PAGE_PREFIX)

    def _service_or_raise(self) -> ImageGenerationService:
        if self._service is None:
            raise RuntimeError("Image Studio 正在初始化，请稍后重试")
        return self._service

    def _comfy_or_raise(self):
        if getattr(self, "_comfy", None) is None:
            self._comfy = ComfyRuntime(self._service_or_raise())
            self._service_or_raise().comfy_runtime = self._comfy
        return self._comfy

    def _apply_settings(self, studio):
        self._studio_settings = studio
        self._settings, self._settings_errors = runtime_settings(self.config, studio)
        self._service_or_raise().update_settings(self._settings)

    def _settings_api(self):
        return SettingsAPI(
            config=self.config,
            data_dir=self.data_dir,
            settings_lock=self._settings_lock,
            get_settings=lambda: self._settings,
            get_studio=lambda: self._studio_settings,
            apply_settings=self._apply_settings,
            get_external=lambda: self._external_gallery,
            configure_external=self._configure_external_gallery,
        )

    def _import_api(self):
        if getattr(self, "_imports_controller", None) is None:
            self._imports_controller = ImportsAPI(
                store=self.store, get_settings=lambda: self._settings
            )
        return self._imports_controller

    def _gallery_api(self):
        if getattr(self, "_gallery_controller", None) is None:
            self._gallery_controller = GalleryAPI(
                store=self.store,
                get_settings=lambda: self._settings,
                get_service=self._service_or_raise,
                get_external=lambda: self._external_gallery,
                run_maintenance=self._run_storage_maintenance,
            )
        return self._gallery_controller

    def _generation_api(self):
        return GenerationAPI(
            store=self.store,
            get_settings=lambda: self._settings,
            get_service=self._service_or_raise,
            peek_service=lambda: self._service,
            serialize_result=_result_payload,
        )

    def _comfy_api(self):
        """Build a stateless API controller around current plugin services."""
        return ComfyAPI(
            get_settings=lambda: self._settings,
            store=self.store,
            get_service=self._service_or_raise,
            get_runtime=self._comfy_or_raise,
            serialize_result=_result_payload,
        )

    async def _api_comfy_import(self):
        return await self._comfy_api()._api_comfy_import()

    async def _api_comfy_inspect(self):
        return await self._comfy_api()._api_comfy_inspect()

    async def _api_comfy_submit(self):
        return await self._comfy_api()._api_comfy_submit()

    async def _api_comfy_jobs(self):
        return await self._comfy_api()._api_comfy_jobs()

    async def _api_comfy_cancel(self):
        return await self._comfy_api()._api_comfy_cancel()

    async def _api_comfy_resume(self):
        return await self._comfy_api()._api_comfy_resume()

    async def _api_comfy_dismiss(self):
        return await self._comfy_api()._api_comfy_dismiss()

    async def _api_comfy_save_workflow(self):
        return await self._settings_api()._api_comfy_save_workflow()

    async def _api_get_appearance(self) -> Any:
        return await preferences._api_get_appearance()

    async def _api_set_appearance(self) -> Any:
        return await preferences._api_set_appearance()

    async def _api_get_gallery_preferences(self) -> Any:
        return await preferences._api_get_gallery_preferences()

    async def _api_set_gallery_preferences(self) -> Any:
        return await preferences._api_set_gallery_preferences()

    async def _api_bootstrap(self) -> Any:
        return await self._settings_api()._api_bootstrap()

    async def _api_get_settings(self) -> Any:
        return await self._settings_api()._api_get_settings()

    async def _api_save_settings(self) -> Any:
        return await self._settings_api()._api_save_settings()

    async def _api_storage_health(self) -> Any:
        return await self._gallery_api()._api_storage_health()

    async def _api_external_status(self) -> Any:
        return await self._gallery_api()._api_external_status()

    async def _api_external_scan(self) -> Any:
        return await self._gallery_api()._api_external_scan()

    async def _api_gallery_as_reference(self) -> Any:
        return await self._gallery_api()._api_gallery_as_reference()

    async def _api_import_inspect(self) -> Any:
        return await self._import_api()._api_import_inspect()

    async def _api_import_node_rules(self) -> Any:
        return await self._import_api()._api_import_node_rules()

    async def _api_import_node_rule_preview(self) -> Any:
        return await self._import_api()._api_import_node_rule_preview()

    async def _api_import_node_rule_save(self) -> Any:
        return await self._import_api()._api_import_node_rule_save()

    async def _api_import_node_rule_delete(self) -> Any:
        return await self._import_api()._api_import_node_rule_delete()

    async def _api_import_check(self) -> Any:
        return await self._import_api()._api_import_check()

    async def _api_import_merge_targets(self) -> Any:
        return await self._import_api()._api_import_merge_targets()

    async def _api_import_prepare(self) -> Any:
        return await self._import_api()._api_import_prepare()

    async def _api_import_upload(self, upload_id: str) -> Any:
        return await self._import_api()._api_import_upload(upload_id)

    async def _expire_import_groups(self) -> None:
        return await self._import_api()._expire_import_groups()

    async def _api_import_group_cancel(self, group_id: str) -> Any:
        return await self._import_api()._api_import_group_cancel(group_id)

    async def _api_import_group_commit(self, group_id: str) -> Any:
        return await self._import_api()._api_import_group_commit(group_id)

    async def _api_resolve_parameters(self) -> Any:
        return await self._import_api()._api_resolve_parameters()

    async def _api_gallery_import_edit(self, generation_id: str) -> Any:
        return await self._import_api()._api_gallery_import_edit(generation_id)

    async def _api_gallery_parameters(self, generation_id: str) -> Any:
        return await self._import_api()._api_gallery_parameters(generation_id)

    async def _api_gallery_favorite(self) -> Any:
        return await self._gallery_api()._api_gallery_favorite()

    async def _api_gallery_title(self) -> Any:
        return await self._gallery_api()._api_gallery_title()

    async def _api_gallery_favorite_status(self) -> Any:
        return await self._gallery_api()._api_gallery_favorite_status()

    async def _api_gallery_delete_images(self) -> Any:
        return await self._gallery_api()._api_gallery_delete_images()

    async def _api_storage_maintenance(self) -> Any:
        return await self._gallery_api()._api_storage_maintenance()

    async def _api_upload_reference(self) -> Any:
        return await self._generation_api()._api_upload_reference()

    async def _api_generate(self) -> Any:
        return await self._generation_api()._api_generate()

    async def _api_test_model(self) -> Any:
        return await self._generation_api()._api_test_model()

    async def _api_provider_models(self) -> Any:
        return await self._generation_api()._api_provider_models()

    async def _api_provider_quota(self) -> Any:
        return await self._generation_api()._api_provider_quota()

    def _test_request(self, provider: ImageProvider, model_id: str):
        return GenerationAPI._test_request(provider, model_id)

    async def _api_gallery_list(self) -> Any:
        return await self._gallery_api()._api_gallery_list()

    async def _api_gallery_detail(self, generation_id: str) -> Any:
        return await self._gallery_api()._api_gallery_detail(generation_id)

    async def _api_gallery_assets(self, generation_id: str) -> Any:
        return await self._gallery_api()._api_gallery_assets(generation_id)

    async def _api_gallery_image_sequence(self) -> Any:
        return await self._gallery_api()._api_gallery_image_sequence()

    async def _api_gallery_image(self, image_id: str) -> Any:
        return await self._gallery_api()._api_gallery_image(image_id)

    async def _api_gallery_image_download(self, image_id: str) -> Any:
        return await self._gallery_api()._api_gallery_image_download(image_id)

    async def _api_gallery_image_info(self, image_id: str) -> Any:
        return await self._gallery_api()._api_gallery_image_info(image_id)

    async def _api_gallery_reference_image(self, reference_id: str) -> Any:
        return await self._gallery_api()._api_gallery_reference_image(reference_id)

    async def _api_gallery_reproduce(self, generation_id: str) -> Any:
        return await self._gallery_api()._api_gallery_reproduce(generation_id)

    async def _api_gallery_delete(self) -> Any:
        return await self._gallery_api()._api_gallery_delete()

    async def _api_gallery_delete_preview(self) -> Any:
        return await self._gallery_api()._api_gallery_delete_preview()

    async def _api_reference_delete(self) -> Any:
        return await self._gallery_api()._api_reference_delete()

    async def _api_gallery_export(self) -> Any:
        return await self._gallery_api()._api_gallery_export()

    async def _api_download_export(self, export_id: str) -> Any:
        return await self._gallery_api()._api_download_export(export_id)

    @filter.command("istudio")
    async def image_gen(self, event: AstrMessageEvent):
        """Generate one or more images through the configured default provider."""

        async for result in run_image_command(
            event,
            get_service=self._service_or_raise,
            get_settings=lambda: self._settings,
            resolve_sources=self._command_reference_sources,
            resolve_references=self._event_references,
            invocation_source=_invocation_source,
        ):
            yield result

    async def _command_reference_sources(
        self, event: AstrMessageEvent, explicit_references: list[str]
    ) -> list[tuple[Any, bool]]:
        messages = event.get_messages() if hasattr(event, "get_messages") else []
        sources = [(item, False) for item in messages if isinstance(item, Image)]
        for item in messages:
            if isinstance(item, Reply):
                sources.extend((image, False) for image in _iter_event_images(item))
        try:
            quoted = await extract_quoted_message_images(event)
        except Exception as exc:
            logger.debug("%s 指令引用图片解析失败: %s", LOG_TAG, type(exc).__name__)
            quoted = []
        sources.extend((item, False) for item in quoted)
        sources.extend((item, False) for item in _provider_request_image_refs(event))
        sources.extend((item, True) for item in explicit_references)
        return sources

    async def _event_references(
        self,
        event: AstrMessageEvent,
        explicit_references: list[Any] | None = None,
        *,
        include_event_references: bool = True,
        ordered_sources: list[tuple[Any, bool]] | None = None,
        max_images: int = 8,
        reject_excess: bool = False,
    ) -> tuple[Any, ...]:
        def limit_error() -> ValueError:
            return ValueError(
                f"当前模型/工具最多允许 {max_images} 张参考图；请减少输入，避免底图、蒙版与角色参考编号错位"
            )

        # Explicit entries have positional meaning, including duplicate images
        # assigned to different NovelAI roles. Validate their count before I/O.
        required_count = (
            sum(required for _, required in ordered_sources)
            if ordered_sources is not None
            else len(explicit_references or [])
        )
        if reject_excess and required_count > max_images:
            raise limit_error()
        service = self._service_or_raise()
        references: list[Any] = []
        seen: set[str] = set()
        # Automatic discovery can encounter aliases for the same image. Read
        # one additional unique image to detect overflow after deduplication.
        read_limit = max_images + int(reject_excess)

        def result() -> tuple[Any, ...]:
            if reject_excess and len(references) > max_images:
                raise limit_error()
            return tuple(references[:max_images])

        workspace_root = await _event_workspace_root(
            event,
            getattr(self, "context", None),
        )

        async def append_reference(raw_ref: str, *, required: bool = False) -> bool:
            if len(references) >= read_limit:
                return True
            try:
                reference = await service.reference_from_media_ref(
                    raw_ref,
                    workspace_root=workspace_root,
                )
            except Exception as exc:
                if required:
                    raise ValueError("显式参考图无法读取或不在允许的目录内") from exc
                logger.debug(
                    "%s 忽略无法读取的事件参考图: %s",
                    LOG_TAG,
                    type(exc).__name__,
                )
                return False
            if reference is None:
                if required:
                    raise ValueError("显式参考图不存在或不是有效图片")
                return False
            digest = hashlib.sha256(reference.data).hexdigest()
            if required or digest not in seen:
                seen.add(digest)
                references.append(reference)
            return True

        if ordered_sources is not None:
            for source, required in ordered_sources:
                if len(references) >= read_limit:
                    break
                if isinstance(source, Image):
                    try:
                        source = await source.convert_to_file_path()
                    except Exception as exc:
                        logger.debug(
                            "%s 指令消息图片无法读取: %s", LOG_TAG, type(exc).__name__
                        )
                        continue
                await append_reference(str(source or ""), required=required)
            return result()

        for item in (explicit_references or [])[:read_limit]:
            if isinstance(item, dict):
                asset_id = str(item.get("asset_id") or "").strip().lower()
                path = str(item.get("path") or "").strip()
                if bool(asset_id) == bool(path):
                    raise ValueError("参考图必须且只能填写 asset_id 或 path")
                if asset_id:
                    loaded = await self.store.load_workflow_image(
                        asset_id,
                        scope_id=_event_scope_id(event),
                        detail="original",
                        preview_max_edge=self._settings.asset_preview_max_edge,
                        preview_quality=self._settings.asset_preview_quality,
                    )
                    if loaded is None:
                        raise ValueError(
                            "参考图资产不存在、原图无法读取或不属于当前会话"
                        )
                    image, internal_path = loaded
                    digest = hashlib.sha256(image.data).hexdigest()
                    seen.add(digest)
                    references.append(
                        ReferenceImage(
                            id=f"asset-{asset_id[:24]}",
                            filename=Path(internal_path).name,
                            data=image.data,
                            mime_type=image.mime_type,
                        )
                    )
                    continue
                await append_reference(path, required=True)
                continue
            await append_reference(str(item or ""), required=True)
        if not include_event_references:
            return result()
        message_chain = event.get_messages() if hasattr(event, "get_messages") else []
        for component in _iter_event_images(message_chain):
            try:
                path = await component.convert_to_file_path()
                await append_reference(path)
            except Exception as exc:
                logger.debug(
                    "%s 忽略无法转换的消息图片: %s",
                    LOG_TAG,
                    type(exc).__name__,
                )
                continue

        try:
            quoted_refs = await extract_quoted_message_images(event)
        except Exception as exc:
            logger.debug(
                "%s AstrBot 引用图片解析失败: %s",
                LOG_TAG,
                type(exc).__name__,
            )
            quoted_refs = []
        for raw_ref in quoted_refs:
            await append_reference(raw_ref)
        for raw_ref in _provider_request_image_refs(event):
            await append_reference(raw_ref)
        return result()

    @filter.on_agent_begin()
    async def prepare_agent_image_context(
        self, event: AstrMessageEvent, run_context: Any
    ) -> None:
        """Scope image caption adaptation to this plugin's current agent event."""
        adapter = getattr(self, "_image_context_adapter", None)
        if self._settings.enable_llm_tool and adapter is not None:
            adapter.bind(event, run_context)

    @filter.on_llm_request()
    async def prepare_agent_delivery_tools(
        self, event: AstrMessageEvent, req: Any
    ) -> None:
        """Retain an isolated full tool set before the runner creates light schemas.

        This hook changes neither the available tool names nor prompt contents.
        No tools are filtered until this message obtains generated image assets.
        """
        if not self._settings.enable_llm_tool:
            return
        tools = getattr(getattr(req, "func_tool", None), "tools", None)
        if not isinstance(tools, list) or not any(
            getattr(tool, "name", "") in {"image_studio_generate", "image_studio_task"}
            for tool in tools
        ):
            return
        owned = _AgentDeliveryToolSet(tools)
        req.func_tool = owned
        _set_event_extra(event, AGENT_DELIVERY_TOOLS_EXTRA_KEY, (req, owned))

    @filter.llm_tool(name="image_studio_get_capabilities")
    async def image_studio_get_capabilities(
        self,
        event: AstrMessageEvent,
        query_type: str = "default",
        mode: str = "",
        model_refs: list[str] | None = None,
        query: str = "",
        provider_id: str = "",
        provider_kind: str = "",
        limit: int = 10,
        offset: int = 0,
    ) -> mcp.types.CallToolResult:
        """查询模型或工作流在 Image Studio 中允许使用的生成模式、参数和提示词要求。

        准备生图时，用户未指定模型则先设置 query_type="default" 查询默认模型。
        用户指定模型或工作流名称时，先设置 query_type="search" 搜索，再用 query_type="model" 和 model_refs 查询选定模型。
        仅当默认模型不支持所需功能时查找其他模型；普通画风或画面描述不是更换默认模型的理由。
        query_type="search" 或 "providers" 只返回目录信息，不包含可直接用于生成的参数说明。

        Args:
            query_type(string): default(查询默认模型参数)/model(使用model_refs查询指定模型参数)/providers(查询服务商)/search(搜索模型或工作流)/all(需要比较全部模型时查询完整参数); 默认 default
            mode(string): 按 text2img/img2img 筛选；省略时查询两种模式
            model_refs(array[string]): 1 至 20 个 provider_id:model_id；query_type=model 时必填，各项独立返回成功结果或错误说明
            query(string): 模型或工作流名称、ID、使用说明，或服务商名称、ID；仅 query_type="search" 或 "providers" 使用。留空时不按关键词筛选，结果仍按 limit 和 offset 分页返回
            provider_id(string): 限定服务商 ID；仅 search/providers 使用
            provider_kind(string): 限定服务商类型，例如 comfyui；仅 search/providers 使用
            limit(number): search/providers 每页数量，整数 1 至 50，默认 10
            offset(number): search/providers 起始位置，非负整数，默认 0
        """

        try:
            payload = query_capabilities(
                self._settings,
                query_type=query_type,
                mode=mode,
                model_refs=model_refs,
                query=query,
                provider_id=provider_id,
                provider_kind=provider_kind,
                limit=limit,
                offset=offset,
            )
        except ValueError as exc:
            return _tool_error(str(exc))
        if payload["query_type"] in {"default", "all", "model"} and payload["models"]:
            _remember_capability_query(
                event, self._settings.revision, payload["models"]
            )
        return _tool_json(payload)

    @filter.llm_tool(name="image_studio_generate")
    async def image_studio_generate(
        self,
        event: AstrMessageEvent,
        prompt: str = "",
        mode: str = "",
        model_ref: str = "",
        parameters: dict[str, Any] | None = None,
        references: list[dict[str, Any]] | None = None,
        request_id: str = "",
    ) -> mcp.types.CallToolResult:
        """生成图片或提交 ComfyUI 工作流，返回图片资产或任务状态。

        调用前须通过 image_studio_get_capabilities 查询目标模型在所选生成模式下可用的参数与提示词要求。
        在处理当前这条用户消息期间，已查询过的模型和模式不必重复查询；修改 prompt 或 parameters 的值也不必重查。
        处理新的用户消息、使用尚未查询的模型或模式，或插件设置发生变化后，需要重新查询。

        Args:
            prompt(string): 生图提示词，格式遵循能力查询结果
            mode(string): 请填写 text2img（文生图）或 img2img（图生图）
            model_ref(string): 模型编号，格式为 provider_id:model_id；省略时使用 mode 对应的默认模型
            parameters(object): 仅填写本次查询结果为该模型列出的参数，使用其中的字段名
            references(array[object]): 参考图片列表；每项填写图片编号 asset_id，或当前工作区文件的路径 path，二者只能选一个
            request_id(string): 可选，仅支持 ComfyUI；同一会话中以相同 request_id 和相同请求参数再次调用时，返回原任务而不新建任务；希望生成新图片时使用新的 request_id 或省略此参数

        Returns:
            Original asset metadata and optional MCP preview content for Agent workflows.
        """

        if not self._settings.enable_llm_tool:
            return _tool_error("Image Studio 的 LLM 生图工具已关闭。")
        try:
            model_ref = _generation_model_ref(model_ref)
            if references is None:
                explicit_references: list[Any] = []
            elif isinstance(references, (list, tuple)):
                explicit_references = list(references)
            else:
                raise ValueError("references 必须是数组")
            requested_mode = str(mode or "").strip()
            normalized_requested_mode = (
                _llm_mode(requested_mode) if requested_mode else ""
            )
            reference_options: dict[str, Any] = {}
            if normalized_requested_mode != "text2img":
                try:
                    reference_provider, reference_model, _ = _select_llm_tool_model(
                        self._settings, "img2img", model_ref=model_ref
                    )
                except ValueError:
                    # With an omitted mode and no explicit images, retain the
                    # existing message-image fallback and text-mode inference.
                    if normalized_requested_mode or explicit_references:
                        raise
                else:
                    if reference_provider.kind in {"novelai_official", "comfyui"}:
                        reference_options = {
                            "max_images": reference_model.llm_max_reference_images,
                            "reject_excess": True,
                        }
            resolved_references = await self._event_references(
                event,
                explicit_references,
                include_event_references=(
                    not explicit_references and normalized_requested_mode != "text2img"
                ),
                **reference_options,
            )
            normalized_mode = normalized_requested_mode or (
                "img2img" if resolved_references else "text2img"
            )
            provider, selected_model, canonical_ref = _select_llm_tool_model(
                self._settings,
                normalized_mode,
                model_ref=model_ref,
            )
            if not _has_capability_query(
                event, self._settings.revision, canonical_ref, normalized_mode
            ):
                raise ValueError(
                    "生成前必须先调用 image_studio_get_capabilities，"
                    f'使用 query_type="model"、model_refs={json.dumps([canonical_ref], ensure_ascii=False)}、mode="{normalized_mode}" 查询该模型可用的参数与提示词要求。'
                    "查询成功后可再次调用 image_studio_generate。"
                )
            if parameters is not None and not isinstance(parameters, dict):
                raise ValueError("parameters 必须是对象")
            dynamic_parameters = {
                key: value
                for key, value in dict(parameters or {}).items()
                if key in selected_model.llm_exposed_parameter_names
            }
            has_negative_prompt = "negative_prompt" in dynamic_parameters
            supplied_negative_prompt = dynamic_parameters.pop("negative_prompt", "")
            if has_negative_prompt and not isinstance(supplied_negative_prompt, str):
                raise ValueError("negative_prompt 必须是字符串")
            effective_negative_prompt = (
                supplied_negative_prompt
                if has_negative_prompt
                else selected_model.llm_negative_prompt_default
            )
            if not isinstance(request_id, str):
                raise ValueError("request_id 必须是字符串")
            if request_id and provider.kind != "comfyui":
                raise ValueError("request_id 仅支持 ComfyUI 工作流")
            values = dict(
                mode=normalized_mode,
                provider_id=provider.id,
                prompt=prompt,
                negative_prompt=effective_negative_prompt,
                model_ref=canonical_ref,
                model="",
                size="",
                count=0,
                parameters=dynamic_parameters,
                references=resolved_references,
                source="llm_tool",
                invocation_source=(
                    _invocation_source(event)
                    if self._settings.history.record_invocation_identity
                    else None
                ),
            )
            service = self._service_or_raise()
            if provider.kind == "comfyui":
                runtime = service.comfy_runtime
                if runtime is None:
                    raise ValueError("ComfyUI 任务服务尚未就绪")
                for key in ("provider_id", "model_ref", "model"):
                    values.pop(key)
                job = await runtime.submit_agent(
                    scope_id=_event_scope_id(event),
                    request_id=request_id or None,
                    wait_seconds=0,
                    provider=provider,
                    model=selected_model,
                    **values,
                )
                return await self._agent_comfy_result(event, runtime, job)
            result = await service.generate(**values)
        except (ValueError, ProviderError) as exc:
            return mcp.types.CallToolResult(
                content=[mcp.types.TextContent(type="text", text=f"生成失败：{exc}")],
                isError=True,
            )
        except Exception as exc:
            logger.warning("%s 生图调用状态未确认: %s", LOG_TAG, type(exc).__name__)
            return _tool_json(
                {
                    "status": "generation_unknown",
                    "message": "本次未取得确定的生图结果；不要自动重复生成。",
                    "next_action": "ComfyUI 请用 image_studio_task 列出当前会话最近任务并查询；其他服务商请核对服务商与画廊记录。",
                },
                is_error=True,
            )
        return await self._agent_generation_result(
            event,
            result,
            generation_status="partial" if result.partial else "succeeded",
        )

    async def _agent_generation_result(
        self,
        event: Any,
        result: Any,
        *,
        task_id: str = "",
        generation_status: str = "succeeded",
    ) -> mcp.types.CallToolResult:
        """Register existing output, independently of the provider invocation."""
        configured_return_mode = self._settings.llm_image_return_mode
        try:
            assets = await self.store.lease_agent_images(
                result.images,
                scope_id=_event_scope_id(event),
                create_preview=configured_return_mode == "preview",
                preview_max_edge=self._settings.asset_preview_max_edge,
                preview_quality=self._settings.asset_preview_quality,
            )
            if len(assets) != len(result.images) or not assets:
                raise ValueError("原图资产保存不完整")
        except Exception as exc:
            logger.warning(
                "%s 保存 Agent 原图资产失败，已阻止临时缓存回退: %s",
                LOG_TAG,
                type(exc).__name__,
            )
            if not task_id:
                task_id = f"result_{uuid.uuid4().hex}"
                recovery = dict(
                    _get_event_extra(event, AGENT_RESULT_RECOVERY_EXTRA_KEY, {}) or {}
                )
                recovery[task_id] = (result, generation_status)
                _set_event_extra(event, AGENT_RESULT_RECOVERY_EXTRA_KEY, recovery)
            return _tool_json(
                {
                    "status": "asset_registration_failed",
                    "generation_status": generation_status,
                    "generation_id": result.generation_id,
                    "task_id": task_id,
                    "message": "图片已经生成，但原图资产保存失败；请勿重新生图。",
                    "next_action": "调用 image_studio_task，并传入本次返回的 task_id，重试取得已生成图片的 asset_id；不会再次调用生图接口。"
                    + (
                        "这个 result_ 开头的编号仅在处理当前这条用户消息期间有效。"
                        if task_id.startswith("result_")
                        else ""
                    ),
                },
                is_error=True,
            )
        _protect_generated_image_delivery(event)
        if configured_return_mode == "original":
            visual_images = result.images
        elif configured_return_mode == "preview":
            visual_images = tuple(
                asset.preview or image
                for asset, image in zip(assets, result.images, strict=True)
            )
        else:
            visual_images = ()

        content: list[Any] = []
        for image in visual_images:
            content.append(
                mcp.types.ImageContent(
                    type="image",
                    data=base64.b64encode(image.data).decode("ascii"),
                    mimeType=image.mime_type,
                )
            )
        manifest = [
            {
                "asset_id": asset.asset_id,
                "mime_type": asset.mime_type,
                "size_bytes": asset.size_bytes,
                "preview_size_bytes": (
                    len(asset.preview.data) if asset.preview is not None else 0
                ),
            }
            for asset in assets
        ]
        content.insert(
            0,
            mcp.types.TextContent(
                type="text",
                text=(
                    json.dumps(
                        {
                            "status": generation_status,
                            "origin": "tool_generated",
                            "generation_id": result.generation_id,
                            **({"task_id": task_id} if task_id else {}),
                            "return_mode": configured_return_mode,
                            "assets": manifest,
                            "warning": result.warning,
                            "message": "这些图片由你调用生图工具生成。本次调用未向当前会话发送图片。",
                            "next_action": _asset_usage_notice(configured_return_mode),
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                ),
            ),
        )
        return track_image_result(event, mcp.types.CallToolResult(content=content))

    @filter.llm_tool(name="image_studio_task")
    async def image_studio_task(
        self,
        event: AstrMessageEvent,
        task_id: str = "",
        wait_seconds: float = 0,
        resume: bool = False,
    ) -> mcp.types.CallToolResult:
        """查询当前会话的 ComfyUI 任务或取得已有结果，不会新建生图请求。

        使用已有任务编号 task_id 查询，无需先查询模型能力。

        Args:
            task_id(string): image_studio_generate 返回的任务编号；留空时列出当前会话最近任务。result_ 开头的编号用于重试取得已生成图片的 asset_id，不会重新生图，且仅在处理当前这条用户消息期间有效
            wait_seconds(number): 最多等待任务完成的秒数，范围 0 至 30；任务完成或等待时间到达后返回当前状态。默认 0，立即返回
            resume(boolean): 是否尝试继续执行同一已保存任务或读取它的结果，默认 false；不会重复提交 ComfyUI 已接收的生成任务，无法确定是否已接收时也不重复提交
        """
        if not self._settings.enable_llm_tool:
            return _tool_error("Image Studio 的 LLM 生图工具已关闭。")
        if not isinstance(task_id, str):
            return _tool_error("task_id 必须是任务编号字符串。")
        if not isinstance(resume, bool):
            return _tool_error("resume 必须是布尔值。")
        try:
            wait_seconds = _agent_wait_budget(
                getattr(self, "context", None), event, wait_seconds
            )
        except ValueError as exc:
            return _tool_error(str(exc))
        task_id = task_id.strip()
        if task_id.startswith("result_"):
            recovery = (
                _get_event_extra(event, AGENT_RESULT_RECOVERY_EXTRA_KEY, {}) or {}
            )
            saved = recovery.get(task_id)
            if saved is None:
                return _tool_error(
                    "当前消息的处理过程中没有这个可重试的图片结果；请检查已保存的画廊记录，不要自动重新生成。"
                )
            result, generation_status = saved
            return await self._agent_generation_result(
                event, result, task_id=task_id, generation_status=generation_status
            )
        try:
            runtime = self._service_or_raise().comfy_runtime
            if runtime is None:
                raise ValueError("ComfyUI 任务服务尚未就绪")
            if not task_id:
                if resume or wait_seconds != 0:
                    raise ValueError("等待或恢复任务时必须填写 task_id")
                return _tool_json(
                    {
                        "tasks": await runtime.list_agent(
                            scope_id=_event_scope_id(event)
                        ),
                        "next_action": "按 task_id 查询原任务；不要因尚未取得结果而重复生成。",
                    }
                )
            job = await runtime.inspect_agent(
                task_id,
                scope_id=_event_scope_id(event),
                wait_seconds=wait_seconds,
                resume=resume,
            )
            return await self._agent_comfy_result(event, runtime, job)
        except (ValueError, ProviderError) as exc:
            return _tool_error(str(exc))
        except Exception as exc:
            logger.warning("%s 读取 Agent 任务失败: %s", LOG_TAG, type(exc).__name__)
            return _tool_json(
                {
                    "status": "task_lookup_failed",
                    "task_id": task_id,
                    "message": "本次未能读取任务状态，不能据此判断生图失败。",
                    "next_action": "稍后查询同一 task_id，不要重新生图。",
                },
                is_error=True,
            )

    async def _agent_comfy_result(
        self, event: Any, runtime: Any, job: dict
    ) -> mcp.types.CallToolResult:
        status = job["status"]
        task_id = job["id"]
        if status in {"succeeded", "partial"}:
            try:
                result = await runtime.result(job)
            except Exception as exc:
                logger.warning(
                    "%s 读取已生成的 Agent 图片失败: %s", LOG_TAG, type(exc).__name__
                )
                return _tool_json(
                    {
                        "status": "result_unavailable",
                        "generation_status": status,
                        "task_id": task_id,
                        "generation_id": job.get("generation_id"),
                        "message": "任务已生成图片，但本次未取得原图；可能已清理或暂时无法读取。",
                        "next_action": "检查画廊记录或稍后查询同一 task_id，不要自动重新生图。",
                    },
                    is_error=True,
                )
            return await self._agent_generation_result(
                event, result, task_id=task_id, generation_status=status
            )
        summary = runtime.public_job(job)
        pending = status not in {"failed", "cancelled", "unknown"}
        return _tool_json(
            {
                "status": "pending" if pending else status,
                "task_id": task_id,
                "request_id": job.get("request", {}).get("agent", {}).get("request_id"),
                "task_status": status,
                "progress": summary.get("progress"),
                "can_resume": summary.get("can_resume", False),
                "error": summary.get("error") or "",
                "next_action": (
                    "调用 image_studio_task，并传入同一 task_id；可设置 wait_seconds=30，最多等待 30 秒后返回任务状态。若尚未完成，可继续查询同一任务，不要重复提交。"
                    if pending
                    else "调用 image_studio_task，传入同一 task_id 并设置 resume=true，尝试继续执行或读取结果；不会重复提交 ComfyUI 已接收的生成任务。"
                    if summary.get("can_resume")
                    else "任务状态不确定，需核对 ComfyUI；不要自动重新提交。"
                    if status == "unknown"
                    else "任务已结束且无法自动恢复；检查错误，重新生图需作为新的请求。"
                ),
            },
            is_error=status in {"failed", "unknown", "cancelled"},
        )

    @filter.llm_tool(name="image_studio_view_asset")
    async def image_studio_view_asset(
        self,
        event: AstrMessageEvent,
        asset_ids: list[str] | None = None,
        detail: str = "preview",
    ) -> mcp.types.CallToolResult:
        """按需批量查看当前会话有权访问的 Image Studio 图片资产。

        查看已有资产无需先查询模型能力，也不会向当前会话发送图片。

        Args:
            asset_ids(array[string]): image_studio_generate 或 image_studio_task 返回的图片编号 asset_id 列表；每次 1 至 8 个，按输入顺序返回图片
            detail(string): preview 或 original; 默认 preview。
        """

        if not self._settings.enable_llm_tool:
            return _tool_error("Image Studio 的 LLM 生图工具已关闭。")
        if not isinstance(asset_ids, list) or not asset_ids:
            return _tool_error("asset_ids 必须是包含 1 至 8 个资产 ID 的数组。")
        if len(asset_ids) > 8:
            return _tool_error("asset_ids 每次最多查看 8 个资产。")
        normalized_detail = str(detail or "preview").strip().lower()
        if normalized_detail not in {"preview", "original"}:
            return _tool_error("detail 仅支持 preview 或 original。")
        loaded_assets: list[tuple[str, Any]] = []
        failures: list[dict[str, Any]] = []
        scope_id = _event_scope_id(event)
        for index, raw_asset_id in enumerate(asset_ids):
            asset_id = str(raw_asset_id or "").strip().lower()
            try:
                loaded = await self.store.load_workflow_image_detailed(
                    asset_id,
                    scope_id=scope_id,
                    detail=normalized_detail,
                    preview_max_edge=self._settings.asset_preview_max_edge,
                    preview_quality=self._settings.asset_preview_quality,
                )
            except Exception as exc:
                logger.warning(
                    "%s 查看工作流资产失败: index=%s error=%s",
                    LOG_TAG,
                    index,
                    type(exc).__name__,
                )
                failures.append(
                    _workflow_asset_failure(index, asset_id, "storage_error")
                )
                continue
            if not loaded.ok or loaded.image is None:
                failures.append(_workflow_asset_failure(index, asset_id, loaded.status))
                continue
            loaded_assets.append((asset_id, loaded.image))
        if failures:
            return _tool_error(
                json.dumps(
                    {
                        "error": "asset_batch_unavailable",
                        "message": "部分资产无法加载，本次未返回任何图片。",
                        "failures": failures,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        manifest = [
            {
                "index": index,
                "asset_id": asset_id,
                "mime_type": image.mime_type,
                "size_bytes": len(image.data),
            }
            for index, (asset_id, image) in enumerate(loaded_assets)
        ]
        response = mcp.types.CallToolResult(
            content=[
                mcp.types.TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "origin": "existing_asset",
                            "detail": normalized_detail,
                            "count": len(manifest),
                            "assets": manifest,
                            "message": "这是已有资产的查看结果，本次没有生成新图片，也未向当前会话发送图片。",
                            "next_action": _asset_usage_notice(normalized_detail),
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                )
            ]
            + [
                mcp.types.ImageContent(
                    type="image",
                    data=base64.b64encode(image.data).decode("ascii"),
                    mimeType=image.mime_type,
                )
                for _asset_id, image in loaded_assets
            ]
        )
        return track_image_result(event, response)

    @filter.llm_tool(name="image_studio_send_output")
    async def image_studio_send_output(
        self,
        event: AstrMessageEvent,
        destination: str,
        messages: list[dict[str, Any]],
    ) -> mcp.types.CallToolResult:
        """向当前会话发送消息或产物，或将本插件原图资产复制到当前 workspace。

        使用已有资产无需先查询模型能力；复制后的工作区文件可交给其他工具处理。
        向当前会话发送图片或其他文件时，plain 项仅用于必要的随附说明，
        不要把准备作为最终回复的完整正文放入 messages；普通最终回复应直接输出。
        随附文字发送成功后，不要在后续回复或工具调用中重复发送或仅改写后再表达相同内容。

        Args:
            destination(string): session 或 workspace；必须明确填写
            messages(array[object]): 有序消息。session 支持 plain/image/record/video/file；plain 使用 text，仅填写必要附言，完整最终正文应直接回复。媒体使用 asset_id/path/url 三选一并可填 name。workspace 仅接受带 asset_id 的图片项
        """

        if not self._settings.enable_llm_tool:
            return _tool_error("Image Studio 的 LLM 生图工具已关闭。")
        normalized_destination = str(destination or "").strip().lower()
        if normalized_destination not in {"session", "workspace"}:
            return _tool_error("destination 必须明确填写 session 或 workspace。")
        if not isinstance(messages, list) or not messages:
            return _tool_error("messages 必须是非空数组。")
        try:
            if normalized_destination == "workspace":
                payload = await self._materialize_assets_to_workspace(event, messages)
                return _tool_json(payload, is_error=payload["status"] != "succeeded")
            count = await self._send_output_to_session(event, messages)
            return _tool_json(
                {
                    "status": "succeeded",
                    "destination": "session",
                    "component_count": count,
                    "message": f"已向当前会话发送 {count} 个消息组件。",
                    "notice": _session_delivery_notice(messages),
                }
            )
        except ValueError as exc:
            return _tool_error(str(exc))
        except Exception as exc:
            logger.warning("%s 工作流产物投递失败: %s", LOG_TAG, type(exc).__name__)
            return _tool_json(
                {
                    "status": "delivery_unknown",
                    "destination": normalized_destination,
                    "message": "本次投递未确认成功；重发前核对当前会话是否已经收到。",
                },
                is_error=True,
            )

    async def _load_workflow_asset(
        self, event: AstrMessageEvent, asset_id: str
    ) -> tuple[Any, str]:
        loaded = await self.store.load_workflow_image(
            str(asset_id or "").strip().lower(),
            scope_id=_event_scope_id(event),
            detail="original",
            preview_max_edge=self._settings.asset_preview_max_edge,
            preview_quality=self._settings.asset_preview_quality,
        )
        if loaded is None:
            raise ValueError("图片资产不存在、原图无法读取或不属于当前会话。")
        return loaded

    async def _materialize_assets_to_workspace(
        self, event: AstrMessageEvent, messages: list[dict[str, Any]]
    ) -> dict[str, Any]:
        runtime = _computer_runtime(self.context, event)
        if runtime not in {"local", "sandbox"}:
            raise ValueError("当前会话未启用 local 或 sandbox 计算工作区。")
        prepared = []
        if len(messages) > 20:
            raise ValueError("workspace 单次最多复制 20 个资产。")
        # Authorize and validate the complete batch before writing any file.
        for index, item in enumerate(messages):
            if not isinstance(item, dict):
                raise ValueError(f"messages[{index}] 必须是对象。")
            if str(item.get("type") or "image").strip().lower() != "image":
                raise ValueError(
                    'destination="workspace" 时，只能复制由 Image Studio 保存的图片。'
                )
            asset_id = str(item.get("asset_id") or "").strip().lower()
            if not asset_id or item.get("path") or item.get("url") or item.get("text"):
                raise ValueError(
                    'destination="workspace" 时，messages 中的每一项只能使用 asset_id 指定图片，并可填写 name 设置文件名。'
                )
            image, internal_path = await self._load_workflow_asset(event, asset_id)
            name = _safe_output_filename(item.get("name"), Path(internal_path).name)
            prepared.append((index, asset_id, image, internal_path, name))
        results: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        workspace_root = (
            await _event_workspace_root(event, self.context)
            if runtime == "local"
            else None
        )
        booter = (
            await get_booter(self.context, event.unified_msg_origin)
            if runtime == "sandbox"
            else None
        )
        for index, asset_id, image, internal_path, name in prepared:
            try:
                if runtime == "local":
                    assert workspace_root is not None
                    target = await asyncio.to_thread(
                        _write_unique_workspace_file,
                        workspace_root / "image-studio-assets",
                        name,
                        image.data,
                    )
                    path = str(target.relative_to(workspace_root))
                else:
                    assert booter is not None
                    uploaded = await booter.upload_file(internal_path, name)
                    if not isinstance(uploaded, dict) or not uploaded.get("success"):
                        raise ValueError("资产上传到当前 sandbox workspace 失败。")
                    path = str(uploaded.get("file_path") or "").strip()
                    if not path:
                        raise ValueError("sandbox 未返回可用的 workspace 路径。")
                results.append({"index": index, "asset_id": asset_id, "path": path})
            except Exception as exc:
                failures.append(
                    {"index": index, "asset_id": asset_id, "error": type(exc).__name__}
                )
        return {
            "destination": "workspace",
            "status": "partial"
            if failures and results
            else "failed"
            if failures
            else "succeeded",
            "workspace_paths": [item["path"] for item in results],
            "results": results,
            "failures": failures,
            "notice": "已成功复制的路径可直接使用；失败项重试前核对目标目录，不要重新生图或重复复制成功项。"
            if failures
            else "资产已复制，可继续用于当前任务。",
        }

    async def _send_output_to_session(
        self, event: AstrMessageEvent, messages: list[dict[str, Any]]
    ) -> int:
        runtime = _computer_runtime(self.context, event)
        workspace_root = (
            await _event_workspace_root(event, self.context)
            if runtime == "local"
            else None
        )
        booter = None
        components: list[Any] = []
        temporary_files: list[Path] = []
        try:
            if len(messages) > 40:
                raise ValueError("session 单次最多投递 40 个消息组件。")
            for index, item in enumerate(messages):
                if not isinstance(item, dict):
                    raise ValueError(f"messages[{index}] 必须是对象。")
                kind = str(item.get("type") or "").strip().lower()
                if kind == "plain":
                    text = str(item.get("text") or "").strip()
                    if not text:
                        raise ValueError(f"messages[{index}] 的 plain 文本为空。")
                    components.append(Plain(text))
                    continue
                if kind not in {"image", "record", "video", "file"}:
                    raise ValueError(f"messages[{index}] 的类型不受支持。")
                asset_id = str(item.get("asset_id") or "").strip().lower()
                path = str(item.get("path") or "").strip()
                url = str(item.get("url") or "").strip()
                if sum(bool(value) for value in (asset_id, path, url)) != 1:
                    raise ValueError(
                        f"messages[{index}] 必须且只能填写 asset_id、path 或 url。"
                    )
                if asset_id:
                    if kind != "image":
                        raise ValueError("Image Studio asset_id 只能作为 image 发送。")
                    image, _internal_path = await self._load_workflow_asset(
                        event, asset_id
                    )
                    components.append(Image.fromBytes(image.data))
                    continue
                if url:
                    if not url.lower().startswith(("http://", "https://")):
                        raise ValueError("媒体 URL 仅支持 http 或 https。")
                    components.append(_url_component(kind, url, item.get("name")))
                    continue
                if runtime == "sandbox" and booter is None:
                    booter = await get_booter(self.context, event.unified_msg_origin)
                local_path = await _resolve_delivery_path(
                    path,
                    runtime=runtime,
                    workspace_root=workspace_root,
                    booter=booter,
                    delivery_dir=self.store.delivery_dir,
                )
                if runtime == "sandbox":
                    temporary_files.append(local_path)
                components.append(_file_component(kind, local_path, item.get("name")))
            await event.send(MessageChain(chain=components))
        finally:
            for path in temporary_files:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
        return len(components)


def _generation_model_ref(value: Any) -> str:
    """Normalize accidental singleton arguments without changing the tool schema."""

    if isinstance(value, list):
        if len(value) != 1 or not isinstance(value[0], str) or not value[0].strip():
            raise ValueError("model_ref 必须是字符串，例如 provider_id:model_id。")
        value = value[0]
    if not isinstance(value, str):
        raise ValueError("model_ref 必须是字符串，例如 provider_id:model_id。")
    return value.strip()


def _tool_json(
    payload: dict[str, Any], *, is_error: bool = False
) -> mcp.types.CallToolResult:
    result = _tool_text_result(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )
    result.isError = is_error
    return result


def _tool_error(message: str) -> mcp.types.CallToolResult:
    return mcp.types.CallToolResult(
        content=[mcp.types.TextContent(type="text", text=message)], isError=True
    )


def _tool_text_result(message: str) -> mcp.types.CallToolResult:
    return mcp.types.CallToolResult(
        content=[mcp.types.TextContent(type="text", text=message)]
    )


def _workflow_asset_failure(index: int, asset_id: str, reason: str) -> dict[str, Any]:
    messages = {
        "invalid_asset_id": "资产 ID 格式错误",
        "not_found": "没有对应的资产记录",
        "access_denied": "当前会话无权访问该资产",
        "file_missing": "资产记录存在，但原图文件缺失",
        "decode_failed": "原图文件存在，但无法解析为图片",
        "storage_error": "数据库或文件系统发生临时异常",
    }
    normalized_reason = reason if reason in messages else "storage_error"
    return {
        "index": index,
        "asset_id": str(asset_id or "")[:128],
        "reason": normalized_reason,
        "message": messages[normalized_reason],
        "retryable": normalized_reason == "storage_error",
    }


def _protect_generated_image_delivery(event: Any) -> None:
    """Restrict conflicting senders only after usable generated assets exist.

    Keep the change on this ProviderRequest. Tool definitions and the original
    set may also be used by other events; never change a global tool's active
    flag or mutate another request's tool list.
    """
    request = _get_event_extra(event, "provider_request")
    tool_set = getattr(request, "func_tool", None)
    getter = getattr(tool_set, "get_tool", None)
    tools = getattr(tool_set, "tools", None)
    if not callable(getter) or not isinstance(tools, list):
        return
    sender = getter("image_studio_send_output")
    if sender is None or not getattr(sender, "active", True):
        return
    blocked = {"send_message_to_user", "pc_send_current_media"}
    if not any(getattr(tool, "name", "") in blocked for tool in tools):
        return
    prepared = _get_event_extra(event, AGENT_DELIVERY_TOOLS_EXTRA_KEY)
    if isinstance(prepared, tuple) and len(prepared) == 2 and prepared[0] is request:
        # In skills_like mode the runner resolves handlers from this original
        # full set even after req.func_tool has become a light schema copy.
        owned = prepared[1]
        for view in owned.views:
            view.tools = [
                tool for tool in view.tools if getattr(tool, "name", "") not in blocked
            ]
        if any(view is tool_set for view in owned.views):
            return
    protected = copy.copy(tool_set)
    protected.tools = [
        tool for tool in tools if getattr(tool, "name", "") not in blocked
    ]
    request.func_tool = protected


def _session_delivery_notice(messages: list[dict[str, Any]]) -> str:
    kinds = {str(item.get("type") or "").strip().lower() for item in messages}
    if "plain" in kinds:
        content = "图片或文件及随附文字" if len(kinds) > 1 else "文字"
        return (
            f"本次{content}已发送给用户。后续回复或工具调用中，不要再次发送这些文字，"
            "也不要仅改写后重复表达相同内容。"
        )
    content = "图片" if kinds == {"image"} else "媒体文件"
    return f"本次{content}已发送给用户，无需再次发送相同内容。"


def _asset_usage_notice(detail: str) -> str:
    """Explain only the assets returned by this call, without global tool policy."""
    visual = {
        "preview": "本次附图是供你查看的预览图。",
        "original": "本次附图为原图。",
        "asset": "本次仅返回图片信息，没有附图；需要看图时，将 asset_id 放入 asset_ids 列表，调用 image_studio_view_asset。",
    }[detail]
    delivery = (
        "在 Image Studio 工具中引用这些图片时，使用返回的图片编号 asset_id。"
        '向用户发送原图时，调用 image_studio_send_output，将 destination 设置为 "session"，'
        '在 messages 中填写 {"type":"image","asset_id":"对应的图片编号"}。'
        '需要交给其他工具处理时，将 destination 设置为 "workspace"，使用相同的 messages 格式复制原图，再使用返回的文件路径。'
    )
    cache = (
        "AstrBot 在本次 Image Studio 附图旁显示的 [Image from tool ..., path='...'] 中，"
        "path 是该附图的临时文件路径；发送、编辑或用作参考图时，请使用上述 asset_id 或复制到工作区后的文件路径。"
        if detail != "asset"
        else ""
    )
    return visual + delivery + cache


def _agent_wait_budget(context: Any, event: Any, seconds: Any) -> float:
    if (
        isinstance(seconds, bool)
        or not isinstance(seconds, (float, int))
        or not math.isfinite(seconds)
        or not 0 <= seconds <= 30
    ):
        raise ValueError("wait_seconds 必须是 0 至 30 秒的数字。")
    try:
        config = context.get_config(
            umo=str(getattr(event, "unified_msg_origin", "") or "")
        )
        timeout = (
            config.get("agent_runner", {})
            .get("config", {})
            .get("misc", {})
            .get("tool_call_timeout", 120)
        )
        if (
            not isinstance(timeout, bool)
            and isinstance(timeout, (int, float))
            and math.isfinite(timeout)
            and timeout > 0
        ):
            return min(float(seconds), max(0.0, timeout - 5.0))
    except (AttributeError, TypeError, ValueError):
        pass
    return float(seconds)


def _event_scope_id(event: Any) -> str:
    scope = str(getattr(event, "unified_msg_origin", "") or "").strip()
    if scope:
        return scope
    existing = str(_get_event_extra(event, "_image_studio_scope_id", "") or "")
    if existing:
        return existing
    generated = f"event:{uuid.uuid4().hex}"
    _set_event_extra(event, "_image_studio_scope_id", generated)
    return generated


def _computer_runtime(context: Any, event: Any) -> str:
    getter = getattr(context, "get_config", None)
    if not callable(getter):
        return "none"
    try:
        config = getter(umo=str(getattr(event, "unified_msg_origin", "") or ""))
    except Exception:
        return "none"
    if not isinstance(config, dict):
        return "none"
    provider_settings = config.get("provider_settings")
    if not isinstance(provider_settings, dict):
        return "none"
    return str(provider_settings.get("computer_use_runtime") or "none").lower()


def _safe_output_filename(value: Any, fallback: str) -> str:
    candidate = Path(str(value or "").replace("\\", "/")).name.strip()
    candidate = re.sub(r"[^A-Za-z0-9._-]+", "_", candidate).strip("._")[:120]
    if not candidate:
        candidate = Path(fallback).name
    return candidate or "asset.png"


def _write_unique_workspace_file(directory: Path, name: str, data: bytes) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    base = Path(name).stem or "asset"
    suffix = Path(name).suffix
    target = directory / name
    index = 1
    while target.exists():
        target = directory / f"{base}-{index}{suffix}"
        index += 1
    temporary = directory / f".{target.name}.{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_bytes(data)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


async def _resolve_delivery_path(
    raw_path: str,
    *,
    runtime: str,
    workspace_root: Path | None,
    booter: Any,
    delivery_dir: Path,
) -> Path:
    if runtime == "local":
        if workspace_root is None:
            raise ValueError("当前会话 workspace 不可用。")
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = workspace_root / candidate
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(workspace_root.resolve(strict=False))
        except (OSError, ValueError) as exc:
            raise ValueError("媒体路径不存在或不在当前 workspace 内。") from exc
        if not resolved.is_file():
            raise ValueError("媒体路径不是文件。")
        return resolved
    if runtime == "sandbox" and booter is not None:
        name = _safe_output_filename(raw_path, "sandbox-output")
        delivery_dir.mkdir(parents=True, exist_ok=True)
        target = delivery_dir / f"{uuid.uuid4().hex}_{name}"
        await booter.download_file(raw_path, str(target))
        if not target.is_file():
            raise ValueError("无法从当前 sandbox workspace 读取媒体文件。")
        return target
    raise ValueError("当前会话未启用可读取 workspace 文件的计算运行时。")


def _file_component(kind: str, path: Path, name: Any) -> Any:
    if kind == "image":
        return Image.fromFileSystem(str(path))
    if kind == "record":
        return Record.fromFileSystem(str(path))
    if kind == "video":
        return Video.fromFileSystem(str(path))
    return File(name=_safe_output_filename(name, path.name), file=str(path))


def _url_component(kind: str, url: str, name: Any) -> Any:
    if kind == "image":
        return Image.fromURL(url)
    if kind == "record":
        return Record.fromURL(url)
    if kind == "video":
        return Video.fromURL(url)
    return File(name=_safe_output_filename(name, Path(url).name), url=url)


def _remember_capability_query(
    event: Any, revision: int, entries: list[dict[str, Any]]
) -> None:
    """Remember model contracts disclosed during the current AstrBot event."""

    getter = getattr(event, "get_extra", None)
    setter = getattr(event, "set_extra", None)
    if not callable(setter):
        return
    current: Any = None
    if callable(getter):
        try:
            current = getter(CAPABILITY_QUERY_EXTRA_KEY, None)
        except TypeError:
            current = getter(CAPABILITY_QUERY_EXTRA_KEY)
        except Exception:
            current = None
    models: dict[str, list[str]] = {}
    if isinstance(current, dict) and current.get("revision") == revision:
        stored_models = current.get("models")
        if isinstance(stored_models, dict):
            models = {
                str(ref): [str(mode) for mode in modes]
                for ref, modes in stored_models.items()
                if isinstance(modes, list)
            }
    for entry in entries:
        ref = str(entry.get("model_ref") or "")
        modes = entry.get("query_modes", entry.get("modes"))
        if not ref or not isinstance(modes, list):
            continue
        models[ref] = sorted(set(models.get(ref, ())) | {str(item) for item in modes})
    setter(CAPABILITY_QUERY_EXTRA_KEY, {"revision": revision, "models": models})


def _has_capability_query(event: Any, revision: int, model_ref: str, mode: str) -> bool:
    getter = getattr(event, "get_extra", None)
    if not callable(getter):
        return False
    try:
        state = getter(CAPABILITY_QUERY_EXTRA_KEY, None)
    except TypeError:
        state = getter(CAPABILITY_QUERY_EXTRA_KEY)
    except Exception:
        return False
    if not isinstance(state, dict) or state.get("revision") != revision:
        return False
    models = state.get("models")
    if not isinstance(models, dict) or mode not in models.get(model_ref, ()):
        return False
    return True


def _llm_mode(value: str) -> str:
    aliases = {
        "text": "text2img",
        "txt2img": "text2img",
        "image": "img2img",
        "edit": "img2img",
    }
    normalized = aliases.get(
        str(value or "").strip().lower(), str(value or "").strip().lower()
    )
    if normalized not in {"text2img", "img2img"}:
        raise ValueError("模式仅支持 text2img 或 img2img")
    return normalized


def _select_llm_tool_model(
    settings: Any,
    mode: str,
    *,
    model_ref: str,
) -> tuple[Any, Any, str]:
    """Resolve an exact LLM-enabled model after capability discovery."""

    explicit_ref = str(model_ref or "").strip()
    requested_ref = explicit_ref or settings.default_model_ref(mode, "llm_tool")
    if not requested_ref:
        raise ValueError(
            '当前模式未设置默认 LLM 生图模型。请调用 image_studio_get_capabilities，设置 query_type="search" 查找模型，'
            '再设置 query_type="model" 并填写 model_refs 查询选定模型的参数；生图时使用该模型的 model_ref。'
        )
    candidates = [
        (provider, candidate, f"{provider.id}:{candidate.id}")
        for provider in settings.providers
        if provider.enabled
        for candidate in provider.models
        if candidate.llm_enabled and candidate.supports(mode)
    ]
    matches = [item for item in candidates if requested_ref in {item[1].id, item[2]}]
    if not matches:
        raise ValueError("所选模型不存在、已停用、不支持当前模式或未向 LLM 工具开放")
    if len(matches) > 1:
        raise ValueError("模型 ID 不唯一，请使用 provider_id:model_id 指定模型")
    return matches[0]


def _result_payload(
    result, *, download_detail: dict[str, Any] | None = None
) -> dict[str, Any]:
    naming_detail = download_detail or {
        "created_at": time.time(),
        "mode": result.request.mode,
        "model": result.request.model,
        "provider_id": result.provider.id,
    }
    used_stems: set[str] = set()
    return {
        "generation_id": result.generation_id,
        "provider_id": result.provider.id,
        "provider_name": result.provider.name,
        "model_ref": f"{result.provider.id}:{result.request.model}",
        "model": result.request.model,
        "mode": result.request.mode,
        "elapsed_ms": result.elapsed_ms,
        "warning": result.warning,
        "images": [
            {
                "mime_type": image.mime_type,
                "data_url": image_data_url(image.data, image.mime_type),
                "download_filename": export_image_filename(
                    naming_detail,
                    image_index=image_index,
                    image_count=len(result.images),
                    mime_type=image.mime_type,
                    used_stems=used_stems,
                ),
            }
            for image_index, image in enumerate(result.images, start=1)
        ],
    }


_QUOTED_ATTACHMENT_IMAGE_URL_RE = re.compile(
    r"(?im)^\s*\[附件\d+\][^\r\n]*?类型\s*[:：]\s*图片\b"
    r"[^\r\n]*?URL\s*[:：]\s*(https?://\S+)"
)


async def _event_workspace_root(event: Any, context: Any) -> Path | None:
    """Resolve the same per-session workspace used by AstrBot computer tools."""

    umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
    if not umo:
        return None
    try:
        return await resolve_workspace_root_for_umo(
            umo,
            getattr(context, "_db", None),
        )
    except Exception as exc:
        logger.debug(
            "%s Agent 工作区解析失败，使用会话默认目录: %s",
            LOG_TAG,
            type(exc).__name__,
        )
        return default_workspace_root(umo)


def _provider_request_image_refs(event: Any) -> list[str]:
    """Read image refs already materialized or serialized by AstrBot."""

    getter = getattr(event, "get_extra", None)
    if not callable(getter):
        return []
    try:
        request = getter("provider_request")
    except Exception:
        return []
    if request is None:
        return []

    refs: list[str] = []
    image_urls = getattr(request, "image_urls", None)
    if isinstance(image_urls, (list, tuple)):
        refs.extend(str(item).strip() for item in image_urls if str(item).strip())

    parts = getattr(request, "extra_user_content_parts", None)
    if isinstance(parts, (list, tuple)):
        for part in parts:
            if isinstance(part, dict):
                text = part.get("text") if part.get("type") == "text" else ""
            else:
                text = getattr(part, "text", "")
            if not isinstance(text, str) or "<Quoted Message>" not in text:
                continue
            refs.extend(
                match.group(1).strip()
                for match in _QUOTED_ATTACHMENT_IMAGE_URL_RE.finditer(text)
            )
    return list(dict.fromkeys(refs))


def _iter_event_images(value: Any):
    if isinstance(value, Image):
        yield value
        return
    if isinstance(value, Reply):
        yield from _iter_event_images(getattr(value, "chain", None))
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_event_images(item)


def _get_event_extra(event: Any, key: str, default: Any = None) -> Any:
    getter = getattr(event, "get_extra", None)
    if not callable(getter):
        return default
    try:
        return getter(key, default)
    except TypeError:
        try:
            return getter(key)
        except (AttributeError, KeyError, TypeError, ValueError):
            return default
    except (AttributeError, KeyError, ValueError):
        return default


def _set_event_extra(event: Any, key: str, value: Any) -> None:
    setter = getattr(event, "set_extra", None)
    if not callable(setter):
        return
    try:
        setter(key, value)
    except (AttributeError, KeyError, TypeError, ValueError):
        return


def _invocation_source(event: Any) -> InvocationSource:
    """Capture a best-effort chat identity snapshot for a generation."""

    def value(raw: Any) -> str:
        text = str(raw or "").strip()
        return "" if text.casefold() in {"n/a", "na", "none", "null"} else text[:240]

    try:
        platform_name = value(event.get_platform_name())
    except Exception:
        platform_name = ""
    try:
        platform_id = value(event.get_platform_id())
    except Exception:
        platform_id = ""
    try:
        group_id = value(event.get_group_id())
    except Exception:
        group_id = ""
    try:
        user_id = value(event.get_sender_id())
    except Exception:
        user_id = ""
    try:
        user_name = value(event.get_sender_name())
    except Exception:
        user_name = ""
    group = getattr(getattr(event, "message_obj", None), "group", None)
    group_name = value(getattr(group, "group_name", "")) if group_id else ""
    if user_name == user_id:
        user_name = ""
    if group_name == group_id:
        group_name = ""
    context_type = "group" if group_id else "private" if user_id else ""
    return InvocationSource(
        context_type=context_type,
        platform_name=platform_name,
        platform_id=platform_id,
        group_id=group_id,
        group_name=group_name,
        user_id=user_id,
        user_name=user_name,
    )
