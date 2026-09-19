"""Durable native-batch splitting, grouped gallery output and safe recovery."""

from __future__ import annotations

import asyncio
import copy
import time
from dataclasses import replace

import pytest
from astrbot_plugin_image_studio.backend.generation import comfyui_runtime
from astrbot_plugin_image_studio.backend.providers.comfyui.client import (
    ComfyExecutionError,
)
from astrbot_plugin_image_studio.backend.generation.comfyui_runtime import ComfyRuntime
from astrbot_plugin_image_studio.backend.models import ImageProvider
from astrbot_plugin_image_studio.tests.support.comfy_runtime import (
    FakeComfyClient,
    image,
    runtime_fixture,
    workflow,
)


class BatchClient(FakeComfyClient):
    def __init__(self):
        super().__init__()
        self.remote = {}
        self.fail_indices = set()
        self.unknown_indices = set()
        self.active = 0
        self.maximum = 0
        self.two_started = asyncio.Event()
        self.extra_outputs = 0
        self.output_counts = None
        self.collection_limits = []
        self.store = None

    async def submit(self, provider, graph, ui_workflow, *, client_id):
        job = await self.store.get_job(client_id) if self.store is not None else None
        index = len(self.remote)
        remote_id = f"remote-{client_id}"
        self.calls.append(("submit", copy.deepcopy(graph), client_id))
        self.remote[remote_id] = {
            "size": graph["2"]["inputs"]["batch_size"],
            "index": index,
            "chunk_index": job["request"].get("chunk_index", 0) if job else index,
        }
        if index in self.unknown_indices:
            raise ComfyExecutionError("response lost", unknown_submission=True)
        return {"prompt_id": remote_id, "node_errors": {}}

    async def wait(self, provider, remote_id, *, on_progress, client_id):
        self.calls.append(("wait", remote_id, client_id))
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        if self.active >= 2:
            self.two_started.set()
        try:
            await on_progress({"status": "running"})
            self.wait_started.set()
            await self.release_wait.wait()
            details = self.remote[remote_id]
            await asyncio.sleep(0.02 if details["index"] == 0 else 0.002)
            if details["index"] in self.fail_indices:
                raise ComfyExecutionError("simulated read timeout")
            return {**self.history, "test_remote_id": remote_id}
        finally:
            self.active -= 1

    async def download_outputs(self, provider, history, outputs, *, output_limit=None):
        details = self.remote[history["test_remote_id"]]
        self.collection_limits.append(output_limit)
        count = (
            self.output_counts[details["chunk_index"]]
            if self.output_counts is not None
            else details["size"] + self.extra_outputs
        )
        colors = ("red", "blue", "green", "yellow")
        return tuple(
            replace(
                image(colors[details["index"] % len(colors)]),
                effective_parameters={"batch_index": details["index"]},
            )
            for _ in range(count)
        )[:output_limit]


async def fixture(tmp_path, monkeypatch, *, native=2, model_limit=2, provider_limit=2):
    runtime, service, provider, _ = await runtime_fixture(
        tmp_path, monkeypatch, config=workflow()
    )
    raw = provider.public_dict()
    raw["max_concurrent_generations"] = provider_limit
    raw["models"][0]["native_batch_size"] = native
    raw["models"][0]["native_batch_size_source"] = "manual"
    raw["models"][0]["max_concurrent_requests"] = model_limit
    provider = ImageProvider.from_mapping(raw)
    service.update_settings(replace(service.settings, providers=(provider,)))
    client = BatchClient()
    client.store = runtime.store
    monkeypatch.setattr(comfyui_runtime, "ComfyClient", lambda _: client)
    return runtime, service, provider, client


async def submit(runtime, provider, count):
    return await runtime.submit(
        provider=provider,
        model=provider.models[0],
        mode="text2img",
        prompt="",
        count=count,
    )


