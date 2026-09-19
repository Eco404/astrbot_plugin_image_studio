"""Durable sampler and multi-workflow progress under overlapping events."""

from __future__ import annotations

import asyncio

from astrbot_plugin_image_studio.backend.generation.comfyui_runtime import ComfyRuntime
from astrbot_plugin_image_studio.backend.providers.comfyui.job_store import (
    ComfyJobStore,
)


async def progress_tree(tmp_path):
    store = ComfyJobStore(tmp_path / "history.sqlite3")
    await store.initialize()
    config = {"api_graph": {"1": {"class_type": "KSampler", "inputs": {"seed": 1}}}}
    revision = await store.save_revision(config)
    for index, identifier in enumerate(("parent", "first", "second", "third")):
        request = {"model": {"name": "workflow", "comfyui": config}}
        if index:
            request.update(parent_job_id="parent", chunk_index=index - 1)
        await store.create_job(
            provider_id="p",
            model_id="m",
            revision_id=revision,
            request=request,
            job_id=identifier,
        )
    await store.update_job(
        "parent",
        status="running",
        result={
            "progress": {
                "status": "running",
                "completed": 0,
                "total": 3,
            }
        },
    )
    return store


async def progress(store, identifier="parent"):
    return ComfyRuntime.public_job(await store.get_job(identifier, light=True))[
        "progress"
    ]


def test_sampler_progress_survives_poll_but_resets_when_execution_changes(tmp_path):
    async def run():
        store = await progress_tree(tmp_path)
        await store.update_progress(
            "first",
            {
                "status": "running",
                "event": "progress",
                "node": "1",
                "value": 18,
                "max": 30,
            },
            status="running",
        )
        assert await progress(store) == {
            "status": "running",
            "completed": 0,
            "total": 3,
            "current": 1,
            "node": "1",
            "value": 18,
            "max": 30,
        }
        await store.update_progress("first", {"status": "running"}, status="running")
        assert (await progress(store))["value"] == 18
        assert (await progress(store, "first"))["value"] == 18
        await store.update_progress(
            "first",
            {"status": "running", "event": "executing", "node": "2"},
            status="running",
        )
        assert "value" not in await progress(store)
        assert (await progress(store))["node"] == "2"
        await store.update_progress("first", {"status": "queued"}, status="submitted")
        assert await progress(store) == {
            "status": "running",
            "completed": 0,
            "total": 3,
        }

    asyncio.run(run())


def test_concurrent_completion_does_not_erase_other_workflow_steps(tmp_path):
    async def run():
        store = await progress_tree(tmp_path)
        await store.update_progress(
            "first",
            {"status": "running", "event": "progress", "value": 18, "max": 30},
            status="running",
        )
        await store.update_progress(
            "second",
            {"status": "running", "event": "progress", "value": 5, "max": 20},
            status="running",
        )
        await store.update_job("first", status="succeeded")
        assert await progress(store) == {
            "status": "running",
            "completed": 1,
            "total": 3,
            "current": 2,
            "value": 5,
            "max": 20,
        }
        await store.update_progress("third", {"status": "queued"}, status="submitted")
        assert (await progress(store))["value"] == 5
        await asyncio.gather(
            store.update_progress(
                "third",
                {"status": "running", "event": "progress", "value": 1, "max": 10},
                status="running",
            ),
            store.update_job("second", status="failed"),
        )
        assert await progress(store) == {
            "status": "running",
            "completed": 2,
            "total": 3,
            "current": 3,
            "value": 1,
            "max": 10,
        }
        await store.update_job("third", status="succeeded")
        assert await progress(store) == {
            "status": "running",
            "completed": 3,
            "total": 3,
        }

    asyncio.run(run())


def test_late_child_progress_cannot_change_a_cancelled_parent(tmp_path):
    async def run():
        store = await progress_tree(tmp_path)
        await store.update_job("parent", status="cancelled")
        previous = await progress(store)
        await store.update_progress(
            "first",
            {"status": "running", "event": "progress", "value": 1, "max": 30},
            status="running",
        )
        assert await progress(store) == previous
        assert (await store.get_job("parent", light=True))["status"] == "cancelled"

    asyncio.run(run())
