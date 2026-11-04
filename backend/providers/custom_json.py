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

_PLACEHOLDER = re.compile(r"\{\{([^{}]*)\}\}")
_PATH = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*|\[[0-9]+\])*")
_PATH_PART = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)|\[([0-9]+)\]")


def build_payload(
    provider: ImageProvider, request: GenerationRequest
) -> dict[str, Any]:
    """Construct the actual mode-specific JSON, preserving structured values."""
    payload = _openai_payload(provider, request)
    payload["references"] = [
        image_data_url(item.data, item.mime_type) for item in request.references
    ]
    raw_template = provider.request_template_for_mode(request.mode)
    if not raw_template.strip():
        return payload
    try:
        template = json.loads(raw_template)
    except json.JSONDecodeError as exc:
        raise ProviderError(
            "自定义 Provider 的请求模板不是合法 JSON",
            request_status="not_submitted",
        ) from exc
    if not isinstance(template, dict):
        raise ProviderError(
            "自定义 Provider 的请求模板必须为 JSON 对象",
            request_status="not_submitted",
        )
    return _substitute_template(template, {**payload, "count": request.count})


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
    payload = build_payload(provider, request)
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
        matches = list(_PLACEHOLDER.finditer(template))
        remainder = _PLACEHOLDER.sub("", template)
        if (
            "{{" in remainder
            or "}}" in remainder
            or any(
                (match.start() > 0 and template[match.start() - 1] == "{")
                or (match.end() < len(template) and template[match.end()] == "}")
                for match in matches
            )
        ):
            raise ProviderError(
                "请求模板包含未正确闭合的占位符，请使用 {{字段名}} 格式",
                request_status="not_submitted",
            )
        if len(matches) == 1 and matches[0].span() == (0, len(template)):
            # Do not recurse into substituted strings/objects. Schema defaults
            # are values, not executable templates; user prompts may contain
            # literal double braces as well.
            return _resolve_placeholder(matches[0].group(1), values)

        def replace(match: re.Match[str]) -> str:
            value = _resolve_placeholder(match.group(1), values)
            return (
                value
                if isinstance(value, str)
                else json.dumps(value, ensure_ascii=False)
            )

        return _PLACEHOLDER.sub(replace, template)
    result = {}
    for key, item in template.items():
        if "{{" in str(key) or "}}" in str(key):
            raise ProviderError(
                "请求模板占位符只能用于字段值，不能用于字段名",
                request_status="not_submitted",
            )
        result[str(key)] = _substitute_template(item, values)
    return result


def _resolve_placeholder(path: str, values: dict[str, Any]) -> Any:
    """Read dictionary fields and bounded list indexes, never Python attributes."""
    if len(path) > 256 or not _PATH.fullmatch(path):
        raise ProviderError(
            "请求模板占位符格式无效；支持 {{字段名}}、{{字段名.子字段}} 和 {{references[0]}}",
            request_status="not_submitted",
        )
    current: Any = values
    for field, index in _PATH_PART.findall(path):
        if field and isinstance(current, dict) and field in current:
            current = current[field]
        elif (
            index
            and len(index) <= 5
            and isinstance(current, (list, tuple))
            and int(index) < min(len(current), 10000)
        ):
            current = current[int(index)]
        else:
            raise ProviderError(
                "请求模板占位符 {{" + path + "}} 不存在，或当前模式未提供对应值",
                request_status="not_submitted",
            )
    return current
