"""Generation orchestration and reproduction planning."""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
from astrbot.core.utils.media_utils import MediaResolver, file_uri_to_path, is_file_uri

from .config import RuntimeSettings
from .models import (
    GeneratedImage,
    GenerationRequest,
    GenerationResult,
    ImageModel,
    ImageProvider,
    InvocationSource,
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
        self._semaphores = self._build_semaphores(settings)

    def update_settings(self, settings: RuntimeSettings) -> None:
        """Swap future-request settings after an atomic configuration save."""

        self.settings = settings
        self._semaphores = self._build_semaphores(settings)

    @staticmethod
    def _build_semaphores(
        settings: RuntimeSettings,
    ) -> dict[str, asyncio.Semaphore]:
        return {
            provider.id: asyncio.Semaphore(provider.max_concurrent_generations)
            for provider in settings.providers
            if provider.id
        }

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
        count: Any = 0,
        parameters: Any = None,
        references: tuple[ReferenceImage, ...] = (),
        source: str = "webui",
        invocation_source: InvocationSource | None = None,
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
        normalized_mode = _mode(mode)
        mode_default_ref = settings.default_model_ref(normalized_mode, source)
        if (
            source == "llm_tool"
            and not str(model_ref or model or "").strip()
            and not mode_default_ref
        ):
            raise ValueError(
                "当前模式未设置默认 LLM 生图模型，请先查询模型能力并传入 model_ref"
            )
        provider, selected_model = self._select_model(
            settings,
            provider_id,
            model_ref,
            model,
            normalized_mode,
            mode_default_ref,
        )
        selection_source = (
            "explicit"
            if str(model_ref or model or "").strip()
            else "mode_default"
            if mode_default_ref
            else "fallback"
        )
        if source == "llm_tool" and not selected_model.llm_enabled:
            raise ValueError("所选模型未向 LLM 工具开放")
        normalized_prompt = str(prompt or "").strip()[:6000]
        if not normalized_prompt:
            raise ValueError("提示词不能为空")
        reference_limit = 0
        if normalized_mode == "img2img":
            reference_limit = (
                selected_model.llm_max_reference_images
                if source == "llm_tool"
                else selected_model.max_reference_images
            )
            if reference_limit <= 0:
                if source == "llm_tool":
                    raise ValueError("当前模型未向 LLM 工具开放可用的参考图数量")
                raise ValueError("当前模型的参考图能力上限为 0，不能用于图生图")
            if not references:
                raise ValueError(
                    "未读取到可用参考图；请使用当前消息或引用消息中的图片，"
                    "或传入当前 Agent 工作区内的有效图片路径"
                )
        normalized_refs = references[:reference_limit]
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
            size=_size(
                size
                or _control_parameter_value(
                    parameters, selected_model, "size", source=source
                ),
                provider.kind,
            ),
            count=max(
                1,
                min(
                    4,
                    _as_int(
                        count
                        if count not in (None, "", 0)
                        else _control_parameter_value(
                            parameters, selected_model, "count", source=source
                        ),
                        1,
                    ),
                ),
            ),
            parameters=_parameters_for_model(parameters, selected_model, source=source),
            references=normalized_refs,
            source=source if source in {"webui", "command", "llm_tool"} else "webui",
            selection_source=selection_source,
            invocation_source=invocation_source or InvocationSource(),
        )
        started = time.perf_counter()
        images = await self.run_provider_request(provider, request)
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

    async def run_provider_request(
        self, provider: ImageProvider, request: GenerationRequest
    ) -> tuple[GeneratedImage, ...]:
        """Execute any generation-like request under its provider concurrency limit."""

        semaphore = self._semaphores.setdefault(
            provider.id, asyncio.Semaphore(provider.max_concurrent_generations)
        )
        async with semaphore:
            return await self.executor.generate(provider, request)

    async def staged_references(self, reference_ids: Any) -> tuple[ReferenceImage, ...]:
        """Resolve an API request's opaque reference IDs."""

        if isinstance(reference_ids, str):
            reference_ids = [reference_ids]
        if not isinstance(reference_ids, list):
            return ()
        return await self.store.load_staged_references(
            [str(item or "") for item in reference_ids]
        )

    async def reference_from_safe_path(
        self,
        raw_path: str,
        *,
        workspace_root: Path | None = None,
    ) -> ReferenceImage | None:
        """Read a local reference from an explicitly allowed AstrBot-owned root."""

        text = str(raw_path or "").strip()
        if not text:
            return None
        if is_file_uri(text):
            text = file_uri_to_path(text)
        path = Path(text).expanduser()
        roots = [
            self.store.data_dir.resolve(strict=False),
            Path(get_astrbot_temp_path()).resolve(strict=False),
        ]
        if workspace_root is not None:
            roots.insert(0, Path(workspace_root).resolve(strict=False))
        candidates = [path] if path.is_absolute() else [root / path for root in roots]
        escaped_existing_path = False
        resolved: Path | None = None
        for candidate in candidates:
            try:
                current = candidate.resolve(strict=True)
            except (OSError, ValueError):
                continue
            if not any(_within(current, root) for root in roots):
                escaped_existing_path = True
                continue
            if current.is_file():
                resolved = current
                break
        if resolved is None:
            if escaped_existing_path:
                raise ValueError(
                    "参考图路径不在当前 Agent 工作区、AstrBot 临时目录或插件数据目录内"
                )
            return None
        raw = await asyncio.to_thread(resolved.read_bytes)
        if not raw or len(raw) > 20 * 1024 * 1024:
            raise ValueError("参考图为空或超过 20 MB 上限")
        mime_type = detect_mime_type(raw, "")
        return ReferenceImage(
            id="local-" + hashlib.sha256(raw).hexdigest()[:24],
            filename=resolved.name,
            data=raw,
            mime_type=mime_type,
        )

    async def reference_from_media_ref(
        self,
        raw_ref: str,
        *,
        workspace_root: Path | None = None,
    ) -> ReferenceImage | None:
        """Materialize an event or Agent image reference into bounded image bytes."""

        text = str(raw_ref or "").strip()
        if not text:
            return None
        if not text.lower().startswith(
            ("http://", "https://", "data:image/", "base64://")
        ):
            return await self.reference_from_safe_path(
                text,
                workspace_root=workspace_root,
            )
        try:
            resolved = await MediaResolver(text, media_type="image").to_base64_data(
                strict=True,
                default_mime_type=None,
            )
        except (OSError, ValueError) as exc:
            raise ValueError("参考图无法读取或不是有效图片") from exc
        if resolved is None:
            return None
        raw = resolved.to_bytes()
        if not raw or len(raw) > 20 * 1024 * 1024:
            raise ValueError("参考图为空或超过 20 MB 上限")
        mime_type = detect_mime_type(raw, resolved.mime_type)
        return ReferenceImage(
            id="media-" + hashlib.sha256(raw).hexdigest()[:24],
            filename=_reference_filename(text, mime_type),
            data=raw,
            mime_type=mime_type,
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
        mode_default_ref: str,
    ) -> tuple[ImageProvider, ImageModel]:
        """Resolve the selected model and its owning provider."""

        explicit_ref = str(model_ref or model_id or "").strip()
        requested_ref = explicit_ref or mode_default_ref
        requested_provider = provider_id
        selected = settings.find_model(requested_ref, requested_provider, mode)
        if selected is None and explicit_ref:
            raise ValueError("所选模型不存在、已禁用或不支持当前生图模式")
        if selected is None and requested_provider and not explicit_ref:
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


