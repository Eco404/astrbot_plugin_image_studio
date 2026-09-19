"""The storage development migration is backed up, atomic and lossless."""

import json
import sqlite3
from contextlib import closing

import pytest

from astrbot_plugin_image_studio.backend.database import schema
from astrbot_plugin_image_studio.backend.database.payloads import (
    PAYLOAD_OWNERS,
    load_payload,
)
from astrbot_plugin_image_studio.backend.providers.comfyui.storage import expand_outputs
from astrbot_plugin_image_studio.tests.support.schema_upgrade_fixtures import (
    create_v3_dev,
)


def create_v3(conn):
    create_v3_dev(conn)
    conn.execute("DROP TABLE schema_meta")
    conn.execute("PRAGMA user_version=3")
    conn.commit()


def snapshot(conn):
    return tuple(conn.iterdump()), conn.execute("PRAGMA user_version").fetchone()[0]


def seed_legacy_workflow(conn):
    graph = {"1": {"class_type": "Seed (rgthree)", "inputs": {"seed": (1 << 64) - 1}}}
    ui = {"nodes": [{"id": 1, "widgets_values": [(1 << 64) - 1]}]}
    value = {
        "api_graph": graph,
        "api_graph_json": json.dumps(graph),
        "workflow": ui,
        "workflow_json": json.dumps(ui),
        "bindings": {},
        "outputs": [],
    }
    text = json.dumps(value)
    conn.execute(
        "INSERT INTO comfy_workflow_revisions VALUES('rev','rev',?,1)", (text,)
    )
    for identity in ("first", "second"):
        request = {"model": {"id": "model", "name": "workflow", "comfyui": value}}
        outputs = [{"effective_parameters": {"_comfyui": value}}]
        conn.execute(
            "INSERT INTO comfy_jobs(id,provider_id,model_id,revision_id,status,request_json,output_refs_json,created_at,updated_at) "
            "VALUES(?,'comfy','model','rev','succeeded',?,?,1,2)",
            (identity, json.dumps(request), json.dumps(outputs)),
        )
    conn.commit()
    return value


def test_v3_snapshots_migrate_into_one_shared_lossless_body(tmp_path):
    with closing(sqlite3.connect(tmp_path / "history.sqlite3")) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        create_v3(conn)
        value = seed_legacy_workflow(conn)
        before = snapshot(conn)
        backup = schema.ensure_release_schema(conn, backup_dir=tmp_path / "backups")
        assert conn.execute("SELECT count(*) FROM storage_payloads").fetchone()[0] == 1
        assert (
            conn.execute("SELECT count(*) FROM storage_payload_refs").fetchone()[0] == 3
        )
        marker = json.loads(
            conn.execute("SELECT config_json FROM comfy_workflow_revisions").fetchone()[
                0
            ]
        )
        assert load_payload(conn, marker) == value
        for request, output in conn.execute(
            "SELECT request_json,output_refs_json FROM comfy_jobs"
        ):
            assert "comfyui" not in json.loads(request)["model"]
            assert (
                expand_outputs(conn, json.loads(output))[0]["effective_parameters"][
                    "_comfyui"
                ]
                == value
            )
        assert conn.execute(
            "SELECT target_version,dev_revision FROM schema_meta"
        ).fetchone() == (4, 1)
        with closing(sqlite3.connect(backup)) as saved:
            assert snapshot(saved) == before
        after = snapshot(conn)
        assert (
            schema.ensure_release_schema(conn, backup_dir=tmp_path / "backups") is None
        )
        assert snapshot(conn) == after
        assert PAYLOAD_OWNERS == schema._PAYLOAD_OWNER_KEYS


@pytest.mark.parametrize(
    "damage", ["marker", "trigger", "index", "expression", "payload_fk"]
)
def test_current_development_layout_is_validated_before_any_writes(tmp_path, damage):
    with closing(sqlite3.connect(tmp_path / "history.sqlite3")) as conn:
        schema.ensure_release_schema(conn, backup_dir=tmp_path / "backups")
        if damage == "marker":
            conn.execute("UPDATE schema_meta SET dev_revision=99")
        elif damage == "trigger":
            conn.execute("DROP TRIGGER storage_refs_delete_comfy_jobs")
        elif damage == "index":
            conn.execute("DROP INDEX idx_comfy_jobs_queue")
        elif damage == "expression":
            conn.execute("DROP INDEX idx_generations_latest_content")
            conn.execute(
                "CREATE INDEX idx_generations_latest_content ON generations(COALESCE(created_at,latest_content_at) DESC,created_at DESC,id DESC)"
            )
        else:
            conn.execute("DROP TABLE storage_payload_refs")
            conn.execute(
                "CREATE TABLE storage_payload_refs(owner_table TEXT NOT NULL,owner_id TEXT NOT NULL,slot TEXT NOT NULL,payload_id TEXT NOT NULL,PRIMARY KEY(owner_table,owner_id,slot))"
            )
            conn.execute(
                "CREATE INDEX idx_storage_payload_refs_payload ON storage_payload_refs(payload_id)"
            )
        conn.commit()
        before = snapshot(conn)
        with pytest.raises(RuntimeError):
            schema.ensure_release_schema(conn, backup_dir=tmp_path / "backups")
        assert snapshot(conn) == before
        assert not (tmp_path / "backups").exists()


def test_payload_migration_failure_rolls_back_every_schema_and_business_change(
    tmp_path, monkeypatch
):
    from astrbot_plugin_image_studio.backend.gallery import storage

    with closing(sqlite3.connect(tmp_path / "history.sqlite3")) as conn:
        create_v3(conn)
        seed_legacy_workflow(conn)
        before = snapshot(conn)

        def fail(target):
            assert (
                target.execute("SELECT count(*) FROM storage_payloads").fetchone()[0]
                == 1
            )
            raise RuntimeError("injected migration failure")

        monkeypatch.setattr(storage, "migrate_gallery_storage", fail)
        with pytest.raises(RuntimeError, match="injected migration failure"):
            schema.ensure_release_schema(conn, backup_dir=tmp_path / "backups")
        assert snapshot(conn) == before
        assert not conn.in_transaction
        backups = list((tmp_path / "backups").glob("*.sqlite3"))
        assert len(backups) == 1
        with closing(sqlite3.connect(backups[0])) as saved:
            assert snapshot(saved) == before


def test_malformed_legacy_json_aborts_without_erasing_the_original(tmp_path):
    with closing(sqlite3.connect(tmp_path / "history.sqlite3")) as conn:
        create_v3(conn)
        seed_legacy_workflow(conn)
        conn.execute("UPDATE comfy_workflow_revisions SET config_json='not valid JSON'")
        conn.commit()
        before = snapshot(conn)
        with pytest.raises(ValueError):
            schema.ensure_release_schema(conn, backup_dir=tmp_path / "backups")
        assert snapshot(conn) == before
        assert len(list((tmp_path / "backups").glob("*.sqlite3"))) == 1


def test_migration_does_not_follow_a_linked_backup_directory(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    backup_dir = tmp_path / "backups"
    backup_dir.symlink_to(outside, target_is_directory=True)
    with closing(sqlite3.connect(tmp_path / "history.sqlite3")) as conn:
        create_v3(conn)
        before = snapshot(conn)
        with pytest.raises(RuntimeError, match="符号链接"):
            schema.ensure_release_schema(conn, backup_dir=backup_dir)
        assert snapshot(conn) == before
        assert list(outside.iterdir()) == []
