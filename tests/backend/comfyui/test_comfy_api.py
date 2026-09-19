"""ComfyUI Page API integration using real handlers and an offline transport."""

from __future__ import annotations

import asyncio
import io
import json
from urllib.parse import urlsplit

import httpx
import pytest
from astrbot_plugin_image_studio.backend.providers.comfyui import (
    job_store as comfy_jobs_module,
)
from astrbot_plugin_image_studio.backend.providers.comfyui import (
    storage as comfy_storage_module,
)
from astrbot_plugin_image_studio.backend.generation.comfyui_runtime import (
    connection_fingerprint,
)
from astrbot_plugin_image_studio.tests.backend.comfyui.test_comfy_provider import (
    Response,
    config,
    definitions,
    graph,
    history,
    png,
)
from astrbot_plugin_image_studio.tests.support.webui_harness import create_app
from PIL import Image, PngImagePlugin

PREFIX = "/astrbot_plugin_image_studio/"


class OfflineComfyTransport:
    def __init__(self):
        self.calls = []
        self.submission_errors = {}
        self.cancel_error = False
        self.uploaded_images = []
        self.unique_jobs = False

    def request(self, method, url, **kwargs):
        path = urlsplit(url).path
        self.calls.append((method, path, kwargs))
        if path == "/object_info":
            return Response(definitions())
        if path == "/upload/image":
            uploaded = next(
                value
                for disposition, _headers, value in kwargs["data"]._fields
                if disposition.get("name") == "image"
            )
            self.uploaded_images.append(uploaded)
            return Response(
                {"name": f"uploaded-{len(self.uploaded_images)}.png", "subfolder": ""}
            )
        if path == "/prompt":
            remote_id = (
                "remote-" + kwargs["json"]["client_id"]
                if self.unique_jobs
                else "remote-job"
            )
            return Response(
                {"prompt_id": remote_id, "node_errors": self.submission_errors}
            )
        if path.startswith("/history/remote-"):
            return Response({path.split("/")[-1]: history("a.png", "b.png")})
        if path == "/view":
            return Response(raw=png())
        if path == "/api/jobs/remote-job/cancel":
            return (
                Response({"error": "maintenance"}, status=503)
                if self.cancel_error
                else Response({"cancelled": True})
            )
        if path == "/queue":
            return Response({"queue_running": [], "queue_pending": []})
        raise AssertionError(f"Unexpected ComfyUI operation {method} {path}")


def provider_mapping():
    return {
        "id": "comfy",
        "name": "Offline Comfy",
        "kind": "comfyui",
        "base_url": "http://comfy.invalid",
        "api_key": "offline-secret",
        "models": [{"id": "workflow", "name": "Raw workflow", "comfyui": config()}],
    }


async def setup(tmp_path):
    app = await create_app(tmp_path, seed=False)
    transport = OfflineComfyTransport()
    app.state.plugin._service.executor.session = transport
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    webui = (await client.get(PREFIX + "settings/get")).json()["webui"]
    webui["providers"].append(provider_mapping())
    response = await client.post(
        PREFIX + "settings/save",
        json={"settings_revision": webui["revision"], "webui": webui},
    )
    assert response.status_code == 200, response.text
    return app, client, transport


async def teardown(app, client):
    if getattr(app.state.plugin, "_comfy", None):
        await app.state.plugin._comfy.close()
    await app.state.plugin._external_gallery.close()
    await app.state.plugin.store.close()
    await client.aclose()


