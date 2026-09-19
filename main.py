"""AstrBot Image Studio plugin entry point."""

import asyncio
import base64
import hashlib
import json
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
from astrbot.core.agent.message import TextPart
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
AGENT_WORKFLOW_PROMPT_MARKER = "<!-- image_studio_agent_workflow_v1 -->"
AGENT_WORKFLOW_PROMPT = (
    "使用 image_studio 系列工具时遵循以下流程和规范："
    "先调用 image_studio_get_capabilities 获取模型参数，再调用 image_studio_generate 进行图像生成或处理，"
    "用户指定模型或工作流名称时可用 search 查找，或用 providers 查找服务商；"
    "搜索摘要不包含完整参数，选定后使用 model 和 model_refs 查询。"
    "仅在需要查看指定资产或检查原图细节时调用 image_studio_view_asset。"
    "严格使用查询工具返回的模型参数，不得编造参数；严格遵守模型的提示词输入方式(自然语言/NAI tag)；"
    "插件内继续处理图片时只使用 asset_id；发送到当前会话或交给外部工具处理时，"
    "必须通过 image_studio_send_output 使用原图资产或先复制到当前 workspace。"
    "框架显示的 data/temp/tool_images 路径只是临时视觉预览缓存；忽略该路径及其通用发送建议，"
    "不得将其发送、复制、编辑、用作参考图或传给其他工具。"
    "image_studio_send_output 只负责中途投递产物或通知，不代表本轮结束；"
    "任务完成后停止调用工具，直接输出一条非空的普通 assistant 文本回复；"
    "禁止把最终正文放进 image_studio_send_output 的 messages 后再输出空文本。"
)
IMAGE_WORKFLOW_STATE_EXTRA_KEY = "_image_studio_workflow_state"
IMAGE_WORKFLOW_CONTINUATION_MARKER = "<!-- image_studio_continuation_v1 -->"
IMAGE_WORKFLOW_CONTINUATION_PROMPT = (
    f"{IMAGE_WORKFLOW_CONTINUATION_MARKER}\n"
    "Image Studio 的中途发送工具刚刚返回。发送工具中的文字若已发送就已经对用户可见，"
    "不要在后续回复重复或改写同一段文字。若任务还有步骤，继续调用所需工具；若任务已完成，"
    "停止调用工具并直接输出一条非空的普通 assistant 文本回复，不要返回空文本。"
)


