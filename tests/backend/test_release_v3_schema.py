"""Promote the frozen final ComfyUI development database without losing jobs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from contextlib import closing

import pytest

from astrbot_plugin_image_studio.backend.database import schema
from astrbot_plugin_image_studio.backend.gallery.store import GenerationStore
from astrbot_plugin_image_studio.backend.providers.comfyui.jobs import ComfyJobStore
from astrbot_plugin_image_studio.tests.support.schema_upgrade_fixtures import (
    DEVELOPMENT_META_STATEMENT,
    V1_STATEMENTS,
    V2_PUBLISHED_MIGRATION,
    V3_FINAL_DEVELOPMENT_STATEMENTS,
    create_final_dev,
    create_v1,
    create_v2,
    create_v3_dev,
)


def snapshot(conn):
    return conn.execute("PRAGMA user_version").fetchone()[0], tuple(conn.iterdump())


def rows(conn):
    return {
        table: conn.execute(f'SELECT * FROM "{table}" ORDER BY id').fetchall()
        for table in ("comfy_workflow_revisions", "comfy_jobs")
    }


def populate_comfy(conn, directory):
    """Write pre-release rows/files directly, without current store/schema helpers."""
    config = {
        "api_graph": {"91": {"class_type": "Seed (rgthree)", "inputs": {"seed": -1}}},
        "bindings": {"seed": {"node_id": "91", "input": "seed", "source": "seed"}},
        "outputs": ["99"],
    }
    encoded = json.dumps(config, sort_keys=True, separators=(",", ":"))
    revision = hashlib.sha256(encoded.encode()).hexdigest()
    conn.execute(
        "INSERT INTO comfy_workflow_revisions VALUES (?,?,?,?)",
        (revision, revision, encoded, 123.0),
    )
    files = {}
    for index, status in enumerate(
        ("queued", "running", "unknown", "finalizing", "succeeded")
    ):
        job_id = f"retained-{status}"
        input_data = f"input-{index}".encode()
        output_data = f"output-{index}".encode()
        input_path = f"{job_id}/0.image"
        output_path = f"{job_id}/0-{hashlib.sha256(output_data).hexdigest()}.image"
        for relative, data in (
            (f"comfyui_inputs/{input_path}", input_data),
            (f"comfyui_outputs/{output_path}", output_data),
        ):
            path = directory / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            files[relative] = data
        reference = {
            "id": "reference",
            "filename": "reference.png",
            "mime_type": "image/png",
            "path": input_path,
            "sha256": hashlib.sha256(input_data).hexdigest(),
            "size_bytes": len(input_data),
        }
        output = {
            "path": output_path,
            "sha256": hashlib.sha256(output_data).hexdigest(),
            "mime_type": "image/png",
            "size_bytes": len(output_data),
            "effective_parameters": {"seed": 123, "_comfyui": config},
            "response_index": index,
        }
        request = {
            "temporary": True,
            "prompt": "preserved prompt",
            "parameters": {"seed": -1, "count": 1},
            "model": {"id": "temporary_workflow", "comfyui": config},
        }
        conn.execute(
            "INSERT INTO comfy_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                job_id,
                "" if status == "queued" else f"remote-{index}",
                "comfy-provider",
                "temporary_workflow",
                revision,
                status,
                json.dumps(request),
                json.dumps([reference]),
                json.dumps([output]),
                json.dumps(
                    {"generation_id": "kept-generation", "queue_dismissed": False}
                ),
                "kept uncertainty" if status == "unknown" else "",
                "kept-generation",
                124.0,
                125.0,
                126.0 if status == "succeeded" else None,
            ),
        )
    conn.commit()
    return revision, config, files


def assert_release(conn):
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    assert (
        conn.execute(
            "SELECT name FROM sqlite_master WHERE name='schema_meta'"
        ).fetchone()
        is None
    )
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_published_migrations_keep_previous_release_sql_immutable():
    assert schema.V1_SCHEMA_STATEMENTS == V1_STATEMENTS
    assert schema.V2_MIGRATION_STATEMENTS == V2_PUBLISHED_MIGRATION
    assert len(schema.V3_MIGRATION_STATEMENTS) == 5
    assert not any(
        "ALTER TABLE" in statement for statement in schema.V3_MIGRATION_STATEMENTS
    )


@pytest.mark.parametrize(
    "baseline", ["empty", "v1", "v2", "1-dev.3", "2-dev.2", "3-dev.1"]
)
def test_all_supported_baselines_converge_to_release_v3(tmp_path, baseline):
    with closing(sqlite3.connect(tmp_path / "history.sqlite3")) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        if baseline == "1-dev.3":
            create_v1(conn)
            conn.execute("PRAGMA user_version=0")
            conn.execute(DEVELOPMENT_META_STATEMENT)
            conn.execute("INSERT INTO schema_meta VALUES (1,1,3)")
            conn.commit()
        elif baseline != "empty":
            {
                "v1": create_v1,
                "v2": create_v2,
                "2-dev.2": create_final_dev,
                "3-dev.1": create_v3_dev,
            }[baseline](conn)
        before = snapshot(conn)
        statements = []
        conn.set_trace_callback(statements.append)
        backup = schema.ensure_release_schema(conn, backup_dir=tmp_path / "backups")
        conn.set_trace_callback(None)
        assert_release(conn)
        assert bool(backup) == (baseline != "empty")
        if backup:
            assert backup.name.startswith("history-pre-v3-")
            with closing(sqlite3.connect(backup)) as saved:
                assert snapshot(saved) == before
        if baseline == "3-dev.1":
            assert not any(
                statement.startswith(("CREATE", "ALTER", "INSERT", "UPDATE", "DELETE"))
                for statement in statements
            )
            assert [
                statement for statement in statements if statement.startswith("DROP")
            ] == ["DROP TABLE schema_meta"]
        if baseline == "v2":
            ddl = [
                statement
                for statement in statements
                if statement.startswith(("CREATE", "ALTER", "DROP"))
            ]
            assert ddl == list(schema.V3_MIGRATION_STATEMENTS)
        with closing(sqlite3.connect(":memory:")) as fresh:
            schema.ensure_release_schema(fresh, backup_dir=tmp_path / "unused")
            assert {
                table: schema._table_shape(conn, table) for table in schema._TABLES
            } == {table: schema._table_shape(fresh, table) for table in schema._TABLES}
        after = snapshot(conn)
        assert (
            schema.ensure_release_schema(conn, backup_dir=tmp_path / "backups") is None
        )
        assert snapshot(conn) == after


@pytest.mark.parametrize("first", ["gallery", "jobs"])
def test_dev_promotion_preserves_workflows_jobs_and_staged_files(tmp_path, first):
    database = tmp_path / "history.sqlite3"
    with closing(sqlite3.connect(database)) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        create_v3_dev(conn)
        revision, config, files = populate_comfy(conn, tmp_path)
        before = rows(conn)
        old_snapshot = snapshot(conn)

    async def run():
        gallery = GenerationStore(tmp_path)
        jobs = ComfyJobStore(database)
        try:
            stores = (gallery, jobs) if first == "gallery" else (jobs, gallery)
            for store in (*stores, *stores):
                await store.initialize()
            assert await jobs.get_revision(revision) == config
            for status in ("queued", "running", "unknown", "finalizing", "succeeded"):
                job_id = f"retained-{status}"
                job = await jobs.get_job(job_id)
                assert job["status"] == status
                assert job["request"]["temporary"] is True
                assert (await jobs.load_references(job_id))[0].data == files[
                    f"comfyui_inputs/{job_id}/0.image"
                ]
                loaded = (await jobs.load_outputs(job_id))[0]
                assert loaded.effective_parameters["_comfyui"] == config
                assert (
                    loaded.data == files["comfyui_outputs/" + job["outputs"][0]["path"]]
                )
        finally:
            await gallery.close()

    asyncio.run(run())
    with closing(sqlite3.connect(database)) as conn:
        assert_release(conn)
        assert rows(conn) == before
    assert {relative: (tmp_path / relative).read_bytes() for relative in files} == files
    backups = list((tmp_path / "backups").glob("*.sqlite3"))
    assert len(backups) == 1
    with closing(sqlite3.connect(backups[0])) as backup:
        assert snapshot(backup) == old_snapshot


@pytest.mark.parametrize("baseline", ["v2", "3-dev.1"])
@pytest.mark.parametrize("failure", ["backup", "stamp", "validation"])
def test_v3_upgrade_failure_preserves_preupgrade_database(
    tmp_path, monkeypatch, baseline, failure
):
    with closing(sqlite3.connect(tmp_path / "history.sqlite3")) as conn:
        (create_v2 if baseline == "v2" else create_v3_dev)(conn)
        if baseline == "3-dev.1":
            populate_comfy(conn, tmp_path)
        before = snapshot(conn)

        def fail(*_args, **_kwargs):
            raise OSError("simulated release failure")

        if failure == "validation":
            validate = schema._validate_release_layout

            def final_validation(target, version=schema.RELEASE_VERSION):
                if (
                    target.in_transaction
                    and version == schema.RELEASE_VERSION
                    and not target.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='schema_meta'"
                    ).fetchone()
                ):
                    fail()
                return validate(target, version)

            monkeypatch.setattr(schema, "_validate_release_layout", final_validation)
        else:
            monkeypatch.setattr(
                schema,
                "_backup_database" if failure == "backup" else "_stamp_release",
                fail,
            )
        with pytest.raises(OSError, match="release failure"):
            schema.ensure_release_schema(conn, backup_dir=tmp_path / "backups")
        assert snapshot(conn) == before
        assert not conn.in_transaction
        backups = list((tmp_path / "backups").glob("*.sqlite3"))
        assert len(backups) == int(failure != "backup")
        if backups:
            with closing(sqlite3.connect(backups[0])) as saved:
                assert snapshot(saved) == before


@pytest.mark.parametrize(
    "corruption",
    [
        "early_dev",
        "future_dev",
        "future_release",
        "missing_marker",
        "wrong_release",
        "foreign_key",
        "default",
        "partial_index",
        "extra_column",
        "marker_row",
        "marker_constraint",
    ],
)
def test_unknown_or_damaged_v3_development_is_rejected_without_backup(
    tmp_path, corruption
):
    with closing(sqlite3.connect(tmp_path / "history.sqlite3")) as conn:
        create_v3_dev(conn)
        if corruption in {"early_dev", "future_dev"}:
            conn.execute(
                "UPDATE schema_meta SET dev_revision=?",
                (0 if corruption == "early_dev" else 2,),
            )
        elif corruption in {"future_release", "wrong_release"}:
            conn.execute(
                f"PRAGMA user_version={4 if corruption == 'future_release' else 3}"
            )
        elif corruption == "missing_marker":
            conn.execute("DROP TABLE schema_meta")
        elif corruption == "marker_row":
            conn.execute("DELETE FROM schema_meta")
        elif corruption == "marker_constraint":
            conn.execute("DROP TABLE schema_meta")
            conn.execute(DEVELOPMENT_META_STATEMENT.replace(" CHECK(id = 1)", ""))
            conn.execute("INSERT INTO schema_meta VALUES (1,3,1)")
        elif corruption == "extra_column":
            conn.execute("ALTER TABLE comfy_jobs ADD COLUMN unexpected TEXT")
        elif corruption == "partial_index":
            conn.execute("DROP INDEX idx_comfy_jobs_remote")
            conn.execute(
                "CREATE UNIQUE INDEX idx_comfy_jobs_remote ON comfy_jobs(provider_id,remote_id) WHERE remote_id = ''"
            )
        else:
            declaration = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='comfy_jobs'"
            ).fetchone()[0]
            declaration = (
                declaration.replace("REFERENCES comfy_workflow_revisions(id)", "")
                if corruption == "foreign_key"
                else declaration.replace("DEFAULT '[]'", "DEFAULT '{}'")
            )
            conn.execute("DROP TABLE comfy_jobs")
            conn.execute(declaration)
            for statement in V3_FINAL_DEVELOPMENT_STATEMENTS[2:]:
                conn.execute(statement)
        conn.commit()
        before = snapshot(conn)
        with pytest.raises(RuntimeError):
            schema.ensure_release_schema(conn, backup_dir=tmp_path / "backups")
        assert snapshot(conn) == before
        assert not (tmp_path / "backups").exists()


@pytest.mark.parametrize("change", ["marker", "business"])
def test_promotion_rechecks_frozen_development_after_backup(
    tmp_path, monkeypatch, change
):
    database = tmp_path / "history.sqlite3"
    with closing(sqlite3.connect(database)) as conn:
        create_v3_dev(conn)
        populate_comfy(conn, tmp_path)
        before = snapshot(conn)
        backup = schema._backup_database

        def changed(target, directory):
            path = backup(target, directory)
            with closing(sqlite3.connect(database)) as other:
                other.execute(
                    "UPDATE schema_meta SET dev_revision=9"
                    if change == "marker"
                    else "UPDATE comfy_jobs SET error='concurrent update'"
                )
                other.commit()
            return path

        monkeypatch.setattr(schema, "_backup_database", changed)
        with pytest.raises(RuntimeError, match="开发修订|备份后发生变化"):
            schema.ensure_release_schema(conn, backup_dir=tmp_path / "backups")
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert conn.execute("SELECT 1 FROM schema_meta").fetchone()
        with closing(
            sqlite3.connect(next((tmp_path / "backups").glob("*.sqlite3")))
        ) as saved:
            assert snapshot(saved) == before
