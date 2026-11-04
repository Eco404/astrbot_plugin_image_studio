"""Agnes AI image requests and the documented Image 2.5 Flash preset.

Reference: https://www.agnes-ai.cn/zh-Hans/docs/agnes-image-25-flash.md
Agnes uses one generation endpoint for both modes, but different Base64
response switches. Reference images always belong to ``extra_body.image``.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import aiohttp

from ..media.images import image_data_url
from .errors import ProviderError

if TYPE_CHECKING:
    from ..models import GeneratedImage, GenerationRequest, ImageProvider
    from .http import ImageResponseReader

AGNES_MODEL_ID = "agnes-image-2.5-flash"
AGNES_BASE_URL = "https://api.agnes-ai.cn/v1"
AGNES_IMAGE_PATH = "/images/generations"
AGNES_SIZES = ("1K", "2K", "3K", "4K")
AGNES_RATIOS = ("1:1", "3:4", "4:3", "16:9", "9:16", "2:3", "3:2", "21:9")


def agnes_parameter_schema() -> dict[str, dict[str, Any]]:
    """Return fresh parameter descriptors shared by configuration and tools."""
    from ..models import BATCH_PARAMETERS

    return {
        "size": {
            "type": "select",
            "label": "尺寸档位",
            "default": "1K",
            "choices": list(AGNES_SIZES),
            "request_key": "size",
            "description": "与宽高比共同确定输出尺寸。",
        },
        "ratio": {
            "type": "select",
            "label": "宽高比",
            "default": "1:1",
            "choices": list(AGNES_RATIOS),
            "request_key": "ratio",
        },
        "count": dict(BATCH_PARAMETERS["count"]),
        "response_format": {
            "type": "select",
            "label": "返回格式",
            "default": "url",
            "choices": ["url", "b64_json"],
            "request_key": "response_format",
            "description": "URL 由插件下载结果图；Base64 直接从响应中读取图片。",
        },
    }


def agnes_model_presets() -> list[dict[str, Any]]:
    """Return built-in capabilities without claiming a remote model query."""
    from ..models import ImageProvider

    return [
        ImageProvider.from_mapping(
            {
                "kind": "agnes",
                "models": [
                    {
                        "id": AGNES_MODEL_ID,
                        "name": "Agnes Image 2.5 Flash",
                        "supports_text2img": True,
                        "supports_img2img": True,
                        "supports_negative_prompt": False,
                        # This is Image Studio's reference limit. Agnes does not
                        # publish a maximum in its current model documentation.
                        "max_reference_images": 8,
                        "parameters": agnes_parameter_schema(),
                        "capability_source": "builtin",
                        "native_batch_size": 1,
                        "native_batch_size_source": "fixed",
                    }
                ],
            }
        )
        .models[0]
        .public_dict()
    ]


def build_payload(
    provider: ImageProvider, request: GenerationRequest
) -> dict[str, Any]:
    """Build the actual JSON body, keeping image transport out of caller schema."""
    from .http import _safe_parameters

    def invalid(message: str) -> ProviderError:
        return ProviderError(message, request_status="not_submitted")

    if request.mode not in {"text2img", "img2img"}:
        raise invalid("Agnes AI 不支持此生成模式")
    if request.count != 1:
        raise invalid("Agnes AI 每次请求生成一张图片，请通过任务队列拆分请求")
    if request.mode == "img2img" and not request.references:
        raise invalid("Agnes AI 图生图需要至少一张参考图片")

    parameters = _safe_parameters(request.parameters)
    raw_extra = parameters.pop("extra_body", {})
    if not isinstance(raw_extra, dict):
        raise invalid("Agnes AI 的 extra_body 必须为对象")
    extra = dict(raw_extra)
    response_format = str(
        parameters.pop("response_format", extra.get("response_format", "url"))
    ).strip()
    if response_format not in {"url", "b64_json"}:
        raise invalid("Agnes AI 返回格式必须为 url 或 b64_json")

    size = str(request.size or parameters.pop("size", "") or "1K").strip()
    if re.fullmatch(r"[1-4][kK]", size):
        size = size.upper()
    elif not re.fullmatch(r"[1-9][0-9]*[xX][1-9][0-9]*", size):
        raise invalid("Agnes AI 尺寸应为 1K、2K、3K、4K 或宽x高")
    ratio = str(parameters.pop("ratio", "1:1")).strip()
    if ratio not in AGNES_RATIOS:
        raise invalid("Agnes AI 不支持此宽高比")

    # These fields are transport controls, not user-provided image data. Remove
    # stale values even when a custom schema supplies an extra_body descriptor.
    parameters.pop("references", None)
    parameters.pop("return_base64", None)
    parameters.pop("negative_prompt", None)
    extra.pop("image", None)
    extra.pop("response_format", None)
    extra.pop("return_base64", None)
    payload = {
        **parameters,
        "model": request.model or provider.model or AGNES_MODEL_ID,
        "prompt": request.prompt,
        "size": size,
        "ratio": ratio,
    }
    if request.mode == "img2img":
        extra["image"] = [
            image_data_url(reference.data, reference.mime_type)
            for reference in request.references
        ]
        extra["response_format"] = response_format
    elif response_format == "b64_json":
        payload["return_base64"] = True
    else:
        extra["response_format"] = "url"
    if extra:
        payload["extra_body"] = extra
    return payload


async def generate(
    session: aiohttp.ClientSession,
    responses: ImageResponseReader,
    provider: ImageProvider,
    request: GenerationRequest,
) -> tuple[GeneratedImage, ...]:
    from .http import _headers, _join_url

    payload = build_payload(provider, request)
    # Both operations use generations, never OpenAI's images/edits endpoint.
    endpoint = _join_url(
        provider.base_url or AGNES_BASE_URL,
        provider.generate_path or AGNES_IMAGE_PATH,
    )
    async with session.post(
        endpoint,
        headers=_headers(provider, bearer=True),
        json=payload,
        proxy=provider.proxy or None,
        timeout=aiohttp.ClientTimeout(total=provider.timeout_seconds),
    ) as response:
        return await responses.read_images(response, provider)
