"""Configuration parsing and validation for Image Studio."""

from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import ImageModel, ImageProvider

SUPPORTED_PROVIDER_KINDS = frozenset(
    {"openai_images", "gemini", "nai_direct", "novelai_official", "custom_json"}
)
STUDIO_CONFIG_FILENAME = "studio_config.json"


@dataclass(frozen=True, slots=True)
class HistorySettings:
    """Retention controls for durable gallery data."""

    enabled: bool
    max_records: int
    max_megabytes: int
    retain_reference_images: bool
    record_invocation_identity: bool = False


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    """Immutable runtime settings created from the plugin configuration."""

    enable_llm_tool: bool
    providers: tuple[ImageProvider, ...]
    history: HistorySettings
    revision: int
    default_page_text2img_model_ref: str = ""
    default_page_img2img_model_ref: str = ""
    default_tool_text2img_model_ref: str = ""
    default_tool_img2img_model_ref: str = ""
    llm_image_return_mode: str = "preview"
    asset_preview_max_edge: int = 768
    asset_preview_quality: int = 80

    def default_model_ref(self, mode: str, source: str) -> str:
        """Return the mode default for a page/command or LLM-tool request."""

        tool = source == "llm_tool"
        if mode == "img2img":
            return (
                self.default_tool_img2img_model_ref
                if tool
                else self.default_page_img2img_model_ref
            )
        return (
            self.default_tool_text2img_model_ref
            if tool
            else self.default_page_text2img_model_ref
        )

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
        "schema_version": 2,
        "revision": 0,
        "providers": [],
        "external_sources": {},
        "history": {
            "enabled": True,
            "max_records": 200,
            "max_megabytes": 2048,
            "retain_reference_images": True,
            "record_invocation_identity": False,
        },
        "generation_defaults": {
            "page": {"text2img_model_ref": "", "img2img_model_ref": ""},
            "tool": {"text2img_model_ref": "", "img2img_model_ref": ""},
        },
        "llm_policy": {
            "natural_language_first": True,
            "nai_auto_selection": "explicit_only",
            "image_return_mode": "preview",
        },
        "asset_policy": {
            "preview_max_edge": 768,
            "preview_quality": 80,
        },
        "ui": {"settings_revision": 0},
    }


