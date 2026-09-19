"""Gemini image-generation request protocol."""

from __future__ import annotations

import base64
from typing import Any
from urllib.parse import quote

import aiohttp

from ..models import GeneratedImage, GenerationRequest, ImageProvider
from .http import ImageResponseReader, _headers, _join_url


async def generate(
    session: aiohttp.ClientSession,
    responses: ImageResponseReader,
    provider: ImageProvider,
    request: GenerationRequest,
) -> tuple[GeneratedImage, ...]:
    path = provider.generate_path or "/v1beta/models/{model}:generateContent"
    path = path.replace("{model}", quote(request.model or provider.model, safe="-_.~"))
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
    async with session.post(
        endpoint,
        headers=headers,
        json=payload,
        timeout=aiohttp.ClientTimeout(total=provider.timeout_seconds),
        proxy=provider.proxy or None,
    ) as response:
        return await responses.read_images(response, provider)