def _parameters_for_model(
    value: Any, model: ImageModel, *, source: str = "webui"
) -> dict[str, Any]:
    """Map friendly schema names to provider request keys at the trust boundary."""

    raw = _parameters(value)
    values: dict[str, Any] = {}
    for name, descriptor in model.parameters.items():
        if "default" not in descriptor:
            continue
        values[name] = descriptor["default"]
    tool_parameters = model.tool.get("parameters")
    if source == "llm_tool" and isinstance(tool_parameters, dict):
        for name, descriptor in tool_parameters.items():
            if (
                name in model.parameters
                and isinstance(descriptor, dict)
                and "default_override" in descriptor
            ):
                values[name] = descriptor["default_override"]
            elif (
                name in model.parameters
                and isinstance(descriptor, dict)
                and "default" in descriptor
            ):
                values[name] = descriptor["default"]
    _expand_parameter_presets(values, model)
    allowed = set(model.parameters)
    if source == "llm_tool" and isinstance(tool_parameters, dict) and tool_parameters:
        allowed = {
            name
            for name, descriptor in tool_parameters.items()
            if not isinstance(descriptor, dict) or descriptor.get("exposed", True)
        }
    mapped: dict[str, Any] = {}
    for key, item in values.items():
        descriptor = model.parameters.get(key)
        if not isinstance(descriptor, dict) or descriptor.get("ui_only"):
            continue
        if key in {"size", "count", "n"}:
            continue
        mapped[str(descriptor.get("request_key") or key)[:96]] = item
    for key, item in raw.items():
        descriptor = model.parameters.get(key)
        if source == "llm_tool" and key not in allowed:
            raise ValueError(f"参数 {key} 未向 LLM 工具开放")
        if isinstance(descriptor, dict) and descriptor.get("ui_only"):
            target = str(descriptor.get("target") or "")
            choice = next(
                (
                    choice
                    for choice in descriptor.get("choices", [])
                    if isinstance(choice, dict)
                    and str(choice.get("value")) == str(item)
                ),
                None,
            )
            if (
                target
                and isinstance(choice, dict)
                and isinstance(choice.get("fill"), str)
            ):
                target_descriptor = model.parameters.get(target, {})
                mapped[str(target_descriptor.get("request_key") or target)[:96]] = (
                    choice["fill"]
                )
            continue
        request_key = (
            str(descriptor.get("request_key") or key)
            if isinstance(descriptor, dict)
            else key
        )
        _validate_parameter_value(key, item, descriptor)
        if request_key not in {"size", "count", "n"}:
            mapped[request_key[:96]] = item
    return mapped