def test_api_import_accepts_json_and_png_but_rejects_ui_only(tmp_path):
    async def run():
        app, client, _ = await setup(tmp_path)
        try:
            api_graph = graph()
            api_graph["4"]["inputs"]["seed"] = 18446744073709551615
            response = await client.post(
                PREFIX + "comfy/import", json={"content": json.dumps(api_graph)}
            )
            assert response.status_code == 200, response.text
            imported = response.json()
            assert (
                json.loads(imported["comfyui"]["api_graph_json"])["4"]["inputs"]["seed"]
                == 18446744073709551615
            )
            assert (
                imported["suggestions"]["node_4_seed"]["default"]
                == "18446744073709551615"
            )
            metadata = PngImagePlugin.PngInfo()
            metadata.add_text("prompt", json.dumps(api_graph))
            metadata.add_text("workflow", json.dumps({"nodes": [], "links": []}))
            image = io.BytesIO()
            Image.new("RGB", (8, 8), "blue").save(image, "PNG", pnginfo=metadata)
            response = await client.post(
                PREFIX + "comfy/import",
                files={"file": ("workflow.png", image.getvalue(), "image/png")},
            )
            assert response.status_code == 200, response.text
            assert (
                json.loads(response.json()["comfyui"]["api_graph_json"])["4"]["inputs"][
                    "seed"
                ]
                == 18446744073709551615
            )
            response = await client.post(
                PREFIX + "comfy/import", json={"content": {"nodes": [], "links": []}}
            )
            assert response.status_code == 400 and "界面" in response.json()["message"]
        finally:
            await teardown(app, client)

    asyncio.run(run())


def test_api_inspect_reports_exact_missing_nodes_and_model_choices(tmp_path):
    async def run():
        app, client, transport = await setup(tmp_path)
        try:
            value = config()
            value["api_graph"]["1"]["inputs"]["ckpt_name"] = "missing.safetensors"
            value["api_graph"]["77"] = {"class_type": "MissingPluginNode", "inputs": {}}
            response = await client.post(
                PREFIX + "comfy/inspect",
                json={"provider_id": "comfy", "comfyui": value},
            )
            assert response.status_code == 200, response.text
            report = response.json()
            assert report["status"] == "blocked"
            assert {issue["node_id"] for issue in report["issues"]} == {"1", "77"}
            assert report["models"][0]["options"] == [
                "model.safetensors",
                "other.safetensors",
            ]
            assert all(path != "/prompt" for _, path, _ in transport.calls)
        finally:
            await teardown(app, client)

    asyncio.run(run())


def test_api_job_submit_poll_gallery_result_and_no_implicit_prompt(tmp_path):
    async def run():
        app, client, transport = await setup(tmp_path)
        try:
            response = await client.post(
                PREFIX + "comfy/jobs",
                json={
                    "provider_id": "comfy",
                    "model": "workflow",
                    "prompt": "",
                    "mode": "text2img",
                },
            )
            assert response.status_code == 200, response.text
            job_id = response.json()["job"]["id"]
            await app.state.plugin._comfy.manager.wait(job_id)
            response = await client.get(PREFIX + "comfy/jobs", params={"id": job_id})
            assert response.status_code == 200, response.text
            job = response.json()["job"]
            assert job["status"] == "succeeded", job
            assert job["generation_id"]
            result = job["result"]
            assert len(result["images"]) == 1
            assert result["generation_id"] == job["generation_id"]
            detail = (
                await client.get(PREFIX + "gallery/detail/" + job["generation_id"])
            ).json()
            assert detail["provider_kind"] == "comfyui"
            assert len(detail["images"]) == 1
            assert len([call for call in transport.calls if call[1] == "/prompt"]) == 1
            assert len([call for call in transport.calls if call[1] == "/view"]) == 1
            refreshed = (await client.get(PREFIX + "comfy/jobs")).json()["jobs"]
            assert job_id not in {item["id"] for item in refreshed}
            # Queue filtering must leave direct result access and gallery data intact.
            assert (
                await client.get(PREFIX + "comfy/jobs", params={"id": job_id})
            ).json()["job"]["generation_id"] == job["generation_id"]
        finally:
            await teardown(app, client)

    asyncio.run(run())


