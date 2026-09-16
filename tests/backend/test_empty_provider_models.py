"""Deleting every configured model must not reactivate a legacy scalar value."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from astrbot_plugin_image_studio.backend.config import (
    load_studio_settings,
    normalize_webui_settings,
    runtime_settings,
    save_studio_settings,
)
from astrbot_plugin_image_studio.backend.models import ImageProvider
from astrbot_plugin_image_studio.tests.support.webui_harness import create_app

PREFIX = "/astrbot_plugin_image_studio/"
KINDS = (
    "comfyui",
    "openai_images",
    "custom_json",
    "gemini",
    "nai_direct",
    "novelai_official",
)


def workflow():
    return {
        "api_graph": {
            "1": {"class_type": "SaveImage", "inputs": {"filename_prefix": "original"}}
        },
        "bindings": {},
        "outputs": ["1"],
    }


def raw_provider(kind="comfyui"):
    return {
        "id": "configured",
        "name": "Configured",
        "kind": kind,
        "model": "original-model",
        "comfyui": workflow() if kind == "comfyui" else None,
    }


@pytest.mark.parametrize("kind", KINDS)
def test_explicit_empty_models_override_stale_scalar_and_remain_empty(kind):
    selected = ImageProvider.from_mapping({**raw_provider(kind), "models": []})
    assert selected.models == ()
    assert selected.model == ""
    public = selected.public_dict()
    assert public["models"] == [] and public["model"] == ""
    assert ImageProvider.from_mapping(public).models == ()
    assert not selected.capabilities.text2img
    assert not selected.capabilities.img2img


@pytest.mark.parametrize("kind", KINDS)
def test_genuinely_legacy_scalar_without_model_list_still_migrates(kind):
    selected = ImageProvider.from_mapping(raw_provider(kind))
    assert [model.id for model in selected.models] == ["original-model"]
    assert selected.model == "original-model"
    assert (
        ImageProvider.from_mapping(selected.public_dict()).models[0].id
        == "original-model"
    )
    if kind == "comfyui":
        assert selected.models[0].comfyui["api_graph"] == workflow()["api_graph"]


def test_comfy_delete_last_workflow_survives_disk_and_runtime_restart(tmp_path):
    before = ImageProvider.from_mapping(
        {**raw_provider(), "models": [{"id": "original-model", "comfyui": workflow()}]}
    )
    edited = before.public_dict()
    edited["models"] = []
    assert edited["model"] == "original-model"
    normalized, errors = normalize_webui_settings({"providers": [edited]})
    assert not errors
    asyncio.run(save_studio_settings(tmp_path, normalized))
    persisted = json.loads(
        (tmp_path / "studio_config.json").read_text(encoding="utf-8")
    )
    assert persisted["providers"][0]["models"] == []
    assert persisted["providers"][0]["model"] == ""
    loaded, errors = load_studio_settings(tmp_path)
    assert not errors
    restarted, errors = runtime_settings({"enable_llm_tool": True}, loaded)
    assert not errors
    assert restarted.providers[0].models == ()
    assert restarted.models_for_mode("text2img") == []
    assert restarted.models_for_mode("img2img") == []
    assert (
        restarted.find_model("configured:original-model", "configured", "text2img")
        is None
    )


def test_deleting_last_workflow_through_page_api_does_not_reappear_in_settings_or_capabilities(
    tmp_path,
):
    async def run():
        app = await create_app(tmp_path, seed=False)
        plugin = app.state.plugin
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                settings = (await client.get(PREFIX + "settings/get")).json()["webui"]
                settings["providers"].append(
                    {
                        **raw_provider(),
                        "models": [{"id": "original-model", "comfyui": workflow()}],
                    }
                )
                saved = await client.post(
                    PREFIX + "settings/save",
                    json={"settings_revision": settings["revision"], "webui": settings},
                )
                assert saved.status_code == 200, saved.text
                current = (await client.get(PREFIX + "settings/get")).json()["webui"]
                target = next(
                    item for item in current["providers"] if item["id"] == "configured"
                )
                assert target["model"] == "original-model"
                target["models"] = []
                deleted = await client.post(
                    PREFIX + "settings/save",
                    json={"settings_revision": current["revision"], "webui": current},
                )
                assert deleted.status_code == 200, deleted.text
                refreshed = (await client.get(PREFIX + "settings/get")).json()["webui"]
                target = next(
                    item
                    for item in refreshed["providers"]
                    if item["id"] == "configured"
                )
                assert target["models"] == [] and target["model"] == ""
                bootstrap = (await client.get(PREFIX + "studio/bootstrap")).json()
                assert not any(
                    model["provider_id"] == "configured"
                    for model in bootstrap["models"]
                )
                capabilities = await plugin.image_studio_get_capabilities(
                    SimpleNamespace(), query_type="all"
                )
                payload = json.loads(capabilities.content[0].text)
                assert not any(
                    item["model_ref"].startswith("configured:")
                    for item in payload["models"]
                )
                rejected = await client.post(
                    PREFIX + "comfy/jobs",
                    json={"provider_id": "configured", "model": "original-model"},
                )
                assert (
                    rejected.status_code == 400
                    and "工作流不存在" in rejected.json()["message"]
                )
                loaded, errors = load_studio_settings(tmp_path)
                assert not errors
                restarted, errors = runtime_settings(plugin.config, loaded)
                assert not errors
                assert restarted.provider("configured").models == ()
                assert restarted.provider("configured").model == ""
        finally:
            if getattr(plugin, "_comfy", None):
                await plugin._comfy.close()
            await plugin._external_gallery.close()
            await plugin.store.close()

    asyncio.run(run())
