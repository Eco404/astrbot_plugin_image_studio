"""HTTP adapters for image-generation providers."""

from __future__ import annotations

import base64
import io
import json
import re
from typing import Any
from urllib.parse import quote

import aiohttp

from .models import GeneratedImage, GenerationRequest, ImageProvider
from .storage import detect_mime_type, image_data_url


class ProviderError(RuntimeError):
    """A user-presentable upstream provider failure."""


class ProviderExecutor:
    """Execute normalized requests through the provider selected by its kind."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session

    async def discover_models(self, provider: ImageProvider) -> list[dict[str, Any]]:
        """Best-effort model discovery for the provider settings page."""

        if provider.kind == "nai_direct":
            return [
                {
                    "id": model_id,
                    "name": model_name,
                    "supports_text2img": True,
                    "supports_img2img": False,
                    "supports_negative_prompt": True,
                    "max_reference_images": 0,
                    "capability_source": "builtin",
                }
                for model_id, model_name in (
                    ("nai-diffusion-4-5-full", "NAI V4.5 完整版"),
                    ("nai-diffusion-5-full", "NAI V5 完整版"),
                )
            ]
        endpoint = _join_url(
            provider.base_url,
            provider.models_path
            or ("/v1beta/models" if provider.kind == "gemini" else "/models"),
        )
        headers = _headers(provider, bearer=provider.kind != "gemini")
        if provider.kind == "gemini" and provider.api_key:
            separator = "&" if "?" in endpoint else "?"
            endpoint += f"{separator}key={quote(provider.api_key, safe='')}"
        try:
            async with self.session.get(
                endpoint,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=provider.timeout_seconds),
            ) as response:
                raw = await response.read()
                if response.status >= 400:
                    detail = raw.decode("utf-8", errors="replace")[:300]
                    raise ProviderError(
                        f"获取模型列表失败：HTTP {response.status} {detail}".strip()
                    )
        except aiohttp.ClientError as exc:
            raise ProviderError("获取模型列表失败：无法连接服务商") from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderError("获取模型列表失败：响应不是合法 JSON") from exc
        values = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(values, list) and isinstance(payload, dict):
            values = payload.get("models")
        if not isinstance(values, list):
            raise ProviderError("获取模型列表失败：未找到 models 或 data 列表")
        models = [
            _discovered_model(item, provider.kind)
            for item in values
            if isinstance(item, dict)
        ]
        return [item for item in models if item["id"]]

    async def generate(
        self,
        provider: ImageProvider,
        request: GenerationRequest,
    ) -> tuple[GeneratedImage, ...]:
        """Run one provider request and normalize its returned images."""

        if provider.kind == "openai_images":
            return await self._openai_images(provider, request)
        if provider.kind == "gemini":
            return await self._gemini(provider, request)
        if provider.kind == "nai_direct":
            return await self._nai_direct(provider, request)
        if provider.kind == "custom_json":
            return await self._custom_json(provider, request)
        raise ProviderError(f"不支持的 Provider 类型: {provider.kind}")

    async def _openai_images(
        self,
        provider: ImageProvider,
        request: GenerationRequest,
    ) -> tuple[GeneratedImage, ...]:
        endpoint = _join_url(
            provider.base_url,
            provider.generate_path
            if request.mode == "text2img"
            else provider.edit_path,
        )
        headers = _headers(provider, bearer=True)
        timeout = aiohttp.ClientTimeout(total=provider.timeout_seconds)
        model = provider.get_model(request.model)
        if request.mode == "img2img" and provider.edit_request_format == "multipart":
            form = aiohttp.FormData()
            form.add_field("model", request.model or provider.model)
            form.add_field("prompt", request.prompt)
            if request.size:
                form.add_field("size", request.size)
            form.add_field("n", str(request.count))
            if model.negative_prompt and request.negative_prompt:
                form.add_field("negative_prompt", request.negative_prompt)
            for key, value in _safe_parameters(request.parameters).items():
                form.add_field(key, _form_value(value))
            for reference in request.references:
                form.add_field(
                    "image",
                    io.BytesIO(reference.data),
                    filename=reference.filename,
                    content_type=reference.mime_type,
                )
            headers.pop("Content-Type", None)
            async with self.session.post(
                endpoint, headers=headers, data=form, timeout=timeout
            ) as response:
                return await self._read_response_images(response, provider)

        payload = _openai_payload(provider, request)
        if request.mode == "img2img":
            payload["image"] = [
                image_data_url(item.data, item.mime_type) for item in request.references
            ]
            if len(payload["image"]) == 1:
                payload["image"] = payload["image"][0]
        async with self.session.post(
            endpoint, headers=headers, json=payload, timeout=timeout
        ) as response:
            return await self._read_response_images(response, provider)

    async def _gemini(
        self,
        provider: ImageProvider,
        request: GenerationRequest,
    ) -> tuple[GeneratedImage, ...]:
        path = provider.generate_path or "/v1beta/models/{model}:generateContent"
        path = path.replace(
            "{model}", quote(request.model or provider.model, safe="-_.~")
        )
        endpoint = _join_url(provider.base_url, path)
        headers = _headers(provider, bearer=False)
        if provider.api_key:
            if "?" in endpoint:
                endpoint += f"&key={quote(provider.api_key, safe='')}"
            else:
                endpoint += f"?key={quote(provider.api_key, safe='')}"
        parts: list[dict[str, Any]] = [{"text": request.prompt}]
        for reference in request.references:
            parts.append(
                {
                    "inlineData": {
                        "mimeType": reference.mime_type,
                        "data": base64.b64encode(reference.data).decode("ascii"),
                    }
                }
            )
        generation_config: dict[str, Any] = {"responseModalities": ["TEXT", "IMAGE"]}
        raw_generation_config = request.parameters.get(
            "generationConfig"
        ) or request.parameters.get("generation_config")
        if isinstance(raw_generation_config, dict):
            generation_config.update(raw_generation_config)
        image_config: dict[str, Any] = {}
        aspect_ratio = request.parameters.get(
            "aspectRatio", request.parameters.get("aspect_ratio")
        )
        image_size = request.parameters.get(
            "imageSize", request.parameters.get("image_size")
        )
        if aspect_ratio:
            image_config["aspectRatio"] = str(aspect_ratio)
        if image_size:
            image_config["imageSize"] = str(image_size)
        if image_config:
            generation_config["imageConfig"] = image_config
        payload: dict[str, Any] = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": generation_config,
        }
        if request.size and not image_config:
            payload["contents"][0]["parts"][0]["text"] += (
                f"\n\nTarget image size: {request.size}."
            )
        async with self.session.post(
            endpoint,
            headers=headers,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=provider.timeout_seconds),
        ) as response:
            return await self._read_response_images(response, provider)

    async def _nai_direct(
        self,
        provider: ImageProvider,
        request: GenerationRequest,
    ) -> tuple[GeneratedImage, ...]:
        if request.mode != "text2img":
            raise ProviderError("NAI 第三方 GET 服务仅支持文生图")
        endpoint = _join_url(provider.base_url, provider.generate_path or "/generate")
        query = _nai_query(provider, request)
        async with self.session.get(
            endpoint,
            params=query,
            headers=_headers(provider, bearer=False),
            timeout=aiohttp.ClientTimeout(total=provider.timeout_seconds),
        ) as response:
            return await self._read_response_images(response, provider)

    async def _custom_json(
        self,
        provider: ImageProvider,
        request: GenerationRequest,
    ) -> tuple[GeneratedImage, ...]:
        endpoint = _join_url(
            provider.base_url,
            provider.generate_path
            if request.mode == "text2img"
            else provider.edit_path,
        )
        payload = _openai_payload(provider, request)
        payload["references"] = [
            image_data_url(item.data, item.mime_type) for item in request.references
        ]
        if provider.request_template.strip():
            try:
                template = json.loads(provider.request_template)
            except json.JSONDecodeError as exc:
                raise ProviderError("自定义 Provider 的请求模板不是合法 JSON") from exc
            payload = _substitute_template(template, payload)
        async with self.session.post(
            endpoint,
            headers=_headers(provider, bearer=True),
            json=payload,
            timeout=aiohttp.ClientTimeout(total=provider.timeout_seconds),
        ) as response:
            return await self._read_response_images(response, provider)

    async def _read_response_images(
        self,
        response: aiohttp.ClientResponse,
        provider: ImageProvider,
    ) -> tuple[GeneratedImage, ...]:
        content_type = str(response.headers.get("Content-Type") or "").lower()
        raw = await response.read()
        if response.status >= 400:
            detail = raw.decode("utf-8", errors="replace")[:500]
            raise ProviderError(
                f"上游返回 HTTP {response.status}: {detail or '无错误详情'}"
            )
        if content_type.startswith("image/"):
            if not raw:
                raise ProviderError("上游返回了空图片")
            return (
                GeneratedImage(
                    raw, detect_mime_type(raw, content_type.split(";", 1)[0])
                ),
            )
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderError("上游返回的既不是图片也不是 JSON") from exc
        images = await self._images_from_payload(payload, provider)
        if not images:
            raise ProviderError("上游未返回可识别的图片结果")
        return tuple(images)

    async def _images_from_payload(
        self, payload: Any, provider: ImageProvider
    ) -> list[GeneratedImage]:
        values = (
            _values_from_path(payload, provider.response_image_path)
            if provider.response_image_path
            else []
        )
        if not values:
            values = _collect_image_values(payload)
        images: list[GeneratedImage] = []
        seen: set[str] = set()
        for value in values[:8]:
            if not isinstance(value, str) or not value or value in seen:
                continue
            seen.add(value)
            decoded = _decode_image_value(value)
            if decoded is not None:
                images.append(GeneratedImage(decoded, detect_mime_type(decoded, "")))
                continue
            if value.startswith(("http://", "https://")):
                downloaded = await self._download_image(value)
                images.append(downloaded)
        return images

    async def _download_image(self, url: str) -> GeneratedImage:
        try:
            async with self.session.get(
                url, timeout=aiohttp.ClientTimeout(total=60)
            ) as response:
                if response.status >= 400:
                    raise ProviderError(f"结果图下载失败: HTTP {response.status}")
                declared_size = int(response.headers.get("Content-Length") or 0)
                if declared_size > 30 * 1024 * 1024:
                    raise ProviderError("结果图超过 30 MB 上限")
                raw = await response.read()
                if not raw or len(raw) > 30 * 1024 * 1024:
                    raise ProviderError("结果图为空或超过 30 MB 上限")
                hint = str(response.headers.get("Content-Type") or "").split(";", 1)[0]
                return GeneratedImage(raw, detect_mime_type(raw, hint))
        except aiohttp.ClientError as exc:
            raise ProviderError("无法下载上游结果图") from exc


def _openai_payload(
    provider: ImageProvider, request: GenerationRequest
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": request.model or provider.model,
        "prompt": request.prompt,
        "n": request.count,
    }
    if request.size:
        payload["size"] = request.size
    if provider.get_model(request.model).negative_prompt and request.negative_prompt:
        payload["negative_prompt"] = request.negative_prompt
    payload.update(_safe_parameters(request.parameters))
    return payload


def _nai_query(provider: ImageProvider, request: GenerationRequest) -> dict[str, str]:
    """Build the third-party nai.sta1n.cn GET query used by nai_image."""

    query = {
        str(key): str(value)
        for key, value in _safe_parameters(request.parameters).items()
    }
    query.update(
        {
            "tag": request.prompt,
            "token": provider.api_key,
            "model": request.model or provider.model,
            "size": request.size or "竖图",
            "nocache": "1",
        }
    )
    if provider.get_model(request.model).negative_prompt and request.negative_prompt:
        query["negative"] = request.negative_prompt
    return query


def _headers(provider: ImageProvider, *, bearer: bool) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    headers.update(_parse_headers(provider.custom_headers))
    if (
        bearer
        and provider.api_key
        and not any(key.lower() == "authorization" for key in headers)
    ):
        headers["Authorization"] = f"Bearer {provider.api_key}"
    return headers


def _parse_headers(raw: str) -> dict[str, str]:
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return {
                str(key): str(value)
                for key, value in parsed.items()
                if str(key).strip()
            }
    except json.JSONDecodeError:
        pass
    headers: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip():
            headers[key.strip()] = value.strip()
    return headers


def _join_url(base_url: str, path: str) -> str:
    if str(path).startswith(("http://", "https://")):
        return str(path)
    return f"{base_url.rstrip('/')}/{str(path).lstrip('/')}"


def _safe_parameters(value: dict[str, Any]) -> dict[str, Any]:
    blocked = {
        "api_key",
        "key",
        "token",
        "authorization",
        "headers",
        "base_url",
        "url",
        "model",
        "prompt",
        "image",
    }
    return {
        str(key): item
        for key, item in (value or {}).items()
        if str(key).lower() not in blocked and _json_value(item)
    }


def _discovered_model(item: dict[str, Any], kind: str) -> dict[str, Any]:
    """Normalize a remote model entry without pretending unknown capabilities are known."""

    model_id = str(item.get("id") or item.get("name") or "").strip()
    if model_id.startswith("models/"):
        model_id = model_id.removeprefix("models/")
    name = str(item.get("displayName") or item.get("name") or model_id).strip()
    methods = item.get("supportedGenerationMethods")
    explicit_text = item.get("supports_text2img")
    explicit_edit = item.get("supports_img2img")
    known = isinstance(explicit_text, bool) or isinstance(explicit_edit, bool)
    if kind == "gemini" and isinstance(methods, list):
        method_names = {str(value) for value in methods}
        if "generateContent" in method_names:
            known = True
            explicit_text = bool(
                any(token in model_id.lower() for token in ("image", "imagen"))
            )
    if not known:
        image_named = any(
            token in model_id.lower() for token in ("image", "imagen", "dall-e")
        )
        explicit_text = image_named
        explicit_edit = False
    try:
        max_reference_images = int(item.get("max_reference_images") or 0)
    except (TypeError, ValueError):
        max_reference_images = 0
    return {
        "id": model_id,
        "name": name or model_id,
        "supports_text2img": bool(explicit_text),
        "supports_img2img": bool(explicit_edit),
        "supports_negative_prompt": bool(item.get("supports_negative_prompt")),
        "max_reference_images": max(0, min(8, max_reference_images)),
        "capability_source": "remote" if known else "unknown",
    }


def _json_value(value: Any) -> bool:
    try:
        json.dumps(value)
        return True
    except (TypeError, ValueError):
        return False


def _form_value(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _substitute_template(template: Any, values: dict[str, Any]) -> Any:
    if isinstance(template, list):
        return [_substitute_template(item, values) for item in template]
    if not isinstance(template, dict):
        if not isinstance(template, str):
            return template
        exact = re.fullmatch(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}", template)
        if exact:
            return values.get(exact.group(1), "")
        return re.sub(
            r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}",
            lambda match: str(values.get(match.group(1), "")),
            template,
        )
    return {
        str(key): _substitute_template(item, values) for key, item in template.items()
    }


def _values_from_path(payload: Any, raw_path: str) -> list[str]:
    current: list[Any] = [payload]
    for segment in raw_path.strip().strip(".").replace("$.", "").split("."):
        if not segment:
            continue
        next_values: list[Any] = []
        for item in current:
            if segment == "*" and isinstance(item, list):
                next_values.extend(item)
            elif isinstance(item, dict) and segment in item:
                next_values.append(item[segment])
            elif (
                isinstance(item, list)
                and segment.isdigit()
                and int(segment) < len(item)
            ):
                next_values.append(item[int(segment)])
        current = next_values
    return [item for item in current if isinstance(item, str)]


def _collect_image_values(payload: Any) -> list[str]:
    values: list[str] = []

    def visit(value: Any, depth: int = 0) -> None:
        if depth > 12:
            return
        if isinstance(value, dict):
            inline = value.get("inlineData") or value.get("inline_data")
            if isinstance(inline, dict) and isinstance(inline.get("data"), str):
                values.append(
                    f"data:{inline.get('mimeType') or inline.get('mime_type') or 'image/png'};base64,{inline['data']}"
                )
            for key, item in value.items():
                if isinstance(item, str) and key.lower() in {
                    "b64_json",
                    "image_base64",
                    "base64",
                    "url",
                    "image_url",
                    "output_url",
                    "image",
                }:
                    values.append(item)
                elif isinstance(item, (dict, list)):
                    visit(item, depth + 1)
        elif isinstance(value, list):
            for item in value:
                visit(item, depth + 1)

    visit(payload)
    return values


def _decode_image_value(value: str) -> bytes | None:
    raw = value.strip()
    if raw.startswith("data:image/") and "," in raw:
        raw = raw.split(",", 1)[1]
    if raw.startswith(("http://", "https://")):
        return None
    try:
        decoded = base64.b64decode(raw, validate=True)
    except Exception:
        return None
    return decoded if decoded else None
