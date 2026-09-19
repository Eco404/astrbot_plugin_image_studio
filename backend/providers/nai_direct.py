"""Third-party NAI GET image requests and account quota lookup."""

from __future__ import annotations

import re
import time
from typing import Any

import aiohttp

from ..models import GeneratedImage, GenerationRequest, ImageProvider
from .errors import ProviderError
from .http import ImageResponseReader, _headers, _join_url, _safe_parameters


async def generate(
    session: aiohttp.ClientSession,
    responses: ImageResponseReader,
    provider: ImageProvider,
    request: GenerationRequest,
) -> tuple[GeneratedImage, ...]:
    if request.mode != "text2img":
        raise ProviderError("NAI 第三方 GET 服务仅支持文生图")
    endpoint = _join_url(provider.base_url, provider.generate_path or "/generate")
    query = _nai_query(provider, request)
    try:
        async with session.get(
            endpoint,
            params=query,
            proxy=provider.proxy or None,
            headers=_headers(provider, bearer=False),
            timeout=aiohttp.ClientTimeout(total=provider.timeout_seconds),
        ) as response:
            return await responses.read_images(response, provider)
    except TimeoutError as exc:
        raise ProviderError("NAI 生图失败：请求超时，可能已消耗额度") from exc
    except aiohttp.ClientError as exc:
        raise ProviderError("NAI 生图失败：无法连接服务商") from exc


async def fetch_quota(
    session: aiohttp.ClientSession, provider: ImageProvider
) -> dict[str, Any]:
    """Read the saved NAI proxy account quota without generating an image."""

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
        async with session.post(
            f"{provider.base_url.rstrip('/')}/api/api/getUser",
            json={"toUserId": provider.api_key},
            proxy=provider.proxy or None,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=15),
            allow_redirects=False,
        ) as response:
            if response.status != 200:
                raise ProviderError(f"额度查询失败：上游返回 HTTP {response.status}")
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
    if isinstance(remaining, str) and re.fullmatch(r"[0-9]{1,30}", remaining.strip()):
        remaining = int(remaining)
    if type(remaining) is not int or remaining < 0:
        raise ProviderError("额度查询失败：上游未返回有效剩余额度")
    enabled = data.get("enabled")
    if not isinstance(enabled, bool):
        raise ProviderError("额度查询失败：上游未返回有效账户状态")
    return {"remaining": remaining, "enabled": enabled, "checked_at": time.time()}


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
