"""Official NovelAI transport, quota and paid Vibe encoding cache lifecycle."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import time
from collections import OrderedDict
from typing import Any

import aiohttp

from ...models import GeneratedImage, GenerationRequest, ImageProvider
from ..errors import ProviderError, ProviderPartialResponseError
from ..http import _join_url, _parse_headers
from .inpaint import composite_inpaint_results
from .protocol import (
    MAX_RESPONSE_BYTES,
    parse_generation_response,
    parse_subscription,
    prepare_generation_payload,
)


class NovelAIClient:
    """Keep paid Vibe encoding caches and locks alive across provider requests."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session
        self._vibe_cache: OrderedDict[str, str] = OrderedDict()
        self._vibe_locks: dict[str, asyncio.Lock] = {}
        self._vibe_cache_bytes = 0
        self._vibe_failures: OrderedDict[str, tuple[float, str]] = OrderedDict()

    async def generate(
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

    async def fetch_quota(self, provider: ImageProvider) -> dict[str, Any]:
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
