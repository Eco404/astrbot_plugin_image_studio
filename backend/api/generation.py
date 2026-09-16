"""Generation and provider discovery Page API."""

from __future__ import annotations

from typing import Any

from astrbot.api.web import error_response, json_response
from astrbot.api.web import request as web_request

from ..models import ImageProvider
from ..media.images import (
    detect_mime_type,
    image_data_url,
)
from ..providers.executor import ProviderError

LOG_TAG = "[ImageStudio]"


class GenerationAPI:
    def __init__(
        self, *, store, get_settings, get_service, peek_service, serialize_result
    ):
        self.store = store
        self.get_settings = get_settings
        self.get_service = get_service
        self.peek_service = peek_service
        self.serialize_result = serialize_result

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
            references = await self.get_service().staged_references(
                body.get("reference_ids"),
                mode=str(body.get("mode") or "text2img"),
                provider_id=str(body.get("provider_id") or ""),
                model_ref=str(body.get("model_ref") or ""),
                model=str(body.get("model") or ""),
            )
            result = await self.get_service().generate(
                mode=str(body.get("mode") or "text2img"),
                provider_id=str(body.get("provider_id") or ""),
                prompt=str(body.get("prompt") or ""),
                negative_prompt=str(body.get("negative_prompt") or ""),
                model_ref=str(body.get("model_ref") or ""),
                model=str(body.get("model") or ""),
                size=str(body.get("size") or ""),
                count=body.get("count"),
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
        return json_response(
            self.serialize_result(result, download_detail=download_detail)
        )

    async def _api_test_model(self) -> Any:
        body = await web_request.json(default={})
        if not isinstance(body, dict) or not isinstance(body.get("provider"), dict):
            return error_response("需要服务商配置", status_code=400)
        try:
            provider = ImageProvider.from_mapping(body["provider"])
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
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
            result = await self.get_service().run_provider_request(
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
            models = await self.get_service().executor.discover_models(provider)
        except (ValueError, ProviderError) as exc:
            return error_response(str(exc), status_code=400)
        return json_response({"models": models, "provider_id": provider.id})

    async def _api_provider_quota(self) -> Any:
        provider_id = str(web_request.query.get("provider_id", "")).strip()
        if not provider_id:
            return error_response("请选择要查询的 NAI 服务商", status_code=400)
        provider = self.get_settings().provider(provider_id)
        if provider is None:
            return error_response("服务商不存在或未启用", status_code=404)
        if provider.kind not in {"nai_direct", "novelai_official"}:
            return error_response("当前服务商不支持额度查询", status_code=400)
        if not provider.api_key.strip():
            return error_response("NAI 服务商尚未配置密钥", status_code=400)
        if self.peek_service() is None:
            return error_response(
                "Image Studio 正在初始化，请稍后重试", status_code=503
            )
        try:
            quota = await self.peek_service().executor.fetch_quota(provider)
        except ProviderError as exc:
            return error_response(str(exc), status_code=502)
        response = json_response({"provider_id": provider.id, **quota})
        response.headers["Cache-Control"] = "no-store"
        return response

    @staticmethod
    def _test_request(provider: ImageProvider, model_id: str):
        from ..models import GenerationRequest

        model = provider.get_model(model_id)
        size = "竖图" if provider.kind == "nai_direct" else "1024x1024"
        parameters: dict[str, Any] = {}
        for name, descriptor in model.active_parameters.items():
            if descriptor.get("ui_only") or "default" not in descriptor:
                continue
            if (
                isinstance(descriptor.get("modes"), list)
                and "text2img" not in descriptor["modes"]
            ):
                continue
            request_key = str(descriptor.get("request_key") or name)
            if request_key == "size":
                size = str(descriptor["default"])
            elif request_key in {"count", "n"}:
                continue
            else:
                parameters[request_key] = descriptor["default"]
        return GenerationRequest(
            mode="text2img",
            provider_id=provider.id,
            prompt="A simple landscape photograph with one tree and clear daylight.",
            negative_prompt=model.negative_prompt_default,
            model=model.id,
            size=size,
            count=1,
            parameters=parameters,
            source="webui",
            native_batch_size=model.native_batch_size,
            max_concurrent_requests=model.max_concurrent_requests,
        )
