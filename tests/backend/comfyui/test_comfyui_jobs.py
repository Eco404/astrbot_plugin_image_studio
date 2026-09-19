from __future__ import annotations

import asyncio
import sqlite3
import time
from contextlib import closing

import pytest
from astrbot_plugin_image_studio.backend.database import schema as schema
from astrbot_plugin_image_studio.backend.providers.comfyui.job_manager import (
    ComfyJobManager,
)
from astrbot_plugin_image_studio.backend.providers.comfyui.job_store import (
    ComfyJobStore,
)
from astrbot_plugin_image_studio.backend.models import GeneratedImage, ReferenceImage
from astrbot_plugin_image_studio.tests.support.schema_upgrade_fixtures import (
    create_v2,
    create_v3_dev,
)

WORKFLOW = {
    "api_graph": {"3": {"class_type": "Sampler", "inputs": {"seed": 10}}},
    "outputs": ["9"],
}


async def store_and_revision(tmp_path):
    store = ComfyJobStore(tmp_path / "history.sqlite3")
    await store.initialize()
    return store, await store.save_revision(WORKFLOW)


def test_published_v2_upgrade_has_verified_backup_and_preserves_data(tmp_path):
    database = tmp_path / "history.sqlite3"
    with closing(sqlite3.connect(database)) as conn:
        create_v2(conn)
        conn.execute(
            "INSERT INTO image_assets VALUES ('asset','kept.png','image/png',5,1,1,123,'available')"
        )
        conn.commit()
        before = tuple(conn.iterdump())
        backup = schema.ensure_release_schema(conn, backup_dir=tmp_path / "backups")
        assert backup
        with closing(sqlite3.connect(backup)) as saved:
            assert tuple(saved.iterdump()) == before
        assert conn.execute("PRAGMA user_version").fetchone() == (3,)
        assert conn.execute(
            "SELECT target_version,dev_revision FROM schema_meta"
        ).fetchone() == (4, 2)
        assert conn.execute("SELECT path FROM image_assets").fetchall() == [
            ("kept.png",)
        ]
        assert (
            schema.ensure_release_schema(conn, backup_dir=tmp_path / "backups") is None
        )


@pytest.mark.parametrize(
    "corruption", ["marker", "missing_table", "missing_index", "unregistered"]
)
def test_current_development_corruption_rejected_before_mutation(tmp_path, corruption):
    with closing(sqlite3.connect(tmp_path / "history.sqlite3")) as conn:
        create_v3_dev(conn)
        if corruption == "marker":
            conn.execute("UPDATE schema_meta SET dev_revision=99")
        elif corruption == "missing_table":
            conn.execute("DROP TABLE comfy_jobs")
        elif corruption == "missing_index":
            conn.execute("DROP INDEX idx_comfy_jobs_remote")
        else:
            conn.execute("DROP TABLE schema_meta")
        conn.commit()
        before = tuple(conn.iterdump())
        with pytest.raises(RuntimeError):
            schema.ensure_release_schema(conn, backup_dir=tmp_path / "backups")
        assert tuple(conn.iterdump()) == before
        assert not (tmp_path / "backups").exists()


def test_revisions_freeze_configs_and_reject_provider_secrets(tmp_path):
    async def run():
        store, revision = await store_and_revision(tmp_path)
        assert (
            await store.save_revision(
                {"outputs": ["9"], "api_graph": WORKFLOW["api_graph"]}
            )
            == revision
        )
        changed = {
            "api_graph": {"3": {"class_type": "Sampler", "inputs": {"seed": 11}}}
        }
        assert await store.save_revision(changed) != revision
        assert await store.get_revision(revision) == WORKFLOW
        with pytest.raises(ValueError, match="密钥"):
            await store.save_revision({"api_key": "do-not-persist"})
        with pytest.raises(ValueError, match="密钥"):
            await store.create_job(
                provider_id="p",
                model_id="m",
                revision_id=revision,
                request={"headers": {"Authorization": "do-not-persist"}},
            )
        assert b"do-not-persist" not in store.db_path.read_bytes()

    asyncio.run(run())


def test_job_retries_freeze_reference_order_without_duplicate_files(tmp_path):
    async def run():
        store, revision = await store_and_revision(tmp_path)
        references = (
            ReferenceImage("a", "../../same.png", b"same-image", "image/png"),
            ReferenceImage("b", "other.png", b"same-image", "image/png"),
        )
        kwargs = dict(
            provider_id="p",
            model_id="m",
            revision_id=revision,
            request={"prompt": "x"},
            references=references,
            job_id="same-job",
        )
        results = await asyncio.gather(*(store.create_job(**kwargs) for _ in range(4)))
        assert len({item["id"] for item in results}) == 1
        assert await store.load_references("same-job") == references
        assert len(list(store.blobs_dir.glob("*.image"))) == 1
        with pytest.raises(ValueError, match="不同参考图"):
            await store.create_job(**{**kwargs, "references": references[:1]})
        with pytest.raises(ValueError, match="不同请求"):
            await store.create_job(**{**kwargs, "request": {"prompt": "changed"}})
        assert await store.load_references("same-job") == references

    asyncio.run(run())


