"""Gallery workflow preparation and temporary execution use persisted providers."""

from __future__ import annotations

import asyncio
import io
import json

import pytest
from astrbot_plugin_image_studio.tests.backend.test_comfy_api import (
    PREFIX,
    setup,
    teardown,
)
from astrbot_plugin_image_studio.tests.backend.test_comfy_provider import config, graph
from PIL import Image, PngImagePlugin


async def empty_workflows(client, *, enabled=True):
    webui = (await client.get(PREFIX + "settings/get")).json()["webui"]
    selected = next(item for item in webui["providers"] if item["id"] == "comfy")
    selected.update(models=[], enabled=enabled)
    response = await client.post(
        PREFIX + "settings/save",
        json={"settings_revision": webui["revision"], "webui": webui},
    )
    assert response.status_code == 200, response.text
    return (await client.get(PREFIX + "settings/get")).json()["webui"]


async def imported_image(app, *, color="blue", api=True):
    info = PngImagePlugin.PngInfo()
    info.add_text(
        "prompt" if api else "workflow",
        json.dumps(graph() if api else {"nodes": [], "links": []}),
    )
    image = io.BytesIO()
    Image.new("RGB", (32, 32), color).save(image, "PNG", pnginfo=info)
    record = await app.state.plugin.store.import_image(
        image.getvalue(), "workflow.png", {}
    )
    detail = await app.state.plugin.store.generation_detail(record["generation_id"])
    return record["generation_id"], detail["images"][0]["id"]


def test_gallery_reads_nested_metadata_with_no_saved_workflows(tmp_path):
    async def run():
        app, client, transport = await setup(tmp_path)
        try:
            saved = await empty_workflows(client)
            generation_id, image_id = await imported_image(app)
            response = await client.post(
                PREFIX + "comfy/import",
                json={"generation_id": generation_id, "image_id": image_id},
            )
            assert response.status_code == 200, response.text
            result = response.json()
            assert result["comfyui"]["api_graph"]["4"]["inputs"]["seed"] == 7
            assert result["providers"] == [{"id": "comfy", "name": "Offline Comfy"}]
            assert (
                not result["historical_snapshot"] and result["matched_model_ref"] == ""
            )
            assert (
                result["model"]["native_batch_size"]
                == result["parameters"]["count"]["default"]
                == 1
            )
            assert result["seed_warnings"]
            assert (await client.get(PREFIX + "settings/get")).json()["webui"] == saved
            assert not transport.calls
        finally:
            await teardown(app, client)

    asyncio.run(run())


def test_gallery_falls_back_to_original_image_when_metadata_index_is_empty(tmp_path):
    async def run():
        app, client, _ = await setup(tmp_path)
        try:
            generation_id, image_id = await imported_image(app)
            with app.state.plugin.store._connect() as conn:
                conn.execute("UPDATE image_metadata SET metadata_json='{}'")
            response = await client.post(
                PREFIX + "comfy/import", json={"image_id": image_id}
            )
            assert response.status_code == 200, response.text
            assert (
                response.json()["comfyui"]["api_graph"]["4"]["class_type"] == "KSampler"
            )
            assert not response.json()["historical_snapshot"]
        finally:
            await teardown(app, client)

    asyncio.run(run())


def test_gallery_snapshot_survives_deleted_saved_workflow_and_does_not_require_original(
    tmp_path, monkeypatch
):
    async def run():
        app, client, _ = await setup(tmp_path)
        try:
            response = await client.post(
                PREFIX + "comfy/jobs",
                json={"provider_id": "comfy", "model": "workflow", "mode": "text2img"},
            )
            job = await app.state.plugin._comfy.manager.wait(
                response.json()["job"]["id"]
            )
            detail = await app.state.plugin.store.generation_detail(
                job["generation_id"]
            )
            body = {
                "generation_id": detail["id"],
                "image_id": detail["images"][0]["id"],
            }
            prepared = await client.post(PREFIX + "comfy/import", json=body)
            assert prepared.status_code == 200, prepared.text
            assert prepared.json()["matched_model_ref"] == "comfy:workflow"
            assert prepared.json()["historical_snapshot"]
            await empty_workflows(client)

            async def never_read(_image_id):
                raise AssertionError(
                    "A valid execution snapshot must not require image bytes"
                )

            monkeypatch.setattr(
                app.state.plugin.store, "read_workflow_image", never_read
            )
            prepared = await client.post(PREFIX + "comfy/import", json=body)
            assert prepared.status_code == 200, prepared.text
            assert prepared.json()["matched_model_ref"] == ""
            assert prepared.json()["historical_snapshot"]
            assert prepared.json()["comfyui"]["api_graph"]
        finally:
            await teardown(app, client)

    asyncio.run(run())


