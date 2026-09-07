"""Generation orchestration and reproduction planning."""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiohttp

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
    MODEL_SCHEDULING_KEYS,
    ReferenceImage,
)
from .providers import ProviderBatchError, ProviderError, ProviderExecutor
from .storage import GenerationStore, detect_mime_type


class _ProviderLimiter:
    """Keep in-flight requests counted when provider settings change."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.active = 0
        self.changed = asyncio.Event()

    def resize(self, limit: int) -> None:
        self.limit = limit
        self.changed.set()

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        while self.active >= self.limit:
            self.changed.clear()
            await self.changed.wait()
        self.active += 1
        try:
            yield
        finally:
            self.active -= 1
            self.changed.set()


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
        self._limiters: dict[str, _ProviderLimiter] = {}
        self._model_limiters: dict[tuple[str, str], _ProviderLimiter] = {}
        self.update_settings(settings)

    def update_settings(self, settings: RuntimeSettings) -> None:
        """Swap future-request settings after an atomic configuration save."""

        self.settings = settings
        for provider in settings.providers:
            limiter = self._limiters.setdefault(
                provider.id, _ProviderLimiter(provider.max_concurrent_generations)
            )
            limiter.resize(provider.max_concurrent_generations)
            for model in provider.models:
                model_limiter = self._model_limiters.setdefault(
                    (provider.id, model.id),
                    _ProviderLimiter(model.max_concurrent_requests),
                )
                model_limiter.resize(model.max_concurrent_requests)

    async def generate(
        self,
        *,
        mode: str,
        provider_id: str,
        prompt: str,
        negative_prompt: str | None = None,
        model_ref: str = "",
        model: str = "",
        size: str = "",
        count: Any = None,
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
        select_model = (
            self._select_command_model if source == "command" else self._select_model
        )
        provider, selected_model = select_model(
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
        advanced_parameters = parameters
        if source == "command":
            normalized_count, advanced_parameters = _command_count(
                count, parameters, selected_model
            )
            if negative_prompt is None:
                negative_prompt = selected_model.negative_prompt_default
        else:
            batch_parameters = _parameters(parameters)
            if source == "llm_tool":
                batch_parameters = {
                    key: value
                    for key, value in batch_parameters.items()
                    if key in selected_model.llm_exposed_parameter_names
                }
            count_name = _control_parameter_name(selected_model, "count")
            supplied_count = (
                count
                if count not in (None, "", 0)
                and (
                    source != "llm_tool"
                    or count_name in selected_model.llm_exposed_parameter_names
                )
                else _control_parameter_value(
                    batch_parameters, selected_model, "count", source=source
                )
            )
            normalized_count, advanced_parameters = _command_count(
                supplied_count, batch_parameters, selected_model
            )
        parameter_values = _resolved_model_values(
            advanced_parameters, selected_model, source=source
        )
        model_parameters = _wire_parameters(parameter_values, selected_model)
        if source == "command":
            model_parameters = {
                key: value
                for key, value in model_parameters.items()
                if key not in {"count", "n"}
            }
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
            count=normalized_count,
            parameters=model_parameters,
            references=normalized_refs,
            source=source if source in {"webui", "command", "llm_tool"} else "webui",
            selection_source=selection_source,
            invocation_source=invocation_source or InvocationSource(),
            native_batch_size=selected_model.native_batch_size,
            max_concurrent_requests=selected_model.max_concurrent_requests,
            local_parameters={
                name: value
                for name, value in parameter_values.items()
                if selected_model.parameters.get(name, {}).get("ui_only")
            },
        )
        started = time.perf_counter()
        warning = ""
        batch_failures: tuple[tuple[int, str], ...] = ()
        try:
            images = await self.run_provider_request(provider, request)
        except ProviderBatchError as exc:
            if not exc.images:
                raise
            images = exc.images
            batch_failures = exc.failures
            warning = str(exc)
        if len(images) != request.count and not batch_failures:
            warning = (
                f"本次目标 {request.count} 张，上游实际返回 {len(images)} 张；"
                "已保留全部返回图片，未自动追加请求。"
            )
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        generation_id = await self.store.record_success(
            provider=provider,
            request=request,
            images=images,
            elapsed_ms=elapsed_ms,
            history=settings.history,
            preview_max_edge=settings.asset_preview_max_edge,
            preview_quality=settings.asset_preview_quality,
            batch_failures=batch_failures,
        )
        await self.store.discard_staged_references(request.references)
        return GenerationResult(
            provider=provider,
            request=request,
            images=images,
            elapsed_ms=elapsed_ms,
            generation_id=generation_id,
            warning=warning,
        )

    async def run_provider_request(
        self, provider: ImageProvider, request: GenerationRequest
    ) -> tuple[GeneratedImage, ...]:
        """Execute any generation-like request under its provider concurrency limit."""

        limiter = self._limiters.setdefault(
            provider.id, _ProviderLimiter(provider.max_concurrent_generations)
        )
        model = provider.get_model(request.model)
        model_limiter = self._model_limiters.setdefault(
            (provider.id, model.id), _ProviderLimiter(model.max_concurrent_requests)
        )
        return await self._run_batch(provider, model, request, limiter, model_limiter)

    async def _run_batch(
        self,
        provider: ImageProvider,
        model: ImageModel,
        request: GenerationRequest,
        limiter: _ProviderLimiter,
        model_limiter: _ProviderLimiter,
    ) -> tuple[GeneratedImage, ...]:
        count = _batch_integer(request.count, "count")
        native_size = _batch_integer(model.native_batch_size, "native_batch_size")
        request_sizes = tuple(
            min(native_size, count - offset) for offset in range(0, count, native_size)
        )
        parameters = {
            key: value
            for key, value in request.parameters.items()
            if key not in MODEL_SCHEDULING_KEYS and key not in {"count", "n"}
        }
        pending = iter(enumerate(request_sizes))
        results: list[tuple[GeneratedImage, ...]] = [()] * len(request_sizes)
        failures: dict[int, str] = {}

        async def worker() -> None:
            for index, size in pending:
                chunk = replace(request, count=size, parameters=dict(parameters))
                try:
                    async with model_limiter.slot():
                        async with limiter.slot():
                            images = await self.executor.generate(provider, chunk)
                    if not images:
                        raise ProviderError("上游未返回可用图片")
                    results[index] = tuple(images)
                except (ProviderError, aiohttp.ClientError, TimeoutError) as exc:
                    failures[index + 1] = (
                        str(exc)[:500]
                        if isinstance(exc, ProviderError)
                        else "请求超时"
                        if isinstance(exc, TimeoutError)
                        else "无法连接服务商"
                    )

        # Shared model slots are acquired before provider slots so queued work
        # for a busy model cannot occupy its provider's entire allowance.
        tasks = [
            asyncio.create_task(worker())
            for _ in range(min(len(request_sizes), model.max_concurrent_requests))
        ]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        images = tuple(image for result in results for image in result)
        if failures:
            raise ProviderBatchError(
                count,
                images,
                tuple(sorted(failures.items())),
                request_sizes=request_sizes,
            )
        return images

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
        tool_image_cache_root = (Path(get_astrbot_temp_path()) / "tool_images").resolve(
            strict=False
        )
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
            if _within(current, tool_image_cache_root):
                raise ValueError("data/temp/tool_images 仅用于视觉预览，不能作为参考图")
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

    async def reproduction_plan(
        self, generation_id: str, image_id: str = ""
    ) -> dict[str, Any]:
        """Return a reproducible draft and stage retained references when available."""

        from .parameter_exchange import export_parameters, resolve_parameters

        detail = await self.store.generation_detail(generation_id, include_assets=False)
        if detail is None:
            raise ValueError("历史生成记录不存在")
        copied = export_parameters(detail, image_id)
        resolved = resolve_parameters(
            copied["content"], self.settings, for_reproduction=True
        )
        imported = detail.get("source") == "import"
        staged = (
            await self.store.stage_generation_references(generation_id)
            if not imported
            else []
        )
        warnings = list(resolved["warnings"])
        if staged:
            warnings = [
                text
                for text in warnings
                if text != "参数文本不包含原始参考图，请补充参考图后生成。"
            ]
        draft = {
            **resolved["draft"],
            "references": staged,
            "provider_available": not resolved["requires_model_selection"],
            "reference_available": bool(staged),
            "candidates": resolved["candidates"],
            "requires_model_selection": resolved["requires_model_selection"],
            "warnings": warnings,
            "unmapped": resolved["unmapped"],
        }
        if detail.get("mode") == "img2img" and not staged:
            draft["notice"] = (
                "历史参考图未保留，已填入全部可恢复参数。请上传新参考图，或在生图页明确选择当前成图作为参考图。"
            )
        elif resolved["requires_model_selection"]:
            draft["notice"] = (
                "历史 Provider 已不可用。请选择下方支持相同模式的 Provider 后再生成。"
            )
        else:
            draft["notice"] = "已按当前模型设置恢复生成参数。"
        if warnings:
            draft["notice"] += " " + "；".join(warnings)
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

    def resolve_command_model(
        self, *, mode: str, provider_id: str = "", model: str = ""
    ) -> tuple[ImageProvider, ImageModel]:
        """Validate command routing before loading potentially costly references."""

        normalized_mode = _mode(mode)
        return self._select_command_model(
            self.settings,
            provider_id,
            "",
            model,
            normalized_mode,
            self.settings.default_model_ref(normalized_mode, "command"),
        )

    def _select_command_model(
        self,
        settings: RuntimeSettings,
        provider_id: str,
        model_ref: str,
        model_id: str,
        mode: str,
        mode_default_ref: str,
    ) -> tuple[ImageProvider, ImageModel]:
        """Resolve commands without silently replacing explicit or stale choices."""

        requested_provider = str(provider_id or "").strip()
        if requested_provider and settings.provider(requested_provider) is None:
            raise ValueError("指定服务商不存在或未启用，请检查 --provider")
        explicit_ref = str(model_ref or model_id or "").strip()
        requested_ref = explicit_ref or str(mode_default_ref or "").strip()
        if not requested_ref:
            raise ValueError(
                "当前模式未设置默认页面生图模型，请在设置中配置或使用 --model"
            )
        matches = [
            (provider, model)
            for provider, model in settings.models_for_mode(mode)
            if requested_ref in {model.id, f"{provider.id}:{model.id}"}
        ]
        if requested_provider:
            owned = [item for item in matches if item[0].id == requested_provider]
            if matches and not owned:
                if not explicit_ref:
                    raise ValueError(
                        "当前模式的默认模型不属于指定服务商，请同时提供 --model"
                    )
                raise ValueError(
                    "指定模型不属于指定服务商，请检查 --provider 和 --model"
                )
            matches = owned
        if not matches:
            if explicit_ref:
                raise ValueError("指定模型不存在、服务商未启用或不支持当前生图模式")
            raise ValueError(
                "当前模式的默认模型已失效，请修改页面默认模型或提供 --model"
            )
        if len(matches) > 1:
            raise ValueError(
                "多个服务商包含同名模型，请使用 --provider 或 --model 服务商ID:模型ID"
            )
        return matches[0]


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


def _command_count(
    explicit_count: Any, parameters: Any, model: ImageModel
) -> tuple[int, dict[str, Any]]:
    """Use page model defaults and bounds for commands, including count aliases."""

    aliases = {"count", "n"}
    descriptors = []
    for name, descriptor in model.parameters.items():
        if name in aliases or descriptor.get("request_key") in {"count", "n"}:
            aliases.add(name)
            descriptors.append((name, descriptor))
    descriptor = next(
        (value for name, value in descriptors if name == "count"),
        descriptors[0][1] if descriptors else {},
    )
    raw = _parameters(parameters)
    raw_count = next(
        (
            raw[name]
            for name in ("count", "n", *(name for name, _ in descriptors))
            if name in raw
        ),
        None,
    )
    supplied = explicit_count if explicit_count is not None else raw_count
    value = supplied if supplied is not None else descriptor.get("default", 1)
    try:
        if isinstance(value, bool):
            raise ValueError
        number = float(value)
        if not math.isfinite(number) or not number.is_integer():
            raise ValueError
        requested = int(number)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("图片数量必须为整数") from exc
    if supplied is not None and requested <= 0:
        raise ValueError("图片数量必须大于 0")
    try:
        lower = max(
            1,
            math.ceil(
                float(descriptor.get("min") if descriptor.get("min") is not None else 1)
            ),
        )
        upper = math.floor(
            float(descriptor.get("max") if descriptor.get("max") is not None else 4)
        )
        if upper < lower:
            raise ValueError
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("模型的图片数量范围配置无效，请检查 count 的 min/max") from exc
    return max(lower, min(upper, requested)), {
        key: value for key, value in raw.items() if key not in aliases
    }


def _batch_integer(value: Any, name: str) -> int:
    try:
        numeric = float(value)
        if (
            isinstance(value, bool)
            or not math.isfinite(numeric)
            or not numeric.is_integer()
        ):
            raise ValueError
        if numeric < 1:
            raise ValueError
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"批次参数 {name} 必须为正整数") from exc
    return int(numeric)


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

    return _wire_parameters(_resolved_model_values(value, model, source=source), model)


def _resolved_model_values(
    value: Any, model: ImageModel, *, source: str = "webui"
) -> dict[str, Any]:
    raw = _parameters(value)
    values: dict[str, Any] = {}
    active_parameters = model.active_parameters
    inactive = set(model.parameters) - set(active_parameters)
    for name, descriptor in active_parameters.items():
        if "default" not in descriptor:
            continue
        values[name] = descriptor["default"]
    tool_parameters = model.tool.get("parameters")
    if source == "llm_tool" and isinstance(tool_parameters, dict):
        for name, descriptor in tool_parameters.items():
            if (
                name in active_parameters
                and isinstance(descriptor, dict)
                and "default_override" in descriptor
            ):
                values[name] = descriptor["default_override"]
            elif (
                name in active_parameters
                and isinstance(descriptor, dict)
                and "default" in descriptor
            ):
                values[name] = descriptor["default"]
    _expand_parameter_presets(values, model)
    allowed = model.llm_exposed_parameter_names
    supplied: dict[str, Any] = {}
    for key, item in raw.items():
        if key in inactive or key in MODEL_SCHEDULING_KEYS:
            continue
        if source == "llm_tool" and key not in allowed:
            continue
        name = (
            key
            if key in active_parameters
            else next(
                (
                    name
                    for name, descriptor in active_parameters.items()
                    if descriptor.get("request_key") == key
                ),
                key,
            )
        )
        _validate_parameter_value(name, item, active_parameters.get(name))
        supplied[name] = item
    # Expand an explicitly chosen preset only when its target was not supplied.
    # This also protects explicit empty artist strings, regardless of key order.
    _expand_parameter_presets(supplied, model, protected=set(supplied))
    values.update(supplied)
    return values


def _wire_parameters(values: dict[str, Any], model: ImageModel) -> dict[str, Any]:
    mapped: dict[str, Any] = {}
    for name, item in values.items():
        descriptor = model.parameters.get(name, {})
        if descriptor.get("ui_only"):
            continue
        key = str(descriptor.get("request_key") or name)
        if key not in {"size", "count", "n"} and key not in MODEL_SCHEDULING_KEYS:
            mapped[key[:96]] = item
    return mapped


def _expand_parameter_presets(
    values: dict[str, Any], model: ImageModel, *, protected: set[str] | None = None
) -> None:
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
        if (
            target
            and target not in (protected or ())
            and isinstance(choice, dict)
            and isinstance(choice.get("fill"), str)
        ):
            values[target] = choice["fill"]


def _control_parameter_name(model: ImageModel, name: str) -> str:
    if name in model.parameters:
        return name
    aliases = {"count", "n"} if name == "count" else {name}
    return next(
        (
            key
            for key, descriptor in model.parameters.items()
            if key in aliases or descriptor.get("request_key") in aliases
        ),
        name,
    )


def _control_parameter_value(
    value: Any, model: ImageModel, name: str, *, source: str
) -> Any:
    name = _control_parameter_name(model, name)
    raw = _parameters(value)
    if name in raw and (
        source != "llm_tool" or name in model.llm_exposed_parameter_names
    ):
        return raw[name]
    descriptor = model.parameters.get(name)
    if not isinstance(descriptor, dict):
        return ""
    tool_parameters = model.tool.get("parameters")
    if source == "llm_tool" and isinstance(tool_parameters, dict):
        policy = tool_parameters.get(name)
        if isinstance(policy, dict) and "default_override" in policy:
            return policy["default_override"]
        if isinstance(policy, dict) and "default" in policy:
            return policy["default"]
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
    if value is None and (
        descriptor.get("nullable") is True
        or ("default" in descriptor and descriptor["default"] is None)
    ):
        return
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
