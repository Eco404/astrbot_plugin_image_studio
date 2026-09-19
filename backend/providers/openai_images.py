"""OpenAI-compatible image-generation and multipart edit requests."""

from __future__ import annotations

import io
from typing import Any

import aiohttp

from ..media.images import image_data_url
from ..models import GeneratedImage, GenerationRequest, ImageProvider
from .http import (
    ImageResponseReader,
    _form_value,
    _headers,
    _join_url,
    _safe_parameters,
)


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
        async with session.post(
            endpoint,
            headers=headers,
            data=form,
            timeout=timeout,
            proxy=provider.proxy or None,
        ) as response:
            return await responses.read_images(response, provider)

    payload = _openai_payload(provider, request)
    if request.mode == "img2img":
        payload["image"] = [
            image_data_url(item.data, item.mime_type) for item in request.references
        ]
        if len(payload["image"]) == 1:
            payload["image"] = payload["image"][0]
    async with session.post(
        endpoint,
        headers=headers,
        json=payload,
        timeout=timeout,
        proxy=provider.proxy or None,
    ) as response:
        return await responses.read_images(response, provider)


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
