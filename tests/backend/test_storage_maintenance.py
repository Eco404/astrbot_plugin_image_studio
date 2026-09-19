from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from contextlib import closing
from types import SimpleNamespace

from astrbot_plugin_image_studio.backend.config import HistorySettings
from astrbot_plugin_image_studio.backend.database.maintenance import (
    compact_database,
    database_space,
    directory_space,
    rotate_backups,
)
from astrbot_plugin_image_studio.backend.gallery.store import GenerationStore


def test_disk_inventory_counts_all_owned_files_without_following_links(tmp_path):
    root = tmp_path / "plugin"
    root.mkdir()
    paths = {
        "history/assets/a.png": ("originals", b"original"),
        "history/thumbnails/a.webp": ("thumbnails", b"preview"),
        "comfyui_inputs/input.png": ("comfy_inputs", b"input"),
        "comfyui_outputs/output.png": ("comfy_outputs", b"output"),
        "comfyui_blobs/shared.png": ("comfy_blobs", b"shared"),
        "history.sqlite3": ("database", b"db"),
        "history.sqlite3-wal": ("database", b"wal"),
        "backups/history-pre-v4.sqlite3": ("backups", b"backup"),
        "import_staging/import.png": ("temporary", b"temporary"),
        "studio_config.json": ("other", b"settings"),
    }
    for relative, (_, content) in paths.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    original = root / "history/assets/a.png"
    os.link(original, root / "history/assets/shared.png")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "large.bin").write_bytes(b"x" * 100_000)
    (root / "external-link").symlink_to(outside, target_is_directory=True)
    report = directory_space(root)
    assert report["file_count"] == len(paths) + 1
    assert (
        report["file_bytes"]
        == sum(len(content) for _, content in paths.values()) + original.stat().st_size
    )
    assert report["skipped_symlinks"] == 1
    assert report["unreadable_entries"] == 0
    assert (
        report["categories"]["originals"]["file_bytes"] == 2 * original.stat().st_size
    )
    assert report["categories"]["database"]["file_bytes"] == 5
    files = [root, *(path for path in root.rglob("*") if not path.is_symlink())]
    allocated = {
        (path.stat().st_dev, path.stat().st_ino): path.stat().st_blocks * 512
        for path in files
    }
    assert report["allocated_bytes"] == sum(allocated.values())
    assert (
        sum(value["allocated_bytes"] for value in report["categories"].values())
        == report["allocated_bytes"]
    )
    assert (outside / "large.bin").exists()


def _backup(path):
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE generations(id TEXT PRIMARY KEY)")
        conn.commit()


def test_backup_rotation_keeps_three_valid_and_current_migration(tmp_path):
    backups = []
    for day in range(1, 7):
        path = (
            tmp_path / f"history-pre-v4-202609{day:02d}T120000000000Z-example.sqlite3"
        )
        _backup(path)
        backups.append(path)
    unrelated = tmp_path / "manual.sqlite3"
    _backup(unrelated)
    malformed = tmp_path / "history-pre-v4-20260907T120000000000Z-broken.sqlite3"
    malformed.write_bytes(b"broken")
    future = tmp_path / "history-pre-v5-20260908T120000000000Z-future.sqlite3"
    _backup(future)
    link = tmp_path / "history-pre-v4-20260909T120000000000Z-link.sqlite3"
    link.symlink_to(unrelated)
    unrelated_db = tmp_path / "history-pre-v4-20260910T120000000000Z-unrelated.sqlite3"
    with closing(sqlite3.connect(unrelated_db)) as conn:
        conn.execute("CREATE TABLE other(id INTEGER)")
    size = backups[1].stat().st_size + backups[2].stat().st_size
    report = rotate_backups(tmp_path, database_version=4, protected=backups[0])
    assert report == {"backups_removed": 2, "backup_bytes_reclaimed": size}
    assert all(
        path.exists()
        for path in [
            backups[0],
            *backups[3:],
            unrelated,
            malformed,
            future,
            link,
            unrelated_db,
        ]
    )
    assert not backups[1].exists() and not backups[2].exists()
    assert (
        rotate_backups(tmp_path, database_version=4, protected=backups[0])[
            "backups_removed"
        ]
        == 0
    )


def test_database_compaction_is_manual_bounded_and_avoids_pending_tasks(tmp_path):
    path = tmp_path / "history.sqlite3"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE data(value BLOB)")
        conn.execute("CREATE TABLE comfy_jobs(status TEXT)")
        conn.executemany("INSERT INTO data VALUES (zeroblob(?))", [(1024 * 1024,)] * 20)
        conn.commit()
        conn.execute("DELETE FROM data")
        conn.commit()
        before = database_space(conn)
        assert before["reusable_bytes"] >= 16 * 1024 * 1024
        assert compact_database(conn, path, deep=False)["reason"] == "manual_deep_only"
        assert database_space(conn) == before
        conn.execute("INSERT INTO comfy_jobs VALUES ('unknown')")
        conn.commit()
        assert (
            compact_database(conn, path, deep=True)["reason"] == "comfy_tasks_pending"
        )
        conn.execute("UPDATE comfy_jobs SET status='succeeded'")
        conn.commit()
        report = compact_database(conn, path, deep=True)
        assert report["compacted"]
        assert report["bytes_reclaimed"] >= 16 * 1024 * 1024
        assert database_space(conn)["reusable_bytes"] == 0
        assert compact_database(conn, path, deep=True)["reason"] == "below_threshold"


