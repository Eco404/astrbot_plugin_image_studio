"""Published ComfyUI results follow gallery removal without losing recovery data."""

from __future__ import annotations

import asyncio
import time

import pytest

from astrbot_plugin_image_studio.backend.generation.comfyui_runtime import ComfyRuntime
from astrbot_plugin_image_studio.backend.providers.comfyui.job_types import (
    RECOVERY_SECONDS,
)
from astrbot_plugin_image_studio.tests.support.comfy_runtime import runtime_fixture


async def cached_result(service, provider, runtime, monkeypatch, *, count=3):
    async def postpone_release(*_args):
        return 0

    # Emulate the pre-upgrade result manifests, or interruption after publication.
    with monkeypatch.context() as patch:
        patch.setattr(runtime.store, "release_gallery_outputs", postpone_release)
        result = await service.generate(
            mode="text2img", provider_id=provider.id, prompt="", count=count
        )
    assert list(runtime.store.blobs_dir.glob("*.image"))
    return result


@pytest.mark.parametrize("removal", ["group", "bulk", "images", "maintenance"])
def test_deleted_published_outputs_release_parent_and_child_caches(
    tmp_path, monkeypatch, removal
):
    async def run():
        runtime, service, provider, _ = await runtime_fixture(
            tmp_path, monkeypatch, native=2
        )
        try:
            result = await cached_result(service, provider, runtime, monkeypatch)
            if removal == "group":
                assert await service.store.delete_generation(result.generation_id)
            elif removal == "bulk":
                deleted = await service.store.delete_generations([result.generation_id])
                assert deleted["deleted"] == [result.generation_id]
            elif removal == "images":
                detail = await service.store.generation_detail(
                    result.generation_id, light=True
                )
                await service.store.delete_images(
                    result.generation_id, [x["id"] for x in detail["images"]]
                )
            else:
                # Existing stores may already contain deleted gallery links.
                service.store.on_results_removed = None
                await service.store.delete_generation(result.generation_id)
                assert await runtime.store.cleanup_terminal_files(
                    terminal_before=time.time() - RECOVERY_SECONDS
                )
            assert not list(runtime.store.blobs_dir.glob("*.image"))
            jobs = await runtime.store.list_jobs()
            assert len(jobs) == 3  # One parent and two completed execution rounds.
            for job in jobs:
                assert not job["archive_state"]
                assert all(x["storage"] == "expired" for x in job["outputs"])
                assert not ComfyRuntime.public_job(job)["result_available"]
                light = await runtime.store.get_job(job["id"], light=True)
                assert not ComfyRuntime.public_job(light)["result_available"]
            with runtime.store._connect() as conn:
                assert (
                    conn.execute(
                        "SELECT COUNT(*) FROM storage_payload_refs "
                        "WHERE owner_table='comfy_jobs' AND slot LIKE 'output:%'"
                    ).fetchone()[0]
                    == 0
                )
            assert (
                await runtime.store.cleanup_terminal_files(
                    terminal_before=time.time() - RECOVERY_SECONDS
                )
                == 0
            )
        finally:
            await runtime.close()

    asyncio.run(run())


def test_partial_gallery_deletion_keeps_remaining_outputs_and_exact_ordinals(
    tmp_path, monkeypatch
):
    async def run():
        runtime, service, provider, _ = await runtime_fixture(
            tmp_path, monkeypatch, native=3
        )
        try:
            result = await cached_result(service, provider, runtime, monkeypatch)
            detail = await service.store.generation_detail(
                result.generation_id, light=True
            )
            await service.store.delete_images(
                result.generation_id, [detail["images"][1]["id"]]
            )
            job = (await runtime.store.list_jobs())[0]
            assert [x["storage"] for x in job["outputs"]] == [
                "gallery",
                "expired",
                "gallery",
            ]
            assert not list(runtime.store.blobs_dir.glob("*.image"))
            remaining = await runtime.result(job)
            assert [x.data for x in remaining.images] == [
                result.images[0].data,
                result.images[2].data,
            ]
            assert "2 张" in remaining.warning
        finally:
            await runtime.close()

    asyncio.run(run())


@pytest.mark.parametrize("status", ["running", "unknown", "failed"])
def test_deleted_gallery_does_not_release_recoverable_task_tree(
    tmp_path, monkeypatch, status
):
    async def run():
        runtime, service, provider, _ = await runtime_fixture(
            tmp_path, monkeypatch, native=2
        )
        try:
            result = await cached_result(service, provider, runtime, monkeypatch)
            jobs = await runtime.store.list_jobs()
            parent = next(j for j in jobs if not j["parent_job_id"])
            child = next(j for j in jobs if j["parent_job_id"])
            # Represent an interrupted historical batch, without attempting to
            # transition a completed job through the public state machine.
            with runtime.store._connect() as conn:
                conn.execute(
                    "UPDATE comfy_jobs SET status=?,finished_at=? WHERE id=?",
                    (status, None if status == "running" else time.time(), child["id"]),
                )
            before = {p.name for p in runtime.store.blobs_dir.glob("*.image")}
            await service.store.delete_generation(result.generation_id)
            await runtime.store.cleanup_terminal_files(
                terminal_before=time.time() - RECOVERY_SECONDS
            )
            assert before == {p.name for p in runtime.store.blobs_dir.glob("*.image")}
            assert len(await runtime.store.load_outputs(parent["id"])) == 3
        finally:
            await runtime.close()

    asyncio.run(run())