def test_gallery_rejects_cross_record_image_and_disabled_reuse(tmp_path, monkeypatch):
    async def run():
        app, client, transport = await setup(tmp_path)
        try:
            first, image_id = await imported_image(app)
            second, _ = await imported_image(app, color="red")
            response = await client.post(
                PREFIX + "comfy/import",
                json={"generation_id": second, "image_id": image_id},
            )
            assert response.status_code == 400 and "不属于" in response.text
            original_header = app.state.plugin.store.queries.generation_header

            def restricted_header(row):
                header = original_header(row)
                header["allowed_actions"]["reference"] = False
                return header

            monkeypatch.setattr(
                app.state.plugin.store.queries, "generation_header", restricted_header
            )
            response = await client.post(
                PREFIX + "comfy/import",
                json={"generation_id": first, "image_id": image_id},
            )
            assert response.status_code == 400 and "未允许复用" in response.text
            assert not transport.calls
        finally:
            await teardown(app, client)

    asyncio.run(run())


def test_gallery_reports_no_provider_and_ui_only_graph_separately(tmp_path):
    async def run():
        app, client, _ = await setup(tmp_path)
        try:
            _, image_id = await imported_image(app, api=False)
            response = await client.post(
                PREFIX + "comfy/import", json={"image_id": image_id}
            )
            assert response.status_code == 400 and "界面" in response.text
            await empty_workflows(client, enabled=False)
            response = await client.post(
                PREFIX + "comfy/import", json={"image_id": image_id}
            )
            assert response.status_code == 400 and "没有已保存并启用" in response.text
        finally:
            await teardown(app, client)

    asyncio.run(run())


def test_temporary_prepare_and_execute_preserve_empty_settings_and_persist_snapshot(
    tmp_path,
):
    async def run():
        app, client, transport = await setup(tmp_path)
        transport.unique_jobs = True
        try:
            saved = await empty_workflows(client)
            payload = {
                "id": "workflow",
                "name": "Only this time",
                "native_batch_size": 2,
                "comfyui": config(),
            }
            prepared = await client.post(
                PREFIX + "comfy/import",
                json={"provider_id": "comfy", "temporary_model": payload},
            )
            assert prepared.status_code == 200, prepared.text
            model = prepared.json()["model"]
            assert model["id"].startswith("temporary_") and prepared.json()["temporary"]
            submitted = await client.post(
                PREFIX + "comfy/jobs",
                json={
                    "provider_id": "comfy",
                    "temporary_model": model,
                    "mode": "text2img",
                    "count": 5,
                    "provider": {"id": "comfy", "api_key": "must-not-use"},
                },
            )
            assert submitted.status_code == 200, submitted.text
            job = await app.state.plugin._comfy.manager.wait(
                submitted.json()["job"]["id"]
            )
            assert job["status"] == "succeeded", job
            assert (
                job["request"]["temporary"]
                and job["request"]["model"]["id"] == model["id"]
            )
            assert job["result"]["batch_plan"]["quotas"] == [2, 2, 1]
            assert (await client.get(PREFIX + "settings/get")).json()["webui"] == saved
            calls = [call for call in transport.calls if call[1] == "/prompt"]
            assert len(calls) == 3
            assert all(
                call[2]["headers"]["Authorization"] == "Bearer offline-secret"
                for call in calls
            )
            result = await client.get(PREFIX + "comfy/jobs", params={"id": job["id"]})
            assert result.json()["job"]["temporary"]
        finally:
            await teardown(app, client)

    asyncio.run(run())


@pytest.mark.parametrize("provider_id", ["missing", "comfy"])
def test_temporary_submit_requires_enabled_persisted_provider(tmp_path, provider_id):
    async def run():
        app, client, transport = await setup(tmp_path)
        try:
            await empty_workflows(client, enabled=False)
            response = await client.post(
                PREFIX + "comfy/jobs",
                json={
                    "provider_id": provider_id,
                    "provider": {
                        "id": "comfy",
                        "kind": "comfyui",
                        "base_url": "http://evil.invalid",
                    },
                    "temporary_model": {"name": "Temporary", "comfyui": config()},
                },
            )
            assert response.status_code == 400 and "已保存并启用" in response.text
            assert not transport.calls
        finally:
            await teardown(app, client)

    asyncio.run(run())


def test_temporary_execution_still_checks_dependencies_before_remote_submission(
    tmp_path,
):
    async def run():
        app, client, transport = await setup(tmp_path)
        try:
            await empty_workflows(client)
            value = config()
            value["api_graph"]["99"] = {"class_type": "MissingThirdParty", "inputs": {}}
            response = await client.post(
                PREFIX + "comfy/jobs",
                json={
                    "provider_id": "comfy",
                    "temporary_model": {"comfyui": value},
                    "mode": "text2img",
                },
            )
            assert response.status_code == 200, response.text
            job = await app.state.plugin._comfy.manager.wait(
                response.json()["job"]["id"]
            )
            assert job["status"] == "failed" and "MissingThirdParty" in job["error"]
            assert not any(call[1] == "/prompt" for call in transport.calls)
        finally:
            await teardown(app, client)

    asyncio.run(run())