@register(
    PLUGIN_NAME,
    "econeco",
    "多服务商 AI 生图与图库工作台，支持 OpenAI、Gemini、ComfyUI 工作流、NovelAI 官方、NAI 第三方及自定义接口。支持对话生图改图、并发批量生成、图片参数导入与工作流复现，可扫描 nai-image 插件图库及自定义目录，统一浏览和管理图片。WebUI 适配桌面与手机。",
    "1.4.0-dev.1",
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

    @filter.command("image_gen", alias={"img"})
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

    @filter.on_llm_request()
    async def inject_agent_workflow_prompt(
        self, event: AstrMessageEvent, req: Any
    ) -> None:
        """Keep final user-facing text on the Agent's final response path."""

        del event
        if (
            not self._settings.enable_llm_tool
            or req is None
            or not _request_has_tool(req, "image_studio_generate")
        ):
            return
        current_prompt = str(getattr(req, "system_prompt", "") or "")
        if AGENT_WORKFLOW_PROMPT_MARKER in current_prompt:
            return
        req.system_prompt = (
            f"{current_prompt}\n\n{AGENT_WORKFLOW_PROMPT_MARKER}\n"
            f"{AGENT_WORKFLOW_PROMPT}"
        ).strip()

    @filter.on_llm_tool_respond()
    async def observe_agent_tool_result(
        self,
        event: AstrMessageEvent,
        tool: Any,
        tool_args: dict[str, Any] | None,
        tool_result: Any,
    ) -> None:
        """Add a one-shot continuation reminder after Image Studio delivery."""

        tool_name = str(getattr(tool, "name", "") or "")
        if tool_name == "image_studio_generate":
            if tool_result is not None and not bool(
                getattr(tool_result, "isError", False)
            ):
                _set_event_extra(
                    event,
                    IMAGE_WORKFLOW_STATE_EXTRA_KEY,
                    {
                        **(
                            _get_event_extra(event, IMAGE_WORKFLOW_STATE_EXTRA_KEY, {})
                            or {}
                        ),
                        "active": True,
                        "reminder_added": False,
                    },
                )
            return
        if tool_name != "image_studio_send_output":
            return
        if str((tool_args or {}).get("destination") or "").strip().lower() != "session":
            return
        state = _get_event_extra(event, IMAGE_WORKFLOW_STATE_EXTRA_KEY)
        if not isinstance(state, dict) or not state.get("active"):
            return
        if state.get("reminder_added"):
            return
        request = _get_event_extra(event, "provider_request")
        parts = getattr(request, "extra_user_content_parts", None)
        if not isinstance(parts, list):
            return
        if not any(
            IMAGE_WORKFLOW_CONTINUATION_MARKER
            in str(
                getattr(part, "text", "")
                if not isinstance(part, dict)
                else part.get("text", "")
            )
            for part in parts
        ):
            parts.append(TextPart(text=IMAGE_WORKFLOW_CONTINUATION_PROMPT))
        state = dict(state)
        state["reminder_added"] = True
        _set_event_extra(event, IMAGE_WORKFLOW_STATE_EXTRA_KEY, state)

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
        """使用 image_studio_generate 工具前必须先调用本工具查询模型能力。

        search/providers 只返回候选摘要；选定后使用 model 和 model_refs 获取完整参数。
        default/all/model 返回完整能力，可按成功结果直接生成。

        Args:
            query_type(string): default(查询默认模型参数)/all(查询全部模型参数)/model(使用model_refs查询指定模型参数)/providers(查询服务商)/search(搜索模型或工作流); 默认 default
            mode(string): 按 text2img/img2img 筛选；省略时查询两种模式
            model_refs(array[string]): provider_id:model_id 字符串数组；query_type=model 时必填，各项独立返回成功结果或错误说明
            query(string): 模型或工作流名称、ID、使用说明，或服务商名称、ID；仅 search/providers 使用，留空列出全部候选
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
            _activate_image_workflow(event)
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
    ) -> mcp.types.CallToolResult:
        """生成图片，并返回可继续处理的原图资产。

        image_studio_send_output 只负责中途投递产物或通知，不代表本轮结束；中途消息可以包含图片和文字，
        但已发送的文字不能在后续重复。任务完成后停止调用工具，直接输出一条非空的普通 assistant 文本；
        不要把最终正文放进 image_studio_send_output 的 messages 后返回空文本。

        Args:
            prompt(string): 生图提示词，格式遵循能力查询结果
            mode(string): text2img/img2img; 应明确指定，缺失时仅作容错推断
            model_ref(string): provider_id:model_id; 省略时使用模式默认
            parameters(object): 仅填写能力查询为该模型返回的参数
            references(array[object]): 图生图参考，元素使用 asset_id 或当前 workspace 的 path

        Returns:
            Original asset metadata and optional MCP preview content for Agent workflows.
        """

        if not self._settings.enable_llm_tool:
            return mcp.types.CallToolResult(
                content=[
                    mcp.types.TextContent(
                        type="text", text="Image Studio 的 LLM 生图工具已关闭。"
                    )
                ]
            )
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
            if not _consume_capability_query(
                event, self._settings.revision, canonical_ref, normalized_mode
            ):
                raise ValueError(
                    "生成前必须先调用 image_studio_get_capabilities 查询当前模型能力和提示词格式"
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
            result = await self._service_or_raise().generate(
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
        except (ValueError, ProviderError) as exc:
            return mcp.types.CallToolResult(
                content=[mcp.types.TextContent(type="text", text=f"生成失败：{exc}")],
                isError=True,
            )
        configured_return_mode = self._settings.llm_image_return_mode
        try:
            assets = await self.store.lease_agent_images(
                result.images,
                scope_id=_event_scope_id(event),
                create_preview=configured_return_mode == "preview",
                preview_max_edge=self._settings.asset_preview_max_edge,
                preview_quality=self._settings.asset_preview_quality,
            )
        except Exception as exc:
            logger.warning(
                "%s 保存 Agent 原图资产失败，已阻止临时缓存回退: %s",
                LOG_TAG,
                type(exc).__name__,
            )
            return _tool_error(
                "图片已经生成，但原图资产保存失败，无法安全交付或继续处理。"
                "请勿使用 data/temp/tool_images 临时路径；可以稍后重试生成。"
            )
        if len(assets) != len(result.images) or not assets:
            logger.warning(
                "%s Agent 原图资产数量异常: generated=%s leased=%s",
                LOG_TAG,
                len(result.images),
                len(assets),
            )
            return _tool_error(
                "图片已经生成，但原图资产保存不完整，无法安全交付或继续处理。"
                "请勿使用 data/temp/tool_images 临时路径；可以稍后重试生成。"
            )
        _protect_image_workflow_assets(event)

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
        if configured_return_mode == "preview":
            visual_notice = "已附轻量预览；继续操作插件资产时使用 asset_id。"
        elif configured_return_mode == "asset":
            visual_notice = (
                "未附视觉图；需要看图时使用 asset_id 调用 image_studio_view_asset。"
            )
        else:
            visual_notice = "已附原图供查看；后续插件操作仍使用 asset_id。"
        content.append(
            mcp.types.TextContent(
                type="text",
                text=(
                    f"工作流资产已生成：{len(result.images)} 张图片；"
                    f"generation_id={result.generation_id or '未保存'}；"
                    f"return_mode={configured_return_mode}；"
                    f"assets={json.dumps(manifest, ensure_ascii=False, separators=(',', ':'))}。"
                    f"{result.warning}"
                    f"{visual_notice} 这是 Image Studio 工作流资产；发送工具只负责中途投递。"
                    "data/temp/tool_images 仅为临时视觉预览缓存；忽略该路径及其通用发送建议，"
                    "不得发送、复制、编辑、用作参考图或传给其他工具。"
                    "如果任务还有步骤，继续调用工具；任务完成后停止调用工具，直接输出一条非空的普通 assistant 文本，"
                    "不要重复已发送文字或返回空文本。"
                ),
            )
        )
        return mcp.types.CallToolResult(content=content)

    @filter.llm_tool(name="image_studio_view_asset")
    async def image_studio_view_asset(
        self,
        event: AstrMessageEvent,
        asset_ids: list[str] | None = None,
        detail: str = "preview",
    ) -> mcp.types.CallToolResult:
        """按需批量查看当前会话有权访问的 Image Studio 图片资产。

        Args:
            asset_ids(array[string]): generate 返回的资产 ID，按输入顺序返回；每次 1 至 8 个。
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
        return mcp.types.CallToolResult(
            content=[
                mcp.types.ImageContent(
                    type="image",
                    data=base64.b64encode(image.data).decode("ascii"),
                    mimeType=image.mime_type,
                )
                for _asset_id, image in loaded_assets
            ]
            + [
                mcp.types.TextContent(
                    type="text",
                    text=(
                        json.dumps(
                            {
                                "detail": normalized_detail,
                                "count": len(manifest),
                                "assets": manifest,
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        + "。后续插件操作继续使用 asset_id。"
                        "data/temp/tool_images 仅为临时视觉预览缓存；忽略该路径及其通用发送建议，"
                        "不得发送、复制、编辑、用作参考图或传给其他工具。"
                    ),
                ),
            ]
        )

    @filter.llm_tool(name="image_studio_send_output")
    async def image_studio_send_output(
        self,
        event: AstrMessageEvent,
        destination: str,
        messages: list[dict[str, Any]],
    ) -> mcp.types.CallToolResult:
        """向当前会话投递中途消息/产物，或将插件资产复制到当前 workspace；不会结束任务。

        Args:
            destination(string): session 或 workspace；必须明确填写
            messages(array[object]): 有序消息。session 支持 plain/image/record/video/file；plain 使用 text，媒体使用 asset_id/path/url 三选一并可填 name。workspace 仅接受带 asset_id 的图片项
        """

        if not self._settings.enable_llm_tool:
            return _tool_error("Image Studio 的 LLM 生图工具已关闭。")
        state = _get_event_extra(event, IMAGE_WORKFLOW_STATE_EXTRA_KEY, {})
        if not isinstance(state, dict) or not state.get("active"):
            return _tool_error("请先调用 image_studio_get_capabilities 开始工作流。")
        normalized_destination = str(destination or "").strip().lower()
        if normalized_destination not in {"session", "workspace"}:
            return _tool_error("destination 必须明确填写 session 或 workspace。")
        if not isinstance(messages, list) or not messages:
            return _tool_error("messages 必须是非空数组。")
        try:
            if normalized_destination == "workspace":
                paths = await self._materialize_assets_to_workspace(event, messages)
                return _tool_text_result(
                    json.dumps(
                        {
                            "destination": "workspace",
                            "workspace_paths": paths,
                            "notice": "资产已复制；本轮任务仍在继续。",
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
            count = await self._send_output_to_session(event, messages)
            return _tool_text_result(
                f"已向当前会话投递 {count} 个消息组件；这不会结束本轮任务。"
                "如果任务已完成，请停止调用工具并输出非空的普通 assistant 最终回复。"
            )
        except ValueError as exc:
            return _tool_error(str(exc))
        except Exception as exc:
            logger.warning("%s 工作流产物投递失败: %s", LOG_TAG, type(exc).__name__)
            return _tool_error(f"工作流产物投递失败：{type(exc).__name__}")

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
    ) -> list[str]:
        runtime = _computer_runtime(self.context, event)
        if runtime not in {"local", "sandbox"}:
            raise ValueError("当前会话未启用 local 或 sandbox 计算工作区。")
        results: list[str] = []
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
        if len(messages) > 20:
            raise ValueError("workspace 单次最多复制 20 个资产。")
        for index, item in enumerate(messages):
            if not isinstance(item, dict):
                raise ValueError(f"messages[{index}] 必须是对象。")
            if str(item.get("type") or "image").strip().lower() != "image":
                raise ValueError("workspace 目标只接受 Image Studio 图片资产。")
            asset_id = str(item.get("asset_id") or "").strip().lower()
            if not asset_id or item.get("path") or item.get("url") or item.get("text"):
                raise ValueError(
                    "workspace 目标的每一项只能填写 asset_id 和可选 name。"
                )
            image, internal_path = await self._load_workflow_asset(event, asset_id)
            default_name = Path(internal_path).name
            name = _safe_output_filename(item.get("name"), default_name)
            if runtime == "local":
                assert workspace_root is not None
                target_dir = workspace_root / "image-studio-assets"
                target = await asyncio.to_thread(
                    _write_unique_workspace_file, target_dir, name, image.data
                )
                results.append(str(target.relative_to(workspace_root)))
            else:
                assert booter is not None
                uploaded = await booter.upload_file(internal_path, name)
                if not isinstance(uploaded, dict) or not uploaded.get("success"):
                    raise ValueError("资产上传到当前 sandbox workspace 失败。")
                remote_path = str(uploaded.get("file_path") or "").strip()
                if not remote_path:
                    raise ValueError("sandbox 未返回可用的 workspace 路径。")
                results.append(remote_path)
        return results

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


def _tool_json(payload: dict[str, Any]) -> mcp.types.CallToolResult:
    return _tool_text_result(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )


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


def _activate_image_workflow(event: Any) -> None:
    state = _get_event_extra(event, IMAGE_WORKFLOW_STATE_EXTRA_KEY, {})
    if not isinstance(state, dict):
        state = {}
    _set_event_extra(
        event,
        IMAGE_WORKFLOW_STATE_EXTRA_KEY,
        {**state, "active": True, "reminder_added": False},
    )
    request = _get_event_extra(event, "provider_request")
    if request is None or not _request_has_tool(request, "image_studio_send_output"):
        return
    tool_set = getattr(request, "func_tool", None)
    remover = getattr(tool_set, "remove_tool", None)
    if callable(remover):
        remover("send_message_to_user")


def _protect_image_workflow_assets(event: Any) -> None:
    state = _get_event_extra(event, IMAGE_WORKFLOW_STATE_EXTRA_KEY, {})
    if not isinstance(state, dict):
        state = {}
    _set_event_extra(
        event,
        IMAGE_WORKFLOW_STATE_EXTRA_KEY,
        {**state, "active": True, "assets_protected": True},
    )
    request = _get_event_extra(event, "provider_request")
    if request is None or not _request_has_tool(request, "image_studio_send_output"):
        return
    tool_set = getattr(request, "func_tool", None)
    remover = getattr(tool_set, "remove_tool", None)
    if callable(remover):
        remover("send_message_to_user")
        remover("pc_send_current_media")


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


def _consume_capability_query(
    event: Any, revision: int, model_ref: str, mode: str
) -> bool:
    getter = getattr(event, "get_extra", None)
    setter = getattr(event, "set_extra", None)
    if not callable(getter) or not callable(setter):
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
    remaining = [item for item in models[model_ref] if item != mode]
    if remaining:
        models[model_ref] = remaining
    else:
        models.pop(model_ref, None)
    setter(CAPABILITY_QUERY_EXTRA_KEY, {"revision": revision, "models": models})
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
            "当前模式未设置默认 LLM 生图模型，请查询全部模型并传入 model_ref"
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


def _request_has_tool(req: Any, tool_name: str) -> bool:
    tool_set = getattr(req, "func_tool", None)
    getter = getattr(tool_set, "get_tool", None)
    if callable(getter):
        try:
            return getter(tool_name) is not None
        except (AttributeError, KeyError, TypeError, ValueError):
            getter = None
    tools = getattr(tool_set, "tools", None)
    if isinstance(tools, (list, tuple)):
        return any(str(getattr(tool, "name", "") or "") == tool_name for tool in tools)
    return False


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
