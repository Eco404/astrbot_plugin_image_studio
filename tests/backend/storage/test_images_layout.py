"""Resumable owned-image layout migration with real SQLite and filesystem faults."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest
from astrbot_plugin_image_studio.backend.database.migrations import images_layout
from astrbot_plugin_image_studio.backend.database.payloads import store_payload
from astrbot_plugin_image_studio.backend.database.schema import ensure_release_schema
from astrbot_plugin_image_studio.backend.gallery.store import GenerationStore


@pytest.fixture
def database(tmp_path):
    root = tmp_path / "plugin"
    root.mkdir()
    connection = sqlite3.connect(root / "history.sqlite3")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    ensure_release_schema(connection, backup_dir=root / "backups")
    connection.commit()
    try:
        yield root, connection
    finally:
        connection.close()


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _insert_generation(connection, identity, *, supplemental="{}"):
    connection.execute(
        "INSERT INTO generations(id,created_at,source,status,mode,provider_id,"
        "provider_name,provider_kind,model,original_prompt,final_prompt,parameters_json,"
        "elapsed_ms,supplemental_json) VALUES (?,1,'webui','success','text2img',"
        "'comfy','ComfyUI','comfyui','workflow','history/assets/raw-prompt',"
        "'history/assets/raw-prompt','{}',1,?)",
        (identity, supplemental),
    )


def _seed(connection, root, *, write_files=True, asset_path=None, with_job=True):
    pixels = b"deterministic original image bytes"
    thumbnail = b"deterministic thumbnail bytes"
    digest = hashlib.sha256(pixels).hexdigest()
    asset_path = asset_path or f"history/assets/{digest[:2]}/{digest}.png"
    thumb_path = f"history/thumbnails/{digest[:2]}/{digest}.webp"
    raw = json.dumps(
        {"text": "history/assets/user-entered-text", "seed": 18446744073709551614}
    )
    _insert_generation(connection, "group-a", supplemental=raw)
    _insert_generation(connection, "group-b")
    connection.execute(
        "INSERT INTO image_assets(id,path,mime_type,size_bytes,created_at) VALUES (?,?, 'image/png',?,1)",
        (digest, asset_path, len(pixels)),
    )
    connection.execute(
        "INSERT INTO image_thumbnails(asset_id,path,mime_type,size_bytes) VALUES (?,?,'image/webp',?)",
        (digest, thumb_path, len(thumbnail)),
    )
    for group in ("group-a", "group-b"):
        connection.execute(
            "INSERT INTO generation_images(id,generation_id,ordinal,asset_id,supplemental_json) VALUES (?,?,0,?,?)",
            (f"{group}-image", group, digest, raw),
        )
    connection.execute(
        "INSERT INTO image_metadata(asset_id,format,parser_version,metadata_json) VALUES (?,'png',1,?)",
        (digest, raw),
    )
    payload = store_payload(
        connection,
        "generations",
        "group-b",
        "supplemental",
        {"workflow": {"path": "history/assets/arbitrary-workflow-value"}},
    )
    connection.execute(
        "UPDATE generations SET supplemental_json=? WHERE id='group-b'",
        (json.dumps(payload),),
    )
    outputs = [
        {
            "storage": "gallery",
            "path": asset_path,
            "sha256": digest,
            "gallery_image_id": "group-a-image",
            "gallery_generation_id": "group-a",
        },
        {
            "storage": "blob",
            "path": "history/assets/not-a-gallery-path.image",
            "sha256": "blob",
        },
        {"storage": "expired", "path": "history/assets/expired-description.png"},
    ]
    if with_job:
        connection.execute(
            "INSERT INTO comfy_workflow_revisions(id,fingerprint,config_json,created_at) VALUES ('revision','fingerprint',?,1)",
            (raw,),
        )
        connection.execute(
            "INSERT INTO comfy_jobs(id,provider_id,model_id,revision_id,status,request_json,"
            "input_refs_json,output_refs_json,result_json,generation_id,created_at,updated_at)"
            " VALUES ('job','comfy','workflow','revision','succeeded',?,?,?,?, 'group-a',1,1)",
            (
                raw,
                json.dumps([{"storage": "blob", "path": "history/assets/input.image"}]),
                json.dumps(outputs),
                raw,
            ),
        )
    connection.commit()
    if write_files:
        _write(root / asset_path.replace("\\", "/"), pixels)
        _write(root / thumb_path, thumbnail)
    return {
        "digest": digest,
        "asset_path": asset_path,
        "thumb_path": thumb_path,
        "pixels": pixels,
        "thumbnail": thumbnail,
        "outputs": outputs,
    }


def _rows(connection, table):
    return [
        tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1")
    ]


def _paths(connection):
    return {
        table: [
            row[0] for row in connection.execute(f"SELECT path FROM {table} ORDER BY 1")
        ]
        for table in ("image_assets", "image_thumbnails")
    }


def _assert_finished(root, connection, data):
    for key, content in (
        ("asset_path", data["pixels"]),
        ("thumb_path", data["thumbnail"]),
    ):
        old = data[key].replace("\\", "/")
        new = "images/" + old.removeprefix("history/")
        assert not (root / old).exists()
        assert (root / new).read_bytes() == content
    assert all(
        path.startswith("images/")
        for paths in _paths(connection).values()
        for path in paths
    )
    assert not (root / images_layout.JOURNAL_NAME).exists()
    assert not connection.in_transaction
    assert not connection.execute("PRAGMA foreign_key_check").fetchall()


def test_fresh_store_initializes_images_directories_without_legacy_tree(tmp_path):
    async def run():
        store = GenerationStore(tmp_path / "fresh")
        await store.initialize()
        assert store.images_dir == tmp_path / "fresh/images"
        assert store.assets_dir.is_dir() and store.thumbnails_dir.is_dir()
        assert store.assets_dir.parent == store.images_dir
        assert store.thumbnails_dir.parent == store.images_dir
        assert not (store.data_dir / "history").exists()
        assert not (store.data_dir / images_layout.JOURNAL_NAME).exists()
        await store.initialize()

    asyncio.run(run())


def test_migration_changes_only_owned_paths_and_gallery_descriptors(database):
    root, connection = database
    data = _seed(connection, root)
    invariant_tables = (
        "generations",
        "generation_images",
        "image_metadata",
        "comfy_workflow_revisions",
        "storage_payloads",
        "storage_payload_refs",
    )
    original = {table: _rows(connection, table) for table in invariant_tables}
    original_job = dict(connection.execute("SELECT * FROM comfy_jobs").fetchone())
    version = connection.execute("PRAGMA user_version").fetchone()[0]

    result = images_layout.migrate_images_layout(connection, root)

    assert result == {"files": 2, "asset_paths": 2, "job_manifests": 1}
    _assert_finished(root, connection, data)
    assert not (root / "history").exists()
    assert {table: _rows(connection, table) for table in invariant_tables} == original
    job = dict(connection.execute("SELECT * FROM comfy_jobs").fetchone())
    expected = copy.deepcopy(data["outputs"])
    expected[0]["path"] = data["asset_path"].replace("history/", "images/", 1)
    assert json.loads(job.pop("output_refs_json")) == expected
    original_job.pop("output_refs_json")
    assert job == original_job
    assert connection.execute("PRAGMA user_version").fetchone()[0] == version
    before_repeat = {
        table: _rows(connection, table) for table in (*invariant_tables, "comfy_jobs")
    }
    assert images_layout.migrate_images_layout(connection, root) == {
        "files": 0,
        "asset_paths": 0,
        "job_manifests": 0,
    }
    assert {table: _rows(connection, table) for table in before_repeat} == before_repeat


def test_unknown_legacy_files_survive_migration_and_startup_orphan_cleanup(database):
    root, connection = database
    data = _seed(connection, root)
    top = _write(root / "history/README.txt", b"user notes")
    nested = _write(root / "history/custom/keep.bin", b"user attachment")
    manual = _write(root / "history/assets/manual.png", b"unregistered original")
    thumbnail_notes = _write(root / "history/thumbnails/notes.json", b'{"note":"keep"}')
    unrelated_new = _write(root / "images/custom/stay.bin", b"existing new data")

    assert images_layout.migrate_images_layout(connection, root)["files"] == 2

    _assert_finished(root, connection, data)
    assert top.read_bytes() == b"user notes"
    assert nested.read_bytes() == b"user attachment"
    assert manual.read_bytes() == b"unregistered original"
    assert thumbnail_notes.read_bytes() == b'{"note":"keep"}'
    assert not (root / "images/assets/manual.png").exists()
    assert unrelated_new.read_bytes() == b"existing new data"
    asyncio.run(GenerationStore(root).initialize())
    assert top.read_bytes() == b"user notes"
    assert nested.read_bytes() == b"user attachment"
    assert manual.read_bytes() == b"unregistered original"
    assert thumbnail_notes.read_bytes() == b'{"note":"keep"}'


@pytest.mark.parametrize("same_content", [True, False])
@pytest.mark.parametrize(
    "path_key,content_key", [("asset_path", "pixels"), ("thumb_path", "thumbnail")]
)
def test_existing_destination_is_verified_before_any_move(
    database, same_content, path_key, content_key
):
    root, connection = database
    data = _seed(connection, root)
    target = _write(
        root / data[path_key].replace("history/", "images/", 1),
        data[content_key] if same_content else b"unrelated destination",
    )
    before = _paths(connection)
    if same_content:
        images_layout.migrate_images_layout(connection, root)
        _assert_finished(root, connection, data)
    else:
        with pytest.raises(RuntimeError, match="冲突|校验"):
            images_layout.migrate_images_layout(connection, root)
        assert _paths(connection) == before
        assert (root / data["asset_path"]).read_bytes() == data["pixels"]
        assert (root / data["thumb_path"]).read_bytes() == data["thumbnail"]
        assert target.read_bytes() == b"unrelated destination"
        assert not (root / images_layout.JOURNAL_NAME).exists()
        assert not connection.in_transaction


def test_already_moved_files_repair_database_paths(database):
    root, connection = database
    data = _seed(connection, root, write_files=False)
    _write(root / data["asset_path"].replace("history/", "images/", 1), data["pixels"])
    _write(
        root / data["thumb_path"].replace("history/", "images/", 1), data["thumbnail"]
    )

    assert images_layout.migrate_images_layout(connection, root)["asset_paths"] == 2
    _assert_finished(root, connection, data)


def test_missing_source_requires_existing_original_to_match_asset_hash(database):
    root, connection = database
    data = _seed(connection, root, write_files=False)
    target = _write(
        root / data["asset_path"].replace("history/", "images/", 1), b"wrong file"
    )
    before = _paths(connection)
    with pytest.raises(RuntimeError, match="资产哈希"):
        images_layout.migrate_images_layout(connection, root)
    assert _paths(connection) == before
    assert target.read_bytes() == b"wrong file"


def test_preexisting_missing_files_are_not_fabricated_or_marked_available(database):
    root, connection = database
    data = _seed(connection, root, write_files=False)
    connection.execute("UPDATE image_assets SET file_state='missing'")
    connection.commit()

    result = images_layout.migrate_images_layout(connection, root)

    assert result == {"files": 0, "asset_paths": 2, "job_manifests": 1}
    assert (
        connection.execute("SELECT file_state FROM image_assets").fetchone()[0]
        == "missing"
    )
    assert _paths(connection)["image_assets"] == [
        data["asset_path"].replace("history/", "images/", 1)
    ]
    assert not list(root.rglob("*.png")) and not list(root.rglob("*.webp"))
    assert not connection.execute("PRAGMA foreign_key_check").fetchall()


def test_journal_file_cannot_disappear_from_both_locations(database):
    root, connection = database
    data = _seed(connection, root, write_files=False)
    journal = {
        "version": 1,
        "id": "a" * 32,
        "files": [
            {
                "source": data["asset_path"],
                "size": len(data["pixels"]),
                "sha256": data["digest"],
            }
        ],
    }
    (root / images_layout.JOURNAL_NAME).write_text(json.dumps(journal))
    before = _paths(connection)
    with pytest.raises(RuntimeError, match="均已丢失"):
        images_layout.migrate_images_layout(connection, root)
    assert _paths(connection) == before
    assert json.loads((root / images_layout.JOURNAL_NAME).read_text()) == journal


@pytest.mark.parametrize(
    "relative,is_directory",
    [
        ("history", True),
        ("history/assets", True),
        ("history/assets/source.png", False),
        ("images", True),
        ("images/assets", True),
        ("images/assets/source.png", False),
        (images_layout.JOURNAL_NAME, False),
        (".images-layout.lock", False),
    ],
)
def test_symlinks_abort_without_following_or_rewriting(
    database, tmp_path, relative, is_directory
):
    root, connection = database
    _seed(connection, root, write_files=False, asset_path="history/assets/source.png")
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = _write(outside / "source.png", b"outside content")
    link = root / relative
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(
        outside if is_directory else sentinel, target_is_directory=is_directory
    )
    before = _paths(connection)
    with pytest.raises(RuntimeError, match="符号链接"):
        images_layout.migrate_images_layout(connection, root)
    assert link.is_symlink()
    assert sentinel.read_bytes() == b"outside content"
    assert _paths(connection) == before
    assert not connection.in_transaction


@pytest.mark.parametrize(
    "unsafe",
    [
        "history/assets/../../outside.png",
        "history/assets/./source.png",
        "history/assets//source.png",
        "history/assets/C:source.png",
        "history/../outside.png",
        "history/custom/source.png",
    ],
)
def test_invalid_legacy_database_paths_abort_before_staging(database, unsafe):
    root, connection = database
    _seed(connection, root, write_files=False, asset_path=unsafe)
    before = _paths(connection)
    with pytest.raises(RuntimeError, match="不安全"):
        images_layout.migrate_images_layout(connection, root)
    assert _paths(connection) == before
    assert not (root / images_layout.JOURNAL_NAME).exists()


def test_windows_legacy_separators_become_portable_relative_paths(database):
    root, connection = database
    data = _seed(connection, root, asset_path="history\\assets\\nested\\source.png")
    images_layout.migrate_images_layout(connection, root)
    _assert_finished(root, connection, data)
    assert _paths(connection)["image_assets"] == ["images/assets/nested/source.png"]


def test_partial_staging_keeps_old_files_and_database_until_retry(
    database, monkeypatch
):
    root, connection = database
    data = _seed(connection, root)
    before = _paths(connection)
    original = images_layout._stage_file
    attempted = []

    def interrupt(root, entry, migration_id):
        attempted.append(entry["source"])
        if len(attempted) == 2:
            raise OSError("injected staging failure")
        original(root, entry, migration_id)

    with monkeypatch.context() as patch:
        patch.setattr(images_layout, "_stage_file", interrupt)
        with pytest.raises(OSError, match="staging failure"):
            images_layout.migrate_images_layout(connection, root)
    assert len(attempted) == 2
    assert _paths(connection) == before
    assert (root / data["asset_path"]).read_bytes() == data["pixels"]
    assert (root / data["thumb_path"]).read_bytes() == data["thumbnail"]
    assert (root / attempted[0].replace("history/", "images/", 1)).is_file()
    assert (root / images_layout.JOURNAL_NAME).is_file()
    assert not connection.in_transaction

    images_layout.migrate_images_layout(connection, root)
    _assert_finished(root, connection, data)


def test_failed_database_commit_rolls_back_all_paths_but_staged_files_resume(
    database, monkeypatch
):
    root, connection = database
    data = _seed(connection, root)
    before = _paths(connection)
    before_job = connection.execute(
        "SELECT output_refs_json FROM comfy_jobs"
    ).fetchone()[0]

    def interrupt(conn, updates, jobs):
        table, key, identity, old, new = updates[0]
        conn.execute(f"UPDATE {table} SET path=? WHERE {key}=?", (new, identity))
        raise sqlite3.OperationalError("injected before commit")

    with monkeypatch.context() as patch:
        patch.setattr(images_layout, "_commit_paths", interrupt)
        with pytest.raises(sqlite3.OperationalError, match="before commit"):
            images_layout.migrate_images_layout(connection, root)
    assert _paths(connection) == before
    assert (
        connection.execute("SELECT output_refs_json FROM comfy_jobs").fetchone()[0]
        == before_job
    )
    for path in (data["asset_path"], data["thumb_path"]):
        assert (root / path).is_file()
        assert (root / path.replace("history/", "images/", 1)).is_file()
    assert (root / images_layout.JOURNAL_NAME).is_file()

    images_layout.migrate_images_layout(connection, root)
    _assert_finished(root, connection, data)


def test_partial_old_file_cleanup_after_commit_resumes_from_journal(
    database, monkeypatch
):
    root, connection = database
    data = _seed(connection, root)
    unlink = Path.unlink

    def interrupt(path, *args, **kwargs):
        if path == root / data["thumb_path"]:
            raise PermissionError("injected old-file cleanup failure")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", interrupt)
        with pytest.raises(PermissionError, match="cleanup failure"):
            images_layout.migrate_images_layout(connection, root)
    assert not (root / data["asset_path"]).exists()
    assert (root / data["thumb_path"]).is_file()
    assert all(
        path.startswith("images/")
        for paths in _paths(connection).values()
        for path in paths
    )
    assert (root / images_layout.JOURNAL_NAME).is_file()
    assert not connection.in_transaction

    result = images_layout.migrate_images_layout(connection, root)
    assert result["asset_paths"] == 0 and result["job_manifests"] == 0
    _assert_finished(root, connection, data)


def test_copy_fallback_removes_interrupted_temporary_and_retries(database, monkeypatch):
    root, connection = database
    data = _seed(connection, root)
    before = _paths(connection)
    original_file = root / data["asset_path"]
    original_file.chmod(0o640)
    os.utime(original_file, ns=(1680000000123000000, 1680000000456000000))
    original_stat = original_file.stat()

    def no_link(*args, **kwargs):
        raise OSError("cross-device link")

    def partial_copy(source, target):
        target.write(source.read(4))
        raise OSError("injected copy failure")

    monkeypatch.setattr(images_layout.os, "link", no_link)
    with monkeypatch.context() as patch:
        patch.setattr(images_layout.shutil, "copyfileobj", partial_copy)
        with pytest.raises(OSError, match="copy failure"):
            images_layout.migrate_images_layout(connection, root)
    assert _paths(connection) == before
    assert not list(root.rglob("*.tmp"))
    assert (root / data["asset_path"]).read_bytes() == data["pixels"]

    images_layout.migrate_images_layout(connection, root)
    _assert_finished(root, connection, data)
    moved_stat = (root / data["asset_path"].replace("history/", "images/", 1)).stat()
    assert moved_stat.st_mode == original_stat.st_mode
    assert moved_stat.st_mtime_ns == original_stat.st_mtime_ns


def test_shared_assets_and_external_originals_retain_their_owners(database, tmp_path):
    root, connection = database
    data = _seed(connection, root)
    external = _write(tmp_path / "external-library/original.png", b"external pixels")
    external_digest = hashlib.sha256(external.read_bytes()).hexdigest()
    external_thumb = "history/thumbnails/external-source/preview.webp"
    _write(root / external_thumb, b"external thumbnail")
    connection.execute(
        "INSERT INTO image_assets(id,path,mime_type,size_bytes,created_at) VALUES (?,?,'image/png',?,1)",
        (external_digest, str(external), external.stat().st_size),
    )
    connection.execute(
        "INSERT INTO image_thumbnails(asset_id,path,mime_type,size_bytes) VALUES (?,?,'image/webp',18)",
        (external_digest, external_thumb),
    )
    connection.execute(
        "INSERT INTO generation_images(id,generation_id,ordinal,asset_id) VALUES ('external-image','group-b',1,?)",
        (external_digest,),
    )
    connection.commit()
    original_owners = _rows(connection, "generation_images")
    external_stat = external.stat()

    result = images_layout.migrate_images_layout(connection, root)

    assert result["files"] == 3 and result["asset_paths"] == 3
    assert _rows(connection, "generation_images") == original_owners
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM generation_images WHERE asset_id=?", (data["digest"],)
        ).fetchone()[0]
        == 2
    )
    assert external.read_bytes() == b"external pixels"
    assert external.stat().st_mtime_ns == external_stat.st_mtime_ns
    assert connection.execute(
        "SELECT path FROM image_assets WHERE id=?", (external_digest,)
    ).fetchone()[0] == str(external)
    assert (
        root / external_thumb.replace("history/", "images/", 1)
    ).read_bytes() == b"external thumbnail"
    assert not connection.execute("PRAGMA foreign_key_check").fetchall()


@pytest.mark.parametrize("changed_owner", ["asset", "job"])
def test_compare_and_swap_refuses_changed_rows_before_commit(
    database, monkeypatch, changed_owner
):
    root, connection = database
    data = _seed(connection, root)
    before = _paths(connection)
    original_commit = images_layout._commit_paths
    before_job = connection.execute(
        "SELECT output_refs_json FROM comfy_jobs"
    ).fetchone()[0]

    def mutate_before_commit(conn, updates, jobs):
        if changed_owner == "asset":
            conn.execute(
                "UPDATE image_assets SET path='images/assets/concurrently-changed.png'"
            )
        else:
            conn.execute("UPDATE comfy_jobs SET output_refs_json='[]'")
        return original_commit(conn, updates, jobs)

    with monkeypatch.context() as patch:
        patch.setattr(images_layout, "_commit_paths", mutate_before_commit)
        with pytest.raises(RuntimeError, match="记录发生变化"):
            images_layout.migrate_images_layout(connection, root)
    assert _paths(connection) == before
    assert (
        connection.execute("SELECT output_refs_json FROM comfy_jobs").fetchone()[0]
        == before_job
    )
    assert (root / data["asset_path"]).is_file()
    assert not connection.in_transaction

    images_layout.migrate_images_layout(connection, root)
    _assert_finished(root, connection, data)


def test_migration_excludes_another_database_writer_while_staging(
    database, monkeypatch
):
    root, connection = database
    data = _seed(connection, root)
    stage = images_layout._stage_file
    locked_attempts = []

    def competing_writer(root, entry, migration_id):
        other = sqlite3.connect(root / "history.sqlite3", timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute(
                    "UPDATE image_assets SET path='images/assets/concurrent.png'"
                )
            locked_attempts.append(entry["source"])
        finally:
            other.close()
        return stage(root, entry, migration_id)

    monkeypatch.setattr(images_layout, "_stage_file", competing_writer)
    images_layout.migrate_images_layout(connection, root)
    assert len(locked_attempts) == 2
    _assert_finished(root, connection, data)


def test_database_destination_collision_is_rejected_before_moving_files(database):
    root, connection = database
    data = _seed(connection, root)
    connection.execute(
        "INSERT INTO image_assets(id,path,mime_type,size_bytes,created_at) VALUES ('other',?,'image/png',1,1)",
        (data["asset_path"].replace("history/", "images/", 1),),
    )
    connection.commit()
    before = _paths(connection)
    with pytest.raises(RuntimeError, match="数据库路径冲突"):
        images_layout.migrate_images_layout(connection, root)
    assert _paths(connection) == before
    assert (root / data["asset_path"]).read_bytes() == data["pixels"]
    assert not (root / "images").exists()


def test_migration_rejects_call_inside_existing_transaction(database):
    root, connection = database
    _seed(connection, root)
    connection.execute("BEGIN")
    with pytest.raises(RuntimeError, match="独立事务"):
        images_layout.migrate_images_layout(connection, root)
    assert connection.in_transaction
    assert not (root / images_layout.JOURNAL_NAME).exists()
    connection.rollback()