def test_api_unbound_workflow_count_plans_runs_and_caps_downloads(tmp_path):
    async def run():
        app, client, transport = await setup(tmp_path)
        transport.unique_jobs = True
        try:
            webui = (await client.get(PREFIX + "settings/get")).json()["webui"]
            model = next(item for item in webui["providers"] if item["id"] == "comfy")[
                "models"
            ][0]
            model["native_batch_size"] = 2
            model["native_batch_size_source"] = "manual"
            response = await client.post(
                PREFIX + "settings/save",
                json={"settings_revision": webui["revision"], "webui": webui},
            )
            assert response.status_code == 200, response.text
            response = await client.post(
                PREFIX + "comfy/jobs",
                json={
                    "provider_id": "comfy",
                    "model": "workflow",
                    "mode": "text2img",
                    "count": 5,
                },
            )
            assert response.status_code == 200, response.text
            job_id = response.json()["job"]["id"]
            completed = await app.state.plugin._comfy.manager.wait(job_id)
            assert completed["status"] == "succeeded", completed
            assert completed["result"]["batch_plan"]["quotas"] == [2, 2, 1]
            result = (
                await client.get(PREFIX + "comfy/jobs", params={"id": job_id})
            ).json()["job"]["result"]
            assert len(result["images"]) == 5
            submissions = [call for call in transport.calls if call[1] == "/prompt"]
            assert len(submissions) == 3
            assert all(
                call[2]["json"]["prompt"]["3"]["inputs"]["batch_size"] == 2
                for call in submissions
            )
            assert len([call for call in transport.calls if call[1] == "/view"]) == 5
        finally:
            await teardown(app, client)

    asyncio.run(run())


def test_api_workflow_save_uses_real_settings_and_rejects_duplicate_ids(tmp_path):
    async def run():
        app, client, _ = await setup(tmp_path)
        try:
            payload = {
                "provider_id": "comfy",
                "model": {
                    "id": "second",
                    "name": "Second workflow",
                    "comfyui": config(),
                },
            }
            response = await client.post(PREFIX + "comfy/workflows", json=payload)
            assert response.status_code == 200, response.text
            assert response.json()["model_ref"] == "comfy:second"
            settings = (await client.get(PREFIX + "settings/get")).json()["webui"]
            saved = next(
                provider
                for provider in settings["providers"]
                if provider["id"] == "comfy"
            )
            assert {model["id"] for model in saved["models"]} == {"workflow", "second"}
            assert (
                await client.post(PREFIX + "comfy/workflows", json=payload)
            ).status_code == 400
        finally:
            await teardown(app, client)

    asyncio.run(run())


def test_api_submit_rejects_nonexistent_model_before_remote_task(tmp_path):
    async def run():
        app, client, transport = await setup(tmp_path)
        try:
            response = await client.post(
                PREFIX + "comfy/jobs", json={"provider_id": "comfy", "model": "missing"}
            )
            assert (
                response.status_code == 400
                and "工作流不存在" in response.json()["message"]
            )
            assert not transport.calls
        finally:
            await teardown(app, client)

    asyncio.run(run())


def test_api_partial_node_validation_never_reports_full_success(tmp_path):
    async def run():
        app, client, transport = await setup(tmp_path)
        transport.submission_errors = {
            "77": {"class_type": "SaveImage", "errors": [{"message": "Missing input"}]}
        }
        try:
            response = await client.post(
                PREFIX + "comfy/jobs",
                json={"provider_id": "comfy", "model": "workflow", "mode": "text2img"},
            )
            job_id = response.json()["job"]["id"]
            await app.state.plugin._comfy.manager.wait(job_id)
            result = (
                await client.get(PREFIX + "comfy/jobs", params={"id": job_id})
            ).json()["job"]
            assert result["status"] == "partial", result
            assert "77" in result["result"]["warning"]
            assert len(result["result"]["images"]) == 1
        finally:
            await teardown(app, client)

    asyncio.run(run())


