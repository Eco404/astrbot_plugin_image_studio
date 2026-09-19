"""Durable recovery and owner-aware cleanup of compact ComfyUI storage."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from contextlib import closing

import pytest

from astrbot_plugin_image_studio.backend.database.payloads import (
    gc_payloads,
    release_payloads,
)
from astrbot_plugin_image_studio.backend.models import GeneratedImage, ReferenceImage
from astrbot_plugin_image_studio.backend.providers.comfyui import jobs as jobs_module
from astrbot_plugin_image_studio.backend.providers.comfyui import (
    storage as compact_storage,
)
from astrbot_plugin_image_studio.backend.providers.comfyui.client import (
    ComfyExecutionError,
)
from astrbot_plugin_image_studio.backend.providers.comfyui.jobs import (
    RECOVERY_SECONDS,
    ComfyJobStore,
)
from astrbot_plugin_image_studio.backend.providers.comfyui.runtime import ComfyRuntime
from .test_comfy_runtime import image, runtime_fixture, workflow
from .test_storage_v4_schema import create_v3, seed_legacy_workflow, snapshot


GRAPH = {
    "91": {"class_type": "Seed (rgthree)", "inputs": {"seed": 18446744073709551614}}
}
CONFIG = {
    "api_graph": GRAPH,
    "api_graph_json": json.dumps(GRAPH),
    "workflow": {"nodes": []},
}


async def prepared(tmp_path):
    store = ComfyJobStore(tmp_path / "history.sqlite3")
    await store.initialize()
    revision = await store.save_revision(CONFIG)
    return store, revision


async def task(store, revision, identifier, **request):
    return await store.create_job(
        provider_id="p",
        model_id="m",
        revision_id=revision,
        job_id=identifier,
        request={"model": {"name": "workflow", "comfyui": CONFIG}, **request},
        references=(ReferenceImage("ref", "reference.png", b"reference", "image/png"),),
    )


def age(store, *identifiers):
    with store._connect() as conn:
        for identifier in identifiers:
            conn.execute(
                "UPDATE comfy_jobs SET finished_at=? WHERE id=?",
                (time.time() - RECOVERY_SECONDS - 60, identifier),
            )
        conn.execute(
            "UPDATE comfy_workflow_revisions SET created_at=?",
            (time.time() - RECOVERY_SECONDS - 60,),
        )


def test_shared_snapshots_and_light_queue_never_decode_heavy_bodies(
    tmp_path, monkeypatch
):
    async def run():
        store, revision = await prepared(tmp_path)
        await task(store, revision, "parent")
        await task(store, revision, "child", parent_job_id="parent")
        output = GeneratedImage(
            b"png", "image/png", {"_comfyui": CONFIG, "seed": 18446744073709551614}
        )
        for identifier in ("parent", "child"):
            await store.save_outputs(identifier, (output,))
            await store.update_job(
                identifier,
                status="running",
                result={"api_graph": GRAPH, "workflow": CONFIG["workflow"]},
            )
        with store._connect() as conn:
            raw = conn.execute(
                "SELECT request_json,output_refs_json,result_json FROM comfy_jobs WHERE id='parent'"
            ).fetchone()
            assert "api_graph" not in raw[0]
            assert "18446744073709551614" not in raw[2]
            assert "_execution_payload" in raw[2]
            assert (
                conn.execute(
                    "SELECT COUNT(DISTINCT payload_id) FROM storage_payload_refs WHERE slot IN ('config','output:0')"
                ).fetchone()[0]
                == 1
            )
        assert await store.load_outputs("child") == (output,)
        assert (await store.get_job("parent"))["request"]["model"]["comfyui"] == CONFIG
        monkeypatch.setattr(
            jobs_module,
            "load_payload",
            lambda *_: pytest.fail("queue decoded a heavy payload"),
        )
        assert [
            job["id"]
            for job in await store.list_jobs(queue_only=True, include_children=False)
        ] == ["parent"]
        assert (await store.get_job("parent", light=True))["model_name"] == "workflow"
        await store.update_progress("parent", {"completed": 1}, status="running")
        assert (await store.get_job("parent", light=True))["result"]["progress"] == {
            "completed": 1
        }

    asyncio.run(run())


@pytest.mark.parametrize("parent_status", ["unknown", "failed", "running"])
def test_recoverable_parent_preserves_expired_successful_child(tmp_path, parent_status):
    async def run():
        store, revision = await prepared(tmp_path)
        await task(store, revision, "parent")
        await task(store, revision, "child", parent_job_id="parent")
        await store.save_outputs(
            "child", (GeneratedImage(b"output", "image/png", {"_comfyui": CONFIG}),)
        )
        await store.update_job("child", status="succeeded")
        await store.update_job(
            "parent", status=parent_status, result={"child_ids": ["child"]}
        )
        age(store, "child")
        await store.cleanup_terminal_files(
            terminal_before=time.time() - RECOVERY_SECONDS
        )
        assert not (await store.get_job("child"))["archive_state"]
        assert await store.load_references("child")
        assert await store.load_outputs("child")
        assert await store.get_revision(revision) == CONFIG

    asyncio.run(run())


def test_unknown_descendant_protects_expired_parent_and_siblings(tmp_path):
    async def run():
        store, revision = await prepared(tmp_path)
        for identifier, parent in (
            ("parent", ""),
            ("uncertain", "parent"),
            ("success", "parent"),
        ):
            await task(store, revision, identifier, parent_job_id=parent)
        await store.update_job(
            "parent", status="failed", result={"child_ids": ["uncertain", "success"]}
        )
        await store.update_job("uncertain", status="unknown")
        await store.update_job("success", status="succeeded")
        age(store, "parent", "uncertain", "success")
        await store.cleanup_terminal_files(
            terminal_before=time.time() - RECOVERY_SECONDS
        )
        assert all(not job["archive_state"] for job in await store.list_jobs())
        parent = await store.get_job("parent")
        assert ComfyRuntime.public_job(parent)["can_resume"] is True
        assert (await store.resume_job("parent"))["status"] == "submitted"

    asyncio.run(run())


def test_expired_failure_is_not_resumable_and_keeps_idempotency_tombstone(tmp_path):
    async def run():
        store, revision = await prepared(tmp_path)
        await task(store, revision, "expired")
        await store.update_job(
            "expired",
            status="failed",
            remote_id="confirmed",
            result={"api_graph": GRAPH},
        )
        age(store, "expired")
        assert (
            ComfyRuntime.public_job(await store.get_job("expired"))["can_resume"]
            is False
        )
        with pytest.raises(ValueError, match="恢复期限"):
            await store.resume_job("expired")
        await store.cleanup_terminal_files(
            terminal_before=time.time() - RECOVERY_SECONDS
        )
        with store._connect() as conn:
            gc_payloads(conn)
            assert (
                conn.execute("SELECT COUNT(*) FROM storage_payload_refs").fetchone()[0]
                == 0
            )
            assert (
                conn.execute("SELECT COUNT(*) FROM storage_payloads").fetchone()[0] == 0
            )
        retry = await task(store, revision, "expired")
        assert retry["archive_state"] == "archived"
        assert retry["remote_id"] == "confirmed"
        assert not list(store.blobs_dir.glob("*.image"))
        with pytest.raises(ValueError, match="不同请求"):
            await task(store, revision, "expired", changed=True)

    asyncio.run(run())


def test_legacy_input_output_migration_and_shared_blob_owners(tmp_path):
    async def run():
        store, revision = await prepared(tmp_path)
        await task(store, revision, "legacy")
        await store.update_job("legacy", status="running")
        # Simulate a published pre-migration manifest, with the same bytes in
        # both input and output directories. A move must not rewrite the image.
        original = b"legacy image and metadata bytes"
        digest = hashlib.sha256(original).hexdigest()
        for root in (store.inputs_dir, store.outputs_dir):
            (root / "legacy").mkdir(parents=True)
            (root / "legacy/0.image").write_bytes(original)
        ref = {
            "path": "legacy/0.image",
            "sha256": digest,
            "size_bytes": len(original),
            "id": "ref",
            "filename": "ref.png",
            "mime_type": "image/png",
        }
        out = {**ref, "effective_parameters": {}, "response_index": 0}
        with store._connect() as conn:
            conn.execute(
                "UPDATE comfy_jobs SET input_refs_json=?,output_refs_json=? WHERE id='legacy'",
                (json.dumps([ref]), json.dumps([out])),
            )
        removed = await store.cleanup_terminal_files(
            terminal_before=time.time() - RECOVERY_SECONDS
        )
        assert (await store.load_references("legacy"))[0].data == original
        assert (await store.load_outputs("legacy"))[0].data == original
        assert not (store.inputs_dir / "legacy").exists()
        assert not (store.outputs_dir / "legacy").exists()
        assert len(list(store.blobs_dir.glob("*.image"))) == 1
        # Two migrated legacy files plus the superseded unowned reference blob.
        assert removed == 3
        assert (
            await store.cleanup_terminal_files(
                terminal_before=time.time() - RECOVERY_SECONDS
            )
            == 0
        )

    asyncio.run(run())


def test_success_reuses_gallery_and_deletion_is_effective_before_expiry(
    tmp_path, monkeypatch
):
    async def run():
        runtime, service, provider, _ = await runtime_fixture(tmp_path, monkeypatch)
        try:
            result = await service.generate(
                mode="text2img", provider_id=provider.id, prompt=""
            )
            assert result.images
            assert not list(runtime.store.blobs_dir.glob("*.image"))
            job = (await runtime.store.list_jobs())[0]
            assert job["outputs"][0]["storage"] == "gallery"
            await service.store.delete_generation(result.generation_id)
            deleted = await runtime.store.get_job(job["id"])
            assert ComfyRuntime.public_job(deleted)["result_available"] is False
            with pytest.raises(ValueError, match="画廊"):
                await runtime.result(deleted)
        finally:
            await runtime.close()

    asyncio.run(run())


def test_maintenance_counts_cache_released_to_gallery(tmp_path, monkeypatch):
    async def run():
        runtime, service, provider, _ = await runtime_fixture(tmp_path, monkeypatch)
        original_release = runtime.store.release_gallery_outputs

        async def postpone_release(*_args):
            return 0

        monkeypatch.setattr(runtime.store, "release_gallery_outputs", postpone_release)
        try:
            result = await service.generate(
                mode="text2img", provider_id=provider.id, prompt=""
            )
            assert result.images
            assert len(list(runtime.store.blobs_dir.glob("*.image"))) == 1
            monkeypatch.setattr(
                runtime.store, "release_gallery_outputs", original_release
            )
            assert (
                await runtime.store.cleanup_terminal_files(
                    terminal_before=time.time() - RECOVERY_SECONDS
                )
                == 1
            )
            assert not list(runtime.store.blobs_dir.glob("*.image"))
            assert (
                await runtime.store.cleanup_terminal_files(
                    terminal_before=time.time() - RECOVERY_SECONDS
                )
                == 0
            )
        finally:
            await runtime.close()

    asyncio.run(run())


def test_confirmed_remote_recovery_does_not_require_expired_reference_bytes(
    tmp_path, monkeypatch
):
    async def run():
        runtime, service, provider, client = await runtime_fixture(
            tmp_path, monkeypatch, config=workflow(reference_count=1)
        )
        try:
            client.wait_error = ComfyExecutionError("connection interrupted")
            job = await runtime.submit(
                provider=provider,
                model=provider.models[0],
                mode="img2img",
                prompt="",
                references=(
                    ReferenceImage(
                        "base", "base.png", image("yellow").data, "image/png"
                    ),
                ),
            )
            failed = await runtime.manager.wait(job["id"])
            assert failed["status"] == "failed" and failed["remote_id"]
            (runtime.store.blobs_dir / failed["references"][0]["path"]).unlink()
            client.wait_error = None
            await runtime.manager.resume(job["id"])
            done = await runtime.manager.wait(job["id"])
            assert done["status"] == "succeeded", done["error"]
            assert len([call for call in client.calls if call[0] == "submit"]) == 1
            assert len([call for call in client.calls if call[0] == "upload"]) == 1
            result = await runtime.result(done)
            assert result.images and "参考图已不可用" in result.warning
            detail = await service.store.generation_detail(
                result.generation_id, light=True
            )
            assert detail["references"] == []
        finally:
            await runtime.close()

    asyncio.run(run())


def test_archive_commits_before_file_collection_failure(tmp_path, monkeypatch):
    async def run():
        store, revision = await prepared(tmp_path)
        await task(store, revision, "expired")
        await store.update_job("expired", status="failed", remote_id="confirmed")
        age(store, "expired")
        collector = store._collect_unreferenced_blobs

        def fail(_connection):
            raise OSError("injected disk failure")

        monkeypatch.setattr(store, "_collect_unreferenced_blobs", fail)
        with pytest.raises(OSError, match="injected"):
            await store.cleanup_terminal_files(
                terminal_before=time.time() - RECOVERY_SECONDS
            )
        archived = await store.get_job("expired")
        assert archived["archive_state"] == "archived"
        assert archived["references"][0]["storage"] == "expired"
        assert ComfyRuntime.public_job(archived)["can_resume"] is False
        monkeypatch.setattr(store, "_collect_unreferenced_blobs", collector)
        await store.cleanup_terminal_files(
            terminal_before=time.time() - RECOVERY_SECONDS
        )
        assert not list(store.blobs_dir.glob("*.image"))

    asyncio.run(run())


def test_batch_remote_recovery_uses_existing_children_when_inputs_are_missing(
    tmp_path, monkeypatch
):
    async def run():
        runtime, service, provider, client = await runtime_fixture(
            tmp_path, monkeypatch, config=workflow(reference_count=1)
        )
        try:
            client.wait_error = ComfyExecutionError("connection interrupted")
            job = await runtime.submit(
                provider=provider,
                model=provider.models[0],
                mode="img2img",
                prompt="",
                count=2,
                references=(
                    ReferenceImage(
                        "base", "base.png", image("yellow").data, "image/png"
                    ),
                ),
            )
            failed = await runtime.manager.wait(job["id"])
            assert (
                failed["status"] == "failed" and len(failed["result"]["child_ids"]) == 2
            )
            (runtime.store.blobs_dir / failed["references"][0]["path"]).unlink()
            client.wait_error = None
            await runtime.manager.resume(job["id"])
            done = await runtime.manager.wait(job["id"])
            assert done["status"] == "succeeded", done["error"]
            assert len([call for call in client.calls if call[0] == "submit"]) == 2
            assert len([call for call in client.calls if call[0] == "upload"]) == 2
            result = await runtime.result(done)
            assert len(result.images) == 2 and "参考图已不可用" in result.warning
            assert (
                await service.store.generation_detail(result.generation_id, light=True)
            )["references"] == []
        finally:
            await runtime.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "cache_root", ["comfyui_blobs", "comfyui_inputs", "comfyui_outputs"]
)
def test_maintenance_rejects_cache_root_symlinks(tmp_path, cache_root):
    async def run():
        data_dir = tmp_path / "plugin"
        data_dir.mkdir()
        store, _ = await prepared(data_dir)
        external = tmp_path / "other-plugin"
        external.mkdir()
        sentinel = external / f"{hashlib.sha256(b'keep').hexdigest()}.image"
        sentinel.write_bytes(b"keep")
        (data_dir / cache_root).symlink_to(external, target_is_directory=True)
        with pytest.raises(ValueError, match="缓存目录"):
            await store.cleanup_terminal_files(
                terminal_before=time.time() - RECOVERY_SECONDS
            )
        assert sentinel.read_bytes() == b"keep"

    asyncio.run(run())


def test_owner_and_payload_reads_hold_one_snapshot(tmp_path, monkeypatch):
    async def run():
        store, revision = await prepared(tmp_path)
        await task(store, revision, "read")
        await store.save_outputs(
            "read", (GeneratedImage(b"output", "image/png", {"_comfyui": CONFIG}),)
        )
        original = jobs_module.load_payload
        observed = []

        def checked(connection, marker):
            observed.append(connection.in_transaction)
            assert connection.in_transaction, (
                "owner read and payload read must share a snapshot"
            )
            return original(connection, marker)

        monkeypatch.setattr(jobs_module, "load_payload", checked)
        monkeypatch.setattr(compact_storage, "load_payload", checked)
        for read in (
            lambda: store.get_revision(revision),
            lambda: store.get_job("read"),
            store.list_jobs,
            lambda: store.load_outputs("read"),
        ):
            before = len(observed)
            await read()
            assert len(observed) > before

    asyncio.run(run())


def test_revision_reader_survives_concurrent_body_collection(tmp_path, monkeypatch):
    async def run():
        store, revision = await prepared(tmp_path)
        with store._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
        original = jobs_module.load_payload
        collected = False

        def collect_between_owner_and_body(connection, marker):
            nonlocal collected
            if not collected:
                collected = True
                with store._connect() as writer:
                    writer.execute(
                        "UPDATE comfy_workflow_revisions SET config_json='{}' WHERE id=?",
                        (revision,),
                    )
                    release_payloads(writer, "comfy_workflow_revisions", revision)
                    gc_payloads(writer)
            return original(connection, marker)

        monkeypatch.setattr(jobs_module, "load_payload", collect_between_owner_and_body)
        assert await store.get_revision(revision) == CONFIG
        assert collected
        assert await store.get_revision(revision) == {}

    asyncio.run(run())


def test_gallery_snapshot_read_stays_in_transaction(tmp_path, monkeypatch):
    async def run():
        runtime, service, provider, _ = await runtime_fixture(tmp_path, monkeypatch)
        try:
            await service.generate(mode="text2img", provider_id=provider.id, prompt="")
            job = (await runtime.store.list_jobs())[0]
            descriptor = {**job["outputs"][0], "effective_parameters": {}}
            original = jobs_module.load_payload
            observed = []

            def checked(connection, marker):
                observed.append(connection.in_transaction)
                assert connection.in_transaction
                return original(connection, marker)

            monkeypatch.setattr(jobs_module, "load_payload", checked)
            restored = await runtime.store.output_parameters(descriptor)
            assert observed and restored["_comfyui"]
        finally:
            await runtime.close()

    asyncio.run(run())


def test_unused_revision_gc_keeps_recent_create_window_and_referenced_tombstones(
    tmp_path,
):
    async def run():
        store, retained = await prepared(tmp_path)
        await task(store, retained, "owner")
        old = await store.save_revision({**CONFIG, "name": "old failed create"})
        recent = await store.save_revision({**CONFIG, "name": "new create in progress"})
        with store._connect() as connection:
            connection.execute(
                "UPDATE comfy_workflow_revisions SET created_at=? WHERE id=?",
                (time.time() - RECOVERY_SECONDS - 60, old),
            )
        await store.cleanup_terminal_files(
            terminal_before=time.time() - RECOVERY_SECONDS
        )
        assert await store.get_revision(old) is None
        assert await store.get_revision(recent) is not None
        assert await store.get_revision(retained) == CONFIG
        with store._connect() as connection:
            assert (
                connection.execute(
                    "SELECT 1 FROM storage_payload_refs WHERE owner_table='comfy_workflow_revisions' AND owner_id=?",
                    (old,),
                ).fetchone()
                is None
            )

    asyncio.run(run())


def test_inconsistent_legacy_request_snapshot_aborts_migration_without_loss(tmp_path):
    from astrbot_plugin_image_studio.backend.database import schema

    with closing(sqlite3.connect(tmp_path / "history.sqlite3")) as connection:
        create_v3(connection)
        seed_legacy_workflow(connection)
        request = json.loads(
            connection.execute(
                "SELECT request_json FROM comfy_jobs WHERE id='second'"
            ).fetchone()[0]
        )
        request["model"]["comfyui"]["api_graph"]["1"]["inputs"]["seed"] = 123
        connection.execute(
            "UPDATE comfy_jobs SET request_json=? WHERE id='second'",
            (json.dumps(request),),
        )
        connection.commit()
        before = snapshot(connection)
        with pytest.raises(ValueError, match="second.*工作流快照与修订不一致"):
            schema.ensure_release_schema(connection, backup_dir=tmp_path / "backups")
        assert snapshot(connection) == before
        assert not connection.in_transaction
        backups = list((tmp_path / "backups").glob("*.sqlite3"))
        assert len(backups) == 1
        with closing(sqlite3.connect(backups[0])) as backup:
            assert snapshot(backup) == before
