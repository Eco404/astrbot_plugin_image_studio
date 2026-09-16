"""Temporary workflow jobs use immutable models without editing provider settings."""

from __future__ import annotations

import asyncio
import copy
from collections import Counter
from dataclasses import replace

import pytest
from astrbot_plugin_image_studio import comfyui_runtime
from astrbot_plugin_image_studio.comfyui import ComfyExecutionError
from astrbot_plugin_image_studio.comfyui_runtime import ComfyRuntime
from astrbot_plugin_image_studio.models import ImageProvider, ReferenceImage
from astrbot_plugin_image_studio.providers import ProviderError
from astrbot_plugin_image_studio.tests.test_comfy_runtime import (
    FakeComfyClient,
    image,
    runtime_fixture,
    workflow,
)


async def empty_provider_runtime(tmp_path, monkeypatch, *, provider_limit=2):
    runtime, service, _, client = await runtime_fixture(tmp_path, monkeypatch)
    provider = ImageProvider.from_mapping(
        {
            "id": "comfy",
            "name": "Saved connection only",
            "kind": "comfyui",
            "base_url": "http://comfy.invalid:8188",
            "api_key": "connection-secret",
            "max_concurrent_generations": provider_limit,
            "models": [],
        }
    )
    assert provider.models == ()
    service.update_settings(replace(service.settings, providers=(provider,)))
    return runtime, service, provider, client


def temporary_model(
    provider,
    *,
    identifier="temporary_" + "a" * 32,
    config=None,
    native=1,
    concurrency=1,
    tool=True,
):
    raw = provider.public_dict()
    raw["models"] = [
        {
            "id": identifier,
            "name": "Only this run",
            "comfyui": config or workflow(),
            "native_batch_size": native,
            "native_batch_size_source": "manual",
            "max_concurrent_requests": concurrency,
            "parameters": {
                "count": {"type": "integer", "default": 1, "min": 1, "max": 16}
            },
            "tool": {"enabled": tool},
        }
    ]
    return ImageProvider.from_mapping(raw).models[0]


async def submit(runtime, provider, model, **values):
    return await runtime.submit(
        provider=provider,
        model=model,
        mode="text2img",
        prompt="",
        temporary=True,
        **values,
    )


def test_temporary_workflow_executes_with_zero_saved_models_and_keeps_settings_clean(
    tmp_path, monkeypatch
):
    async def run():
        runtime, service, provider, client = await empty_provider_runtime(
            tmp_path, monkeypatch
        )
        model = temporary_model(provider)
        before = copy.deepcopy(service.settings)
        try:
            job = await submit(runtime, provider, model)
            done = await runtime.manager.wait(job["id"])
            assert done["status"] == "succeeded", done
            assert done["request"]["temporary"] is True
            assert "temporary" not in done["request"]["values"]
            result = await runtime.result(done)
            assert len(result.images) == 1
            assert result.request.model == model.id
            assert result.provider.models[0].id == model.id
            assert runtime.public_job(done)["temporary"] is True
            assert (
                service.settings == before
                and service.settings.providers[0].models == ()
            )
            assert len([call for call in client.calls if call[0] == "submit"]) == 1
            detail = await service.store.generation_detail(
                result.generation_id, include_assets=False
            )
            snapshot = detail["images"][0]["supplemental"]["comfyui"]
            assert snapshot["temporary"] is True
            assert snapshot["workflow_name"] == "Only this run"
            assert snapshot["parameters_schema"]
            assert b"connection-secret" not in runtime.store.db_path.read_bytes()
        finally:
            await runtime.close()

    asyncio.run(run())


def test_temporary_job_freezes_reference_order_workflow_and_parameter_schema(
    tmp_path, monkeypatch
):
    async def run():
        runtime, service, provider, client = await empty_provider_runtime(
            tmp_path, monkeypatch
        )
        config = workflow(reference_count=2)
        model = temporary_model(provider, config=config)
        references = (
            ReferenceImage("base", "base.png", image("red").data, "image/png"),
            ReferenceImage("style", "style.png", image("blue").data, "image/png"),
        )
        before = copy.deepcopy(service.settings)
        client.release_wait.clear()
        try:
            job = await runtime.submit(
                provider=provider,
                model=model,
                mode="img2img",
                prompt="",
                references=references,
                temporary=True,
            )
            await asyncio.wait_for(client.wait_started.wait(), 3)
            model.comfyui["api_graph"]["1"]["inputs"]["text"] = (
                "edited after submission"
            )
            model.parameters["count"]["default"] = 9
            client.release_wait.set()
            done = await runtime.manager.wait(job["id"])
            assert done["status"] == "succeeded"
            assert done["request"]["model"]["parameters"]["count"]["default"] == 1
            revision = await runtime.store.get_revision(done["revision_id"])
            assert revision["api_graph"]["1"]["inputs"]["text"] == "fixed prompt"
            assert await runtime.store.load_references(job["id"]) == references
            assert (
                next(call[1] for call in client.calls if call[0] == "upload")
                == references
            )
            assert service.settings == before
        finally:
            await runtime.close()

    asyncio.run(run())


