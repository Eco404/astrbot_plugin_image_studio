"""AstrBot Image Studio plugin entry point."""

import asyncio
import base64
import copy
import hashlib
import json
import os
import re
import shlex
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
from astrbot.api.web import error_response, file_response, json_response
from astrbot.api.web import request as web_request
from astrbot.core.agent.message import TextPart
from astrbot.core.computer.computer_client import get_booter
from astrbot.core.utils.quoted_message import extract_quoted_message_images
from astrbot.core.workspace import (
    default_workspace_root,
    resolve_workspace_root_for_umo,
)
from starlette.background import BackgroundTask

from .config import (
    load_studio_settings,
    normalize_webui_settings,
    runtime_settings,
    save_studio_settings,
)
from .models import ImageProvider, InvocationSource, ReferenceImage
from .providers import ProviderError, ProviderExecutor
from .service import ImageGenerationService
from .storage import (
    GenerationStore,
    detect_mime_type,
    export_image_filename,
    image_data_url,
)

PLUGIN_NAME = "astrbot_plugin_image_studio"
PAGE_PREFIX = f"/{PLUGIN_NAME}"
LOG_TAG = "[ImageStudio]"
CAPABILITY_QUERY_EXTRA_KEY = "_image_studio_capability_queries"
AGENT_WORKFLOW_PROMPT_MARKER = "<!-- image_studio_agent_workflow_v1 -->"
AGENT_WORKFLOW_PROMPT = (
    "使用 image_studio 系列工具时遵循以下流程和规范："
    "先调用 image_studio_get_capabilities 获取模型参数，再调用 image_studio_generate 进行图像生成或处理，"
    "ImageContent 可能只是预览，只有需要看图时才调用 image_studio_view_asset。"
    "严格使用查询工具返回的模型参数，不得编造参数；严格遵守模型的提示词输入方式(自然语言/NAI tag)；"
    "插件资产只使用 asset_id，通过 image_studio_send_output 发送到当前会话或复制到当前 workspace。"
    "image_studio_send_output 只负责中途投递产物或通知，不代表本轮结束；中途消息可以包含图片和文字，"
    "但已经发送的文字不得在后续 assistant 文本回复中重复。"
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
    "local",
    "多 Provider 生图、画廊与 Agent 可读图片工具。",
    "0.5.1",
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
        self._settings_lock = asyncio.Lock()
        self._maintenance_task: asyncio.Task[None] | None = None
        self._exports: dict[str, tuple[Path, float]] = {}
        self._studio_settings, studio_errors = load_studio_settings(Path(self.data_dir))
        self._settings, runtime_errors = runtime_settings(config, self._studio_settings)
        self._settings_errors = [*studio_errors, *runtime_errors]

    async def initialize(self) -> None:
        """Initialize storage, HTTP resources, and Page API routes."""

        await self.store.initialize()
        await self.store.run_maintenance(
            self._settings.history,
            preview_max_edge=self._settings.asset_preview_max_edge,
            preview_quality=self._settings.asset_preview_quality,
        )
        self._maintenance_task = asyncio.create_task(
            self._maintenance_loop(), name="image-studio-maintenance"
        )
        self._session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=12, limit_per_host=6),
            timeout=aiohttp.ClientTimeout(total=180),
        )
        self._service = ImageGenerationService(
            settings=self._settings,
            executor=ProviderExecutor(self._session),
            store=self.store,
        )
        self._register_web_apis()
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

    async def _maintenance_loop(self) -> None:
        while True:
            await asyncio.sleep(3600)
            try:
                await self.store.run_maintenance(
                    self._settings.history,
                    preview_max_edge=self._settings.asset_preview_max_edge,
                    preview_quality=self._settings.asset_preview_quality,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("%s 定时存储维护失败: %s", LOG_TAG, type(exc).__name__)

    def _register_web_apis(self) -> None:
        routes = (
            (
                "studio/bootstrap",
                self._api_bootstrap,
                ["GET"],
                "Image Studio: bootstrap",
            ),
            ("studio/generate", self._api_generate, ["POST"], "Image Studio: generate"),
            (
                "studio/reference/upload",
                self._api_upload_reference,
                ["POST"],
                "Image Studio: upload reference",
            ),
            (
                "settings/get",
                self._api_get_settings,
                ["GET"],
                "Image Studio: get settings",
            ),
            (
                "settings/save",
                self._api_save_settings,
                ["POST"],
                "Image Studio: save settings",
            ),
            (
                "storage/health",
                self._api_storage_health,
                ["GET"],
                "Image Studio: storage health",
            ),
            (
                "storage/maintenance",
                self._api_storage_maintenance,
                ["POST"],
                "Image Studio: storage maintenance",
            ),
            (
                "model/test",
                self._api_test_model,
                ["POST"],
                "Image Studio: test model",
            ),
            (
                "provider/models",
                self._api_provider_models,
                ["POST"],
                "Image Studio: discover provider models",
            ),
            (
                "gallery/list",
                self._api_gallery_list,
                ["GET"],
                "Image Studio: list gallery",
            ),
            (
                "gallery/detail/<generation_id>",
                self._api_gallery_detail,
                ["GET"],
                "Image Studio: gallery detail",
            ),
            (
                "gallery/assets/<generation_id>",
                self._api_gallery_assets,
                ["GET"],
                "Image Studio: gallery assets",
            ),
            (
                "gallery/image-sequence",
                self._api_gallery_image_sequence,
                ["GET"],
                "Image Studio: gallery image sequence",
            ),
            (
                "gallery/image/<image_id>",
                self._api_gallery_image,
                ["GET"],
                "Image Studio: gallery image data",
            ),
            (
                "gallery/download/<image_id>",
                self._api_gallery_image_download,
                ["GET"],
                "Image Studio: download gallery image",
            ),
            (
                "gallery/reproduce/<generation_id>",
                self._api_gallery_reproduce,
                ["POST"],
                "Image Studio: reproduce draft",
            ),
            (
                "gallery/delete",
                self._api_gallery_delete,
                ["POST"],
                "Image Studio: delete gallery records",
            ),
            (
                "gallery/reference/delete",
                self._api_reference_delete,
                ["POST"],
                "Image Studio: delete reference",
            ),
            (
                "gallery/export",
                self._api_gallery_export,
                ["POST"],
                "Image Studio: export gallery",
            ),
            (
                "gallery/export/<export_id>",
                self._api_download_export,
                ["GET"],
                "Image Studio: download export",
            ),
        )
        for suffix, handler, methods, description in routes:
            self.context.register_web_api(
                f"{PAGE_PREFIX}/{suffix}", handler, methods, description
            )

    def _service_or_raise(self) -> ImageGenerationService:
        if self._service is None:
            raise RuntimeError("Image Studio 正在初始化，请稍后重试")
        return self._service

    async def _api_bootstrap(self) -> Any:
        return json_response(
            {
                "settings_revision": self._settings.revision,
                "defaults": {
                    "text2img_model_ref": self._settings.default_model_ref(
                        "text2img", "webui"
                    ),
                    "img2img_model_ref": self._settings.default_model_ref(
                        "img2img", "webui"
                    ),
                },
                "providers": [
                    provider.public_dict()
                    for provider in self._settings.providers
                    if provider.enabled
                ],
                "models": [
                    {
                        **model.public_dict(),
                        "provider_id": provider.id,
                        "provider_name": provider.name,
                        "provider_kind": provider.kind,
                        "model_ref": f"{provider.id}:{model.id}",
                    }
                    for provider in self._settings.providers
                    if provider.enabled
                    for model in provider.models
                ],
                "modes": ["text2img", "img2img"],
            }
        )

    async def _api_get_settings(self) -> Any:
        studio, errors = normalize_webui_settings(self._studio_settings)
        return json_response(
            {
                "base": {
                    "enable_llm_tool": bool(self.config.get("enable_llm_tool", True)),
                },
                "studio": studio,
                "webui": studio,
                "validation_errors": errors,
            }
        )

    async def _api_save_settings(self) -> Any:
        body = await web_request.json(default={})
        if not isinstance(body, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)
        expected_revision = _as_int(body.get("settings_revision"), -1)
        async with self._settings_lock:
            current_studio, _ = normalize_webui_settings(self._studio_settings)
            current_revision = _as_int(current_studio.get("revision"), 0)
            if expected_revision < 0:
                expected_revision = _as_int(
                    body.get("studio", body.get("webui", {}))
                    .get("ui", {})
                    .get("settings_revision"),
                    -1,
                )
            if expected_revision != current_revision:
                return error_response(
                    "设置已被其他操作更新，请刷新后再保存", status_code=409
                )
            candidate, errors = normalize_webui_settings(
                body.get("studio", body.get("webui"))
            )
            if errors:
                return error_response("；".join(errors), status_code=400)
            candidate["revision"] = current_revision + 1
            candidate["ui"]["settings_revision"] = candidate["revision"]
            base = body.get("base") if isinstance(body.get("base"), dict) else {}
            previous = copy.deepcopy(dict(self.config))
            previous_studio = copy.deepcopy(self._studio_settings)
            for key in ("enable_llm_tool",):
                if key in base:
                    self.config[key] = base[key]
            try:
                await save_studio_settings(Path(self.data_dir), candidate)
                await _save_config_async(self.config)
            except Exception as exc:
                self.config.clear()
                self.config.update(previous)
                try:
                    await save_studio_settings(Path(self.data_dir), previous_studio)
                except Exception:
                    logger.warning("%s 恢复插件配置文件失败", LOG_TAG)
                logger.warning(
                    "%s 保存 WebUI 配置失败: %s", LOG_TAG, type(exc).__name__
                )
                return error_response("配置保存失败", status_code=500)
            self._studio_settings = candidate
            self._settings, self._settings_errors = runtime_settings(
                self.config, self._studio_settings
            )
            self._service_or_raise().update_settings(self._settings)
        return json_response({"settings_revision": self._settings.revision})

    async def _api_storage_health(self) -> Any:
        return json_response(await self.store.maintenance_report())

    async def _api_storage_maintenance(self) -> Any:
        body = await web_request.json(default={})
        if not isinstance(body, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)
        report = await self.store.run_maintenance(
            self._settings.history,
            preview_max_edge=self._settings.asset_preview_max_edge,
            preview_quality=self._settings.asset_preview_quality,
            deep=bool(body.get("deep", False)),
        )
        return json_response(report)

    async def _api_upload_reference(self) -> Any:
        files = await web_request.files()
        upload = files.get("file")
        if upload is None:
            return error_response("缺少参考图文件", status_code=400)
        if (
            upload.content_length is not None
            and upload.content_length > 20 * 1024 * 1024
        ):
            return error_response("参考图不能超过 20 MB", status_code=400)
        raw = await upload.read()
        mime_type = detect_mime_type(raw, str(upload.content_type or ""))
        if not raw or mime_type not in {
            "image/png",
            "image/jpeg",
            "image/webp",
            "image/gif",
        }:
            return error_response("仅支持 PNG、JPEG、WebP 或 GIF 图片", status_code=400)
        try:
            data = await self.store.stage_reference(
                filename=str(upload.filename or "reference"),
                content=raw,
                mime_type=mime_type,
            )
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        return json_response(data)

    async def _api_generate(self) -> Any:
        body = await web_request.json(default={})
        if not isinstance(body, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)
        try:
            references = await self._service_or_raise().staged_references(
                body.get("reference_ids")
            )
            result = await self._service_or_raise().generate(
                mode=str(body.get("mode") or "text2img"),
                provider_id=str(body.get("provider_id") or ""),
                prompt=str(body.get("prompt") or ""),
                negative_prompt=str(body.get("negative_prompt") or ""),
                model_ref=str(body.get("model_ref") or ""),
                model=str(body.get("model") or ""),
                size=str(body.get("size") or ""),
                count=body.get("count", 1),
                parameters=body.get("parameters"),
                references=references,
                source="webui",
            )
        except (ValueError, ProviderError) as exc:
            return error_response(str(exc), status_code=400)
        download_detail = (
            await self.store.generation_detail(
                result.generation_id, include_assets=False
            )
            if result.generation_id
            else None
        )
        return json_response(_result_payload(result, download_detail=download_detail))

    async def _api_test_model(self) -> Any:
        body = await web_request.json(default={})
        if not isinstance(body, dict) or not isinstance(body.get("provider"), dict):
            return error_response("需要服务商配置", status_code=400)
        provider = ImageProvider.from_mapping(body["provider"])
        model_id = str(body.get("model_id") or "").strip()
        test_model = next(
            (item for item in provider.models if item.id == model_id), None
        )
        if not provider.id or not provider.base_url or test_model is None:
            return error_response(
                "服务商需要 id、base_url 和有效的测试模型", status_code=400
            )
        if not test_model.text2img:
            return error_response(
                "当前模型仅支持图生图，无法进行无参考图测试", status_code=400
            )
        try:
            result = await self._service_or_raise().run_provider_request(
                provider,
                self._test_request(provider, test_model.id),
            )
        except (ValueError, ProviderError) as exc:
            return error_response(str(exc), status_code=400)
        return json_response(
            {
                "ok": True,
                "image_count": len(result),
                "preview_data_url": image_data_url(result[0].data, result[0].mime_type),
            }
        )

    async def _api_provider_models(self) -> Any:
        body = await web_request.json(default={})
        if not isinstance(body, dict) or not isinstance(body.get("provider"), dict):
            return error_response("需要服务商配置", status_code=400)
        try:
            provider = ImageProvider.from_mapping(body["provider"])
            if not provider.id or not provider.base_url:
                return error_response("服务商需要 id 和 base_url", status_code=400)
            models = await self._service_or_raise().executor.discover_models(provider)
        except (ValueError, ProviderError) as exc:
            return error_response(str(exc), status_code=400)
        return json_response({"models": models, "provider_id": provider.id})

    def _test_request(self, provider: ImageProvider, model_id: str):
        from .models import GenerationRequest

        model = provider.get_model(model_id)
        size = "竖图" if provider.kind == "nai_direct" else "1024x1024"
        count = 1
        parameters: dict[str, Any] = {}
        for name, descriptor in model.parameters.items():
            if descriptor.get("ui_only") or "default" not in descriptor:
                continue
            request_key = str(descriptor.get("request_key") or name)
            if request_key == "size":
                size = str(descriptor["default"])
            elif request_key in {"count", "n"}:
                count = _as_int(descriptor["default"], 1)
            else:
                parameters[request_key] = descriptor["default"]
        return GenerationRequest(
            mode="text2img",
            provider_id=provider.id,
            prompt="A simple landscape photograph with one tree and clear daylight.",
            negative_prompt=model.negative_prompt_default,
            model=model.id,
            size=size,
            count=count,
            parameters=parameters,
            source="webui",
        )

    async def _api_gallery_list(self) -> Any:
        filters = {
            "query": web_request.query.get("query", ""),
            "provider_id": web_request.query.get("provider_id", ""),
            "mode": web_request.query.get("mode", ""),
            "source": web_request.query.get("source", ""),
            "limit": web_request.query.get("limit", 24),
            "offset": web_request.query.get("offset", 0),
        }
        return json_response(await self.store.list_generations(filters))

    async def _api_gallery_detail(self, generation_id: str) -> Any:
        include_assets = str(web_request.query.get("assets", "1")).lower() not in {
            "0",
            "false",
            "no",
        }
        detail = await self.store.generation_detail(
            generation_id, include_assets=include_assets
        )
        if detail is None:
            return error_response("生成记录不存在", status_code=404)
        return json_response(detail)

    async def _api_gallery_assets(self, generation_id: str) -> Any:
        detail = await self.store.generation_detail(generation_id, include_assets=True)
        if detail is None:
            return error_response("生成记录不存在", status_code=404)
        return json_response(
            {"images": detail["images"], "references": detail["references"]}
        )

    async def _api_gallery_image_sequence(self) -> Any:
        filters = {
            "query": web_request.query.get("query", ""),
            "provider_id": web_request.query.get("provider_id", ""),
            "mode": web_request.query.get("mode", ""),
            "source": web_request.query.get("source", ""),
        }
        items = await self.store.gallery_image_sequence(filters)
        return json_response({"items": items, "total": len(items)})

    async def _api_gallery_image(self, image_id: str) -> Any:
        detail = str(web_request.query.get("detail", "preview")).strip().lower()
        if detail not in {"preview", "original"}:
            return error_response(
                "图片读取方式仅支持 preview 或 original", status_code=400
            )
        image = await self.store.gallery_image_data(image_id, detail=detail)
        if image is None:
            return error_response("生成图片不存在", status_code=404)
        return json_response(image)

    async def _api_gallery_image_download(self, image_id: str) -> Any:
        image = await self.store.gallery_image_file(image_id)
        if image is None:
            return error_response("生成图片不存在", status_code=404)
        path, mime_type, filename = image
        return file_response(path, filename=filename, content_type=mime_type)

    async def _api_gallery_reproduce(self, generation_id: str) -> Any:
        try:
            plan = await self._service_or_raise().reproduction_plan(generation_id)
        except ValueError as exc:
            return error_response(str(exc), status_code=404)
        return json_response(plan)

    async def _api_gallery_delete(self) -> Any:
        body = await web_request.json(default={})
        ids = body.get("ids") if isinstance(body, dict) else []
        if not isinstance(ids, list) or not ids:
            return error_response("请选择要删除的生成记录", status_code=400)
        deleted: list[str] = []
        failed: list[str] = []
        for generation_id in ids[:200]:
            if await self.store.delete_generation(str(generation_id or "")):
                deleted.append(str(generation_id))
            else:
                failed.append(str(generation_id))
        return json_response({"deleted": deleted, "failed": failed})

    async def _api_reference_delete(self) -> Any:
        body = await web_request.json(default={})
        reference_id = (
            str(body.get("reference_id") or "") if isinstance(body, dict) else ""
        )
        if not await self.store.delete_reference(reference_id):
            return error_response("参考图不存在或已删除", status_code=404)
        return json_response({"reference_id": reference_id})

    async def _api_gallery_export(self) -> Any:
        body = await web_request.json(default={})
        ids = body.get("ids") if isinstance(body, dict) else []
        if not isinstance(ids, list):
            return error_response("ids 必须是列表", status_code=400)
        try:
            path = await self.store.export_generations(
                [str(item or "") for item in ids]
            )
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        await self.store.cleanup_exports()
        export_id = uuid.uuid4().hex
        self._exports[export_id] = (path, time.time())
        return json_response(
            {"download_endpoint": f"gallery/export/{export_id}", "filename": path.name}
        )

    async def _api_download_export(self, export_id: str) -> Any:
        item = self._exports.get(export_id)
        if item is None:
            return error_response("导出文件已过期，请重新导出", status_code=404)
        path, created_at = item
        if time.time() - created_at > 3600 or not path.is_file():
            self._exports.pop(export_id, None)
            await asyncio.to_thread(_remove_export_file, path)
            return error_response("导出文件已过期，请重新导出", status_code=404)
        response = file_response(
            path,
            filename=path.name,
            content_type="application/zip",
        )
        self._exports.pop(export_id, None)
        response.background = BackgroundTask(_remove_export_file, path)
        return response

    @filter.command("image_gen", alias={"img"})
    async def image_gen(self, event: AstrMessageEvent):
        """Generate one or more images through the configured default provider."""

        try:
            options = _parse_command(str(event.message_str or ""))
            references = await self._event_references(
                event, options.get("reference_paths", [])
            )
            mode = options["mode"]
            if not options["mode_explicit"] and references:
                mode = "img2img"
            result = await self._service_or_raise().generate(
                mode=mode,
                provider_id=options["provider_id"],
                prompt=options["prompt"],
                negative_prompt=options["negative_prompt"],
                model=options["model"],
                size=options["size"],
                count=options["count"],
                parameters=options["parameters"],
                references=references,
                source="command",
                invocation_source=(
                    _invocation_source(event)
                    if self._settings.history.record_invocation_identity
                    else None
                ),
            )
        except (ValueError, ProviderError) as exc:
            yield event.plain_result(f"生图失败：{exc}")
            return
        chain: list[Any] = [Plain(f"已生成 {len(result.images)} 张图片")]
        chain.extend(Image.fromBytes(image.data) for image in result.images)
        yield event.chain_result(chain)

    async def _event_references(
        self,
        event: AstrMessageEvent,
        explicit_references: list[Any] | None = None,
        *,
        include_event_references: bool = True,
    ) -> tuple[Any, ...]:
        service = self._service_or_raise()
        references: list[Any] = []
        seen: set[str] = set()
        workspace_root = await _event_workspace_root(
            event,
            getattr(self, "context", None),
        )

        async def append_reference(raw_ref: str, *, required: bool = False) -> bool:
            if len(references) >= 8:
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
            if digest not in seen:
                seen.add(digest)
                references.append(reference)
            return True

        for item in (explicit_references or [])[:8]:
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
                        retention_hours=self._settings.asset_lease_hours,
                    )
                    if loaded is None:
                        raise ValueError("参考图资产不存在、已过期或不属于当前会话")
                    image, internal_path = loaded
                    digest = hashlib.sha256(image.data).hexdigest()
                    if digest not in seen:
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
            return tuple(references[:8])
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
        return tuple(references[:8])

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
        model_ref: str = "",
    ) -> mcp.types.CallToolResult:
        """使用 image_studio_generate 工具前必须先调用本工具查询模型能力。

        Args:
            query_type(string): default(查询默认模型参数)/all(查询全部模型参数)/model(搭配model_ref参数查询指定模型参数); 默认 default
            mode(string): text2img/img2img; query_type=default 时可省略
            model_ref(string): provider_id:model_id; 仅在 query_type=model 时使用
        """

        if not self._settings.enable_llm_tool:
            return mcp.types.CallToolResult(
                content=[
                    mcp.types.TextContent(
                        type="text", text="Image Studio 的 LLM 生图工具已关闭。"
                    )
                ],
                isError=True,
            )
        normalized_mode = str(mode or "").strip().lower()
        if normalized_mode not in {"", "text2img", "img2img"}:
            return mcp.types.CallToolResult(
                content=[
                    mcp.types.TextContent(
                        type="text", text="mode 仅支持 text2img 或 img2img。"
                    )
                ],
                isError=True,
            )
        normalized_query_type = str(query_type or "default").strip().lower()
        requested_ref = str(model_ref or "").strip()
        if normalized_query_type != "model" and requested_ref:
            normalized_query_type = "model"
        if normalized_query_type not in {"all", "default", "model"}:
            return _tool_error("query_type 仅支持 all、default 或 model。")
        requested_refs: set[str] = set()
        default_modes_by_ref: dict[str, list[str]] = {}
        if normalized_query_type == "default":
            target_modes = (
                (normalized_mode,) if normalized_mode else ("text2img", "img2img")
            )
            for target_mode in target_modes:
                default_ref = self._settings.default_model_ref(target_mode, "llm_tool")
                if not default_ref:
                    continue
                requested_refs.add(default_ref)
                default_modes_by_ref.setdefault(default_ref, []).append(target_mode)
            if not requested_refs:
                return _tool_error(
                    "当前模式尚未设置默认 LLM 生图模型。"
                    if normalized_mode
                    else "尚未设置可用的文生图或图生图默认 LLM 模型。"
                )
        elif normalized_query_type == "model" and not requested_ref:
            return _tool_error("查询指定模型时必须传入 model_ref。")
        elif normalized_query_type == "model":
            requested_refs.add(requested_ref)
        entries: list[dict[str, Any]] = []
        for provider in self._settings.providers:
            if not provider.enabled:
                continue
            for model in provider.models:
                if not model.llm_enabled:
                    continue
                ref = f"{provider.id}:{model.id}"
                matched_requested_ref = next(
                    (
                        candidate
                        for candidate in requested_refs
                        if candidate in {ref, model.id}
                    ),
                    "",
                )
                if requested_refs and not matched_requested_ref:
                    continue
                modes = [
                    candidate
                    for candidate, supported in (
                        ("text2img", model.supports("text2img")),
                        ("img2img", model.supports("img2img")),
                    )
                    if supported
                ]
                if normalized_mode and normalized_mode not in modes:
                    continue
                default_for_modes = [
                    candidate
                    for candidate in default_modes_by_ref.get(
                        matched_requested_ref or ref, ()
                    )
                    if candidate in modes
                ]
                if normalized_query_type == "default" and not default_for_modes:
                    continue
                query_modes = (
                    default_for_modes
                    if normalized_query_type == "default"
                    else [normalized_mode]
                    if normalized_mode
                    else modes
                )
                tool = model.tool
                exposed_parameters: dict[str, Any] = {}
                configured_parameters = tool.get("parameters")
                exposed_parameter_names = model.llm_exposed_parameter_names
                for name, descriptor in model.parameters.items():
                    # negative_prompt is a reserved dynamic field whose exposure
                    # is controlled separately from the model schema.
                    if name == "negative_prompt":
                        continue
                    if name not in exposed_parameter_names:
                        continue
                    if (
                        isinstance(configured_parameters, dict)
                        and configured_parameters
                    ):
                        policy = configured_parameters.get(name)
                        if not isinstance(policy, dict) or not policy.get(
                            "exposed", True
                        ):
                            continue
                        visible = _llm_parameter_descriptor(descriptor, policy)
                        if "default_override" in policy:
                            visible["default"] = policy["default_override"]
                        exposed_parameters[name] = visible
                    else:
                        exposed_parameters[name] = _llm_parameter_descriptor(
                            descriptor, {}
                        )
                if model.llm_negative_prompt_enabled:
                    exposed_parameters["negative_prompt"] = {
                        "type": "string",
                        "description": (
                            "专用反向提示词，只填写不希望出现在画面中的内容；"
                            "省略时使用模型配置中的默认反向提示词。"
                        ),
                        "default": model.negative_prompt_default,
                    }
                prompt_profile = tool.get("prompt_profile", "natural_language")
                prompt_instructions = tool.get("prompt_instructions", "")
                entry = {
                    "model_ref": ref,
                    "provider_name": provider.name,
                    "model_name": model.name,
                    "modes": modes,
                    "query_modes": query_modes,
                    "max_reference_images": model.llm_max_reference_images,
                    "selection_description": tool.get("selection_description", ""),
                    "prompt_contract": {
                        "format": prompt_profile,
                        "instruction": prompt_instructions,
                        "negative_prompt": (
                            "仅在 parameters 返回该字段时使用。"
                            if model.llm_negative_prompt_enabled
                            else "不要传入 negative_prompt。"
                        ),
                    },
                    "parameters": exposed_parameters,
                }
                if default_for_modes:
                    entry["default_for_modes"] = default_for_modes
                entries.append(entry)
        for candidate in requested_refs:
            if ":" in candidate:
                continue
            matching_entries = [
                entry
                for entry in entries
                if str(entry.get("model_ref") or "").endswith(f":{candidate}")
            ]
            if len(matching_entries) > 1:
                return _tool_error("模型 ID 不唯一，请使用 provider_id:model_id 查询。")
        if not entries:
            return _tool_error(
                "没有符合条件的模型；模型可能不存在、已停用、不支持该模式或未向 LLM 工具开放。"
            )
        _remember_capability_query(event, self._settings.revision, entries)
        _activate_image_workflow(event)
        if normalized_query_type == "default":
            next_action = (
                "按请求模式选择 default_for_modes；普通画面描述不是能力缺口。满足明确能力则直接生成，"
                "否则查询相同 mode 的 all。"
                if not normalized_mode
                else "普通画面描述不是能力缺口；满足明确能力则直接生成，否则查询相同 mode 的 all。"
            )
        elif normalized_query_type == "all":
            next_action = "选择满足要求的模型直接生成，无需再次 model 查询。"
        else:
            next_action = "按 prompt_contract 和 parameters 直接生成。"
        payload = {
            "query_type": normalized_query_type,
            "next_action": next_action,
            "asset_policy": {
                "return_mode": self._settings.llm_image_return_mode,
                "preview_max_edge": self._settings.asset_preview_max_edge,
                "retention_hours": self._settings.asset_lease_hours,
                "private_asset_handle": "asset_id",
                "view_tool": "image_studio_view_asset",
                "delivery_tool": "image_studio_send_output",
            },
            "default_model_refs": {
                "text2img": self._settings.default_model_ref("text2img", "llm_tool"),
                "img2img": self._settings.default_model_ref("img2img", "llm_tool"),
            },
            "models": entries,
        }
        return mcp.types.CallToolResult(
            content=[
                mcp.types.TextContent(
                    type="text",
                    text=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                )
            ]
        )

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
            resolved_references = await self._event_references(
                event,
                explicit_references,
                include_event_references=(
                    not explicit_references and normalized_requested_mode != "text2img"
                ),
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
                else selected_model.negative_prompt_default
                if selected_model.negative_prompt
                else ""
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
        actual_return_mode = configured_return_mode
        try:
            assets = await self.store.lease_agent_images(
                result.images,
                scope_id=_event_scope_id(event),
                create_preview=configured_return_mode == "preview",
                preview_max_edge=self._settings.asset_preview_max_edge,
                preview_quality=self._settings.asset_preview_quality,
                retention_hours=self._settings.asset_lease_hours,
            )
        except Exception as exc:
            logger.warning(
                "%s 暂存 Agent 原图失败，回退为完整图片返回: %s",
                LOG_TAG,
                type(exc).__name__,
            )
            assets = ()
            actual_return_mode = "original_fallback"

        if actual_return_mode in {"original", "original_fallback"}:
            visual_images = result.images
        elif actual_return_mode == "preview":
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
        if actual_return_mode == "preview":
            visual_notice = "ImageContent 是轻量预览；继续操作插件资产时使用 asset_id。"
        elif actual_return_mode == "asset":
            visual_notice = (
                "未附视觉图；需要看图时使用 asset_id 调用 image_studio_view_asset。"
            )
        else:
            visual_notice = (
                "ImageContent 为原图；后续插件操作仍使用 asset_id。"
                if assets
                else "资产暂存失败，仅返回完整 ImageContent。"
            )
        content.append(
            mcp.types.TextContent(
                type="text",
                text=(
                    f"工作流资产已生成：{len(result.images)} 张图片；"
                    f"generation_id={result.generation_id or '未保存'}；"
                    f"return_mode={actual_return_mode}；"
                    f"assets={json.dumps(manifest, ensure_ascii=False, separators=(',', ':'))}。"
                    f"{visual_notice} 这是 Image Studio 工作流资产；发送工具只负责中途投递。"
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
        asset_id: str = "",
        detail: str = "preview",
    ) -> mcp.types.CallToolResult:
        """按需查看当前会话有权访问的 Image Studio 图片资产。

        Args:
            asset_id(string): generate 返回的资产 ID。
            detail(string): preview 或 original; 默认 preview。
        """

        if not self._settings.enable_llm_tool:
            return _tool_error("Image Studio 的 LLM 生图工具已关闭。")
        normalized_detail = str(detail or "preview").strip().lower()
        if normalized_detail not in {"preview", "original"}:
            return _tool_error("detail 仅支持 preview 或 original。")
        loaded = await self.store.load_workflow_image(
            asset_id,
            scope_id=_event_scope_id(event),
            detail=normalized_detail,
            preview_max_edge=self._settings.asset_preview_max_edge,
            preview_quality=self._settings.asset_preview_quality,
            retention_hours=self._settings.asset_lease_hours,
        )
        if loaded is None:
            return _tool_error("图片资产不存在或已超过临时保留时间。")
        image, _internal_path = loaded
        return mcp.types.CallToolResult(
            content=[
                mcp.types.ImageContent(
                    type="image",
                    data=base64.b64encode(image.data).decode("ascii"),
                    mimeType=image.mime_type,
                ),
                mcp.types.TextContent(
                    type="text",
                    text=f"已加载 {normalized_detail}；后续插件操作继续使用 asset_id。",
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
            retention_hours=self._settings.asset_lease_hours,
        )
        if loaded is None:
            raise ValueError("图片资产不存在、已过期或不属于当前会话。")
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


def _tool_error(message: str) -> mcp.types.CallToolResult:
    return mcp.types.CallToolResult(
        content=[mcp.types.TextContent(type="text", text=message)], isError=True
    )


def _tool_text_result(message: str) -> mcp.types.CallToolResult:
    return mcp.types.CallToolResult(
        content=[mcp.types.TextContent(type="text", text=message)]
    )


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


async def _save_config_async(config: Any) -> None:
    save = getattr(config, "save_config_async", None)
    if callable(save):
        await save()
        return
    save = getattr(config, "save_config", None)
    if callable(save):
        result = save()
        if asyncio.iscoroutine(result):
            await result
        return
    raise RuntimeError("当前插件配置对象不支持保存")


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


def _llm_parameter_descriptor(
    descriptor: dict[str, Any], policy: dict[str, Any]
) -> dict[str, Any]:
    visible = {
        key: descriptor[key]
        for key in ("type", "default", "min", "max", "step")
        if key in descriptor
    }
    description = str(
        policy.get("description")
        or descriptor.get("description")
        or descriptor.get("label")
        or ""
    )
    if description:
        visible["description"] = description
    choices = descriptor.get("choices")
    choice_descriptions = policy.get("choice_descriptions")
    if isinstance(choices, list):
        visible_choices: list[dict[str, Any]] = []
        for choice in choices:
            value = choice.get("value") if isinstance(choice, dict) else choice
            label = choice.get("label", value) if isinstance(choice, dict) else value
            choice_description = (
                choice_descriptions.get(str(value), "")
                if isinstance(choice_descriptions, dict)
                else ""
            )
            visible_choice = {"value": value}
            if label != value:
                visible_choice["label"] = label
            if choice_description:
                visible_choice["description"] = choice_description
            visible_choices.append(visible_choice)
        visible["choices"] = visible_choices
    return visible


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


def _parse_command(raw: str) -> dict[str, Any]:
    text = raw.strip()
    for prefix in ("/image_gen", "image_gen", "/img", "img"):
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
            break
    tokens = shlex.split(text)
    values: dict[str, Any] = {
        "mode": "text2img",
        "provider_id": "",
        "model": "",
        "size": "",
        "negative_prompt": "",
        "count": 1,
        "reference_paths": [],
        "mode_explicit": False,
        "parameters": {},
    }
    prompt_parts: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("--") and "=" in token:
            key, value = token[2:].split("=", 1)
        elif token.startswith("--") and index + 1 < len(tokens):
            key, value = token[2:], tokens[index + 1]
            index += 1
        else:
            prompt_parts.append(token)
            index += 1
            continue
        if key == "mode":
            values["mode"] = value
            values["mode_explicit"] = True
        elif key == "provider":
            values["provider_id"] = value
        elif key == "model":
            values["model"] = value
        elif key == "size":
            values["size"] = value
        elif key == "negative":
            values["negative_prompt"] = value
        elif key == "n":
            values["count"] = _as_int(value, 1)
        elif key == "ref":
            values["reference_paths"].append(value)
        elif key.startswith("param-"):
            values["parameters"][key.removeprefix("param-")] = value
        else:
            prompt_parts.append(token)
        index += 1
    values["prompt"] = " ".join(prompt_parts).strip()
    if not values["prompt"]:
        raise ValueError(
            "用法：/image_gen <提示词> [--provider id] [--mode text2img|img2img] [--size 1024x1024] [--ref 路径]"
        )
    return values


def _remove_export_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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
