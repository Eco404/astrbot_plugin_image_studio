"""Settings API; persistence and rollback stay within the original shared lock."""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.web import error_response, json_response
from astrbot.api.web import request as web_request

from ..config import normalize_webui_settings, save_studio_settings
from ..models import novelai_model_presets

LOG_TAG = "[ImageStudio]"


class SettingsAPI:
    def __init__(
        self,
        *,
        config,
        data_dir,
        settings_lock,
        get_settings,
        get_studio,
        apply_settings,
        get_external,
        configure_external,
    ):
        self.config = config
        self.data_dir = data_dir
        self.settings_lock = settings_lock
        self.get_settings = get_settings
        self.get_studio = get_studio
        self.apply_settings = apply_settings
        self.get_external = get_external
        self.configure_external = configure_external

    async def _api_comfy_save_workflow(self):
        try:
            body = await web_request.json(default={})
            provider_id = str(body.get("provider_id") or "")
            model = body.get("model")
            if not isinstance(model, dict) or not model.get("id"):
                raise ValueError("需要工作流 ID 与配置")
            async with self.settings_lock:
                candidate = copy.deepcopy(self.get_studio())
                provider = next(
                    (
                        item
                        for item in candidate["providers"]
                        if item["id"] == provider_id and item["kind"] == "comfyui"
                    ),
                    None,
                )
                if provider is None:
                    raise ValueError("ComfyUI 服务商不存在，请先保存服务商")
                if any(
                    item["id"] == model["id"] for item in provider.get("models", [])
                ):
                    raise ValueError("工作流 ID 已存在，请使用新 ID 或在设置页编辑")
                if len(provider.get("models", [])) >= 32:
                    raise ValueError(
                        "每个服务商最多配置 32 个工作流，请先移除不再使用的条目"
                    )
                provider.setdefault("models", []).append(model)
                candidate, errors = normalize_webui_settings(candidate)
                if errors:
                    raise ValueError("；".join(errors))
                candidate["revision"] = self.get_settings().revision + 1
                candidate["ui"]["settings_revision"] = candidate["revision"]
                await save_studio_settings(Path(self.data_dir), candidate)
                self.apply_settings(candidate)
            saved = self.get_settings().provider(provider_id).get_model(model["id"])
            return json_response(
                {
                    "model": saved.public_dict(),
                    "model_ref": f"{provider_id}:{saved.id}",
                    "settings_revision": candidate["revision"],
                }
            )
        except ValueError as exc:
            return error_response(str(exc), status_code=400)

    async def _api_bootstrap(self) -> Any:
        return json_response(
            {
                "settings_revision": self.get_settings().revision,
                "defaults": {
                    "text2img_model_ref": self.get_settings().default_model_ref(
                        "text2img", "webui"
                    ),
                    "img2img_model_ref": self.get_settings().default_model_ref(
                        "img2img", "webui"
                    ),
                },
                "providers": [
                    provider.public_dict()
                    for provider in self.get_settings().providers
                    if provider.enabled
                ],
                "models": [
                    {
                        **model.public_dict(),
                        "provider_id": provider.id,
                        "provider_name": provider.name,
                        "provider_kind": provider.kind,
                        "model_ref": f"{provider.id}:{model.id}",
                    }
                    for provider in self.get_settings().providers
                    if provider.enabled
                    for model in provider.models
                ],
                "modes": ["text2img", "img2img"],
                "novelai_models": novelai_model_presets(),
            }
        )

    async def _api_get_settings(self) -> Any:
        studio, errors = normalize_webui_settings(self.get_studio())
        return json_response(
            {
                "base": {
                    "enable_llm_tool": bool(self.config.get("enable_llm_tool", True)),
                },
                "studio": studio,
                "webui": studio,
                "novelai_models": novelai_model_presets(),
                "validation_errors": errors,
            }
        )

    async def _api_save_settings(self) -> Any:
        body = await web_request.json(default={})
        if not isinstance(body, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)
        expected_revision = _as_int(body.get("settings_revision"), -1)
        warnings: list[str] = []
        async with self.settings_lock:
            current_studio, _ = normalize_webui_settings(self.get_studio())
            current_revision = _as_int(current_studio.get("revision"), 0)
            if expected_revision < 0:
                expected_revision = _as_int(
                    body.get("studio", body.get("webui", {}))
                    .get("ui", {})
                    .get("settings_revision"),
                    -1,
                )
            if expected_revision != current_revision:
                return error_response(
                    "设置已被其他操作更新，请刷新后再保存", status_code=409
                )
            candidate, errors = normalize_webui_settings(
                body.get("studio", body.get("webui"))
            )
            if errors:
                return error_response("；".join(errors), status_code=400)
            try:
                await asyncio.to_thread(
                    self.get_external().validate_configuration,
                    candidate["external_sources"],
                )
            except (ValueError, OSError) as exc:
                return error_response(str(exc), status_code=400)
            candidate["revision"] = current_revision + 1
            candidate["ui"]["settings_revision"] = candidate["revision"]
            base = body.get("base") if isinstance(body.get("base"), dict) else {}
            previous = copy.deepcopy(dict(self.config))
            previous_studio = copy.deepcopy(self.get_studio())
            for key in ("enable_llm_tool",):
                if key in base:
                    self.config[key] = base[key]
            try:
                await save_studio_settings(Path(self.data_dir), candidate)
                await _save_config_async(self.config)
            except Exception as exc:
                self.config.clear()
                self.config.update(previous)
                try:
                    await save_studio_settings(Path(self.data_dir), previous_studio)
                except Exception:
                    logger.warning("%s 恢复插件配置文件失败", LOG_TAG)
                logger.warning(
                    "%s 保存 WebUI 配置失败: %s", LOG_TAG, type(exc).__name__
                )
                return error_response("配置保存失败", status_code=500)
            self.apply_settings(candidate)
            try:
                await self.configure_external()
            except Exception as exc:
                logger.exception("%s 设置已保存，但外部图库配置应用失败", LOG_TAG)
                warnings.append(
                    f"设置已保存，但外部图库配置未能应用：{type(exc).__name__}。请检查存储状态后重新保存。"
                )
        return json_response(
            {"settings_revision": self.get_settings().revision, "warnings": warnings}
        )


async def _save_config_async(config: Any) -> None:
    save = getattr(config, "save_config_async", None)
    if callable(save):
        await save()
        return
    save = getattr(config, "save_config", None)
    if callable(save):
        result = save()
        if asyncio.iscoroutine(result):
            await result
        return
    raise RuntimeError("当前插件配置对象不支持保存")


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
