"""Recover verified external-file changes without periodic polling or duplicate scans."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from astrbot_plugin_image_studio.external_gallery import ExternalGalleryManager
from astrbot_plugin_image_studio.storage import ExternalDeleteError
from astrbot_plugin_image_studio.tests.test_external_storage import (
    add,
    fingerprint,
    image_bytes,
    setup,
)


@pytest.mark.parametrize("change", ["missing", "changed"])
@pytest.mark.parametrize(
    "operation", ["original", "preview", "download", "reference", "detail", "export"]
)
def test_external_read_operations_report_verified_changes(tmp_path, change, operation):
    async def run():
        store, root = await setup(tmp_path)
        try:
            result, path = await add(store, root)
            reports = []
            store.set_external_issue_handler(
                lambda source, reason: reports.append((source, reason))
            )
            if change == "missing":
                path.unlink()
            else:
                path.write_bytes(image_bytes("blue"))
            if operation in {"original", "preview"}:
                await store.gallery_image_data(result["image_id"], detail=operation)
            elif operation == "download":
                assert await store.gallery_image_file(result["image_id"]) is None
            elif operation == "reference":
                with pytest.raises(ValueError, match="不存在"):
                    await store.stage_gallery_reference(result["image_id"])
            elif operation == "detail":
                await store.generation_detail(result["generation_id"])
            else:
                await store.export_generations([result["generation_id"]])
            assert reports == [("nai", change)]
        finally:
            await store.close()

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["gallery", "preview", "detail", "image-info"])
@pytest.mark.parametrize("missing_row", [False, True])
def test_missing_external_preview_reports_source(tmp_path, operation, missing_row):
    async def run():
        store, root = await setup(tmp_path)
        try:
            result, _ = await add(store, root)
            with store._connect() as conn:
                row = conn.execute("SELECT path FROM image_thumbnails").fetchone()
                (store.data_dir / row["path"]).unlink()
                if missing_row:
                    conn.execute("DELETE FROM image_thumbnails")
            reports = []
            store.set_external_issue_handler(
                lambda source, reason: reports.append((source, reason))
            )
            if operation == "gallery":
                await store.list_generations({})
            elif operation == "preview":
                assert (
                    await store.gallery_image_data(result["image_id"], detail="preview")
                    is None
                )
            elif operation == "detail":
                await store.generation_detail(
                    result["generation_id"], include_assets=False
                )
            else:
                await store.gallery_image_info(result["image_id"])
            assert reports == [("nai", "thumbnail_missing")]
        finally:
            await store.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "failure", ["permission", "invalid_path", "disabled", "action_denied"]
)
def test_permission_and_source_policy_failures_do_not_trigger_scan(
    tmp_path, monkeypatch, failure
):
    async def run():
        store, root = await setup(tmp_path)
        try:
            result, path = await add(store, root)
            reports = []
            store.set_external_issue_handler(
                lambda source, reason: reports.append((source, reason))
            )
            if failure in {"permission", "invalid_path"}:

                def reject(*args, **kwargs):
                    if failure == "permission":
                        raise PermissionError("access denied")
                    raise ValueError("外部图片不能使用符号链接")

                monkeypatch.setattr(store, "_external_path", reject)
                assert await store.gallery_image_file(result["image_id"]) is None
            elif failure == "disabled":
                await store.configure_external_source("nai", "NAI", root, False)
                path.unlink()
                assert await store.gallery_image_file(result["image_id"]) is None
                assert (
                    await store.gallery_image_data(result["image_id"], detail="preview")
                    is None
                )
            else:
                await store.configure_external_source(
                    "nai",
                    "NAI",
                    root,
                    True,
                    permissions={
                        "download": False,
                        "reference": False,
                        "delete": False,
                    },
                )
                path.unlink()
                with pytest.raises(ValueError):
                    await store.gallery_image_file(result["image_id"])
                with pytest.raises(ValueError):
                    await store.stage_gallery_reference(result["image_id"])
                with pytest.raises(ValueError):
                    await store.delete_generation(result["generation_id"])
            assert reports == []
        finally:
            await store.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "failure", ["missing", "changed", "sidecar_changed", "permission"]
)
def test_external_delete_reports_only_identity_changes(tmp_path, monkeypatch, failure):
    async def run():
        store, root = await setup(tmp_path)
        try:
            sidecar = root / "nai_1.yaml"
            sidecar.write_text("tag: before\n")
            result, path = await add(
                store, root, sidecars={sidecar.name: fingerprint(sidecar)}
            )
            reports = []
            store.set_external_issue_handler(
                lambda source, reason: reports.append((source, reason))
            )
            if failure == "missing":
                path.unlink()
                await store.delete_generation(result["generation_id"])
                assert reports == [("nai", "missing")]
                return
            if failure == "changed":
                path.write_bytes(image_bytes("blue"))
            elif failure == "sidecar_changed":
                sidecar.write_text("tag: after changed\n")
            else:
                actual_unlink = Path.unlink

                def deny(candidate, *args, **kwargs):
                    if candidate == path:
                        raise PermissionError("access denied")
                    return actual_unlink(candidate, *args, **kwargs)

                monkeypatch.setattr(Path, "unlink", deny)
            with pytest.raises(ExternalDeleteError):
                await store.delete_generation(result["generation_id"])
            assert reports == ([] if failure == "permission" else [("nai", "changed")])
            assert path.is_file()
        finally:
            await store.close()

    asyncio.run(run())


@pytest.mark.parametrize("change", ["missing", "changed", "thumbnail_missing"])
def test_detected_failure_immediately_repairs_only_its_source(tmp_path, change):
    async def run():
        store, root = await setup(tmp_path)
        await add(store, root)
        other = tmp_path / "other"
        other.mkdir()
        (other / "other.png").write_bytes(image_bytes("green"))
        manager = ExternalGalleryManager(store, store.data_dir, scan_interval=3600)
        try:
            await manager.configure(
                {
                    "nai": {"type": "directory", "path": str(root), "enabled": True},
                    "other": {"type": "directory", "path": str(other), "enabled": True},
                }
            )
            await manager.start()
            await asyncio.gather(*manager._tasks.values())
            initial_tasks = dict(manager._tasks)
            item = next(
                item
                for item in (await store.list_generations({}))["items"]
                if item["external_source"]["id"] == "nai"
            )
            path = root / "nai_1.png"
            if change == "missing":
                path.unlink()
            elif change == "changed":
                path.write_bytes(image_bytes("blue"))
            else:
                with store._connect() as conn:
                    thumbnail = conn.execute(
                        "SELECT t.path FROM image_thumbnails t JOIN generation_images i ON i.asset_id=t.asset_id WHERE i.id=?",
                        (item["image_id"],),
                    ).fetchone()
                (store.data_dir / thumbnail["path"]).unlink()
            await store.gallery_image_data(
                item["image_id"],
                detail="preview" if change == "thumbnail_missing" else "original",
            )
            await asyncio.sleep(0)
            assert manager._tasks["nai"] is not initial_tasks["nai"]
            assert manager._tasks["other"] is initial_tasks["other"]
            await asyncio.wait_for(manager._tasks["nai"], 10)
            gallery = await store.list_generations({})
            recovered = [
                item
                for item in gallery["items"]
                if item["external_source"]["id"] == "nai"
            ]
            if change == "missing":
                assert recovered == []
            else:
                assert len(recovered) == 1
                assert recovered[0]["thumbnail_data_url"]
                resolved = await store.gallery_image_file(recovered[0]["image_id"])
                assert (
                    resolved is not None
                    and resolved[0].read_bytes() == path.read_bytes()
                )
            assert (
                len(
                    [
                        item
                        for item in gallery["items"]
                        if item["external_source"]["id"] == "other"
                    ]
                )
                == 1
            )
        finally:
            await manager.close()
            await store.close()

    asyncio.run(run())


def test_bursts_and_active_scans_reuse_one_task_without_queued_followup(
    tmp_path, monkeypatch
):
    async def run():
        store, root = await setup(tmp_path)
        await add(store, root)
        manager = ExternalGalleryManager(store, store.data_dir, scan_interval=3600)
        gate, entered = asyncio.Event(), asyncio.Event()
        calls = []
        try:
            config = {"nai": {"type": "directory", "path": str(root), "enabled": True}}
            await manager.configure(config)
            await manager.start()
            await manager._tasks["nai"]

            async def blocked(source_id, epoch):
                calls.append((source_id, epoch))
                entered.set()
                await gate.wait()

            monkeypatch.setattr(manager, "_scan", blocked)
            await asyncio.gather(
                *(
                    asyncio.to_thread(manager._notify_source_issue, "nai", "missing")
                    for _ in range(30)
                )
            )
            await asyncio.wait_for(entered.wait(), 5)
            active_task = manager._tasks["nai"]
            for _ in range(20):
                manager._notify_source_issue("nai", "changed")
                await manager.request_scan("nai")
            await asyncio.sleep(0)
            assert manager._tasks["nai"] is active_task and len(calls) == 1
            gate.set()
            await active_task
            await asyncio.sleep(0)
            assert manager._tasks["nai"] is active_task and len(calls) == 1
            config["nai"]["enabled"] = False
            await manager.configure(config)
            manager._notify_source_issue("nai", "missing")
            manager._notify_source_issue("unknown", "missing")
            await asyncio.sleep(0)
            assert "nai" not in manager._tasks and len(calls) == 1
            await manager.configure({})
            manager._notify_source_issue("nai", "missing")
            await asyncio.sleep(0)
            assert not manager._tasks and len(calls) == 1
        finally:
            gate.set()
            await manager.close()
            assert store._external_issue_handler is None
            manager._notify_source_issue("nai", "missing")
            await store.close()

    asyncio.run(run())


@pytest.mark.parametrize("change", ["definition", "disable"])
def test_reconfiguration_blocks_recovery_until_new_source_is_ready(
    tmp_path, monkeypatch, change
):
    async def run():
        store, root = await setup(tmp_path)
        await add(store, root)
        replacement = tmp_path / "replacement"
        replacement.mkdir()
        (replacement / "new.png").write_bytes(image_bytes("blue"))
        manager = ExternalGalleryManager(store, store.data_dir, scan_interval=3600)
        gate, entered = asyncio.Event(), asyncio.Event()
        configuring = None
        try:
            config = {"nai": {"type": "directory", "path": str(root), "enabled": True}}
            await manager.configure(config)
            await manager.start()
            await manager._tasks["nai"]
            actual_stop, actual_scan = manager._stop_source, manager._scan
            scanned_roots = []

            async def blocked_stop(source_id):
                await actual_stop(source_id)
                entered.set()
                await gate.wait()

            async def tracked_scan(source_id, epoch):
                scanned_roots.append(
                    manager.adapters[source_id].resolve_root(store.data_dir)
                )
                await actual_scan(source_id, epoch)

            monkeypatch.setattr(manager, "_stop_source", blocked_stop)
            monkeypatch.setattr(manager, "_scan", tracked_scan)
            if change == "definition":
                config["nai"]["path"] = str(replacement)
            else:
                config["nai"]["enabled"] = False
            configuring = asyncio.create_task(manager.configure(config))
            await asyncio.wait_for(entered.wait(), 5)
            manager._notify_source_issue("nai", "changed")
            await asyncio.sleep(0)
            assert not manager._enabled["nai"]
            assert not manager._tasks and not scanned_roots
            gate.set()
            await configuring
            if change == "definition":
                await manager._tasks["nai"]
                assert scanned_roots == [replacement]
                assert (await store.list_generations({}))["total"] == 1
            else:
                assert not manager._tasks and not scanned_roots
        finally:
            gate.set()
            if configuring is not None:
                await configuring
            await manager.close()
            await store.close()

    asyncio.run(run())