def test_api_cancel_upstream_failure_is_presentable_and_preserves_job(tmp_path):
    async def run():
        app, client, transport = await setup(tmp_path)
        transport.cancel_error = True
        try:
            runtime = app.state.plugin._comfy_or_raise()
            provider = app.state.plugin._settings.provider("comfy")
            revision_id = await runtime.store.save_revision(config())
            job = await runtime.store.create_job(
                provider_id="comfy",
                model_id="workflow",
                revision_id=revision_id,
                request={
                    "connection": connection_fingerprint(provider),
                    "model": provider.models[0].public_dict(),
                    "values": {},
                },
            )
            await runtime.store.update_job(
                job["id"], status="running", remote_id="remote-job"
            )
            response = await client.post(
                PREFIX + "comfy/jobs/cancel", json={"id": job["id"]}
            )
            assert response.status_code == 400, response.text
            assert "503" in response.json()["message"]
            assert (await runtime.store.get_job(job["id"]))["status"] == "running"
            assert all(path != "/interrupt" for _, path, _ in transport.calls)
        finally:
            await teardown(app, client)

    asyncio.run(run())


def workflow_with_references(count):
    value = config()
    for index in range(count):
        node_id = str(index + 10)
        value["api_graph"][node_id] = {
            "class_type": "LoadImage",
            "inputs": {"image": "server-existing.png"},
        }
        value["bindings"][f"reference_{index}"] = {
            "source": "reference",
            "type": "image",
            "reference_index": index,
            "node_id": node_id,
            "input_name": "image",
            "required": True,
        }
    return value


async def stage_page_images(client, count):
    reference_ids, originals = [], []
    for index, color in enumerate(("red", "blue", "green")[:count]):
        output = io.BytesIO()
        Image.new("RGB", (12, 16), color).save(output, "PNG")
        content = output.getvalue()
        response = await client.post(
            PREFIX + "studio/reference/upload",
            files={"file": (f"reference-{index}.png", content, "image/png")},
        )
        assert response.status_code == 200, response.text
        reference_ids.append(response.json()["id"])
        originals.append(content)
    return reference_ids, originals


async def change_current_reference_count(client, count):
    settings = (await client.get(PREFIX + "settings/get")).json()["webui"]
    provider = next(item for item in settings["providers"] if item["id"] == "comfy")
    provider["models"][0]["comfyui"] = workflow_with_references(count)
    response = await client.post(
        PREFIX + "settings/save",
        json={"settings_revision": settings["revision"], "webui": settings},
    )
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("current_reference_count", [0, 1])
def test_api_historical_workflow_uses_its_reference_capability_after_template_change(
    tmp_path, current_reference_count
):
    async def run():
        app, client, transport = await setup(tmp_path)
        try:
            await change_current_reference_count(client, current_reference_count)
            reference_ids, originals = await stage_page_images(client, 2)
            response = await client.post(
                PREFIX + "comfy/jobs",
                json={
                    "provider_id": "comfy",
                    "model": "workflow",
                    "mode": "img2img",
                    "prompt": "",
                    "reference_ids": reference_ids,
                    "comfyui": workflow_with_references(2),
                },
            )
            assert response.status_code == 200, response.text
            job_id = response.json()["job"]["id"]
            done = await app.state.plugin._comfy.manager.wait(job_id)
            assert done["status"] == "succeeded", done
            assert transport.uploaded_images == originals
            submitted = next(
                kwargs["json"]["prompt"]
                for _, path, kwargs in transport.calls
                if path == "/prompt"
            )
            assert submitted["10"]["inputs"]["image"] == "uploaded-1.png"
            assert submitted["11"]["inputs"]["image"] == "uploaded-2.png"
            saved_refs = await app.state.plugin._comfy.store.load_references(job_id)
            assert [item.id for item in saved_refs] == reference_ids
            assert [item.data for item in saved_refs] == originals
            # The override is per request; it must not edit the live template.
            current = app.state.plugin._settings.provider("comfy").models[0]
            assert (
                current.comfyui_capabilities["max_reference_images"]
                == current_reference_count
            )
        finally:
            await teardown(app, client)

    asyncio.run(run())


