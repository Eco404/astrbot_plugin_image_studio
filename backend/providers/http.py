"""Shared provider headers, request values and HTTP image-response decoding."""

from __future__ import annotations

import base64
import json
from typing import Any

import aiohttp

from ..media.images import detect_mime_type
from ..models import GeneratedImage, ImageProvider
from .errors import ProviderError


class ImageResponseReader:
    """Decode image results using the executor-owned HTTP session."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session

    async def read_images(
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
        images = await self.images_from_payload(payload, provider)
        if not images:
            raise ProviderError("上游未返回可识别的图片结果")
        return tuple(images)

    async def images_from_payload(
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
                downloaded = await self.download_image(value, provider)
                images.append(downloaded)
        return images

    async def download_image(self, url: str, provider: ImageProvider) -> GeneratedImage:
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


def _json_value(value: Any) -> bool:
    try:
        json.dumps(value)
        return True
    except (TypeError, ValueError):
        return False


def _form_value(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


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