def test_temporary_job_restarts_without_model_registration_or_second_submission(
    tmp_path, monkeypatch
):
    async def run():
        runtime, service, provider, client = await empty_provider_runtime(
            tmp_path, monkeypatch
        )
        model = temporary_model(provider, native=3)
        client.release_wait.clear()
        job = await submit(runtime, provider, model, count=2)
        await asyncio.wait_for(client.wait_started.wait(), 3)
        await runtime.close()
        restarted = ComfyRuntime(service)
        try:
            client.release_wait.set()
            await restarted.start()
            done = await restarted.manager.wait(job["id"])
            assert done["status"] == "succeeded"
            assert len((await restarted.result(done)).images) == 2
            assert done["result"]["collection_limit"] == 2
            assert len([call for call in client.calls if call[0] == "submit"]) == 1
            assert len([call for call in client.calls if call[0] == "wait"]) == 2
            assert service.settings.providers[0].models == ()
            assert restarted.public_job(done)["temporary"] is True
        finally:
            await restarted.close()

    asyncio.run(run())


def test_temporary_fixed_batches_keep_quotas_and_group_into_one_history(
    tmp_path, monkeypatch
):
    async def run():
        runtime, service, provider, client = await empty_provider_runtime(
            tmp_path, monkeypatch
        )
        model = temporary_model(provider, native=2)
        try:
            job = await submit(runtime, provider, model, count=5)
            done = await asyncio.wait_for(runtime.manager.wait(job["id"]), 5)
            assert done["status"] == "succeeded", done
            result = await runtime.result(done)
            assert len(result.images) == 5
            assert done["result"]["batch_plan"]["quotas"] == [2, 2, 1]
            assert sorted(
                call[3] for call in client.calls if call[0] == "download"
            ) == [1, 2, 2]
            assert len([call for call in client.calls if call[0] == "submit"]) == 3
            assert all(
                call[1]["2"]["inputs"]["batch_size"] == 3
                for call in client.calls
                if call[0] == "submit"
            )
            jobs = await runtime.store.list_jobs()
            assert len(jobs) == 4 and all(item["request"]["temporary"] for item in jobs)
            assert all(
                item.effective_parameters["_comfyui"]["temporary"]
                for item in result.images
            )
            with service.store._connect() as connection:
                assert (
                    connection.execute("SELECT COUNT(*) FROM generations").fetchone()[0]
                    == 1
                )
                assert (
                    connection.execute(
                        "SELECT COUNT(*) FROM generation_images"
                    ).fetchone()[0]
                    == 5
                )
            assert service.settings.providers[0].models == ()
        finally:
            await runtime.close()

    asyncio.run(run())


class ConcurrencyClient(FakeComfyClient):
    def __init__(self, store):
        super().__init__()
        self.store = store
        self.active = 0
        self.peak = 0
        self.by_model = Counter()
        self.peak_by_model = Counter()
        self.started = asyncio.Queue()

    async def wait(self, provider, remote_id, *, on_progress, client_id):
        job = await self.store.get_job(client_id)
        key = job["model_id"]
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.by_model[key] += 1
        self.peak_by_model[key] = max(self.peak_by_model[key], self.by_model[key])
        await on_progress({"status": "running"})
        self.started.put_nowait(key)
        try:
            await self.release_wait.wait()
            return self.history
        finally:
            self.active -= 1
            self.by_model[key] -= 1


