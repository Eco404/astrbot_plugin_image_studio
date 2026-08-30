"""Generation orchestration and reproduction planning."""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any

from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

from .config import RuntimeSettings
from .models import (
    GenerationRequest,
    GenerationResult,
    ImageModel,
    ImageProvider,
    ReferenceImage,
)
from .providers import ProviderExecutor
from .storage import GenerationStore, detect_mime_type


class ImageGenerationService:
    """Validate requests, select providers, and persist successful results."""

    def __init__(
        self,
        *,
        settings: RuntimeSettings,
        executor: ProviderExecutor,
        store: GenerationStore,
    ) -> None:
        self.settings = settings
        self.executor = executor
        self.store = store
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_generations)

    def update_settings(self, settings: RuntimeSettings) -> None:
        """Swap future-request settings after an atomic configuration save."""

        self.settings = settings
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_generations)

    async def generate(
        self,
        *,
        mode: str,
        provider_id: str,
        prompt: str,
        negative_prompt: str = "",
        model_ref: str = "",
        model: str = "",
        size: str = "",
        count: Any = 1,
        parameters: Any = None,
        references: tuple[ReferenceImage, ...] = (),
        source: str = "webui",
    ) -> GenerationResult:
        """Generate images through a validated provider request.

        Args:
            mode: ``text2img`` or ``img2img``.
            provider_id: Requested provider ID, or an empty string for default.
            prompt: Original image prompt.
            negative_prompt: Optional negative prompt.
            model_ref: Stable provider/model reference selected by the WebUI.
            model: Backward-compatible model ID override.
            size: Optional one-request size override.
            count: Requested image count.
            parameters: Provider-safe advanced request parameters.
            references: Already materialized reference images.
            source: Invocation source for gallery metadata.

        Returns:
            A normalized generation result.

        Raises:
            ValueError: When the request is invalid or no provider can serve it.
            ProviderError: When the provider fails.
        """

        settings = self.settings
        if not settings.enabled:
            raise ValueError("Image Studio 已关闭")
        normalized_mode = _mode(mode)
        provider, selected_model = self._select_model(
            settings, provider_id, model_ref, model, normalized_mode
        )
        normalized_prompt = str(prompt or "").strip()[:6000]
        if not normalized_prompt:
            raise ValueError("提示词不能为空")
        normalized_refs = references[: selected_model.max_reference_images]
        if normalized_mode == "img2img" and not normalized_refs:
            raise ValueError("图生图需要至少一张参考图")
        request = GenerationRequest(
            mode=normalized_mode,
            provider_id=provider.id,
            prompt=normalized_prompt,
            negative_prompt=(
                str(negative_prompt or "").strip()[:4000]
                if selected_model.negative_prompt
                else ""
            ),
            model=selected_model.id,
            size=_size(size or settings.default_size, provider.kind),
            count=max(1, min(4, _as_int(count, settings.default_count))),
            parameters=_parameters_for_model(parameters, selected_model),
            references=normalized_refs,
            source=source if source in {"webui", "command", "llm_tool"} else "webui",
        )
        started = time.perf_counter()
        async with self._semaphore:
            images = await self.executor.generate(provider, request)
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        generation_id = await self.store.record_success(
            provider=provider,
            request=request,
            images=images,
            elapsed_ms=elapsed_ms,
            history=settings.history,
        )
        await self.store.discard_staged_references(request.references)
        return GenerationResult(
            provider=provider,
            request=request,
            images=images,
            elapsed_ms=elapsed_ms,
            generation_id=generation_id,
        )

    async def staged_references(self, reference_ids: Any) -> tuple[ReferenceImage, ...]:
        """Resolve an API request's opaque reference IDs."""

        if isinstance(reference_ids, str):
            reference_ids = [reference_ids]
        if not isinstance(reference_ids, list):
            return ()
        return await self.store.load_staged_references(
            [str(item or "") for item in reference_ids]
        )

    async def reference_from_safe_path(self, raw_path: str) -> ReferenceImage | None:
        """Read a local Agent reference only from plugin data or AstrBot temp roots."""

        text = str(raw_path or "").strip()
        if not text:
            return None
        path = Path(text).expanduser()
        try:
            resolved = path.resolve(strict=True)
        except (OSError, ValueError):
            return None
        roots = (self.store.data_dir, Path(get_astrbot_temp_path()).resolve())
        if not any(_within(resolved, root) for root in roots):
            raise ValueError("参考图路径不在允许的 AstrBot 临时目录或插件数据目录内")
        raw = await asyncio.to_thread(resolved.read_bytes)
        if not raw or len(raw) > 20 * 1024 * 1024:
            raise ValueError("参考图为空或超过 20 MB 上限")
        return ReferenceImage(
            id="local-" + resolved.name[:32],
            filename=resolved.name,
            data=raw,
            mime_type=detect_mime_type(raw, ""),
        )

    async def reproduction_plan(self, generation_id: str) -> dict[str, Any]:
        """Return a reproducible draft and stage retained references when available."""

        detail = await self.store.generation_detail(generation_id, include_assets=False)
        if detail is None:
            raise ValueError("历史生成记录不存在")
        parameters = (
            detail.get("parameters")
            if isinstance(detail.get("parameters"), dict)
            else {}
        )
        staged = await self.store.stage_generation_references(generation_id)
        provider = self.settings.provider(str(detail.get("provider_id") or ""))
        compatible = self.settings.models_for_mode(
            str(detail.get("mode") or "text2img")
        )
        model_ref = (
            f"{detail.get('provider_id')}:{detail.get('model')}"
            if provider and detail.get("model")
            else ""
        )
        draft = {
            "mode": detail.get("mode"),
            "provider_id": detail.get("provider_id") if provider else "",
            "model_ref": model_ref if provider else "",
            "model": detail.get("model"),
            "prompt": detail.get("original_prompt"),
            "negative_prompt": parameters.get("negative_prompt", ""),
            "size": parameters.get("size", ""),
            "count": parameters.get("count", 1),
            "parameters": parameters.get("parameters", {}),
            "references": staged,
            "provider_available": provider is not None,
            "reference_available": bool(staged),
            "candidates": [
                {
                    **model.public_dict(),
                    "provider_id": provider_item.id,
                    "provider_name": provider_item.name,
                    "provider_kind": provider_item.kind,
                    "model_ref": f"{provider_item.id}:{model.id}",
                }
                for provider_item, model in compatible
            ],
        }
        if detail.get("mode") == "img2img" and not staged:
            draft["notice"] = (
                "历史参考图未保留，已填入全部可恢复参数。请上传新参考图，或在生图页明确选择当前成图作为参考图。"
            )
        elif provider is None or not any(
            item.id == detail.get("model") for item in provider.models
        ):
            draft["notice"] = (
                "历史 Provider 已不可用。请选择下方支持相同模式的 Provider 后再生成。"
            )
        else:
            draft["notice"] = "已填入历史生成参数。"
        return draft

    def _select_model(
        self,
        settings: RuntimeSettings,
        provider_id: str,
        model_ref: str,
        model_id: str,
        mode: str,
    ) -> tuple[ImageProvider, ImageModel]:
        """Resolve the selected model and its owning provider."""

        requested_ref = model_ref or model_id or settings.default_model_ref
        requested_provider = provider_id or settings.default_provider_id
        selected = settings.find_model(requested_ref, requested_provider, mode)
        if selected is None and model_ref:
            raise ValueError("所选模型不存在、已禁用或不支持当前生图模式")
        if selected is None and requested_provider:
            selected = settings.find_model(requested_ref, "", mode)
        if selected is None:
            raise ValueError("当前模式没有可用模型，请先在设置页完成服务商和模型配置")
        return selected