def test_fixed_count_splits_into_durable_children_and_one_ordered_gallery_group(
    tmp_path, monkeypatch
):
    async def run():
        runtime, service, provider, client = await fixture(tmp_path, monkeypatch)
        client.release_wait.clear()
        try:
            job = await submit(runtime, provider, 5)
            await asyncio.wait_for(client.two_started.wait(), 3)
            client.release_wait.set()
            completed = await asyncio.wait_for(runtime.manager.wait(job["id"]), 5)
            assert completed["status"] == "succeeded", completed
            result = await runtime.result(completed)
            assert len(result.images) == 5 and result.request.count == 5
            assert [entry.response_index for entry in result.images] == [1, 2, 3, 4, 5]
            jobs = await runtime.store.list_jobs()
            children = [
                item
                for item in jobs
                if item["request"].get("parent_job_id") == job["id"]
            ]
            assert len(children) == 3
            assert [
                item["id"]
                for item in await runtime.store.list_jobs(
                    limit=1, include_children=False
                )
            ] == [job["id"]]
            indices = {
                child["id"]: child["request"]["chunk_index"] for child in children
            }
            assert [
                indices[entry.effective_parameters["comfy_child_job_id"]]
                for entry in result.images
            ] == [0, 0, 1, 1, 2]
            assert all(not child["generation_id"] for child in children)
            assert all(child["remote_id"] for child in children)
            assert sorted(
                call[1]["2"]["inputs"]["batch_size"]
                for call in client.calls
                if call[0] == "submit"
            ) == [3, 3, 3]
            assert sorted(client.collection_limits) == [1, 2, 2]
            assert completed["result"]["batch_plan"]["quotas"] == [2, 2, 1]
            assert client.maximum == 2
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
            assert all(
                item.effective_parameters["_comfyui"]["parameters_schema"]
                for item in result.images
            )
        finally:
            await runtime.close()

    asyncio.run(run())


@pytest.mark.parametrize("model_limit,provider_limit", [(1, 1), (1, 3), (3, 1)])
def test_parent_does_not_hold_a_slot_and_children_respect_both_limits(
    tmp_path, monkeypatch, model_limit, provider_limit
):
    async def run():
        runtime, _, provider, client = await fixture(
            tmp_path,
            monkeypatch,
            native=1,
            model_limit=model_limit,
            provider_limit=provider_limit,
        )
        try:
            job = await submit(runtime, provider, 3)
            completed = await asyncio.wait_for(runtime.manager.wait(job["id"]), 5)
            assert completed["status"] == "succeeded", completed
            assert client.maximum == 1
        finally:
            await runtime.close()

    asyncio.run(run())


def test_partial_child_failure_preserves_all_successes_in_one_record(
    tmp_path, monkeypatch
):
    async def run():
        runtime, service, provider, client = await fixture(tmp_path, monkeypatch)
        client.fail_indices.add(1)
        try:
            job = await submit(runtime, provider, 5)
            completed = await runtime.manager.wait(job["id"])
            assert completed["status"] == "partial", completed
            result = await runtime.result(completed)
            expected = 5 - sum(
                min(2, 5 - item["chunk_index"] * 2)
                for item in client.remote.values()
                if item["index"] in client.fail_indices
            )
            assert len(result.images) == expected
            assert "第 " in result.warning
            with service.store._connect() as connection:
                assert (
                    connection.execute("SELECT COUNT(*) FROM generations").fetchone()[0]
                    == 1
                )
                assert (
                    connection.execute(
                        "SELECT COUNT(*) FROM generation_images"
                    ).fetchone()[0]
                    == expected
                )
        finally:
            await runtime.close()

    asyncio.run(run())


def test_extra_images_from_selected_outputs_are_truncated_without_warning(
    tmp_path, monkeypatch
):
    async def run():
        runtime, _, provider, client = await fixture(tmp_path, monkeypatch)
        client.extra_outputs = 1
        try:
            job = await submit(runtime, provider, 3)
            completed = await runtime.manager.wait(job["id"])
            result = await runtime.result(completed)
            assert len(result.images) == 3
            assert not result.warning
            assert completed["status"] == "succeeded"
        finally:
            await runtime.close()

    asyncio.run(run())


def test_restart_reuses_children_and_never_resubmits_confirmed_remote_tasks(
    tmp_path, monkeypatch
):
    async def run():
        runtime, service, provider, client = await fixture(tmp_path, monkeypatch)
        client.release_wait.clear()
        job = await submit(runtime, provider, 5)
        await asyncio.wait_for(client.two_started.wait(), 3)
        await runtime.close()
        restarted = ComfyRuntime(service)
        service.comfy_runtime = restarted
        try:
            client.release_wait.set()
            await restarted.start()
            completed = await asyncio.wait_for(restarted.manager.wait(job["id"]), 5)
            assert completed["status"] == "succeeded", completed
            submissions = [call[2] for call in client.calls if call[0] == "submit"]
            assert len(submissions) == len(set(submissions)) == 3
            assert len((await restarted.result(completed)).images) == 5
            with service.store._connect() as connection:
                assert (
                    connection.execute("SELECT COUNT(*) FROM generations").fetchone()[0]
                    == 1
                )
        finally:
            await restarted.close()

    asyncio.run(run())


