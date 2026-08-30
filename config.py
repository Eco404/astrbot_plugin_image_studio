"""Configuration parsing and validation for Image Studio."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any

from .models import ImageModel, ImageProvider

SUPPORTED_PROVIDER_KINDS = frozenset(
    {"openai_images", "gemini", "nai_direct", "custom_json"}
)


@dataclass(frozen=True, slots=True)
class HistorySettings:
    """Retention controls for durable gallery data."""

    enabled: bool
    max_records: int
    max_megabytes: int
    retain_reference_images: bool


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    """Immutable runtime settings created from the plugin configuration."""

    enable_llm_tool: bool
    max_concurrent_generations: int
    providers: tuple[ImageProvider, ...]
    default_provider_id: str
    default_size: str
    default_count: int
    history: HistorySettings
    revision: int
    default_model_ref: str = ""

    def provider(self, provider_id: str) -> ImageProvider | None:
        """Return an enabled provider by stable ID."""

        for provider in self.providers:
            if provider.id == provider_id and provider.enabled:
                return provider
        return None

    def providers_for_mode(self, mode: str) -> list[ImageProvider]:
        """Return enabled providers capable of a requested generation mode."""

        return [
            provider
            for provider in self.providers
            if provider.enabled and provider.capabilities.supports(mode)
        ]

    def models_for_mode(self, mode: str) -> list[tuple[ImageProvider, ImageModel]]:
        """Return enabled model entries that support the requested mode."""

        return [
            (provider, model)
            for provider in self.providers
            if provider.enabled
            for model in provider.models
            if model.supports(mode)
        ]

    def find_model(
        self, model_ref: str, provider_id: str, mode: str
    ) -> tuple[ImageProvider, ImageModel] | None:
        """Resolve a model reference, preferring an explicitly selected provider."""

        requested_ref = str(model_ref or "").strip()
        requested_provider = str(provider_id or "").strip()
        candidates = self.models_for_mode(mode)
        if requested_provider:
            candidates = [
                item for item in candidates if item[0].id == requested_provider
            ]
        if requested_ref:
            for provider, model in candidates:
                if requested_ref in {model.id, f"{provider.id}:{model.id}"}:
                    return provider, model
        return candidates[0] if candidates else None


def default_webui_settings() -> dict[str, Any]:
    """Return defaults for the WebUI-owned portion of plugin configuration."""

    return {
        "providers": [],
        "history": {
            "enabled": True,
            "max_records": 200,
            "max_megabytes": 2048,
            "retain_reference_images": True,
        },
        "generation_defaults": {
            "provider_id": "",
            "model_ref": "",
            "size": "1024x1024",
            "count": 1,
        },
        "ui": {"settings_revision": 0},
    }


def normalize_webui_settings(value: Any) -> tuple[dict[str, Any], list[str]]:
    """Normalize and validate persisted WebUI configuration.

    Args:
        value: Raw ``webui_managed`` value from AstrBot configuration.

    Returns:
        The canonical configuration and human-readable validation errors.
    """

    source = copy.deepcopy(value) if isinstance(value, dict) else {}
    merged = default_webui_settings()
    _deep_merge(merged, source)
    errors: list[str] = []
    raw_providers = merged.get("providers")
    if not isinstance(raw_providers, list):
        raw_providers = []
        errors.append("providers 必须是列表")

    normalized_providers: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(raw_providers[:24], start=1):
        if not isinstance(raw, dict):
            errors.append(f"Provider #{index} 必须是对象")
            continue
        provider = ImageProvider.from_mapping(raw)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", provider.id):
            errors.append(f"Provider #{index} 的 id 只能包含字母、数字、- 和 _")
            continue
        if provider.id in seen_ids:
            errors.append(f"Provider id 重复: {provider.id}")
            continue
        seen_ids.add(provider.id)
        if not provider.name:
            errors.append(f"Provider {provider.id} 缺少名称")
        if provider.kind not in SUPPORTED_PROVIDER_KINDS:
            errors.append(f"Provider {provider.id} 的 kind 不受支持")
        if not provider.base_url:
            errors.append(f"Provider {provider.id} 缺少 base_url")
        if not provider.models:
            errors.append(f"Provider {provider.id} 至少需要一个模型")
        model_ids: set[str] = set()
        for model in provider.models:
            if model.id in model_ids:
                errors.append(f"Provider {provider.id} 的模型 id 重复: {model.id}")
            model_ids.add(model.id)
        if provider.edit_request_format not in {"multipart", "json_data_url"}:
            errors.append(f"Provider {provider.id} 的 edit_request_format 无效")
        normalized_providers.append(provider.public_dict())
    merged["providers"] = normalized_providers

    history = merged.get("history") if isinstance(merged.get("history"), dict) else {}
    history["enabled"] = _as_bool(history.get("enabled"), True)
    history["max_records"] = max(
        -1, min(100000, _as_int(history.get("max_records"), 200))
    )
    history["max_megabytes"] = max(
        0, min(10240, _as_int(history.get("max_megabytes"), 2048))
    )
    history["retain_reference_images"] = _as_bool(
        history.get("retain_reference_images"), True
    )
    merged["history"] = history

    defaults = (
        merged.get("generation_defaults")
        if isinstance(merged.get("generation_defaults"), dict)
        else {}
    )
    defaults["provider_id"] = str(defaults.get("provider_id") or "").strip()[:64]
    defaults["model_ref"] = str(defaults.get("model_ref") or "").strip()[:240]
    defaults["size"] = str(defaults.get("size") or "1024x1024").strip()[:40]
    defaults["count"] = max(1, min(4, _as_int(defaults.get("count"), 1)))
    merged["generation_defaults"] = defaults
    ui = merged.get("ui") if isinstance(merged.get("ui"), dict) else {}
    ui["settings_revision"] = max(0, _as_int(ui.get("settings_revision"), 0))
    merged["ui"] = ui
    return merged, errors


def runtime_settings(config: dict[str, Any]) -> tuple[RuntimeSettings, list[str]]:
    """Build runtime settings from an AstrBot plugin config object."""

    webui, errors = normalize_webui_settings(config.get("webui_managed"))
    providers = tuple(ImageProvider.from_mapping(item) for item in webui["providers"])
    history_raw = webui["history"]
    return RuntimeSettings(
        enable_llm_tool=_as_bool(config.get("enable_llm_tool"), True),
        max_concurrent_generations=max(
            1, min(8, _as_int(config.get("max_concurrent_generations"), 2))
        ),
        providers=providers,
        default_provider_id=webui["generation_defaults"]["provider_id"],
        default_model_ref=webui["generation_defaults"]["model_ref"],
        default_size=webui["generation_defaults"]["size"],
        default_count=webui["generation_defaults"]["count"],
        history=HistorySettings(
            enabled=history_raw["enabled"],
            max_records=history_raw["max_records"],
            max_megabytes=history_raw["max_megabytes"],
            retain_reference_images=history_raw["retain_reference_images"],
        ),
        revision=webui["ui"]["settings_revision"],
    ), errors


def _deep_merge(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
            "enabled",
            "enable",
            "开启",
        }
    return default if value is None else bool(value)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
