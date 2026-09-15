"""HTTP adapters for image-generation providers."""

from __future__ import annotations

import asyncio
import base64
import io
import hashlib
import json
import re
import time
from collections import OrderedDict
from typing import Any
from urllib.parse import quote

import aiohttp

from .models import (
    GeneratedImage,
    GenerationRequest,
    ImageProvider,
    positive_batch_size,
)
from .novelai import (
    MAX_RESPONSE_BYTES,
    prepare_generation_payload,
    parse_generation_response,
    parse_subscription,
)
from .novelai_inpaint import composite_inpaint_results
from .storage import detect_mime_type, image_data_url


class ProviderError(RuntimeError):
    """A user-presentable upstream provider failure."""


class ProviderPartialResponseError(ProviderError):
    """Preserve valid images when another result in one response is invalid."""

    def __init__(
        self,
        images: tuple[GeneratedImage, ...],
        failures: tuple[tuple[int, str], ...],
    ) -> None:
        self.images = images
        self.failures = failures
        details = "；".join(f"第 {index} 项：{reason}" for index, reason in failures)
        super().__init__(
            f"生图响应中有 {len(failures)} 项无效，已保留 {len(images)} 张图片。"
            f"{details}。请求可能已消耗额度，请勿自动重试。"
        )


class ProviderBatchError(ProviderError):
    """Carry all successful images through a partially failed split batch."""

    def __init__(
        self,
        requested: int,
        images: tuple[GeneratedImage, ...],
        failures: tuple[tuple[int, str], ...],
        *,
        request_sizes: tuple[int, ...] = (),
        actual_response_counts: tuple[int, ...] = (),
    ) -> None:
        self.images = images
        self.failures = failures
        sizes = request_sizes or (1,) * requested
        failed_images = sum(
            max(
                0,
                sizes[index - 1]
                - (actual_response_counts[index - 1] if actual_response_counts else 0),
            )
            for index, _ in failures
        )
        details = "；".join(
            f"第 {index} 次请求（计划 {sizes[index - 1]} 张）：{reason}"
            for index, reason in failures
        )
        failure_label = (
            "失败或部分失败"
            if actual_response_counts
            and any(actual_response_counts[index - 1] for index, _ in failures)
            else "失败"
        )
        summary = (
            f"本批目标 {requested} 张，发送 {len(sizes)} 次请求，"
            f"成功 {len(sizes) - len(failures)} 次，{failure_label} {len(failures)} 次"
            f"（涉及目标图片 {failed_images} 张）；实际返回 {len(images)} 张，已全部保留。"
        )
        super().__init__(
            summary + f"{details}。失败请求可能已消耗额度，请勿自动重试整个批次。"
        )