def test_job_staging_never_removes_preexisting_directory_on_failed_create(tmp_path):
    async def run():
        store, revision = await store_and_revision(tmp_path)
        directory = store.inputs_dir / "existing"
        directory.mkdir(parents=True)
        original = directory / "retained.image"
        original.write_bytes(b"previous-process-orphan")
        with pytest.raises(FileExistsError):
            await store.create_job(
                provider_id="p",
                model_id="m",
                revision_id=revision,
                request={},
                references=(ReferenceImage("x", "x.png", b"new", "image/png"),),
                job_id="existing",
            )
        assert original.read_bytes() == b"previous-process-orphan"

    asyncio.run(run())


def test_tampered_input_and_output_files_report_precise_errors(tmp_path):
    async def run():
        store, revision = await store_and_revision(tmp_path)
        job = await store.create_job(
            provider_id="p",
            model_id="m",
            revision_id=revision,
            request={},
            references=(ReferenceImage("x", "portrait.png", b"original", "image/png"),),
        )
        (store.blobs_dir / job["references"][0]["path"]).write_bytes(b"replaced")
        with pytest.raises(ValueError, match="portrait.png"):
            await store.load_references(job["id"])
        outputs = (GeneratedImage(b"output", "image/png", {"seed": 1}, 0),)
        descriptors = await store.save_outputs(job["id"], outputs)
        assert await store.load_outputs(job["id"]) == outputs
        assert await store.save_outputs(job["id"], outputs) == descriptors
        with pytest.raises(ValueError, match="不同的输出"):
            await store.save_outputs(
                job["id"], (GeneratedImage(b"changed", "image/png"),)
            )
        (store.blobs_dir / descriptors[0]["path"]).unlink()
        with pytest.raises(ValueError, match="第 1 张输出图片"):
            await store.load_outputs(job["id"])

    asyncio.run(run())


def test_remote_id_is_immutable_and_completion_cannot_be_reopened(tmp_path):
    async def run():
        store, revision = await store_and_revision(tmp_path)
        job = await store.create_job(
            provider_id="p", model_id="m", revision_id=revision, request={}
        )
        await store.update_job(job["id"], status="submitted", remote_id="remote-one")
        untouched = await store.update_job(
            job["id"], status="unknown", expected_status="submitting"
        )
        assert untouched["status"] == "submitted"
        with pytest.raises(ValueError, match="不能重新绑定"):
            await store.update_job(job["id"], remote_id="remote-two")
        finished = await store.update_job(
            job["id"], status="succeeded", generation_id="gallery-one"
        )
        assert finished["finished_at"]
        with pytest.raises(ValueError, match="已经结束"):
            await store.update_job(job["id"], status="submitting")

    asyncio.run(run())


def test_browser_wait_cancellation_does_not_cancel_remote_job(tmp_path):
    async def run():
        store, _ = await store_and_revision(tmp_path)
        started, finish = asyncio.Event(), asyncio.Event()
        calls = []

        async def execute(job):
            calls.append(job["id"])
            await store.update_job(job["id"], status="submitted", remote_id="remote-id")
            started.set()
            await finish.wait()
            return {"generation_id": "gallery-result"}

        manager = ComfyJobManager(store, execute)
        args = dict(
            provider_id="p",
            model_id="m",
            workflow=WORKFLOW,
            request={},
            job_id="one-job",
        )
        job = await manager.submit(**args)
        await started.wait()
        waiter = asyncio.create_task(manager.wait(job["id"]))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        await manager.submit(**args)
        finish.set()
        result = await manager.wait(job["id"])
        assert result["status"] == "succeeded"
        assert result["result"]["generation_id"] == "gallery-result"
        assert calls == [job["id"]]
        await manager.close()

    asyncio.run(run())


def test_restart_resumes_known_remote_id_but_never_resubmits_uncertain_job(tmp_path):
    async def run():
        store, revision = await store_and_revision(tmp_path)
        uncertain = await store.create_job(
            provider_id="p", model_id="m", revision_id=revision, request={}
        )
        await store.update_job(uncertain["id"], status="submitting")
        confirmed = await store.create_job(
            provider_id="p", model_id="m", revision_id=revision, request={}
        )
        await store.update_job(
            confirmed["id"], status="running", remote_id="existing-prompt"
        )
        calls = []

        async def resume(job):
            calls.append((job["id"], job["remote_id"]))
            return {"status": "partial", "generation_id": "result"}

        manager = ComfyJobManager(store, resume)
        await manager.resume_pending()
        assert (await manager.wait(uncertain["id"]))["status"] == "unknown"
        assert (await manager.wait(confirmed["id"]))["status"] == "partial"
        assert calls == [(confirmed["id"], "existing-prompt")]
        await manager.close()

    asyncio.run(run())