def test_backup_rotation_accepts_only_known_development_baselines(tmp_path):
    known = []
    for index, version in enumerate(("1-dev.3", "2-dev.2", "3-dev.1", "4-dev.1"), 1):
        path = (
            tmp_path
            / f"history-pre-v{version}-202609{index:02d}T125244076168Z-u31lndtb.sqlite3"
        )
        _backup(path)
        known.append(path)
    newest = []
    for day in (10, 11, 12):
        path = tmp_path / f"history-pre-v4-202609{day}T125244076168Z-release.sqlite3"
        _backup(path)
        newest.append(path)
    unknown = []
    for version in ("1-dev.1", "2-dev.3", "3-dev.2", "4-dev.2", "5-dev.1", "5"):
        path = (
            tmp_path
            / f"history-pre-v{version}-20260901T125244076168Z-unverified.sqlite3"
        )
        _backup(path)
        unknown.append(path)
    report = rotate_backups(tmp_path, database_version=4)
    assert report["backups_removed"] == 4
    assert all(not path.exists() for path in known)
    assert all(path.exists() for path in [*newest, *unknown])


def test_storage_health_disk_totals_do_not_change_history_quota(tmp_path):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        cache = tmp_path / "comfyui_outputs" / "test.bin"
        cache.parent.mkdir()
        cache.write_bytes(b"x" * 16384)
        with store._connect() as conn:
            before = conn.total_changes
            pages = database_space(conn)
            assert conn.total_changes == before
        report = await store.maintenance_report()
        assert report["stats"]["size_bytes"] == 0
        assert report["stats"]["disk"]["file_bytes"] >= pages["size_bytes"] + 16384
        assert (
            report["stats"]["disk"]["categories"]["comfy_outputs"]["file_bytes"]
            == 16384
        )
        quota = await store.retention_status(HistorySettings(True, 0, 1, False))
        assert quota["size_bytes"] == 0 and not quota["over_limit"]
        assert "disk" not in store.maintenance.storage_stats()
        report = await store.run_maintenance(
            HistorySettings(False, 0, 0, False),
            preview_max_edge=256,
            preview_quality=80,
            extra_repaired={"comfy_files": 2},
        )
        assert report["repaired"]["comfy_files"] == 2
        assert (await store.maintenance_report())["repaired"]["comfy_files"] == 2
        assert report["database"]["reason"] == "manual_deep_only"
        await store.close()

    asyncio.run(run())


def test_maintenance_does_not_traverse_symlinked_owned_directories(tmp_path):
    async def run():
        store = GenerationStore(tmp_path / "plugin")
        await store.initialize()
        outside = tmp_path / "outside"
        outside.mkdir()
        original = outside / "keep.png"
        original.write_bytes(b"owned by another service")
        old = time.time() - 3 * 86400
        os.utime(original, (old, old))
        for directory in (store.assets_dir, store.staging_dir):
            if directory.exists():
                directory.rmdir()
            directory.symlink_to(outside, target_is_directory=True)
        assert store.maintenance.cleanup_orphaned_asset_files() == 0
        assert store.maintenance.cleanup_stale_files(time.time()) == 0
        assert original.read_bytes() == b"owned by another service"
        await store.close()

    asyncio.run(run())


def test_all_plugin_maintenance_uses_task_cleanup_before_gallery_gc():
    from astrbot_plugin_image_studio.main import ImageStudioPlugin

    async def run():
        events = []

        async def cleanup(*, terminal_before):
            assert terminal_before > 0
            events.append("comfy")
            return 3

        async def imports():
            events.append("imports")

        async def gallery(history, **options):
            events.append("gallery")
            assert options["deep"] is True
            assert options["extra_repaired"] == {"comfy_files": 3}
            return {"status": "healthy"}

        plugin = ImageStudioPlugin.__new__(ImageStudioPlugin)
        plugin._storage_maintenance_lock = asyncio.Lock()
        plugin._comfy = SimpleNamespace(
            store=SimpleNamespace(cleanup_terminal_files=cleanup)
        )
        plugin._expire_import_groups = imports
        plugin.store = SimpleNamespace(run_maintenance=gallery)
        plugin._settings = SimpleNamespace(
            history=None, asset_preview_max_edge=256, asset_preview_quality=80
        )
        assert await plugin._run_storage_maintenance(deep=True) == {"status": "healthy"}
        assert events == ["comfy", "imports", "gallery"]

    asyncio.run(run())
