"""Upgrade fixtures distinguish already authorized legacy jobs from new requests."""

from __future__ import annotations

import asyncio
import copy
from dataclasses import replace

import pytest
from astrbot_plugin_image_studio.backend.providers.comfyui import (
    runtime as comfyui_runtime,
)
from astrbot_plugin_image_studio.backend.providers.comfyui.runtime import (
    ComfyRuntime,
    connection_fingerprint,
)
from astrbot_plugin_image_studio.backend.models import ImageProvider
from astrbot_plugin_image_studio.tests.backend.test_comfy_runtime import (
    FakeComfyClient,
    image,
    runtime_fixture,
    workflow,
)


def old_provider(*, bound=False, native=1):
    """No execution_policy in this persisted model or its workflow revision."""
    return ImageProvider.from_mapping(
        {
            "id": "comfy",
            "name": "Before fixed scheduling",
            "kind": "comfyui",
            "base_url": "http://comfy.invalid:8188",
            "models": [
                {
                    "id": "workflow",
                    "name": "Legacy workflow",
                    "comfyui": workflow(count_bound=bound),
                    "native_batch_size": native,
                    "native_batch_size_source": "manual",
                    "max_concurrent_requests": 2,
                    "parameters": {
                        "count": {"type": "integer", "default": 1, "min": 1, "max": 16}
                    },
                    "tool": {"enabled": True},
                }
            ],
        },
        migrate_comfyui=False,
    )


async def submit_old_job(runtime, provider, *, count=1):
    snapshot = provider.models[0].public_dict()
    config = snapshot["comfyui"]
    assert "execution_policy" not in config
    return await runtime.manager.submit(
        provider_id=provider.id,
        model_id=provider.models[0].id,
        workflow=config,
        request={
            "values": {
                "mode": "text2img",
                "prompt": "",
                "count": count,
                "source": "webui",
            },
            "model": snapshot,
            "provider_name": provider.name,
            "connection": connection_fingerprint(provider),
        },
    )


class LegacyBatchClient(FakeComfyClient):
    def __init__(self, *, extras=0, block_submission=False):
        super().__init__()
        self.remote = {}
        self.extras = extras
        self.block_submission = block_submission
        self.submission_gate = asyncio.Event()
        self.two_submitting = asyncio.Event()
        self.two_waiting = asyncio.Event()

    async def submit(self, provider, graph, ui_workflow, *, client_id):
        remote_id = f"legacy-{client_id}"
        self.calls.append(("submit", copy.deepcopy(graph), client_id))
        self.remote[remote_id] = copy.deepcopy(graph)
        if len(self.remote) >= 2:
            self.two_submitting.set()
        if self.block_submission:
            await self.submission_gate.wait()
        return {"prompt_id": remote_id, "node_errors": {}}

    async def wait(self, provider, remote_id, *, on_progress, client_id):
        self.calls.append(("wait", remote_id, client_id))
        await on_progress({"status": "running"})
        if len([call for call in self.calls if call[0] == "wait"]) >= 2:
            self.two_waiting.set()
        self.wait_started.set()
        await self.release_wait.wait()
        return {**self.history, "remote_fixture_id": remote_id}

    async def download_outputs(self, provider, history, outputs, *, output_limit=None):
        self.calls.append(("download", history["remote_fixture_id"], output_limit))
        graph = self.remote[history["remote_fixture_id"]]
        count = graph["2"]["inputs"]["batch_size"] + self.extras
        images = tuple(image("red") for _ in range(count))
        return images[:output_limit] if output_limit is not None else images


def test_unmarked_queued_job_runs_old_unbound_output_contract(tmp_path, monkeypatch):
    async def run():
        runtime, _, _, client = await runtime_fixture(tmp_path, monkeypatch)
        try:
            job = await submit_old_job(runtime, old_provider())
            done = await runtime.manager.wait(job["id"])
            assert done["status"] == "succeeded"
            assert len((await runtime.result(done)).images) == 3
            assert [call[3] for call in client.calls if call[0] == "download"] == [None]
            assert len([call for call in client.calls if call[0] == "submit"]) == 1
            assert "execution_policy" not in done["request"]["model"]["comfyui"]
        finally:
            await runtime.close()

    asyncio.run(run())