def test_shutdown_preserves_phase_and_inputs_for_restart(tmp_path):
    async def run():
        store, _ = await store_and_revision(tmp_path)
        started = asyncio.Event()

        async def execute(job):
            await store.update_job(job["id"], status="submitting")
            started.set()
            await asyncio.Future()

        manager = ComfyJobManager(store, execute)
        job = await manager.submit(
            provider_id="p",
            model_id="m",
            workflow=WORKFLOW,
            request={},
            references=(ReferenceImage("a", "a.png", b"keep", "image/png"),),
        )
        await started.wait()
        await manager.close()
        assert (await store.get_job(job["id"]))["status"] == "submitting"
        assert (await store.load_references(job["id"]))[0].data == b"keep"
        with pytest.raises(RuntimeError, match="已关闭"):
            await manager.submit(
                provider_id="p", model_id="m", workflow=WORKFLOW, request={}
            )

    asyncio.run(run())


def test_explicit_resume_only_monitors_confirmed_remote_tasks(tmp_path):
    async def run():
        store, revision = await store_and_revision(tmp_path)
        failed = await store.create_job(
            provider_id="p", model_id="m", revision_id=revision, request={}
        )
        await store.update_job(
            failed["id"],
            status="failed",
            remote_id="already-submitted",
            error="timeout",
        )
        unknown = await store.create_job(
            provider_id="p", model_id="m", revision_id=revision, request={}
        )
        await store.update_job(
            unknown["id"], status="unknown", error="disconnected during post"
        )
        calls = []

        async def execute(job):
            calls.append(job["remote_id"])
            return {"generation_id": "resumed-gallery-result"}

        manager = ComfyJobManager(store, execute)
        resumed = await manager.resume(failed["id"])
        assert resumed["status"] == "submitted"
        assert resumed["error"] == ""
        assert (await manager.wait(failed["id"]))["status"] == "succeeded"
        with pytest.raises(ValueError, match="远端编号"):
            await manager.resume(unknown["id"])
        assert calls == ["already-submitted"]
        await manager.close()

    asyncio.run(run())


def test_repeated_startup_recovery_does_not_invalidate_active_submission(tmp_path):
    async def run():
        store, _ = await store_and_revision(tmp_path)
        started, finish = asyncio.Event(), asyncio.Event()

        async def execute(job):
            await store.update_job(job["id"], status="submitting")
            started.set()
            await finish.wait()
            await store.update_job(job["id"], status="submitted", remote_id="confirmed")
            return {}

        manager = ComfyJobManager(store, execute)
        job = await manager.submit(
            provider_id="p", model_id="m", workflow=WORKFLOW, request={}
        )
        await started.wait()
        await manager.resume_pending()
        assert (await store.get_job(job["id"]))["status"] == "submitting"
        finish.set()
        assert (await manager.wait(job["id"]))["status"] == "succeeded"
        await manager.close()

    asyncio.run(run())


def test_cleanup_only_removes_expired_terminal_staging_not_unknown_or_gallery(tmp_path):
    async def run():
        store, revision = await store_and_revision(tmp_path)
        identifiers = {}
        for status in ("succeeded", "running", "unknown"):
            job = await store.create_job(
                provider_id="p",
                model_id="m",
                revision_id=revision,
                request={},
                references=(ReferenceImage("a", "a.png", b"keep", "image/png"),),
            )
            await store.save_outputs(
                job["id"], (GeneratedImage(b"output", "image/png"),)
            )
            await store.update_job(job["id"], status=status)
            identifiers[status] = job["id"]
        assert await store.cleanup_terminal_files(terminal_before=time.time() + 1) == 0
        assert (await store.get_job(identifiers["succeeded"]))[
            "archive_state"
        ] == "archived"
        with pytest.raises(ValueError, match="已过期"):
            await store.load_references(identifiers["succeeded"])
        for status in ("running", "unknown"):
            assert await store.load_references(identifiers[status])
            assert await store.load_outputs(identifiers[status])
        assert await store.get_revision(revision) == WORKFLOW
        assert await store.cleanup_terminal_files(terminal_before=time.time() + 1) == 0

    asyncio.run(run())


