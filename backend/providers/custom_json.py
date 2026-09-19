"""User-supplied JSON request templates for image providers."""

from __future__ import annotations

import json
import re
from typing import Any

import aiohttp

from ..media.images import image_data_url
from ..models import GeneratedImage, GenerationRequest, ImageProvider
from .errors import ProviderError
from .http import ImageResponseReader, _headers, _join_url
from .openai_images import _openai_payload


async def generate(
    session: aiohttp.ClientSession,
    responses: ImageResponseReader,
    provider: ImageProvider,
    request: GenerationRequest,
) -> tuple[GeneratedImage, ...]:
    endpoint = _join_url(
        provider.base_url,
        provider.generate_path if request.mode == "text2img" else provider.edit_path,
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
        payload = _substitute_template(template, {**payload, "count": request.count})
    async with session.post(
        endpoint,
        headers=_headers(provider, bearer=True),
        proxy=provider.proxy or None,
        json=payload,
        timeout=aiohttp.ClientTimeout(total=provider.timeout_seconds),
    ) as response:
        return await responses.read_images(response, provider)


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