def test_unmarked_submitted_job_recovers_all_outputs_after_live_config_migration(
    tmp_path, monkeypatch
):
    async def run():
        runtime, service, _, client = await runtime_fixture(tmp_path, monkeypatch)
        client.release_wait.clear()
        job = await submit_old_job(runtime, old_provider())
        await asyncio.wait_for(client.wait_started.wait(), 3)
        await runtime.close()
        migrated = ImageProvider.from_mapping(old_provider().public_dict())
        assert migrated.models[0].comfyui["execution_policy"] == "fixed_outputs_v1"
        service.update_settings(replace(service.settings, providers=(migrated,)))
        restarted = ComfyRuntime(service)
        try:
            client.release_wait.set()
            await restarted.start()
            done = await restarted.manager.wait(job["id"])
            assert done["status"] == "succeeded"
            assert len((await restarted.result(done)).images) == 3
            assert len([call for call in client.calls if call[0] == "submit"]) == 1
            assert [call[3] for call in client.calls if call[0] == "download"] == [None]
            assert "collection_limit" not in done["result"]
        finally:
            await restarted.close()

    asyncio.run(run())


def test_unmarked_completed_result_is_not_reclassified_or_trimmed(
    tmp_path, monkeypatch
):
    async def run():
        runtime, service, _, client = await runtime_fixture(tmp_path, monkeypatch)
        try:
            job = await submit_old_job(runtime, old_provider())
            done = await runtime.manager.wait(job["id"])
            before = await runtime.result(done)
            migrated = ImageProvider.from_mapping(old_provider().public_dict())
            service.update_settings(replace(service.settings, providers=(migrated,)))
            calls_before = len(client.calls)
            after = await runtime.result(done)
            assert before.images == after.images and len(after.images) == 3
            assert "execution_policy" not in after.provider.models[0].comfyui
            assert len(client.calls) == calls_before
            assert await runtime.store.get_job(job["id"]) == done
        finally:
            await runtime.close()

    asyncio.run(run())


@pytest.mark.parametrize("extras", [0, 1])
def test_legacy_children_recover_count_binding_and_keep_all_old_outputs(
    tmp_path, monkeypatch, extras
):
    async def run():
        runtime, service, _, _ = await runtime_fixture(tmp_path, monkeypatch)
        client = LegacyBatchClient(extras=extras)
        client.release_wait.clear()
        monkeypatch.setattr(comfyui_runtime, "ComfyClient", lambda _: client)
        job = await submit_old_job(runtime, old_provider(bound=True, native=2), count=5)
        await asyncio.wait_for(client.two_waiting.wait(), 3)
        await runtime.close()
        # Older versions persisted child IDs and request sizes without the new
        # execution-policy plan. Recovery must recognize this as legacy work.
        saved = await runtime.store.get_job(job["id"])
        old_result = dict(saved["result"])
        old_result.pop("batch_plan", None)
        await runtime.store.update_job(job["id"], result=old_result)
        restarted = ComfyRuntime(service)
        try:
            client.release_wait.set()
            await restarted.start()
            done = await asyncio.wait_for(restarted.manager.wait(job["id"]), 5)
            assert done["status"] == ("partial" if extras else "succeeded"), done
            graphs = [call[1] for call in client.calls if call[0] == "submit"]
            assert sorted(graph["2"]["inputs"]["batch_size"] for graph in graphs) == [
                1,
                2,
                2,
            ]
            assert len(graphs) == 3
            assert len((await restarted.result(done)).images) == 5 + extras * 3
            assert all(
                call[2] is None for call in client.calls if call[0] == "download"
            )
        finally:
            await restarted.close()

    asyncio.run(run())