def test_queue_filter_runs_before_limit_and_does_not_hide_old_active_or_failed_jobs(
    tmp_path,
):
    async def run():
        store, revision = await store_and_revision(tmp_path)
        active = await store.create_job(
            provider_id="p", model_id="m", revision_id=revision, request={}
        )
        await store.update_job(active["id"], status="running")
        failed = await store.create_job(
            provider_id="p", model_id="m", revision_id=revision, request={}
        )
        await store.update_job(failed["id"], status="failed", error="retained failure")
        for _ in range(105):
            completed = await store.create_job(
                provider_id="p", model_id="m", revision_id=revision, request={}
            )
            await store.update_job(completed["id"], status="succeeded")
        child = await store.create_job(
            provider_id="p",
            model_id="m",
            revision_id=revision,
            request={"parent_job_id": active["id"]},
        )
        default = await store.list_jobs(limit=2, include_children=False)
        assert all(job["status"] == "succeeded" for job in default)
        queued = await store.list_jobs(limit=2, include_children=False, queue_only=True)
        assert [job["id"] for job in queued] == [failed["id"], active["id"]]
        assert child["id"] not in {job["id"] for job in queued}
        assert await store.list_jobs(provider_id="different", queue_only=True) == []

    asyncio.run(run())


@pytest.mark.parametrize(
    "status", ["succeeded", "partial", "failed", "cancelled", "unknown"]
)
def test_dismissing_terminal_task_persists_without_deleting_outputs_or_changing_expiry(
    tmp_path, status
):
    async def run():
        store, revision = await store_and_revision(tmp_path)
        references = (ReferenceImage("a", "a.png", b"reference", "image/png"),)
        outputs = (GeneratedImage(b"result", "image/png", {"seed": 12}, 2),)
        job = await store.create_job(
            provider_id="p",
            model_id="m",
            revision_id=revision,
            request={"prompt": "unchanged"},
            references=references,
        )
        await store.save_outputs(job["id"], outputs)
        before = await store.update_job(
            job["id"],
            status=status,
            result={"warning": "retained warning", "nested": {"keep": [1, 2]}},
            error="retained diagnostic",
            generation_id="gallery-id",
        )
        dismissed = await store.dismiss_job(job["id"])
        for name in (
            "request",
            "references",
            "outputs",
            "status",
            "error",
            "generation_id",
            "finished_at",
            "created_at",
        ):
            assert dismissed[name] == before[name]
        assert dismissed["result"] == {**before["result"], "queue_dismissed": True}
        assert await store.dismiss_job(job["id"]) == dismissed
        reopened = ComfyJobStore(store.db_path)
        assert await reopened.list_jobs(queue_only=True) == []
        assert (await reopened.list_jobs())[0]["id"] == job["id"]
        assert await reopened.load_references(job["id"]) == references
        assert await reopened.load_outputs(job["id"]) == outputs
        assert await reopened.get_revision(revision) == WORKFLOW

    asyncio.run(run())


@pytest.mark.parametrize(
    "status",
    [
        "queued",
        "preparing",
        "submitting",
        "submitted",
        "running",
        "downloading",
        "finalizing",
    ],
)
def test_dismiss_refuses_active_tasks_without_mutating_state(tmp_path, status):
    async def run():
        store, revision = await store_and_revision(tmp_path)
        job = await store.create_job(
            provider_id="p", model_id="m", revision_id=revision, request={}
        )
        before = await store.update_job(job["id"], status=status)
        with pytest.raises(ValueError, match="仍在进行中"):
            await store.dismiss_job(job["id"])
        assert await store.get_job(job["id"]) == before
        assert [item["id"] for item in await store.list_jobs(queue_only=True)] == [
            job["id"]
        ]

    asyncio.run(run())


def test_explicit_resume_reveals_dismissed_task_without_repeating_unknown_submission(
    tmp_path,
):
    async def run():
        store, revision = await store_and_revision(tmp_path)
        confirmed = await store.create_job(
            provider_id="p", model_id="m", revision_id=revision, request={}
        )
        await store.update_job(
            confirmed["id"],
            status="failed",
            remote_id="existing",
            result={"api_graph": WORKFLOW["api_graph"]},
        )
        await store.dismiss_job(confirmed["id"])
        resumed = await store.resume_job(confirmed["id"])
        assert resumed["status"] == "submitted"
        assert resumed["remote_id"] == "existing"
        assert "queue_dismissed" not in resumed["result"]
        assert resumed["result"]["api_graph"] == WORKFLOW["api_graph"]
        assert [item["id"] for item in await store.list_jobs(queue_only=True)] == [
            confirmed["id"]
        ]
        unknown = await store.create_job(
            provider_id="p", model_id="m", revision_id=revision, request={}
        )
        await store.update_job(unknown["id"], status="unknown")
        hidden = await store.dismiss_job(unknown["id"])
        with pytest.raises(ValueError, match="远端编号"):
            await store.resume_job(unknown["id"])
        assert await store.get_job(unknown["id"]) == hidden

    asyncio.run(run())
