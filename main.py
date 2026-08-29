"""AstrBot Image Studio plugin entry point."""

import asyncio
import base64
import copy
import shlex
import time
import uuid
from pathlib import Path
from typing import Any

import aiohttp
import mcp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.api.web import error_response, json_response
from astrbot.api.web import request as web_request

from .config import normalize_webui_settings, runtime_settings
from .models import ImageProvider
from .providers import ProviderError, ProviderExecutor
from .service import ImageGenerationService
from .storage import GenerationStore, detect_mime_type, image_data_url


PLUGIN_NAME = "astrbot_plugin_image_gen"
PAGE_PREFIX = f"/{PLUGIN_NAME}"
LOG_TAG = "[ImageStudio]"


@register(
    PLUGIN_NAME,
    "local",
    "多 Provider 生图、画廊与 Agent 可读图片工具。",
    "0.1.0",
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
        self._settings, self._settings_errors = runtime_settings(config)

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
                "provider/test",
                self._api_test_provider,
                ["POST"],
                "Image Studio: test provider",
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
                "enabled": self._settings.enabled,
                "defaults": {
                    "provider_id": self._settings.default_provider_id,
                    "size": self._settings.default_size,
                    "count": self._settings.default_count,
                },
                "providers": [
                    provider.public_dict()
                    for provider in self._settings.providers
                    if provider.enabled
                ],
                "modes": ["text2img", "img2img"],
            }
        )

    async def _api_get_settings(self) -> Any:
        webui, errors = normalize_webui_settings(self.config.get("webui_managed"))
        return json_response(
            {
                "base": {
                    "enabled": bool(self.config.get("enabled", True)),
                    "enable_llm_tool": bool(self.config.get("enable_llm_tool", True)),
                    "max_concurrent_generations": int(
                        self.config.get("max_concurrent_generations", 2) or 2
                    ),
                },
                "webui": webui,
                "validation_errors": errors,
            }
        )

    async def _api_save_settings(self) -> Any:
        body = await web_request.json(default={})
        if not isinstance(body, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)
        expected_revision = _as_int(body.get("settings_revision"), -1)
        async with self._settings_lock:
            current_webui, _ = normalize_webui_settings(
                self.config.get("webui_managed")
            )
            current_revision = _as_int(
                current_webui.get("ui", {}).get("settings_revision"), 0
            )
            if expected_revision != current_revision:
                return error_response(
                    "设置已被其他操作更新，请刷新后再保存", status_code=409
                )
            candidate, errors = normalize_webui_settings(body.get("webui"))
            if errors:
                return error_response("；".join(errors), status_code=400)
            candidate["ui"]["settings_revision"] = current_revision + 1
            base = body.get("base") if isinstance(body.get("base"), dict) else {}
            previous = copy.deepcopy(dict(self.config))
            self.config["webui_managed"] = candidate
            for key in ("enabled", "enable_llm_tool", "max_concurrent_generations"):
                if key in base:
                    self.config[key] = base[key]
            try:
                await _save_config_async(self.config)
            except Exception as exc:
                self.config.clear()
                self.config.update(previous)
                logger.warning(
                    "%s 保存 WebUI 配置失败: %s", LOG_TAG, type(exc).__name__
                )
                return error_response("配置保存失败", status_code=500)
            self._settings, self._settings_errors = runtime_settings(self.config)
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

    async def _api_test_provider(self) -> Any:
        body = await web_request.json(default={})
        if not isinstance(body, dict) or not isinstance(body.get("provider"), dict):
            return error_response("需要 provider 配置", status_code=400)
        provider = ImageProvider.from_mapping(body["provider"])
        if not provider.id or not provider.base_url or not provider.model:
            return error_response(
                "Provider 需要 id、base_url 和 model", status_code=400
            )
        try:
            result = await self._service_or_raise().executor.generate(
                provider,
                self._test_request(provider.id, provider.model),
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

    def _test_request(self, provider_id: str, model: str):
        from .models import GenerationRequest

        return GenerationRequest(
            mode="text2img",
            provider_id=provider_id,
            prompt="A simple landscape photograph with one tree and clear daylight.",
            model=model,
            size="1024x1024",
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
        detail = await self.store.generation_detail(generation_id)
        if detail is None:
            return error_response("生成记录不存在", status_code=404)
        return json_response(detail)

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
        from aiohttp import web

        item = self._exports.get(export_id)
        if item is None or time.time() - item[1] > 3600 or not item[0].is_file():
            return error_response("导出文件已过期，请重新导出", status_code=404)
        return web.FileResponse(
            item[0],
            headers={"Content-Disposition": f'attachment; filename="{item[0].name}"'},
        )

    @filter.command("image_gen", alias={"img"})
    async def image_gen(self, event: AstrMessageEvent):
        """Generate one or more images through the configured default provider."""

        try:
            options = _parse_command(str(event.message_str or ""))
            references = ()
            if options["reference_path"]:
                reference = await self._service_or_raise().reference_from_safe_path(
                    options["reference_path"]
                )
                references = (reference,) if reference is not None else ()
            result = await self._service_or_raise().generate(
                mode=options["mode"],
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

    @filter.llm_tool(name="image_gen_generate")
    async def image_gen_generate(
        self,
        event: AstrMessageEvent,
        prompt: str = "",
        mode: str = "text2img",
        provider_id: str = "",
        model: str = "",
        size: str = "",
        negative_prompt: str = "",
        reference_image_path: str = "",
    ) -> mcp.types.CallToolResult:
        """Generate an image and return it to the Agent as visual tool content.

        Args:
            prompt(string): Image description.
            mode(string): ``text2img`` or ``img2img``.
            provider_id(string): Optional configured provider ID.
            model(string): Optional configured-model override.
            size(string): Optional image size.
            negative_prompt(string): Optional negative prompt.
            reference_image_path(string): A prior Agent tool-image path for image-to-image.

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
            reference = await self._service_or_raise().reference_from_safe_path(
                reference_image_path
            )
            result = await self._service_or_raise().generate(
                mode=mode,
                provider_id=provider_id,
                prompt=prompt,
                negative_prompt=negative_prompt,
                model=model,
                size=size,
                count=1,
                references=(reference,) if reference is not None else (),
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
        "reference_path": "",
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
            values["reference_path"] = value
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


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
