"""Agent task handoff, scoped recovery and explicit invocation idempotency."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace

import pytest

from astrbot_plugin_image_studio.backend.generation.comfyui_runtime import ComfyRuntime
from astrbot_plugin_image_studio.backend.providers.comfyui.client import (
    ComfyExecutionError,
)
from astrbot_plugin_image_studio.backend.providers.comfyui.job_types import (
    RECOVERY_SECONDS,
)
from astrbot_plugin_image_studio.tests.support.comfy_runtime import runtime_fixture

SCOPE = "test:private:agent-user"


async def submit(runtime, provider, **changes):
    return await runtime.submit_agent(
        provider=provider,
        model=provider.models[0],
        mode="text2img",
        prompt=changes.pop("prompt", ""),
        scope_id=changes.pop("scope_id", SCOPE),
        request_id=changes.pop("request_id", "invocation-1"),
        **changes,
    )


def test_short_task_returns_result_and_private_scope_is_independent_of_history_identity(
    tmp_path, monkeypatch
):
    async def run():
        runtime, service, provider, _client = await runtime_fixture(
            tmp_path, monkeypatch
        )
        service.update_settings(
            replace(
                service.settings,
                history=replace(
                    service.settings.history, record_invocation_identity=False
                ),
            )
        )
        try:
            job = await submit(runtime, provider, wait_seconds=5)
            assert job["status"] == "succeeded"
            result = await runtime.result(job)
            assert result.images and result.generation_id
            assert job["request"]["values"]["source"] == "llm_tool"
            with runtime.store._connect() as conn:
                stored = conn.execute(
                    "SELECT request_json FROM comfy_jobs WHERE id=?", (job["id"],)
                ).fetchone()[0]
                assert SCOPE not in stored
                assert json.loads(stored)["agent"]["scope_hash"]
                assert (
                    conn.execute("SELECT user_id FROM generations").fetchone()[0] == ""
                )
            with pytest.raises(ValueError, match="不属于当前会话"):
                await runtime.inspect_agent(job["id"], scope_id="another:session")
        finally:
            await runtime.close()
            await service.store.close()

    asyncio.run(run())


def test_bounded_wait_keeps_job_running_and_concurrent_same_invocation_submits_once(
    tmp_path, monkeypatch
):
    async def run():
        runtime, service, provider, client = await runtime_fixture(
            tmp_path, monkeypatch
        )
        client.release_wait.clear()
        try:
            jobs = await asyncio.gather(
                *(submit(runtime, provider, wait_seconds=0) for _ in range(5))
            )
            assert len({job["id"] for job in jobs}) == 1
            task_id = jobs[0]["id"]
            await client.wait_started.wait()
            pending = await runtime.inspect_agent(
                task_id, scope_id=SCOPE, wait_seconds=0.01
            )
            assert pending["status"] == "running"
            assert not runtime.manager._tasks[task_id].done()
            assert sum(call[0] == "submit" for call in client.calls) == 1
            client.release_wait.set()
            completed = await runtime.inspect_agent(
                task_id, scope_id=SCOPE, wait_seconds=5
            )
            assert completed["status"] == "succeeded"
            again = await submit(runtime, provider, wait_seconds=5)
            assert again["id"] == task_id and again["status"] == "succeeded"
            assert sum(call[0] == "submit" for call in client.calls) == 1
        finally:
            await runtime.close()
            await service.store.close()

    asyncio.run(run())


def test_invocation_id_rejects_changed_request_and_never_deduplicates_by_prompt(
    tmp_path, monkeypatch
):
    async def run():
        runtime, service, provider, client = await runtime_fixture(
            tmp_path, monkeypatch
        )
        try:
            first = await submit(runtime, provider, wait_seconds=5)
            with pytest.raises(ValueError, match="不同请求"):
                await submit(runtime, provider, prompt="changed prompt")
            second = await submit(
                runtime, provider, request_id="invocation-2", wait_seconds=5
            )
            other_scope = await submit(
                runtime, provider, scope_id="other:session", wait_seconds=5
            )
            unkeyed = await submit(runtime, provider, request_id=None, wait_seconds=5)
            another_unkeyed = await submit(
                runtime, provider, request_id=None, wait_seconds=5
            )
            assert (
                len(
                    {
                        job["id"]
                        for job in (
                            first,
                            second,
                            other_scope,
                            unkeyed,
                            another_unkeyed,
                        )
                    }
                )
                == 5
            )
            assert sum(call[0] == "submit" for call in client.calls) == 5
        finally:
            await runtime.close()
            await service.store.close()

    asyncio.run(run())


def test_caller_cancellation_can_find_task_and_restart_recovers_without_resubmitting(
    tmp_path, monkeypatch
):
    async def run():
        runtime, service, provider, client = await runtime_fixture(
            tmp_path, monkeypatch
        )
        client.release_wait.clear()
        try:
            waiter = asyncio.create_task(submit(runtime, provider, wait_seconds=30))
            await client.wait_started.wait()
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            recent = await runtime.list_agent(scope_id=SCOPE)
            assert len(recent) == 1
            task_id = recent[0]["task_id"]
            assert recent[0]["request_id"] == "invocation-1"
            assert recent[0]["status"] == "running"
            assert not runtime.manager._tasks[task_id].done()
            await runtime.close()
            runtime = ComfyRuntime(service)
            service.comfy_runtime = runtime
            await runtime.start()
            client.release_wait.set()
            job = await runtime.inspect_agent(task_id, scope_id=SCOPE, wait_seconds=5)
            assert job["status"] == "succeeded"
            assert sum(call[0] == "submit" for call in client.calls) == 1
        finally:
            await runtime.close()
            await service.store.close()

    asyncio.run(run())


def test_explicit_resume_recovers_acknowledged_failure_and_checks_scope_first(
    tmp_path, monkeypatch
):
    async def run():
        runtime, service, provider, client = await runtime_fixture(
            tmp_path, monkeypatch
        )
        client.wait_error = ComfyExecutionError("simulated connection lost")
        try:
            failed = await submit(runtime, provider, wait_seconds=5)
            assert failed["status"] == "failed"
            assert runtime.public_job(failed)["can_resume"]
            client.wait_error = None
            with pytest.raises(ValueError, match="不属于当前会话"):
                await runtime.inspect_agent(
                    failed["id"], scope_id="other:session", resume=True
                )
            assert (await runtime.store.get_job(failed["id"]))["status"] == "failed"
            recovered = await runtime.inspect_agent(
                failed["id"], scope_id=SCOPE, resume=True, wait_seconds=5
            )
            assert recovered["id"] == failed["id"]
            assert recovered["status"] == "succeeded"
            assert sum(call[0] == "submit" for call in client.calls) == 1
        finally:
            await runtime.close()
            await service.store.close()

    asyncio.run(run())


def test_explicit_resume_starts_a_committed_job_whose_caller_left_before_scheduling(
    tmp_path, monkeypatch
):
    async def run():
        runtime, service, provider, client = await runtime_fixture(
            tmp_path, monkeypatch
        )
        original_start = runtime.manager._start

        async def interrupted(_job):
            raise asyncio.CancelledError

        try:
            monkeypatch.setattr(runtime.manager, "_start", interrupted)
            with pytest.raises(asyncio.CancelledError):
                await submit(runtime, provider)
            monkeypatch.setattr(runtime.manager, "_start", original_start)
            recent = await runtime.list_agent(scope_id=SCOPE)
            assert len(recent) == 1 and recent[0]["status"] == "queued"
            task_id = recent[0]["task_id"]
            observed = await runtime.inspect_agent(task_id, scope_id=SCOPE)
            assert observed["status"] == "queued" and not client.calls
            recovered = await runtime.inspect_agent(
                task_id, scope_id=SCOPE, resume=True, wait_seconds=5
            )
            assert recovered["id"] == task_id and recovered["status"] == "succeeded"
            assert sum(call[0] == "submit" for call in client.calls) == 1
        finally:
            await runtime.close()
            await service.store.close()

    asyncio.run(run())


def test_unknown_submission_cannot_be_resubmitted_by_query_resume_or_same_request(
    tmp_path, monkeypatch
):
    async def run():
        runtime, service, provider, client = await runtime_fixture(
            tmp_path, monkeypatch
        )
        client.submit_error = ComfyExecutionError(
            "unknown submission", unknown_submission=True
        )
        try:
            job = await submit(runtime, provider, wait_seconds=5)
            assert job["status"] == "unknown"
            assert not runtime.public_job(job)["can_resume"]
            observed = await runtime.inspect_agent(job["id"], scope_id=SCOPE)
            assert observed["status"] == "unknown"
            with pytest.raises(ValueError, match="没有已确认"):
                await runtime.inspect_agent(job["id"], scope_id=SCOPE, resume=True)
            repeated = await submit(runtime, provider, wait_seconds=5)
            assert repeated["id"] == job["id"] and repeated["status"] == "unknown"
            assert sum(call[0] == "submit" for call in client.calls) == 1
        finally:
            await runtime.close()
            await service.store.close()

    asyncio.run(run())


def test_unscoped_webui_task_is_not_exposed_to_agent(tmp_path, monkeypatch):
    async def run():
        runtime, service, provider, _client = await runtime_fixture(
            tmp_path, monkeypatch
        )
        try:
            job = await runtime.submit(
                provider=provider, model=provider.models[0], mode="text2img", prompt=""
            )
            await runtime.manager.wait(job["id"])
            with pytest.raises(ValueError, match="不属于当前会话"):
                await runtime.inspect_agent(job["id"], scope_id=SCOPE)
            assert await runtime.list_agent(scope_id=SCOPE) == []
        finally:
            await runtime.close()
            await service.store.close()

    asyncio.run(run())


def test_archive_keeps_authorization_and_idempotency_while_gallery_result_remains_readable(
    tmp_path, monkeypatch
):
    async def run():
        runtime, service, provider, client = await runtime_fixture(
            tmp_path, monkeypatch
        )
        try:
            job = await submit(runtime, provider, wait_seconds=5)
            with runtime.store._connect() as conn:
                conn.execute(
                    "UPDATE comfy_jobs SET finished_at=? WHERE id=?",
                    (time.time() - RECOVERY_SECONDS - 10, job["id"]),
                )
            await runtime.store.cleanup_terminal_files(
                terminal_before=time.time() - RECOVERY_SECONDS
            )
            archived = await runtime.inspect_agent(job["id"], scope_id=SCOPE)
            assert archived["archive_state"] == "archived"
            assert archived["request"]["agent"] == job["request"]["agent"]
            assert (await runtime.result(archived)).images
            with pytest.raises(ValueError, match="不属于当前会话"):
                await runtime.inspect_agent(job["id"], scope_id="other:session")
            repeated = await submit(runtime, provider, wait_seconds=5)
            assert repeated["id"] == job["id"]
            assert repeated["archive_state"] == "archived"
            assert sum(call[0] == "submit" for call in client.calls) == 1
            recent = await runtime.list_agent(scope_id=SCOPE)
            assert recent[0]["task_id"] == job["id"]
        finally:
            await runtime.close()
            await service.store.close()

    asyncio.run(run())


def test_recent_task_list_is_bounded_scoped_and_never_expands_heavy_snapshots(
    tmp_path, monkeypatch
):
    async def run():
        runtime, service, provider, _client = await runtime_fixture(
            tmp_path, monkeypatch
        )
        try:
            jobs = [
                await submit(
                    runtime, provider, request_id=f"invocation-{index}", wait_seconds=5
                )
                for index in range(3)
            ]
            await submit(runtime, provider, scope_id="other:session", wait_seconds=5)
            parent = jobs[0]
            await runtime.store.create_job(
                provider_id=provider.id,
                model_id=provider.models[0].id,
                revision_id=parent["revision_id"],
                request={**parent["request"], "parent_job_id": parent["id"]},
                job_id="child",
            )

            def unexpected(*_args):
                pytest.fail("recent task summaries must not decode heavy payloads")

            monkeypatch.setattr(
                "astrbot_plugin_image_studio.backend.providers.comfyui.job_store.load_payload",
                unexpected,
            )
            recent = await runtime.list_agent(scope_id=SCOPE, limit=2)
            assert [item["task_id"] for item in recent] == [
                jobs[2]["id"],
                jobs[1]["id"],
            ]
            assert all(
                "request" not in item
                and "outputs" not in item
                and "scope_hash" not in item
                for item in recent
            )
        finally:
            await runtime.close()
            await service.store.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "changes",
    [
        {"scope_id": ""},
        {"request_id": ""},
        {"request_id": []},
        {"wait_seconds": True},
        {"wait_seconds": -1},
        {"wait_seconds": 31},
        {"wait_seconds": float("nan")},
    ],
)
def test_bad_agent_submission_options_fail_before_creating_any_task(
    tmp_path, monkeypatch, changes
):
    async def run():
        runtime, service, provider, client = await runtime_fixture(
            tmp_path, monkeypatch
        )
        try:
            with pytest.raises(ValueError):
                await submit(runtime, provider, **changes)
            assert await runtime.store.list_jobs() == []
            assert client.calls == []
        finally:
            await runtime.close()
            await service.store.close()

    asyncio.run(run())