def normalize_webui_settings(value: Any) -> tuple[dict[str, Any], list[str]]:
    """Normalize and validate the plugin-owned Studio configuration.

    Args:
        value: Raw value loaded from ``studio_config.json`` or a WebUI draft.

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
        try:
            provider = ImageProvider.from_mapping(raw)
        except ValueError as exc:
            errors.append(f"Provider #{index}：{exc}")
            continue
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
        0, min(100000, _as_int(history.get("max_records"), 200))
    )
    history["max_megabytes"] = max(
        0, min(10240, _as_int(history.get("max_megabytes"), 2048))
    )
    history["retain_reference_images"] = _as_bool(
        history.get("retain_reference_images"), True
    )
    history["record_invocation_identity"] = _as_bool(
        history.get("record_invocation_identity"), False
    )
    merged["history"] = history

    external = merged.get("external_sources")
    if not isinstance(external, dict):
        errors.append("外部图库设置必须是对象")
        external = {}
    if len(external) > 32:
        errors.append("最多配置 32 个外部图库")
    normalized_external = {}
    for source_id, value in external.items():
        if not isinstance(source_id, str) or not re.fullmatch(
            r"[a-z][a-z0-9_]{0,47}", source_id
        ):
            errors.append("外部图库来源 ID 无效")
            continue
        entry = value if isinstance(value, dict) else {"enabled": value}
        kind = entry.get("type", "nai" if source_id == "nai" else "")
        if not isinstance(kind, str) or kind not in {"nai", "directory"}:
            errors.append(f"外部图库 {source_id} 的类型不受支持")
            continue
        name = entry.get(
            "name", "nai-image 插件图库" if kind == "nai" else "自定义图库"
        )
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            errors.append(f"外部图库 {source_id} 的名称应为 1 至 80 个字符")
            continue
        path = entry.get("path", "") if kind == "directory" else ""
        if not isinstance(path, str) or len(path) > 4096 or "\x00" in path:
            errors.append(f"外部图库 {name} 的目录无效")
            continue
        if kind == "directory" and (
            not path.strip() or not Path(path.strip()).is_absolute()
        ):
            errors.append(f"外部图库 {name} 必须填写容器内的绝对目录路径")
        permissions = entry.get("permissions", {})
        if not isinstance(permissions, dict):
            errors.append(f"外部图库 {name} 的操作权限必须是对象")
            permissions = {}
        normalized_external[source_id] = {
            "type": kind,
            "name": name.strip(),
            "path": path.strip(),
            "enabled": _as_bool(entry.get("enabled"), False),
            "recursive": _as_bool(entry.get("recursive"), False),
            "permissions": {
                action: _as_bool(
                    permissions.get(action),
                    kind == "nai" if action == "delete" else True,
                )
                for action in ("favorite", "delete", "download", "reference")
            },
        }
    merged["external_sources"] = normalized_external

    raw_defaults = (
        merged.get("generation_defaults")
        if isinstance(merged.get("generation_defaults"), dict)
        else {}
    )
    defaults: dict[str, dict[str, str]] = {}
    for scope in ("page", "tool"):
        raw_scope = (
            raw_defaults.get(scope) if isinstance(raw_defaults.get(scope), dict) else {}
        )
        defaults[scope] = {
            "text2img_model_ref": str(
                raw_scope.get("text2img_model_ref") or ""
            ).strip()[:240],
            "img2img_model_ref": str(raw_scope.get("img2img_model_ref") or "").strip()[
                :240
            ],
        }
    merged["generation_defaults"] = defaults
    policy = (
        merged.get("llm_policy") if isinstance(merged.get("llm_policy"), dict) else {}
    )
    policy["natural_language_first"] = _as_bool(
        policy.get("natural_language_first"), True
    )
    selection = str(policy.get("nai_auto_selection") or "explicit_only")
    policy["nai_auto_selection"] = (
        selection if selection in {"explicit_only", "allowed"} else "explicit_only"
    )
    image_return_mode = str(policy.get("image_return_mode") or "preview")
    policy["image_return_mode"] = (
        image_return_mode
        if image_return_mode in {"asset", "preview", "original"}
        else "preview"
    )
    policy.pop("preview_max_edge", None)
    policy.pop("preview_quality", None)
    policy.pop("asset_retention_hours", None)
    merged["llm_policy"] = policy
    asset_policy = (
        merged.get("asset_policy")
        if isinstance(merged.get("asset_policy"), dict)
        else {}
    )
    asset_policy["preview_max_edge"] = max(
        256, min(2048, _as_int(asset_policy.get("preview_max_edge"), 768))
    )
    asset_policy["preview_quality"] = max(
        40, min(95, _as_int(asset_policy.get("preview_quality"), 80))
    )
    # Workflow retention is an internal policy; old saved values must not survive.
    asset_policy.pop("lease_hours", None)
    merged["asset_policy"] = asset_policy
    merged["schema_version"] = max(2, _as_int(merged.get("schema_version"), 2))
    merged["revision"] = max(
        0,
        _as_int(merged.get("revision"), 0),
    )
    ui = merged.get("ui") if isinstance(merged.get("ui"), dict) else {}
    ui["settings_revision"] = max(0, _as_int(ui.get("settings_revision"), 0))
    merged["ui"] = ui
    return merged, errors


def load_studio_settings(data_dir: Path) -> tuple[dict[str, Any], list[str]]:
    """Load the plugin-owned configuration file, falling back safely on errors."""

    path = data_dir / STUDIO_CONFIG_FILENAME
    if not path.is_file():
        return default_webui_settings(), []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        backup = path.with_suffix(path.suffix + ".bak")
        try:
            raw = json.loads(backup.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return default_webui_settings(), [
                "studio_config.json 无法读取，已使用默认设置"
            ]
    normalized, errors = normalize_webui_settings(raw)
    return normalized, errors


async def save_studio_settings(data_dir: Path, value: dict[str, Any]) -> None:
    """Atomically persist normalized plugin-owned settings with a backup."""

    normalized, errors = normalize_webui_settings(value)
    if errors:
        raise ValueError("；".join(errors))
    path = data_dir / STUDIO_CONFIG_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(normalized, ensure_ascii=False, indent=2) + "\n"
    await _atomic_write_text(path, payload)


async def _atomic_write_text(path: Path, value: str) -> None:
    import asyncio

    await asyncio.to_thread(_atomic_write_text_sync, path, value)


def _atomic_write_text_sync(path: Path, value: str) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    backup = path.with_suffix(path.suffix + ".bak")
    try:
        temporary.write_text(value, encoding="utf-8")
        if path.is_file():
            os.replace(path, backup)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def runtime_settings(
    config: dict[str, Any], studio_settings: dict[str, Any] | None = None
) -> tuple[RuntimeSettings, list[str]]:
    """Build runtime settings from an AstrBot plugin config object."""

    source = studio_settings if isinstance(studio_settings, dict) else {}
    webui, errors = normalize_webui_settings(source)
    providers = tuple(ImageProvider.from_mapping(item) for item in webui["providers"])
    history_raw = webui["history"]
    llm_policy = webui["llm_policy"]
    asset_policy = webui["asset_policy"]
    return RuntimeSettings(
        enable_llm_tool=_as_bool(config.get("enable_llm_tool"), True),
        providers=providers,
        default_page_text2img_model_ref=webui["generation_defaults"]["page"][
            "text2img_model_ref"
        ],
        default_page_img2img_model_ref=webui["generation_defaults"]["page"][
            "img2img_model_ref"
        ],
        default_tool_text2img_model_ref=webui["generation_defaults"]["tool"][
            "text2img_model_ref"
        ],
        default_tool_img2img_model_ref=webui["generation_defaults"]["tool"][
            "img2img_model_ref"
        ],
        history=HistorySettings(
            enabled=history_raw["enabled"],
            max_records=history_raw["max_records"],
            max_megabytes=history_raw["max_megabytes"],
            retain_reference_images=history_raw["retain_reference_images"],
            record_invocation_identity=history_raw["record_invocation_identity"],
        ),
        llm_image_return_mode=llm_policy["image_return_mode"],
        asset_preview_max_edge=asset_policy["preview_max_edge"],
        asset_preview_quality=asset_policy["preview_quality"],
        revision=webui.get("revision", webui["ui"]["settings_revision"]),
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