def test_parent_cannot_resubmit_children_left_submitting_without_remote_ids(
    tmp_path, monkeypatch
):
    async def run():
        runtime, service, _, _ = await runtime_fixture(tmp_path, monkeypatch)
        client = LegacyBatchClient(block_submission=True)
        monkeypatch.setattr(comfyui_runtime, "ComfyClient", lambda _: client)
        job = await submit_old_job(runtime, old_provider(bound=True, native=1), count=2)
        await asyncio.wait_for(client.two_submitting.wait(), 3)
        await runtime.close()
        children = [
            item
            for item in await runtime.store.list_jobs()
            if item["request"].get("parent_job_id") == job["id"]
        ]
        assert len(children) == 2
        assert all(
            item["status"] == "submitting" and not item["remote_id"]
            for item in children
        )
        restarted = ComfyRuntime(service)
        try:
            # Start only the parent to exercise the execution-level guard, without
            # relying on resume_pending having classified uncertain children first.
            client.block_submission = False
            await restarted.manager._start(await restarted.store.get_job(job["id"]))
            done = await asyncio.wait_for(restarted.manager.wait(job["id"]), 5)
            assert done["status"] == "failed"
            assert len([call for call in client.calls if call[0] == "submit"]) == 2
            recovered_children = await asyncio.gather(
                *(restarted.store.get_job(item["id"]) for item in children)
            )
            assert all(item["status"] == "unknown" for item in recovered_children)
        finally:
            await restarted.close()

    asyncio.run(run())


def test_fixed_submitted_single_run_retains_quota_after_settings_change(
    tmp_path, monkeypatch
):
    async def run():
        runtime, service, provider, client = await runtime_fixture(
            tmp_path, monkeypatch, config=workflow(count_bound=True), native=3
        )
        client.release_wait.clear()
        job = await runtime.submit(
            provider=provider,
            model=provider.models[0],
            mode="text2img",
            prompt="",
            count=2,
        )
        await asyncio.wait_for(client.wait_started.wait(), 3)
        before = await runtime.store.get_job(job["id"])
        assert before["result"]["collection_limit"] == 2
        assert before["request"]["model"]["native_batch_size"] == 3
        await runtime.close()
        changed = provider.public_dict()
        changed["models"][0]["native_batch_size"] = 1
        changed["models"][0]["parameters"]["count"]["default"] = 7
        changed["models"][0]["comfyui"] = workflow()
        changed["models"][0]["comfyui"]["api_graph"]["2"]["inputs"]["batch_size"] = 99
        service.update_settings(
            replace(service.settings, providers=(ImageProvider.from_mapping(changed),))
        )
        restarted = ComfyRuntime(service)
        try:
            client.release_wait.set()
            await restarted.start()
            done = await restarted.manager.wait(job["id"])
            assert done["status"] == "succeeded", done
            assert len((await restarted.result(done)).images) == 2
            assert done["result"]["collection_limit"] == 2
            assert [call[3] for call in client.calls if call[0] == "download"] == [2]
            graphs = [call[1] for call in client.calls if call[0] == "submit"]
            assert len(graphs) == 1 and graphs[0]["2"]["inputs"]["batch_size"] == 3
            assert len(await restarted.store.list_jobs()) == 1
        finally:
            await restarted.close()

    asyncio.run(run())


def test_legacy_native_batch_snapshot_ignores_new_provider_discovery(
    tmp_path, monkeypatch
):
    async def run():
        runtime, service, _, _ = await runtime_fixture(tmp_path, monkeypatch)
        raw = old_provider(bound=True, native=2).public_dict()
        raw["models"][0]["native_batch_size_source"] = "remote"
        historical = ImageProvider.from_mapping(raw, migrate_comfyui=False)
        current = ImageProvider.from_mapping(
            {
                **raw,
                "discovered_models": [
                    {
                        "id": "workflow",
                        "native_batch_size": 9,
                        "native_batch_size_source": "remote",
                    }
                ],
            }
        )
        assert historical.models[0].native_batch_size == 2
        assert current.discovered_models[0]["native_batch_size"] == 9
        service.update_settings(replace(service.settings, providers=(current,)))
        try:
            revision = await runtime.store.save_revision(historical.models[0].comfyui)
            job = await runtime.store.create_job(
                provider_id="comfy",
                model_id="workflow",
                revision_id=revision,
                request={
                    "values": {"mode": "text2img", "prompt": "", "count": 5},
                    "model": historical.models[0].public_dict(),
                    "connection": connection_fingerprint(historical),
                },
            )
            restored = await runtime._provider(job)
            assert restored.models[0].native_batch_size == 2
            assert restored.models[0].comfyui["bindings"]["count"]["source"] == "count"
        finally:
            await runtime.close()

    asyncio.run(run())