def _mode(value: str) -> str:
    raw = str(value or "").strip().lower()
    aliases = {
        "text": "text2img",
        "txt2img": "text2img",
        "image": "img2img",
        "edit": "img2img",
    }
    normalized = aliases.get(raw, raw)
    if normalized not in {"text2img", "img2img"}:
        raise ValueError("模式仅支持 text2img 或 img2img")
    return normalized


def _size(value: str, provider_kind: str = "") -> str:
    raw = str(value or "").strip()
    if provider_kind == "nai_direct" and raw in {
        "竖图",
        "横图",
        "方图",
        "2K竖图",
        "2K横图",
        "2K方图",
        "4K竖图",
        "4K横图",
        "4K方图",
    }:
        return raw
    text = raw.lower()
    if not text:
        return ""
    if not re.fullmatch(r"\d{2,5}x\d{2,5}|[1-4]k", text):
        raise ValueError("尺寸格式应为 1024x1024 或 1K-4K")
    return text


def _parameters(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    clean: dict[str, Any] = {}
    for key, item in value.items():
        name = str(key or "").strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", name):
            continue
        try:
            import json

            json.dumps(item)
        except (TypeError, ValueError):
            continue
        clean[name] = item
    return clean


def _parameters_for_model(value: Any, model: ImageModel) -> dict[str, Any]:
    """Map friendly schema names to provider request keys at the trust boundary."""

    raw = _parameters(value)
    mapped: dict[str, Any] = {}
    for key, item in raw.items():
        descriptor = model.parameters.get(key)
        if isinstance(descriptor, dict) and descriptor.get("ui_only"):
            continue
        request_key = (
            str(descriptor.get("request_key") or key)
            if isinstance(descriptor, dict)
            else key
        )
        mapped[request_key[:96]] = item
    return mapped


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