@pytest.mark.parametrize("override", [False, True])
def test_api_historical_reference_override_does_not_bypass_mode_or_count_limits(
    tmp_path, override
):
    async def run():
        app, client, transport = await setup(tmp_path)
        try:
            reference_ids, _ = await stage_page_images(client, 3 if override else 2)
            payload = {
                "provider_id": "comfy",
                "model": "workflow",
                "mode": "img2img",
                "prompt": "",
                "reference_ids": reference_ids,
            }
            if override:
                payload["comfyui"] = workflow_with_references(2)
            response = await client.post(PREFIX + "comfy/jobs", json=payload)
            assert response.status_code == 400, response.text
            message = response.json()["message"]
            assert (
                "2" in message if override else "模式" in message or "图生图" in message
            )
            assert not transport.calls
            assert not await app.state.plugin._comfy_or_raise().store.list_jobs()
        finally:
            await teardown(app, client)

    asyncio.run(run())


def test_api_failed_task_dismissal_persists_and_keeps_diagnostic_record(tmp_path):
    async def run():
        app, client, transport = await setup(tmp_path)
        try:
            broken = config()
            broken["api_graph"]["99"] = {"class_type": "MissingNode", "inputs": {}}
            response = await client.post(
                PREFIX + "comfy/jobs",
                json={"provider_id": "comfy", "model": "workflow", "comfyui": broken},
            )
            assert response.status_code == 200, response.text
            identifier = response.json()["job"]["id"]
            before = await app.state.plugin._comfy.manager.wait(identifier)
            assert before["status"] == "failed"
            assert identifier in {
                item["id"]
                for item in (await client.get(PREFIX + "comfy/jobs")).json()["jobs"]
            }
            dismissed = await client.post(
                PREFIX + "comfy/jobs/dismiss", json={"id": identifier}
            )
            assert dismissed.status_code == 200, dismissed.text
            assert dismissed.json()["job"]["status"] == "failed"
            assert (await client.get(PREFIX + "comfy/jobs")).json()["jobs"] == []
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as reloaded_page:
                assert (await reloaded_page.get(PREFIX + "comfy/jobs")).json()[
                    "jobs"
                ] == []
                direct = await reloaded_page.get(
                    PREFIX + "comfy/jobs", params={"id": identifier}
                )
                assert direct.status_code == 200
                assert direct.json()["job"]["error"] == before["error"]
            after = await app.state.plugin._comfy.store.get_job(identifier)
            assert after["finished_at"] == before["finished_at"]
            assert after["request"] == before["request"]
            assert after["result"]["queue_dismissed"] is True
            assert not [call for call in transport.calls if call[1] == "/prompt"]
        finally:
            await teardown(app, client)

    asyncio.run(run())


def test_api_queue_dismiss_rejects_active_missing_and_invalid_requests(tmp_path):
    async def run():
        app, client, transport = await setup(tmp_path)
        try:
            runtime = app.state.plugin._comfy_or_raise()
            revision = await runtime.store.save_revision(config())
            job = await runtime.store.create_job(
                provider_id="comfy",
                model_id="workflow",
                revision_id=revision,
                request={},
            )
            before = await runtime.store.update_job(job["id"], status="running")
            response = await client.post(
                PREFIX + "comfy/jobs/dismiss", json={"id": job["id"]}
            )
            assert response.status_code == 400
            assert "仍在进行中" in response.json()["message"]
            assert await runtime.store.get_job(job["id"]) == before
            assert job["id"] in {
                item["id"]
                for item in (await client.get(PREFIX + "comfy/jobs")).json()["jobs"]
            }
            for payload in ({"id": "missing"}, [], "invalid"):
                assert (
                    await client.post(PREFIX + "comfy/jobs/dismiss", json=payload)
                ).status_code == 400
            assert not transport.calls
        finally:
            await teardown(app, client)

    asyncio.run(run())