def test_failed_parent_resumes_only_known_remote_children(tmp_path, monkeypatch):
    async def run():
        runtime, _, provider, client = await fixture(tmp_path, monkeypatch, native=1)
        client.fail_indices.update({0, 1})
        try:
            job = await submit(runtime, provider, 2)
            failed = await runtime.manager.wait(job["id"])
            assert failed["status"] == "failed", failed
            assert not failed["remote_id"] and runtime.public_job(failed)["can_resume"]
            client.fail_indices.clear()
            await runtime.manager.resume(job["id"])
            completed = await runtime.manager.wait(job["id"])
            assert completed["status"] == "succeeded", completed
            assert len([call for call in client.calls if call[0] == "submit"]) == 2
            assert len((await runtime.result(completed)).images) == 2
        finally:
            await runtime.close()

    asyncio.run(run())


def test_unknown_child_is_not_resubmitted_when_parent_is_retried(tmp_path, monkeypatch):
    async def run():
        runtime, _, provider, client = await fixture(tmp_path, monkeypatch, native=1)
        client.unknown_indices.update({0, 1})
        try:
            job = await submit(runtime, provider, 2)
            failed = await runtime.manager.wait(job["id"])
            assert failed["status"] == "failed"
            await runtime.manager.resume(job["id"])
            retried = await runtime.manager.wait(job["id"])
            assert retried["status"] == "failed"
            assert len([call for call in client.calls if call[0] == "submit"]) == 2
        finally:
            await runtime.close()

    asyncio.run(run())


def test_parent_cancel_targets_only_its_children_and_stops_queued_chunks(
    tmp_path, monkeypatch
):
    async def run():
        runtime, service, provider, client = await fixture(
            tmp_path, monkeypatch, native=1
        )
        client.release_wait.clear()
        try:
            job = await submit(runtime, provider, 3)
            await asyncio.wait_for(client.two_started.wait(), 3)
            cancelled = await runtime.cancel(job["id"])
            assert cancelled["status"] == "cancelled"
            calls = [call for call in client.calls if call[0] == "cancel"]
            assert len(calls) == 2
            assert len([call for call in client.calls if call[0] == "submit"]) == 2
            client.release_wait.set()
            await asyncio.sleep(0.05)
            with service.store._connect() as connection:
                assert (
                    connection.execute("SELECT COUNT(*) FROM generations").fetchone()[0]
                    == 0
                )
        finally:
            await runtime.close()

    asyncio.run(run())


def test_cleanup_preserves_completed_child_files_while_parent_is_active(
    tmp_path, monkeypatch
):
    async def run():
        runtime, _, provider, _ = await fixture(tmp_path, monkeypatch)
        try:
            revision = await runtime.store.save_revision(workflow(count_bound=True))
            parent = await runtime.store.create_job(
                provider_id="comfy",
                model_id="workflow",
                revision_id=revision,
                request={},
            )
            child = await runtime.store.create_job(
                provider_id="comfy",
                model_id="workflow",
                revision_id=revision,
                request={"parent_job_id": parent["id"]},
            )
            await runtime.store.save_outputs(child["id"], (image("red"),))
            await runtime.store.update_job(child["id"], status="succeeded")
            assert (
                await runtime.store.cleanup_terminal_files(
                    terminal_before=time.time() + 1
                )
                == 0
            )
            assert len(await runtime.store.load_outputs(child["id"])) == 1
        finally:
            await runtime.close()

    asyncio.run(run())


def test_unknown_child_keeps_parent_cancellation_unconfirmed(tmp_path, monkeypatch):
    async def run():
        runtime, _, provider, client = await fixture(tmp_path, monkeypatch, native=1)
        client.unknown_indices.add(0)
        client.release_wait.clear()
        try:
            job = await submit(runtime, provider, 3)
            await asyncio.wait_for(client.two_started.wait(), 3)
            with pytest.raises(ValueError, match="提交结果未知"):
                await runtime.cancel(job["id"])
            assert (await runtime.store.get_job(job["id"]))["status"] != "cancelled"
            assert len([call for call in client.calls if call[0] == "cancel"]) == 2
            client.release_wait.set()
            finished = await asyncio.wait_for(runtime.manager.wait(job["id"]), 3)
            assert finished["status"] == "failed"
        finally:
            client.release_wait.set()
            await runtime.close()

    asyncio.run(run())


