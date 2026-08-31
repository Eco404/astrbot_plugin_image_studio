"""AstrBot Image Studio plugin entry point."""

import asyncio
import base64
import copy
import hashlib
import json
import re
import shlex
import time
import uuid
from pathlib import Path
from typing import Any

import aiohttp
import mcp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain, Reply
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.api.web import error_response, file_response, json_response
from astrbot.api.web import request as web_request
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
from .models import ImageProvider, InvocationSource
from .providers import ProviderError, ProviderExecutor
from .service import ImageGenerationService
from .storage import GenerationStore, detect_mime_type, image_data_url

PLUGIN_NAME = "astrbot_plugin_image_studio"
PAGE_PREFIX = f"/{PLUGIN_NAME}"
LOG_TAG = "[ImageStudio]"
CAPABILITY_QUERY_EXTRA_KEY = "_image_studio_capability_queries"


@register(
    PLUGIN_NAME,
    "local",
    "多 Provider 生图、画廊与 Agent 可读图片工具。",
    "0.3.2",
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
        self._exports: dict[str, tuple[Path, float]] = {}
        self._studio_settings, studio_errors = load_studio_settings(Path(self.data_dir))
        self._settings, runtime_errors = runtime_settings(config, self._studio_settings)
        self._settings_errors = [*studio_errors, *runtime_errors]

    async def initialize(self) -> None:
        """Initialize storage, HTTP resources, and Page API routes."""

        await self.store.initialize()
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

        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
        self._service = None

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
        return json_response(_result_payload(result))

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
        self, event: AstrMessageEvent, explicit_paths: list[str] | None = None
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

        for raw_path in (explicit_paths or [])[:8]:
            await append_reference(raw_path, required=True)
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

    @filter.llm_tool(name="image_studio_get_capabilities")
    async def image_studio_get_capabilities(
        self,
        event: AstrMessageEvent,
        query_type: str = "default",
        mode: str = "",
        model_ref: str = "",
    ) -> mcp.types.CallToolResult:
        """查询当前可供 LLM 使用的生图模型、选择规则和参数。

        每次调用 image_studio_generate 前都必须先调用本工具。常规生图必须首先使用
        default，并根据是否有参考图指定 text2img 或 img2img；默认模型没有明确能力缺口时，
        直接生成，不得继续查询 all。只有默认模型缺少用户明确要求的模式、参考图数量或参数，
        默认模型不可用，用户要求比较模型，或指定了模型类型却不知道 model_ref 时，才使用
        all 并尽量携带 mode。用户明确指定 model_ref 时直接查询 model。

        Args:
            query_type(string): default、all 或 model。常规请求默认使用 default。
            mode(string): 可选的 text2img 或 img2img，用于筛选模式。
            model_ref(string): query_type=model 时必填的 provider_id:model_id。
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
        if normalized_query_type == "default":
            if not normalized_mode:
                return _tool_error("查询默认模型时必须指定 mode。")
            requested_ref = self._settings.default_model_ref(
                normalized_mode, "llm_tool"
            )
            if not requested_ref:
                return _tool_error("当前模式尚未设置默认 LLM 生图模型。")
        elif normalized_query_type == "model" and not requested_ref:
            return _tool_error("查询指定模型时必须传入 model_ref。")
        entries: list[dict[str, Any]] = []
        for provider in self._settings.providers:
            if not provider.enabled:
                continue
            for model in provider.models:
                if not model.llm_enabled:
                    continue
                ref = f"{provider.id}:{model.id}"
                if requested_ref and requested_ref not in {ref, model.id}:
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
                tool = model.tool
                exposed_parameters: dict[str, Any] = {}
                configured_parameters = tool.get("parameters")
                for name, descriptor in model.parameters.items():
                    # negative_prompt is a reserved dynamic field whose exposure
                    # is controlled separately from the model schema.
                    if name == "negative_prompt":
                        continue
                    if (
                        descriptor.get("ui_only")
                        and str(descriptor.get("type") or "").lower() != "preset"
                    ):
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
                entries.append(
                    {
                        "model_ref": ref,
                        "provider_name": provider.name,
                        "model_name": model.name,
                        "modes": modes,
                        "supports_negative_prompt": model.negative_prompt,
                        "negative_prompt_exposed": (model.llm_negative_prompt_enabled),
                        "max_reference_images": model.llm_max_reference_images,
                        "selection_description": tool.get("selection_description", ""),
                        "prompt_profile": prompt_profile,
                        "prompt_instructions": prompt_instructions,
                        "prompt_contract": {
                            "format": prompt_profile,
                            "instruction": prompt_instructions,
                            "negative_prompt": (
                                "需要时仅通过 parameters.negative_prompt 传入。"
                                if model.llm_negative_prompt_enabled
                                else "不要传入 negative_prompt。"
                            ),
                        },
                        "parameters": exposed_parameters,
                    }
                )
        if requested_ref and len(entries) > 1 and ":" not in requested_ref:
            return _tool_error("模型 ID 不唯一，请使用 provider_id:model_id 查询。")
        if not entries:
            return _tool_error(
                "没有符合条件的模型；模型可能不存在、已停用、不支持该模式或未向 LLM 工具开放。"
            )
        _remember_capability_query(event, self._settings.revision, entries)
        if normalized_query_type == "default":
            next_action = (
                "普通主体、画风、构图和文字描述可由提示词表达，不属于能力缺口。若返回模型满足用户明确"
                "要求的模式、参考图数量和参数，立即调用 image_studio_generate，不得查询 all；仅在存在"
                "可指出的能力缺口时，使用相同 mode 查询 all。"
            )
        elif normalized_query_type == "all":
            next_action = (
                "从返回结果中选择满足明确要求的模型并直接调用 image_studio_generate，"
                "无需再使用 model 查询。"
            )
        else:
            next_action = "按照返回模型的 prompt_contract 和 parameters 直接调用 image_studio_generate。"
        payload = {
            "query_type": normalized_query_type,
            "usage": (
                "本次查询结果只授权当前轮次中列出的模型和模式。生成时必须遵守每个模型的 "
                "prompt_contract，且只能传入 parameters 中列出的动态参数。"
            ),
            "routing_contract": {
                "default_first": (
                    "未指定模型或模型类型的常规请求，必须先查询对应 mode 的默认模型。"
                ),
                "capability_gap_only": (
                    "只有缺少用户明确要求的模式、参考图数量或暴露参数，才算默认模型不符合；"
                    "普通画面内容和审美描述不算能力缺口。"
                ),
                "query_all_only_when": (
                    "默认模型存在明确能力缺口或不可用，用户要求比较模型，或指定了模型类型但不知道 model_ref。"
                ),
                "next_action": next_action,
            },
            "workflow_contract": {
                "generation_result": (
                    "image_studio_generate 返回可继续处理的图片资产；生成成功不等于整个用户任务已经完成。"
                ),
                "delivery_tool": (
                    "send_message_to_user 只执行即时发送，不会结束本轮 Agent。可用于发送阶段产物或必要的"
                    "中途文字；凡通过它发送的内容都已对目标可见，后续步骤和最终回复不得复述。"
                ),
                "final_response": (
                    "完成剩余处理后仍需正常结束本轮；最终回复只补充尚未发送的用户可见内容。"
                ),
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
                    type="text", text=json.dumps(payload, ensure_ascii=False, indent=2)
                )
            ]
        )

    @filter.llm_tool(name="image_studio_generate")
    async def image_studio_generate(
        self,
        event: AstrMessageEvent,
        prompt: str = "",
        mode: str = "",
        provider_id: str = "",
        model_ref: str = "",
        model: str = "",
        size: str = "",
        reference_image_path: str = "",
        count: int = 0,
        parameters: dict[str, Any] | None = None,
        reference_image_paths: list[str] | None = None,
    ) -> mcp.types.CallToolResult:
        """使用 Image Studio 生成或修改图片，并返回 Agent 可继续处理的工作流资产。

        每次生成前都必须先调用 image_studio_get_capabilities。未指定模型或模型类型的常规
        请求必须先查询对应 mode 的默认模型；默认模型满足明确能力要求时直接生成，不得查询
        all。只有默认模型存在明确能力缺口或不可用、用户要求比较模型，或指定了模型类型却不知
        道 model_ref 时才查询 all。明确 model_ref 时查询 model。严格使用查询结果指定的提示词
        格式，不要把自然语言模型的提示词写成 NAI tag 串，也不要传入未列出的参数。
        用户消息或引用消息中的图片可直接用于图生图；不要编造模型、参数或参考图路径。
        生成成功不代表整个用户任务已经完成：如果还需要多次生图、
        改图、拼接、制作 GIF 或其他处理，继续使用返回的缓存图片，除非用户需要查看阶段结果，
        否则不要发送中间产物。send_message_to_user 只执行即时发送，不会结束本轮 Agent；它可用于
        发送产物或必要的中途文字。凡通过它发送的内容都已对目标可见，后续步骤和最终回复不得复述。

        Args:
            prompt(string): 希望生成或修改的图片描述。
            mode(string): ``text2img`` or ``img2img``，省略时根据参考图自动判断。
            provider_id(string): 可选的已配置服务商 ID。
            model_ref(string): 稳定的 provider_id:model_id，可选。
            model(string): 可选的兼容模型 ID；优先使用 model_ref。
            size(string): 可选的图片尺寸。
            reference_image_path(string): Agent 已有工具图片的可选路径；相对路径按当前 Agent 工作区解析。
            count(number): 生成数量，受模型和服务商限制。
            parameters(object): 能力查询返回并向 LLM 暴露的动态参数；只有查询结果列出时才可包含 negative_prompt。
            reference_image_paths(array[string]): 多张 Agent 已有工具图片的路径；当前或引用消息图片通常无需填写。

        Returns:
            Workflow metadata and image MCP content. AstrBot caches images for later Agent steps.
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
            paths = list(reference_image_paths or [])
            if reference_image_path:
                paths.insert(0, reference_image_path)
            references = await self._event_references(event, paths)
            normalized_mode = str(mode or "").strip() or (
                "img2img" if references else "text2img"
            )
            normalized_mode = _llm_mode(normalized_mode)
            provider, selected_model, canonical_ref = _select_llm_tool_model(
                self._settings,
                normalized_mode,
                provider_id=provider_id,
                model_ref=model_ref,
                model=model,
            )
            if not _consume_capability_query(
                event, self._settings.revision, canonical_ref, normalized_mode
            ):
                raise ValueError(
                    "生成前必须先调用 image_studio_get_capabilities 查询当前模型能力和提示词格式"
                )
            if parameters is not None and not isinstance(parameters, dict):
                raise ValueError("parameters 必须是对象")
            dynamic_parameters = dict(parameters or {})
            has_negative_prompt = "negative_prompt" in dynamic_parameters
            supplied_negative_prompt = dynamic_parameters.pop("negative_prompt", "")
            if has_negative_prompt and not selected_model.negative_prompt:
                raise ValueError("当前模型不支持专用反向提示词，请移除 negative_prompt")
            if has_negative_prompt and not selected_model.llm_negative_prompt_enabled:
                raise ValueError("negative_prompt 未在当前模型的工具配置中向 LLM 开放")
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
                size=size,
                count=count,
                parameters=dynamic_parameters,
                references=references,
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
        content: list[Any] = [
            mcp.types.TextContent(
                type="text",
                text=(
                    f"工作流资产已生成：{len(result.images)} 张图片；"
                    f"generation_id={result.generation_id or '未保存'}。"
                    "图片会作为可继续处理的缓存资产返回。若任务还需多次生图、改图、拼接或制作 GIF，"
                    "继续处理，除非用户需要查看阶段结果，否则不要发送中间产物。send_message_to_user "
                    "只执行即时发送，不会结束本轮 Agent；可用它发送产物或必要的中途文字，但已发送内容"
                    "已经对目标可见，后续步骤和最终回复不得复述。完成剩余任务后仍需正常结束本轮。"
                ),
            )
        ]
        for image in result.images:
            content.append(
                mcp.types.ImageContent(
                    type="image",
                    data=base64.b64encode(image.data).decode("ascii"),
                    mimeType=image.mime_type,
                )
            )
        return mcp.types.CallToolResult(content=content)


def _tool_error(message: str) -> mcp.types.CallToolResult:
    return mcp.types.CallToolResult(
        content=[mcp.types.TextContent(type="text", text=message)], isError=True
    )


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
        modes = entry.get("modes")
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
    provider_id: str,
    model_ref: str,
    model: str,
) -> tuple[Any, Any, str]:
    """Resolve an exact LLM-enabled model after capability discovery."""

    requested_provider = str(provider_id or "").strip()
    explicit_ref = str(model_ref or model or "").strip()
    requested_ref = explicit_ref or settings.default_model_ref(mode, "llm_tool")
    if not requested_ref:
        raise ValueError(
            "当前模式未设置默认 LLM 生图模型，请查询全部模型并传入 model_ref"
        )
    candidates = [
        (provider, candidate, f"{provider.id}:{candidate.id}")
        for provider in settings.providers
        if provider.enabled
        and (not requested_provider or provider.id == requested_provider)
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


def _result_payload(result) -> dict[str, Any]:
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
            }
            for image in result.images
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
    visible["description"] = str(
        policy.get("description")
        or descriptor.get("description")
        or descriptor.get("label")
        or ""
    )
    choices = descriptor.get("choices")
    choice_descriptions = policy.get("choice_descriptions")
    if isinstance(choices, list):
        visible["choices"] = [
            {
                "value": choice.get("value"),
                "label": choice.get("label", choice.get("value")),
                "description": (
                    choice_descriptions.get(str(choice.get("value")), "")
                    if isinstance(choice_descriptions, dict)
                    else ""
                ),
            }
            if isinstance(choice, dict)
            else {
                "value": choice,
                "label": choice,
                "description": (
                    choice_descriptions.get(str(choice), "")
                    if isinstance(choice_descriptions, dict)
                    else ""
                ),
            }
            for choice in choices
        ]
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
