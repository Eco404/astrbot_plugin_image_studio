"""AstrBot Image Studio plugin entry point."""

import asyncio
import base64
import copy
import hashlib
import json
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
from starlette.background import BackgroundTask

from .config import (
    load_studio_settings,
    normalize_webui_settings,
    runtime_settings,
    save_studio_settings,
)
from .models import ImageProvider
from .providers import ProviderError, ProviderExecutor
from .service import ImageGenerationService
from .storage import GenerationStore, detect_mime_type, image_data_url

PLUGIN_NAME = "astrbot_plugin_image_studio"
PAGE_PREFIX = f"/{PLUGIN_NAME}"
LOG_TAG = "[ImageStudio]"


@register(
    PLUGIN_NAME,
    "local",
    "多 Provider 生图、画廊与 Agent 可读图片工具。",
    "0.2.0",
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
                    "provider_id": self._settings.default_provider_id,
                    "model_ref": self._settings.default_model_ref,
                    "size": self._settings.default_size,
                    "count": self._settings.default_count,
                    "text2img_model_ref": self._settings.default_text2img_model_ref,
                    "img2img_model_ref": self._settings.default_img2img_model_ref,
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
        for raw_path in explicit_paths or []:
            reference = await service.reference_from_safe_path(raw_path)
            if reference is not None:
                digest = hashlib.sha256(reference.data).hexdigest()
                if digest not in seen:
                    seen.add(digest)
                    references.append(reference)
        message_chain = event.get_messages() if hasattr(event, "get_messages") else []
        for component in _iter_event_images(message_chain):
            try:
                path = await component.convert_to_file_path()
                reference = await service.reference_from_safe_path(path)
            except (OSError, ValueError):
                continue
            if reference is None:
                continue
            digest = hashlib.sha256(reference.data).hexdigest()
            if digest not in seen:
                seen.add(digest)
                references.append(reference)
        return tuple(references[:8])

    @filter.llm_tool(name="image_gen_get_capabilities")
    async def image_gen_get_capabilities(
        self,
        event: AstrMessageEvent,
        mode: str = "",
        model_ref: str = "",
    ) -> mcp.types.CallToolResult:
        """查询当前可供 LLM 使用的生图模型、选择规则和参数。

        用户明确要求 NAI、指定模型或特殊参数，或者不能确定模型能力时，先调用本工具，
        再严格按照返回的 model_ref、prompt_profile、prompt_instructions 和参数说明调用
        image_gen_generate。一般自然语言生图无需预先查询。不要编造模型或参数。

        Args:
            mode(string): 可选的 text2img 或 img2img，用于筛选模式。
            model_ref(string): 可选的 provider_id:model_id，用于查询单个模型详情。
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
        requested_ref = str(model_ref or "").strip()
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
                entries.append(
                    {
                        "model_ref": ref,
                        "provider_name": provider.name,
                        "model_name": model.name,
                        "modes": modes,
                        "supports_negative_prompt": model.negative_prompt,
                        "max_reference_images": model.llm_max_reference_images,
                        "selection_description": tool.get("selection_description", ""),
                        "prompt_profile": tool.get(
                            "prompt_profile", "natural_language"
                        ),
                        "prompt_instructions": tool.get("prompt_instructions", ""),
                        "parameters": exposed_parameters,
                    }
                )
        if requested_ref and not entries:
            return mcp.types.CallToolResult(
                content=[
                    mcp.types.TextContent(
                        type="text", text="指定模型不存在、已停用或未向 LLM 工具开放。"
                    )
                ],
                isError=True,
            )
        payload = {
            "usage": "一般需求使用默认自然语言模型；明确要求 NAI 或特殊参数时选择对应 model_ref。",
            "models": entries,
        }
        return mcp.types.CallToolResult(
            content=[
                mcp.types.TextContent(
                    type="text", text=json.dumps(payload, ensure_ascii=False, indent=2)
                )
            ]
        )

    @filter.llm_tool(name="image_gen_generate")
    async def image_gen_generate(
        self,
        event: AstrMessageEvent,
        prompt: str = "",
        mode: str = "",
        provider_id: str = "",
        model_ref: str = "",
        model: str = "",
        size: str = "",
        negative_prompt: str = "",
        reference_image_path: str = "",
        count: int = 0,
        parameters: dict[str, Any] | None = None,
        reference_image_paths: list[str] | None = None,
    ) -> mcp.types.CallToolResult:
        """使用 Image Studio 生成或修改图片，并返回 Agent 可读取的图片内容。

        一般自然语言生图可直接调用，省略 model_ref 时使用对应模式的默认模型。用户明确
        要求 NAI、指定模型或特殊参数，或者不能确定模型能力时，先调用
        image_gen_get_capabilities。用户消息或引用消息中的图片可直接用于图生图；不要编造
        模型、参数或参考图路径。

        Args:
            prompt(string): 希望生成或修改的图片描述。
            mode(string): ``text2img`` or ``img2img``，省略时根据参考图自动判断。
            provider_id(string): 可选的已配置服务商 ID。
            model_ref(string): 稳定的 provider_id:model_id，可选。
            model(string): 可选的兼容模型 ID；优先使用 model_ref。
            size(string): 可选的图片尺寸。
            negative_prompt(string): 当前模型支持时使用的可选反向提示词。
            reference_image_path(string): Agent 已有工具图片的可选路径，用于图生图。
            count(number): 生成数量，受模型和服务商限制。
            parameters(object): 能力查询工具返回并向 LLM 暴露的动态参数。
            reference_image_paths(array[string]): 多张 Agent 已有工具图片的路径。

        Returns:
            Text and image MCP content. AstrBot caches image content for the next Agent step.
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
            result = await self._service_or_raise().generate(
                mode=normalized_mode,
                provider_id=provider_id,
                prompt=prompt,
                negative_prompt=negative_prompt,
                model_ref=model_ref,
                model=model,
                size=size,
                count=count,
                parameters=parameters,
                references=references,
                source="llm_tool",
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
                    f"已生成 {len(result.images)} 张图片。请先查看图片；确认满足用户请求后，"
                    "使用 send_message_to_user 发送对应缓存图片。"
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