@pytest.mark.parametrize("provider_limit", [1, 2])
def test_temporary_workflows_share_provider_slots_and_stable_model_slots(
    tmp_path, monkeypatch, provider_limit
):
    async def run():
        runtime, service, provider, _ = await empty_provider_runtime(
            tmp_path, monkeypatch, provider_limit=provider_limit
        )
        client = ConcurrencyClient(runtime.store)
        client.release_wait.clear()
        monkeypatch.setattr(comfyui_runtime, "ComfyClient", lambda _: client)
        models = [
            temporary_model(provider, identifier="temporary_" + letter * 32)
            for letter in ("a", "b")
        ]
        try:
            jobs = [
                await submit(runtime, provider, model)
                for model in (models[0], models[0], models[1], models[1])
            ]
            started = [
                await asyncio.wait_for(client.started.get(), 3)
                for _ in range(provider_limit)
            ]
            assert len(set(started)) == provider_limit
            client.release_wait.set()
            done = await asyncio.gather(
                *(runtime.manager.wait(job["id"]) for job in jobs)
            )
            assert all(job["status"] == "succeeded" for job in done)
            assert client.peak == provider_limit
            assert all(client.peak_by_model[model.id] == 1 for model in models)
            assert service.settings.providers[0].models == ()
        finally:
            client.release_wait.set()
            await runtime.close()

    asyncio.run(run())


def test_disabled_provider_blocks_temporary_resume_until_reenabled(
    tmp_path, monkeypatch
):
    async def run():
        runtime, service, provider, client = await empty_provider_runtime(
            tmp_path, monkeypatch
        )
        client.release_wait.clear()
        job = await submit(runtime, provider, temporary_model(provider))
        await asyncio.wait_for(client.wait_started.wait(), 3)
        await runtime.close()
        disabled = ImageProvider.from_mapping(
            {**provider.public_dict(), "enabled": False}
        )
        service.update_settings(replace(service.settings, providers=(disabled,)))
        restarted = ComfyRuntime(service)
        try:
            calls = len(client.calls)
            await restarted.start()
            failed = await restarted.manager.wait(job["id"])
            assert failed["status"] == "failed" and "停用" in failed["error"]
            assert len(client.calls) == calls
            service.update_settings(replace(service.settings, providers=(provider,)))
            client.release_wait.set()
            await restarted.manager.resume(job["id"])
            done = await restarted.manager.wait(job["id"])
            assert done["status"] == "succeeded"
            assert len([call for call in client.calls if call[0] == "submit"]) == 1
        finally:
            await restarted.close()

    asyncio.run(run())


def test_temporary_model_still_enforces_tool_access(tmp_path, monkeypatch):
    async def run():
        runtime, _, provider, client = await empty_provider_runtime(
            tmp_path, monkeypatch
        )
        try:
            with pytest.raises(ProviderError, match="未向 LLM"):
                await runtime.generate(
                    provider=provider,
                    model=temporary_model(provider, tool=False),
                    mode="text2img",
                    prompt="",
                    temporary=True,
                    source="llm_tool",
                )
            assert not client.calls
        finally:
            await runtime.close()

    asyncio.run(run())


def test_temporary_unknown_submission_cannot_be_resubmitted_on_restart(
    tmp_path, monkeypatch
):
    async def run():
        runtime, service, provider, client = await empty_provider_runtime(
            tmp_path, monkeypatch
        )
        client.submit_error = ComfyExecutionError(
            "response lost", unknown_submission=True
        )
        job = await submit(runtime, provider, temporary_model(provider))
        unknown = await runtime.manager.wait(job["id"])
        assert unknown["status"] == "unknown"
        assert runtime.public_job(unknown)["temporary"] is True
        await runtime.close()
        restarted = ComfyRuntime(service)
        try:
            await restarted.start()
            with pytest.raises(ValueError, match="远端编号"):
                await restarted.manager.resume(job["id"])
            assert (await restarted.store.get_job(job["id"]))["status"] == "unknown"
            assert len([call for call in client.calls if call[0] == "submit"]) == 1
            assert service.settings.providers[0].models == ()
        finally:
            await restarted.close()

    asyncio.run(run())


def test_temporary_job_recovery_never_contacts_changed_provider_origin(
    tmp_path, monkeypatch
):
    async def run():
        runtime, service, provider, client = await empty_provider_runtime(
            tmp_path, monkeypatch
        )
        client.release_wait.clear()
        job = await submit(runtime, provider, temporary_model(provider))
        await asyncio.wait_for(client.wait_started.wait(), 3)
        await runtime.close()
        changed = ImageProvider.from_mapping(
            {**provider.public_dict(), "base_url": "http://different.invalid:8188"}
        )
        service.update_settings(replace(service.settings, providers=(changed,)))
        restarted = ComfyRuntime(service)
        try:
            calls_before = len(client.calls)
            await restarted.start()
            done = await restarted.manager.wait(job["id"])
            assert done["status"] == "failed"
            assert "地址已改变" in done["error"]
            assert len(client.calls) == calls_before
        finally:
            await restarted.close()

    asyncio.run(run())
