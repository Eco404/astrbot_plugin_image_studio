"""Offline Custom JSON request previews using the generation request builders."""

from __future__ import annotations

import base64
from dataclasses import replace
from typing import Any
from urllib.parse import unquote, urlsplit

from ..media.images import image_data_url
from ..models import GenerationRequest, ImageProvider, ReferenceImage
from ..observability.store import sanitize
from ..providers.custom_json import build_request
from ..providers.http import _parse_headers
from .service import (
    _command_count,
    _control_parameter_value,
    _provider_batch_groups,
    _provider_batch_parameters,
    _resolved_model_values,
    _size,
    _wire_parameters,
)

EXAMPLE_PROMPT = "一只坐在窗边的橘猫，柔和的自然光。"
# An embedded 1x1 PNG is sufficient to exercise the real Data URI conversion.
# Previewing never reads/stages/uploads a user image or contacts the provider.
_EXAMPLE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/"
    "l1sAAAAASUVORK5CYII="
)
_EXAMPLE_URI = image_data_url(_EXAMPLE_PNG, "image/png")
_EXAMPLE_REFERENCE = (
    "[示例参考图：image/png Data URI；实际请求使用所选图片的 Base64 数据]"
)


def sanitize_preview(value: Any, draft: dict[str, Any]) -> Any:
    """Use log redaction, additionally hiding known draft credentials anywhere."""
    secrets = [str(draft.get("api_key") or "")]
    for name, header in _parse_headers(str(draft.get("custom_headers") or "")).items():
        # Header names vary by provider; retain common non-credential values.
        if name.lower() not in {"accept", "content-type", "user-agent"}:
            secrets.append(header)
            if name.lower() == "authorization" and " " in header:
                secrets.append(header.partition(" ")[2])
    for key in ("base_url", "generate_path", "edit_path", "proxy"):
        try:
            parsed = urlsplit(str(draft.get(key) or ""))
            if parsed.username:
                secrets.extend((parsed.username, unquote(parsed.username)))
            if parsed.password:
                secrets.extend((parsed.password, unquote(parsed.password)))
        except ValueError:
            pass

    def mark_reference(item: Any) -> Any:
        if isinstance(item, str):
            return item.replace(_EXAMPLE_URI, _EXAMPLE_REFERENCE)
        if isinstance(item, dict):
            return {key: mark_reference(val) for key, val in item.items()}
        if isinstance(item, list):
            return [mark_reference(val) for val in item]
        return item

    return sanitize(mark_reference(value), secrets=tuple(secrets))


def preview_custom_request(
    draft: dict[str, Any], model_index: Any, mode: Any
) -> dict[str, Any]:
    """Build requests from an unsaved model selected by position, never by ID."""
    if draft.get("kind") != "custom_json":
        raise ValueError("当前仅支持自定义 JSON 服务商的请求预览")
    models = draft.get("models")
    if not isinstance(models, list) or not models:
        raise ValueError("请先添加模型，再预览请求")
    if (
        isinstance(model_index, bool)
        or not isinstance(model_index, int)
        or not 0 <= model_index < len(models)
    ):
        raise ValueError("请选择有效的预览模型")
    raw_model = models[model_index]
    if not isinstance(raw_model, dict) or not str(raw_model.get("id") or "").strip():
        raise ValueError("请先填写所选模型的 ID")
    if mode not in ("text2img", "img2img"):
        raise ValueError("预览模式仅支持 text2img 或 img2img")
    # Only the selected draft model is normalized. Another incomplete/duplicate
    # model must neither affect this preview nor become selected by get_model().
    provider = ImageProvider.from_mapping({**draft, "models": [raw_model]})
    if not provider.base_url:
        raise ValueError("请先填写服务商的接口地址")
    model = provider.models[0].for_mode(mode)
    if not model.supports(mode):
        raise ValueError("所选模型不支持当前预览模式")
    if mode == "img2img" and model.max_reference_images <= 0:
        raise ValueError("当前模型未开放图生图参考图输入")

    count, parameters = _command_count(
        _control_parameter_value({}, model, "count", source="webui"), {}, model
    )
    values = _resolved_model_values(parameters, model, source="webui")
    request = GenerationRequest(
        mode=mode,
        provider_id=provider.id,
        model=model.id,
        prompt=EXAMPLE_PROMPT,
        negative_prompt=(
            str(model.negative_prompt_default or "").strip()[:4000]
            if model.negative_prompt
            else ""
        ),
        size=_size(
            _control_parameter_value({}, model, "size", source="webui"), provider.kind
        ),
        count=count,
        parameters=_wire_parameters(values, model),
        references=tuple(
            ReferenceImage(
                f"preview-{index}", f"example-{index}.png", _EXAMPLE_PNG, "image/png"
            )
            for index in range(1, model.max_reference_images + 1)
        )
        if mode == "img2img"
        else (),
        native_batch_size=model.native_batch_size,
        max_concurrent_requests=model.max_concurrent_requests,
    )
    batches = []
    for size, repeat in _provider_batch_groups(count, model.native_batch_size):
        chunk = replace(
            request, count=size, parameters=_provider_batch_parameters(request)
        )
        built = build_request(provider, chunk)
        batches.append(
            {
                "repeat": repeat,
                "count": size,
                "method": "POST",
                "url": built["url"],
                "headers": built["headers"],
                "proxy": built["proxy"],
                "body": built["json"],
            }
        )
    notes = [
        "预览使用当前未保存配置及所选模型在当前模式下的默认值，未发送请求。",
        "提示词使用示例文字；实际请求将使用生图时填写的提示词。",
        "密钥、认证信息及可能包含凭据的自定义请求头已隐藏。",
    ]
    if mode == "img2img":
        notes.append(
            f"本次按模型参考图上限使用 {model.max_reference_images} 张示例图；"
            "实际请求将按所选图片的张数及内容生成 Data URI。"
        )
    request_count = sum(batch["repeat"] for batch in batches)
    if request_count > 1:
        notes.append("总张数超过模型单次出图上限时按批次提交；相同请求内容合并展示。")
    return sanitize_preview(
        {
            "mode": mode,
            "model": {"id": model.id, "name": model.name},
            "requested_count": count,
            "request_count": request_count,
            "batches": batches,
            "notes": notes,
        },
        draft,
    )