def test_parent_progress_updates_before_all_children_finish_and_survives_summary(
    tmp_path, monkeypatch
):
    async def run():
        runtime, _, provider, client = await fixture(tmp_path, monkeypatch, native=1)
        release_first = asyncio.Event()
        original_wait = client.wait

        async def delayed_wait(selected_provider, remote_id, **kwargs):
            if client.remote[remote_id]["index"] == 0:
                await release_first.wait()
            return await original_wait(selected_provider, remote_id, **kwargs)

        client.wait = delayed_wait
        try:
            job = await submit(runtime, provider, 2)

            async def midpoint():
                while True:
                    current = await runtime.store.get_job(job["id"])
                    progress = (current.get("result") or {}).get("progress") or {}
                    if progress.get("completed") == 1:
                        return current
                    await asyncio.sleep(0.01)

            intermediate = await asyncio.wait_for(midpoint(), 3)
            assert intermediate["status"] == "running"
            assert intermediate["result"]["progress"]["total"] == 2
            release_first.set()
            finished = await asyncio.wait_for(runtime.manager.wait(job["id"]), 3)
            assert finished["status"] == "succeeded"
            assert len(finished["result"]["child_ids"]) == 2
            assert runtime.public_job(finished)["progress"] == {
                "status": "completed",
                "completed": 2,
                "total": 2,
            }
        finally:
            release_first.set()
            await runtime.close()

    asyncio.run(run())


def alias_provider(provider, *, hidden_count=False):
    raw = provider.public_dict()
    definition = workflow()
    definition["bindings"].update(
        {
            "bound_seed": {
                "source": "parameter",
                "type": "number",
                "node_id": "3",
                "input_name": "seed",
            },
            "bound_width": {
                "source": "parameter",
                "type": "number",
                "node_id": "2",
                "input_name": "width",
            },
        }
    )
    raw["models"][0].update(
        {
            "comfyui": definition,
            "parameters": {
                "human_seed": {
                    "type": "integer",
                    "request_key": "bound_seed",
                    "default": 42,
                },
                "hidden_width": {
                    "type": "integer",
                    "request_key": "bound_width",
                    "default": 640,
                },
                "count": {"type": "integer", "default": 1, "min": 1, "max": 16},
            },
            "tool": {
                "enabled": True,
                "parameters": {
                    "human_seed": {"exposed": True},
                    "hidden_width": {"exposed": False},
                    "count": {
                        "exposed": not hidden_count,
                        **({"default_override": 3} if hidden_count else {}),
                    },
                },
            },
        }
    )
    return ImageProvider.from_mapping(raw)


@pytest.mark.parametrize("source", ["webui", "llm_tool"])
def test_split_children_restore_public_aliases_without_bypassing_tool_permissions(
    tmp_path, monkeypatch, source
):
    async def run():
        runtime, service, provider, client = await fixture(
            tmp_path, monkeypatch, native=1
        )
        provider = alias_provider(provider)
        service.update_settings(replace(service.settings, providers=(provider,)))
        try:
            result = await asyncio.wait_for(
                service.generate(
                    mode="text2img",
                    provider_id="comfy",
                    prompt="",
                    source=source,
                    count=2,
                    parameters={"human_seed": 8675309, "hidden_width": 2048},
                ),
                5,
            )
            assert len(result.images) == 2
            graphs = [call[1] for call in client.calls if call[0] == "submit"]
            assert len(graphs) == 2
            assert all(item["3"]["inputs"]["seed"] == 8675309 for item in graphs)
            expected_width = 640 if source == "llm_tool" else 2048
            assert all(
                item["2"]["inputs"]["width"] == expected_width for item in graphs
            )
        finally:
            await runtime.close()

    asyncio.run(run())


def test_hidden_tool_count_default_is_resolved_once_before_native_splitting(
    tmp_path, monkeypatch
):
    async def run():
        runtime, service, provider, client = await fixture(
            tmp_path, monkeypatch, native=1
        )
        provider = alias_provider(provider, hidden_count=True)
        service.update_settings(replace(service.settings, providers=(provider,)))
        try:
            result = await asyncio.wait_for(
                service.generate(
                    mode="text2img",
                    provider_id="comfy",
                    prompt="",
                    source="llm_tool",
                    count=16,
                    parameters={"human_seed": 987654, "count": 16},
                ),
                5,
            )
            assert result.request.count == 3 and len(result.images) == 3
            graphs = [call[1] for call in client.calls if call[0] == "submit"]
            assert len(graphs) == 3
            assert all(item["2"]["inputs"]["batch_size"] == 3 for item in graphs)
            assert all(item["3"]["inputs"]["seed"] == 987654 for item in graphs)
            jobs = await runtime.store.list_jobs()
            roots = [item for item in jobs if not item["request"].get("parent_job_id")]
            assert len(roots) == 1 and len(jobs) == 4
            assert all(
                item["request"].get("parent_job_id") == roots[0]["id"]
                for item in jobs
                if item not in roots
            )
        finally:
            await runtime.close()

    asyncio.run(run())