@pytest.mark.parametrize("status", ["queued", "running", "failed", "unknown"])
def test_api_single_job_poll_does_not_decode_workflow_bodies(
    tmp_path, monkeypatch, status
):
    async def run():
        app, client, _ = await setup(tmp_path)
        try:
            runtime = app.state.plugin._comfy_or_raise()
            revision = await runtime.store.save_revision(config())
            job = await runtime.store.create_job(
                provider_id="comfy",
                model_id="workflow",
                revision_id=revision,
                request={"model": {"name": "Observed workflow", "comfyui": config()}},
            )
            await runtime.store.update_job(
                job["id"],
                status=status,
                result={"api_graph": graph(), "progress": {"value": 2, "max": 10}},
            )

            def unexpected(*_args, **_kwargs):
                pytest.fail("an unfinished single-job poll decoded a workflow body")

            monkeypatch.setattr(comfy_jobs_module, "load_payload", unexpected)
            monkeypatch.setattr(comfy_storage_module, "load_payload", unexpected)
            response = await client.get(PREFIX + "comfy/jobs", params={"id": job["id"]})
            assert response.status_code == 200, response.text
            payload = response.json()["job"]
            assert payload["status"] == status
            assert payload["model_name"] == "Observed workflow"
            assert payload["progress"] == {"value": 2, "max": 10}
            assert "result" not in payload
        finally:
            await teardown(app, client)

    asyncio.run(run())


@pytest.mark.parametrize("second_status", [None, "running"])
def test_api_completed_poll_rechecks_disappearing_or_changed_record(
    tmp_path, monkeypatch, second_status
):
    async def run():
        app, client, _ = await setup(tmp_path)
        try:
            runtime = app.state.plugin._comfy_or_raise()
            calls = []

            async def read(identifier, *, light=False):
                calls.append(light)
                if light:
                    return {"id": identifier, "status": "succeeded"}
                return (
                    {"id": identifier, "status": second_status}
                    if second_status
                    else None
                )

            async def unexpected(_job):
                pytest.fail("result serializer used a stale terminal status")

            monkeypatch.setattr(runtime.store, "get_job", read)
            monkeypatch.setattr(runtime, "result", unexpected)
            response = await client.get(PREFIX + "comfy/jobs", params={"id": "changed"})
            assert calls == [True, False]
            if second_status is None:
                assert response.status_code == 404
            else:
                assert response.status_code == 200
                assert response.json()["job"]["status"] == second_status
                assert "result" not in response.json()["job"]
        finally:
            await teardown(app, client)

    asyncio.run(run())


def test_api_explicit_resume_reveals_dismissed_remote_task_without_resubmit(
    tmp_path, monkeypatch
):
    async def run():
        app, client, transport = await setup(tmp_path)
        release = asyncio.Event()
        try:
            runtime = app.state.plugin._comfy_or_raise()
            provider = app.state.plugin._settings.provider("comfy")
            revision = await runtime.store.save_revision(config())
            job = await runtime.store.create_job(
                provider_id="comfy",
                model_id="workflow",
                revision_id=revision,
                request={
                    "connection": connection_fingerprint(provider),
                    "model": provider.models[0].public_dict(),
                    "values": {"mode": "text2img", "prompt": ""},
                },
            )
            await runtime.store.update_job(
                job["id"],
                status="failed",
                remote_id="remote-job",
                error="previous connection timeout",
            )
            assert (
                await client.post(PREFIX + "comfy/jobs/dismiss", json={"id": job["id"]})
            ).status_code == 200
            assert (await client.get(PREFIX + "comfy/jobs")).json()["jobs"] == []
            original_run = runtime.manager.run

            async def hold_resume(value):
                await release.wait()
                return await original_run(value)

            monkeypatch.setattr(runtime.manager, "run", hold_resume)
            response = await client.post(
                PREFIX + "comfy/jobs/resume", json={"id": job["id"]}
            )
            assert response.status_code == 200, response.text
            queued = (await client.get(PREFIX + "comfy/jobs")).json()["jobs"]
            assert [item["id"] for item in queued] == [job["id"]]
            assert queued[0]["status"] == "submitted"
            resumed = await runtime.store.get_job(job["id"])
            assert "queue_dismissed" not in resumed["result"]
            release.set()
            assert (await runtime.manager.wait(job["id"]))["status"] == "succeeded"
            assert (await client.get(PREFIX + "comfy/jobs")).json()["jobs"] == []
            assert not [call for call in transport.calls if call[1] == "/prompt"]
        finally:
            release.set()
            await teardown(app, client)

    asyncio.run(run())