def test_shared_blob_keeps_other_task_owner_after_published_link_is_removed(
    tmp_path, monkeypatch
):
    async def run():
        runtime, service, provider, _ = await runtime_fixture(tmp_path, monkeypatch)
        try:
            result = await cached_result(
                service, provider, runtime, monkeypatch, count=1
            )
            source = (await runtime.store.list_jobs())[0]
            other = await runtime.store.create_job(
                provider_id=source["provider_id"],
                model_id=source["model_id"],
                revision_id=source["revision_id"],
                request=source["request"],
                job_id="other_owner",
            )
            await runtime.store.save_outputs(other["id"], result.images)
            await service.store.delete_generation(result.generation_id)
            assert len(list(runtime.store.blobs_dir.glob("*.image"))) == 1
            assert (await runtime.store.load_outputs(other["id"]))[
                0
            ].data == result.images[0].data
            assert (await runtime.store.get_job(source["id"]))["outputs"][0][
                "storage"
            ] == "expired"
        finally:
            await runtime.close()

    asyncio.run(run())


def test_success_without_gallery_publication_keeps_result_within_window(
    tmp_path, monkeypatch
):
    async def run():
        runtime, service, provider, _ = await runtime_fixture(
            tmp_path, monkeypatch, history=False
        )
        try:
            result = await service.generate(
                mode="text2img", provider_id=provider.id, prompt=""
            )
            assert not result.generation_id
            job = (await runtime.store.list_jobs())[0]
            assert await runtime.store.reconcile_gallery_outputs() == 0
            assert await runtime.store.load_outputs(job["id"])
        finally:
            await runtime.close()

    asyncio.run(run())


def test_retained_gallery_link_with_missing_file_keeps_cache(tmp_path, monkeypatch):
    async def run():
        runtime, service, provider, _ = await runtime_fixture(tmp_path, monkeypatch)
        try:
            await cached_result(service, provider, runtime, monkeypatch, count=1)
            with service.store._connect() as conn:
                conn.execute("UPDATE image_assets SET file_state='missing'")
            assert await runtime.store.reconcile_gallery_outputs() == 0
            assert len(list(runtime.store.blobs_dir.glob("*.image"))) == 1
        finally:
            await runtime.close()

    asyncio.run(run())


@pytest.mark.parametrize("hours,expired", [(23, False), (25, True)])
def test_recovery_window_is_24_hours_before_and_after_maintenance(
    tmp_path, monkeypatch, hours, expired
):
    async def run():
        runtime, service, provider, _ = await runtime_fixture(tmp_path, monkeypatch)
        try:
            await cached_result(service, provider, runtime, monkeypatch, count=1)
            job = (await runtime.store.list_jobs())[0]
            with runtime.store._connect() as conn:
                conn.execute(
                    "UPDATE comfy_jobs SET status='failed',finished_at=? WHERE id=?",
                    (time.time() - hours * 3600, job["id"]),
                )
            assert (
                ComfyRuntime.public_job(await runtime.store.get_job(job["id"]))[
                    "can_resume"
                ]
                is not expired
            )
            await runtime.store.cleanup_terminal_files(
                terminal_before=time.time() - RECOVERY_SECONDS
            )
            after = await runtime.store.get_job(job["id"])
            assert bool(after["archive_state"]) is expired
            if expired:
                with pytest.raises(ValueError, match="24 小时"):
                    await runtime.store.resume_job(job["id"])
            else:
                assert (await runtime.store.resume_job(job["id"]))[
                    "status"
                ] == "submitted"
        finally:
            await runtime.close()

    asyncio.run(run())


def test_failed_post_delete_cleanup_is_retried_by_maintenance(tmp_path, monkeypatch):
    async def run():
        runtime, service, provider, _ = await runtime_fixture(tmp_path, monkeypatch)
        try:
            result = await cached_result(
                service, provider, runtime, monkeypatch, count=1
            )

            async def failed_cleanup(_ids):
                raise OSError("temporary disk failure")

            service.store.on_results_removed = failed_cleanup
            assert await service.store.delete_generation(result.generation_id)
            assert (
                await runtime.store.cleanup_terminal_files(
                    terminal_before=time.time() - RECOVERY_SECONDS
                )
                == 1
            )
            assert not list(runtime.store.blobs_dir.glob("*.image"))
        finally:
            await runtime.close()

    asyncio.run(run())
