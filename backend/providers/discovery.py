"""Best-effort remote model discovery and capability normalization."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

import aiohttp

from ..models import ImageProvider, positive_batch_size
from .errors import ProviderError
from .http import _headers, _join_url


async def discover_models(
    session: aiohttp.ClientSession, provider: ImageProvider
) -> list[dict[str, Any]]:
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
        async with session.get(
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