class ProviderExecutor:
    """Execute normalized requests through the provider selected by its kind."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session
        self._vibe_cache: OrderedDict[str, str] = OrderedDict()
        self._vibe_locks: dict[str, asyncio.Lock] = {}
        self._vibe_cache_bytes = 0
        self._vibe_failures: OrderedDict[str, tuple[float, str]] = OrderedDict()

    async def fetch_quota(self, provider: ImageProvider) -> dict[str, Any]:
        """Read the saved NAI proxy account quota without generating an image."""

        if provider.kind == "novelai_official":
            return await self._novelai_quota(provider)
        if not provider.enabled or provider.kind != "nai_direct":
            raise ProviderError("仅支持查询已启用的 NAI 服务商额度")
        if not provider.api_key.strip():
            raise ProviderError("NAI 服务商尚未配置密钥")
        headers = {
            key: value
            for key, value in _headers(provider, bearer=False).items()
            if key.lower() != "content-type"
        }
        headers["Content-Type"] = "application/json"
        try:
            async with self.session.post(
                f"{provider.base_url.rstrip('/')}/api/api/getUser",
                json={"toUserId": provider.api_key},
                proxy=provider.proxy or None,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    raise ProviderError(
                        f"额度查询失败：上游返回 HTTP {response.status}"
                    )
                payload = await response.json()
        except TimeoutError as exc:
            raise ProviderError("额度查询超时，请稍后重试") from exc
        except (aiohttp.ContentTypeError, ValueError, UnicodeDecodeError) as exc:
            raise ProviderError("额度查询失败：上游响应格式无效") from exc
        except aiohttp.ClientError as exc:
            raise ProviderError("额度查询失败：无法连接服务商") from exc
        if not isinstance(payload, dict):
            raise ProviderError("额度查询失败：上游响应格式无效")
        if payload.get("status") != "ok":
            raise ProviderError("额度查询失败：上游未确认账户，请检查密钥或账户状态")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ProviderError("额度查询失败：上游未返回有效账户信息")
        remaining = data.get("value")
        if isinstance(remaining, str) and re.fullmatch(
            r"[0-9]{1,30}", remaining.strip()
        ):
            remaining = int(remaining)
        if type(remaining) is not int or remaining < 0:
            raise ProviderError("额度查询失败：上游未返回有效剩余额度")
        enabled = data.get("enabled")
        if not isinstance(enabled, bool):
            raise ProviderError("额度查询失败：上游未返回有效账户状态")
        return {"remaining": remaining, "enabled": enabled, "checked_at": time.time()}

    async def discover_models(self, provider: ImageProvider) -> list[dict[str, Any]]:
        """Best-effort model discovery for the provider settings page."""

        if provider.kind == "nai_direct":
            raise ProviderError("NAI 第三方接口不支持获取模型列表")
        if provider.kind == "novelai_official":
            raise ProviderError(
                "NovelAI 官方接口未提供图片模型自动发现；请选择内置模型预设"
            )
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
                proxy=provider.proxy or None,
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
        if provider.kind == "novelai_official":
            return await self._novelai_official(provider, request)
        if provider.kind == "custom_json":
            return await self._custom_json(provider, request)
        if provider.kind == "comfyui":
            from .comfyui import ComfyClient, ComfyExecutionError

            try:
                return await ComfyClient(self.session).execute(provider, request)
            except ComfyExecutionError as exc:
                if exc.images:
                    raise ProviderPartialResponseError(
                        exc.images, exc.failures or ((1, str(exc)),)
                    ) from exc
                raise ProviderError(str(exc)) from exc
        raise ProviderError(f"不支持的 Provider 类型: {provider.kind}")

    async def _novelai_official(
        self, provider: ImageProvider, request: GenerationRequest
    ) -> tuple[GeneratedImage, ...]:
        if not provider.enabled:
            raise ProviderError("NovelAI 官方服务商未启用")
        if not provider.api_key.strip():
            raise ProviderError("NovelAI 官方服务商尚未配置 Persistent API Token")
        if request.mode == "img2img" and not provider.get_model(request.model).img2img:
            raise ProviderError("所选 NovelAI 模型尚未启用图生图")
        try:
            payload, effective, vibes = await asyncio.to_thread(
                prepare_generation_payload, request, request.model or provider.model
            )
        except ValueError as exc:
            raise ProviderError(str(exc)) from exc
        if vibes:
            # Validate and prepare the whole request before incurring encoding fees.
            payload["parameters"]["reference_image_multiple"] = [
                await self._encode_vibe(provider, item) for item in vibes
            ]
        path = (
            provider.edit_path if request.mode == "img2img" else provider.generate_path
        )
        endpoint = _join_url(provider.base_url, path or "/ai/generate-image")
        try:
            async with self.session.post(
                endpoint,
                json=payload,
                headers=_novelai_headers(provider),
                proxy=provider.proxy or None,
                timeout=aiohttp.ClientTimeout(total=provider.timeout_seconds),
                allow_redirects=False,
            ) as response:
                if response.status not in {200, 201}:
                    raise ProviderError(
                        await _novelai_generation_error(response, provider)
                    )
                raw = await _bounded_response_body(response, MAX_RESPONSE_BYTES)
                content_type = str(response.headers.get("Content-Type") or "")
        except TimeoutError as exc:
            raise ProviderError(
                "NovelAI 生图超时，可能已消耗额度；不会自动重试，请先检查结果"
            ) from exc
        except aiohttp.ClientError as exc:
            raise ProviderError(
                "NovelAI 生图连接中断，可能已消耗额度；不会自动重试"
            ) from exc
        try:
            images, failures = await asyncio.to_thread(
                parse_generation_response,
                raw,
                content_type,
                effective,
                expected_count=request.count,
            )
        except ValueError as exc:
            raise ProviderError(str(exc)) from exc
        if payload["action"] == "infill":
            composed = []
            failures = list(failures)
            for position, image in enumerate(images, 1):
                try:
                    result = await asyncio.to_thread(
                        composite_inpaint_results, (image,), payload
                    )
                    composed.extend(result)
                except ValueError as exc:
                    composed.append(image)
                    failures.append(
                        (position, f"未完成蒙版合成，已保留上游原始返回：{exc}")
                    )
            images = tuple(composed)
        if failures:
            raise ProviderPartialResponseError(images, tuple(failures))
        if not images:
            raise ProviderError("NovelAI 未返回可识别的图片")
        return images

    async def _encode_vibe(
        self, provider: ImageProvider, payload: dict[str, Any]
    ) -> str:
        """Cache paid encodings per account, endpoint, model, image and extract level."""
        key = hashlib.sha256(
            json.dumps(
                [provider.base_url, provider.api_key.strip(), payload],
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        lock = self._vibe_locks.setdefault(key, asyncio.Lock())
        try:
            async with lock:
                previous = self._vibe_failures.get(key)
                if previous and previous[0] > time.monotonic():
                    raise ProviderError(
                        previous[1]
                        + "；同一编码近期失败，60 秒内不重复请求，请稍后手动重试"
                    )
                if key in self._vibe_cache:
                    self._vibe_cache.move_to_end(key)
                    return self._vibe_cache[key]
                try:
                    async with self.session.post(
                        _join_url(provider.base_url, "/ai/encode-vibe"),
                        json=payload,
                        headers={
                            **_novelai_headers(provider),
                            "Accept": "application/octet-stream",
                        },
                        proxy=provider.proxy or None,
                        timeout=aiohttp.ClientTimeout(total=provider.timeout_seconds),
                        allow_redirects=False,
                    ) as response:
                        if response.status not in {200, 201}:
                            reason = await _novelai_generation_error(response, provider)
                            raise ProviderError(
                                f"Vibe 编码失败：{reason}；未发送生图请求"
                            )
                        raw = await _bounded_response_body(response, 16 * 1024 * 1024)
                        if (
                            not raw
                            or "json"
                            in response.headers.get("Content-Type", "").lower()
                            or raw.lstrip().startswith((b"{", b"<"))
                        ):
                            raise ProviderError("Vibe 编码返回格式无效；未发送生图请求")
                except (aiohttp.ClientError, TimeoutError) as exc:
                    message = "Vibe 编码请求失败，可能已产生编码费用；不会自动重试，未发送生图请求"
                    self._remember_vibe_failure(key, message)
                    raise ProviderError(message) from exc
                except ProviderError as exc:
                    self._remember_vibe_failure(key, str(exc))
                    raise
                except asyncio.CancelledError:
                    self._remember_vibe_failure(
                        key, "Vibe 编码等待已取消，官方可能仍在处理，未发送生图请求"
                    )
                    raise
                encoded = base64.b64encode(raw).decode("ascii")
                while self._vibe_cache and (
                    len(self._vibe_cache) >= 32
                    or self._vibe_cache_bytes + len(encoded) > 64 * 1024 * 1024
                ):
                    _, evicted = self._vibe_cache.popitem(last=False)
                    self._vibe_cache_bytes -= len(evicted)
                self._vibe_cache[key] = encoded
                self._vibe_failures.pop(key, None)
                self._vibe_cache_bytes += len(encoded)
                return encoded
        finally:
            # Keep a shared lock until its queued callers have also completed.
            if not lock.locked() and not getattr(lock, "_waiters", None):
                self._vibe_locks.pop(key, None)

    def _remember_vibe_failure(self, key: str, message: str) -> None:
        self._vibe_failures[key] = (time.monotonic() + 60, message)
        self._vibe_failures.move_to_end(key)
        while len(self._vibe_failures) > 32:
            self._vibe_failures.popitem(last=False)

    async def _novelai_quota(self, provider: ImageProvider) -> dict[str, Any]:
        if not provider.enabled:
            raise ProviderError("NovelAI 官方服务商未启用")
        if not provider.api_key.strip():
            raise ProviderError("NovelAI 官方服务商尚未配置 Persistent API Token")
        try:
            async with self.session.get(
                _join_url(provider.base_url, "/user/subscription"),
                proxy=provider.proxy or None,
                headers=_novelai_headers(provider),
                timeout=aiohttp.ClientTimeout(total=15),
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    raise ProviderError(
                        _novelai_http_error(response.status, operation="额度查询")
                    )
                raw = await _bounded_response_body(response, 1024 * 1024)
            payload = json.loads(raw)
            return parse_subscription(payload)
        except TimeoutError as exc:
            raise ProviderError("NovelAI 额度查询超时，请稍后重试") from exc
        except aiohttp.ClientError as exc:
            raise ProviderError("NovelAI 额度查询失败：无法连接服务商") from exc
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProviderError("NovelAI 额度查询失败：上游响应格式无效") from exc

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
            image_field = "image[]" if len(request.references) > 1 else "image"
            for reference in request.references:
                form.add_field(
                    image_field,
                    io.BytesIO(reference.data),
                    filename=reference.filename,
                    content_type=reference.mime_type,
                )
            headers.pop("Content-Type", None)
            async with self.session.post(
                endpoint,
                headers=headers,
                data=form,
                timeout=timeout,
                proxy=provider.proxy or None,
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
            endpoint,
            headers=headers,
            json=payload,
            timeout=timeout,
            proxy=provider.proxy or None,
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
        if request.count > 1 or "candidateCount" in generation_config:
            generation_config["candidateCount"] = request.count
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
            proxy=provider.proxy or None,
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
        try:
            async with self.session.get(
                endpoint,
                params=query,
                proxy=provider.proxy or None,
                headers=_headers(provider, bearer=False),
                timeout=aiohttp.ClientTimeout(total=provider.timeout_seconds),
            ) as response:
                return await self._read_response_images(response, provider)
        except TimeoutError as exc:
            raise ProviderError("NAI 生图失败：请求超时，可能已消耗额度") from exc
        except aiohttp.ClientError as exc:
            raise ProviderError("NAI 生图失败：无法连接服务商") from exc

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
            payload = _substitute_template(
                template, {**payload, "count": request.count}
            )
        async with self.session.post(
            endpoint,
            headers=_headers(provider, bearer=True),
            proxy=provider.proxy or None,
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
        for value in values:
            if not isinstance(value, str) or not value or value in seen:
                continue
            seen.add(value)
            decoded = _decode_image_value(value)
            if decoded is not None:
                images.append(GeneratedImage(decoded, detect_mime_type(decoded, "")))
                continue
            if value.startswith(("http://", "https://")):
                downloaded = await self._download_image(value, provider)
                images.append(downloaded)
        return images

    async def _download_image(
        self, url: str, provider: ImageProvider
    ) -> GeneratedImage:
        try:
            async with self.session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=60),
                proxy=provider.proxy or None,
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


async def _bounded_response_body(response: aiohttp.ClientResponse, limit: int) -> bytes:
    """Bound both advertised and streamed response size before decoding."""

    declared = str(response.headers.get("Content-Length") or "")
    if declared.isdigit() and int(declared) > limit:
        raise ProviderError("NovelAI 响应超过插件大小上限")
    content = response.content
    chunks = bytearray()
    async for chunk in content.iter_chunked(64 * 1024):
        chunks.extend(chunk)
        if len(chunks) > limit:
            raise ProviderError("NovelAI 响应超过插件大小上限")
    return bytes(chunks)


def _novelai_headers(provider: ImageProvider) -> dict[str, str]:
    headers = {
        key: value
        for key, value in _parse_headers(provider.custom_headers).items()
        if key.lower() not in {"authorization", "content-type", "accept"}
    }
    headers.update(
        {
            "Authorization": f"Bearer {provider.api_key.strip()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
    )
    return headers


async def _novelai_generation_error(
    response: aiohttp.ClientResponse, provider: ImageProvider
) -> str:
    message = _novelai_http_error(response.status, operation="生图")
    if (
        response.status not in {400, 422}
        or "json" not in str(response.headers.get("Content-Type", "")).lower()
    ):
        return message
    try:
        body = await _bounded_response_body(response, 16 * 1024)
        payload = json.loads(body)
    except (ValueError, ProviderError, aiohttp.ClientError, TimeoutError):
        return message
    if not isinstance(payload, dict):
        return message
    detail = payload.get("message", payload.get("error", ""))
    if isinstance(detail, dict):
        detail = detail.get("message", "")
    if not isinstance(detail, str):
        return message
    for secret in [
        provider.api_key.strip(),
        *_parse_headers(provider.custom_headers).values(),
    ]:
        if secret:
            detail = detail.replace(secret, "[已隐藏]")
    detail = re.sub(
        r"pst-[A-Za-z0-9._~-]+|Bearer\s+\S+", "[已隐藏]", detail, flags=re.I
    )
    detail = re.sub(r"https?://\S+|[A-Za-z0-9+/=_-]{64,}", "[已隐藏]", detail)
    detail = " ".join(detail.split())[:300]
    if re.search(r"\brecaptcha\b", detail, re.I) and re.search(
        r"\btrial\b", detail, re.I
    ):
        return (
            f"NovelAI 生图失败：HTTP {response.status}，免费试用需要官方验证码验证；"
            "当前插件尚未接入试用验证码流程，请在 NovelAI 官网使用免费试用，"
            "或使用具备生图权限和额度的账户。"
        )
    return (
        f"{message}；上游说明：{detail}" if detail and detail != "[已隐藏]" else message
    )


def _novelai_http_error(status: int, *, operation: str) -> str:
    reasons = {
        400: "请求参数无效，请检查模型、尺寸和采样参数",
        401: "Token 无效或已失效",
        403: "账户权限或订阅不允许本次操作",
        402: "Anlas 或使用额度不足",
        409: "账户已有正在执行的任务",
        422: "请求参数组合不受支持",
        429: "账户限流，请稍后手动重试",
    }
    reason = reasons.get(
        status, "上游服务异常" if status >= 500 else "上游返回非成功响应"
    )
    return f"NovelAI {operation}失败：HTTP {status}，{reason}"


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
        if key not in {"count", "n", "concurrency"}
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
    if provider.get_model(request.model).negative_prompt and (
        request.negative_prompt or request.source == "command"
    ):
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
        "count",
        "n",
        "concurrency",
        "batch_mode",
        "native_count_supported",
        "native_batch_size",
        "native_batch_size_source",
        "max_concurrent_requests",
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
    native_size = _discovered_native_batch_size(item)
    return {
        "id": model_id,
        "name": name or model_id,
        "supports_text2img": bool(explicit_text),
        "supports_img2img": bool(explicit_edit),
        "supports_negative_prompt": bool(item.get("supports_negative_prompt")),
        "max_reference_images": max(0, min(8, max_reference_images)),
        "capability_source": "remote" if known else "unknown",
        "native_batch_size": native_size or 1,
        "native_batch_size_source": "remote" if native_size else "default",
    }


def _discovered_native_batch_size(item: dict[str, Any]) -> int | None:
    """Read explicit output-count limits; IDs and token limits are not evidence."""

    for limits in (item, item.get("capabilities"), item.get("limits")):
        if not isinstance(limits, dict):
            continue
        for key in (
            "native_batch_size",
            "max_batch_size",
            "max_images_per_request",
            "max_output_images",
        ):
            parsed = positive_batch_size(limits.get(key))
            if parsed is not None:
                return parsed
    schemas = [item.get("parameters")]
    for key in ("input_schema", "inputSchema", "request_schema"):
        schema = item.get(key)
        if isinstance(schema, dict):
            schemas.append(schema.get("properties"))
    for schema in schemas:
        if not isinstance(schema, dict):
            continue
        for key in ("n", "num_images", "number_of_images"):
            descriptor = schema.get(key)
            if not isinstance(descriptor, dict):
                continue
            for bound in ("maximum", "max"):
                parsed = positive_batch_size(descriptor.get(bound))
                if parsed is not None:
                    return parsed
    return None


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