def _expand_parameter_presets(values: dict[str, Any], model: ImageModel) -> None:
    for name, descriptor in model.parameters.items():
        if str(descriptor.get("type") or "").lower() != "preset":
            continue
        target = str(descriptor.get("target") or "")
        choice = next(
            (
                choice
                for choice in descriptor.get("choices", [])
                if isinstance(choice, dict)
                and str(choice.get("value")) == str(values.get(name))
            ),
            None,
        )
        if target and isinstance(choice, dict) and isinstance(choice.get("fill"), str):
            values[target] = choice["fill"]


def _control_parameter_value(
    value: Any, model: ImageModel, name: str, *, source: str
) -> Any:
    raw = _parameters(value)
    if name in raw:
        return raw[name]
    descriptor = model.parameters.get(name)
    if not isinstance(descriptor, dict):
        return ""
    tool_parameters = model.tool.get("parameters")
    if source == "llm_tool" and isinstance(tool_parameters, dict):
        policy = tool_parameters.get(name)
        if isinstance(policy, dict) and "default_override" in policy:
            return policy["default_override"]
    return descriptor.get("default", "")


def _validate_parameter_value(
    name: str, value: Any, descriptor: dict[str, Any] | None
) -> None:
    if not isinstance(descriptor, dict):
        return
    choices = descriptor.get("choices")
    if isinstance(choices, list):
        values = {
            str(choice.get("value")) if isinstance(choice, dict) else str(choice)
            for choice in choices
        }
        if str(value) not in values:
            raise ValueError(f"参数 {name} 的值不在可选范围内")
    parameter_type = str(descriptor.get("type") or "").lower()
    if parameter_type in {"number", "int", "integer", "float"}:
        try:
            numeric = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"参数 {name} 必须是数字") from exc
        if descriptor.get("min") is not None and numeric < float(descriptor["min"]):
            raise ValueError(f"参数 {name} 不能小于 {descriptor['min']}")
        if descriptor.get("max") is not None and numeric > float(descriptor["max"]):
            raise ValueError(f"参数 {name} 不能大于 {descriptor['max']}")


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


def _reference_filename(value: str, mime_type: str) -> str:
    """Return a provider-safe filename for a local, remote, or encoded image."""

    try:
        name = Path(urlsplit(value).path).name
    except ValueError:
        name = ""
    if name and Path(name).suffix.lower() in {
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".gif",
    }:
        return name[:120]
    suffix = {
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }.get(mime_type, ".png")
    return f"reference{suffix}"
