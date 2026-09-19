"""Final 4-dev.2 promotion preserves bodies while published v3 upgrades once."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import zlib
from contextlib import closing

import pytest

from astrbot_plugin_image_studio.backend.database import schema
from astrbot_plugin_image_studio.backend.database.migrations import v4_storage
from astrbot_plugin_image_studio.backend.database.payloads import load_payload
from astrbot_plugin_image_studio.tests.support.schema_upgrade_fixtures import (
    V4_FINAL_DEVELOPMENT_STATEMENTS,
    create_v3_dev,
    create_v4_dev,
)


def snapshot(conn):
    return conn.execute("PRAGMA user_version").fetchone()[0], tuple(conn.iterdump())


def business_rows(conn):
    return {
        table: conn.execute(f'SELECT * FROM "{table}" ORDER BY 1').fetchall()
        for (table,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name!='schema_meta'"
        )
    }


def populate_final_dev(conn):
    """Frozen encoding with a uint64 seed, retaining exact source JSON text."""
    graph = {"91": {"class_type": "Seed (rgthree)", "inputs": {"seed": (1 << 64) - 1}}}
    workflow = {"nodes": [{"id": 91, "widgets_values": [(1 << 64) - 1]}]}
    value = {
        "api_graph_json": json.dumps(graph, indent=2),
        "workflow_json": json.dumps(workflow, indent=2),
        "bindings": {},
        "outputs": ["99"],
    }
    raw = json.dumps(
        {"value": value, "restore_objects": ["api_graph", "workflow"]},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    digest = hashlib.sha256(b"workflow\0" + raw).hexdigest()
    marker = {"__image_studio_payload__": digest}
    encoded = json.dumps(marker)
    conn.execute(
        "INSERT INTO storage_payloads VALUES(?,'workflow','zlib',?,?,123)",
        (digest, zlib.compress(raw, 6), len(raw)),
    )
    conn.execute(
        "INSERT INTO generations(id,created_at,source,status,mode,provider_id,provider_name,provider_kind,model,original_prompt,final_prompt,parameters_json,elapsed_ms,is_favorite,title,latest_content_at,import_key) "
        "VALUES('g',123,'import','succeeded','text2img','p','ComfyUI','comfyui','workflow','kept prompt','kept prompt','{}',1,1,'保留标题 🐈',124,'imported')"
    )
    conn.execute(
        "INSERT INTO image_assets VALUES('a','images/assets/a.png','image/png',1234,24,32,123,'available')"
    )
    conn.execute(
        "INSERT INTO image_thumbnails VALUES('a','images/thumbnails/a.webp','image/webp',123,256,80)"
    )
    conn.execute(
        "INSERT INTO generation_images VALUES('i','g',0,'a','{}',124,'workflow','text2img')"
    )
    conn.execute(
        "INSERT INTO generation_references VALUES('ref','g',0,'reference.png','image/png',1234,1,NULL,'a')"
    )
    conn.execute("INSERT INTO agent_asset_leases VALUES('lease','a','scope',1,2,3,4)")
    conn.execute("INSERT INTO image_metadata VALUES('a','comfyui',12,?)", (encoded,))
    conn.execute("INSERT INTO import_batches VALUES('batch','fingerprint','{}',999)")
    conn.execute(
        "INSERT INTO external_sources(id,name,root_path,enabled,permissions_json) VALUES('nai','external','/outside',1,'{\"delete\":false}')"
    )
    conn.execute(
        "INSERT INTO external_records(generation_id,source_id,relative_path,asset_id,fingerprint,sidecar_fingerprint,size_bytes,mtime_ns,time_source,metadata_created_at,file_birthtime,time_policy_version) "
        "VALUES('g','nai','image.png','a','fingerprint','sidecar',1234,999,'metadata',123,122,1)"
    )
    conn.execute(
        "INSERT INTO comfy_workflow_revisions VALUES('r','r',?,123)", (encoded,)
    )
    conn.execute(
        "INSERT INTO comfy_jobs(id,provider_id,model_id,revision_id,status,request_json,input_refs_json,output_refs_json,created_at,updated_at,generation_id,model_name,request_fingerprint) "
        "VALUES('job','p','m','r','unknown','{\"seed\":18446744073709551615}','[{\"path\":\"blobs/input.image\"}]',?,123,124,'g','工作流','fingerprint')",
        (json.dumps([{"effective_parameters": {"_comfyui": marker}}]),),
    )
    conn.executemany(
        "INSERT INTO storage_payload_refs VALUES(?,?,?,?)",
        [
            ("comfy_workflow_revisions", "r", "config", digest),
            ("comfy_jobs", "job", "outputs/0/_comfyui", digest),
            ("image_metadata", "a", "metadata", digest),
        ],
    )
    conn.commit()
    return marker, {**value, "api_graph": graph, "workflow": workflow}


def test_final_development_promotion_only_changes_markers_and_is_idempotent(
    tmp_path, monkeypatch
):
    with closing(sqlite3.connect(tmp_path / "history.sqlite3")) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        create_v4_dev(conn)
        marker, value = populate_final_dev(conn)
        before = snapshot(conn)
        before_rows = business_rows(conn)

        def unexpected(*_args):
            pytest.fail("final development promotion must not convert bodies again")

        monkeypatch.setattr(v4_storage, "migrate_comfy_storage", unexpected)
        monkeypatch.setattr(v4_storage, "migrate_gallery_storage", unexpected)
        statements = []
        conn.set_trace_callback(statements.append)
        backup = schema.ensure_release_schema(conn, backup_dir=tmp_path / "backups")
        conn.set_trace_callback(None)
        assert conn.execute("PRAGMA user_version").fetchone() == (4,)
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='schema_meta'"
        ).fetchone()
        assert business_rows(conn) == before_rows
        assert load_payload(conn, marker) == value
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert [
            statement
            for statement in statements
            if statement.startswith(
                ("CREATE", "ALTER", "DROP", "INSERT", "UPDATE", "DELETE")
            )
        ] == ["DROP TABLE schema_meta"]
        with closing(sqlite3.connect(backup)) as saved:
            assert snapshot(saved) == before
        after = snapshot(conn)
        assert (
            schema.ensure_release_schema(conn, backup_dir=tmp_path / "backups") is None
        )
        assert snapshot(conn) == after
        assert list((tmp_path / "backups").glob("*.sqlite3")) == [backup]


def test_final_development_layout_matches_fresh_release(tmp_path):
    with (
        closing(sqlite3.connect(":memory:")) as final_dev,
        closing(sqlite3.connect(":memory:")) as fresh,
    ):
        create_v4_dev(final_dev)
        schema.ensure_release_schema(final_dev, backup_dir=tmp_path / "backups")
        schema.ensure_release_schema(fresh, backup_dir=tmp_path / "unused")
        assert snapshot(final_dev) == snapshot(fresh)


@pytest.mark.parametrize("baseline", ["v3", "4-dev.2"])
@pytest.mark.parametrize("failure", ["backup", "stamp"])
def test_backup_or_partial_stamp_failure_preserves_source(
    tmp_path, monkeypatch, baseline, failure
):
    with closing(sqlite3.connect(tmp_path / "history.sqlite3")) as conn:
        if baseline == "v3":
            create_v3_dev(conn)
            conn.execute("DROP TABLE schema_meta")
            conn.execute("PRAGMA user_version=3")
            conn.commit()
        else:
            create_v4_dev(conn)
            populate_final_dev(conn)
        before = snapshot(conn)

        def fail_backup(*_args):
            raise OSError("simulated backup failure")

        def fail_stamp(target):
            target.execute("PRAGMA user_version=4")
            assert not target.execute(
                "SELECT 1 FROM sqlite_master WHERE name='schema_meta'"
            ).fetchone()
            raise OSError("simulated stamp failure")

        monkeypatch.setattr(
            schema,
            "_backup_database" if failure == "backup" else "_stamp_release",
            fail_backup if failure == "backup" else fail_stamp,
        )
        with pytest.raises(OSError, match="simulated"):
            schema.ensure_release_schema(conn, backup_dir=tmp_path / "backups")
        assert snapshot(conn) == before
        assert not conn.in_transaction
        backups = list((tmp_path / "backups").glob("*.sqlite3"))
        assert len(backups) == (failure == "stamp")
        if backups:
            with closing(sqlite3.connect(backups[0])) as saved:
                assert snapshot(saved) == before


@pytest.mark.parametrize("revision", [0, 1, 3, 99])
def test_unsupported_development_revision_is_rejected_with_upgrade_advice(
    tmp_path, revision
):
    with closing(sqlite3.connect(tmp_path / "history.sqlite3")) as conn:
        create_v4_dev(conn, revision=revision)
        before = snapshot(conn)
        with pytest.raises(RuntimeError, match="6153f83.*4-dev.2"):
            schema.ensure_release_schema(conn, backup_dir=tmp_path / "backups")
        assert snapshot(conn) == before
        assert not (tmp_path / "backups").exists()


@pytest.mark.parametrize(
    "damage", ["title", "trigger", "wrong_release", "false_final_marker"]
)
def test_final_marker_cannot_bypass_layout_validation(tmp_path, damage):
    with closing(sqlite3.connect(tmp_path / "history.sqlite3")) as conn:
        create_v4_dev(conn, revision=1 if damage == "false_final_marker" else 2)
        if damage == "title":
            conn.execute("ALTER TABLE generations DROP COLUMN title")
        elif damage == "trigger":
            conn.execute("DROP TRIGGER storage_refs_delete_comfy_jobs")
        elif damage == "wrong_release":
            conn.execute("PRAGMA user_version=4")
        else:
            conn.execute("UPDATE schema_meta SET dev_revision=2")
        conn.commit()
        before = snapshot(conn)
        with pytest.raises(RuntimeError):
            schema.ensure_release_schema(conn, backup_dir=tmp_path / "backups")
        assert snapshot(conn) == before
        assert not (tmp_path / "backups").exists()


@pytest.mark.parametrize("change", ["marker", "title"])
def test_final_development_is_rechecked_under_lock_after_backup(
    tmp_path, monkeypatch, change
):
    database = tmp_path / "history.sqlite3"
    with closing(sqlite3.connect(database)) as conn:
        create_v4_dev(conn)
        populate_final_dev(conn)
        before = snapshot(conn)
        original_backup = schema._backup_database

        def changed(target, directory):
            backup = original_backup(target, directory)
            with closing(sqlite3.connect(database)) as other:
                other.execute(
                    "UPDATE schema_meta SET dev_revision=99"
                    if change == "marker"
                    else "UPDATE generations SET title='concurrent'"
                )
                other.commit()
            return backup

        monkeypatch.setattr(schema, "_backup_database", changed)
        with pytest.raises(RuntimeError, match="开发修订|备份后发生变化"):
            schema.ensure_release_schema(conn, backup_dir=tmp_path / "backups")
        assert conn.execute("PRAGMA user_version").fetchone() == (3,)
        assert conn.execute("SELECT 1 FROM schema_meta").fetchone()
        with closing(
            sqlite3.connect(next((tmp_path / "backups").glob("*.sqlite3")))
        ) as saved:
            assert snapshot(saved) == before


def test_development_revision_steps_are_absent_from_release_sql():
    statements = (*schema.V4_MIGRATION_STATEMENTS, *schema.PAYLOAD_TRIGGER_STATEMENTS)
    assert sorted(statements) == sorted(V4_FINAL_DEVELOPMENT_STATEMENTS)
    assert not any("schema_meta" in statement for statement in schema.SCHEMA_STATEMENTS)
