"""The storage development migration is backed up, atomic and lossless."""

import json
import sqlite3
from contextlib import closing

import pytest

from astrbot_plugin_image_studio.backend.database import schema
from astrbot_plugin_image_studio.backend.database.migrations import v4_storage
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
        ).fetchone() == (4, 2)
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

        monkeypatch.setattr(v4_storage, "migrate_gallery_storage", fail)
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


def test_v4_migration_is_independent_of_current_repository_codecs(
    tmp_path, monkeypatch
):
    from astrbot_plugin_image_studio.backend.gallery import projection, storage
    from astrbot_plugin_image_studio.backend.providers.comfyui import (
        storage as task_storage,
    )

    with closing(sqlite3.connect(tmp_path / "history.sqlite3")) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        create_v3(conn)
        workflow = seed_legacy_workflow(conn)
        metadata = {
            "format": "comfyui",
            "parser_version": 12,
            "rules_fingerprint": "original-rules",
            "raw": {"prompt": workflow["api_graph_json"]},
            "normalized": {
                "prompt": "saved prompt",
                "seed": (1 << 64) - 1,
                "parameters": {"steps": 6, "workflow": workflow},
                "prompt_candidates": [{"node_id": "1", "value": "original evidence"}],
            },
        }
        supplemental = {
            "original_filename": "legacy.png",
            "generated_at": 123,
            "model": "saved model",
            "mode": "text2img",
            "overrides": {"prompt": "manual prompt"},
            "display_parameters": {
                **metadata["normalized"],
                "prompt": "manual prompt",
            },
            "comfyui": workflow,
        }
        conn.execute(
            "INSERT INTO generations(id,created_at,source,status,mode,provider_id,"
            "provider_name,provider_kind,model,original_prompt,final_prompt,"
            "parameters_json,elapsed_ms,supplemental_json) "
            "VALUES('group',100,'import','success','text2img','','','','','','','{}',0,?)",
            (json.dumps(supplemental),),
        )
        conn.execute(
            "INSERT INTO image_assets(id,path,mime_type,size_bytes,created_at) "
            "VALUES('asset','history/assets/asset.png','image/png',12,100)"
        )
        conn.execute(
            "INSERT INTO generation_images VALUES('image','group',0,'asset',?)",
            (json.dumps(supplemental),),
        )
        conn.execute(
            "INSERT INTO image_metadata VALUES('asset','comfyui',12,?)",
            (json.dumps(metadata),),
        )
        conn.commit()

        def current_behavior_changed(*_args, **_kwargs):
            raise AssertionError("migration depended on a current repository codec")

        with monkeypatch.context() as patched:
            for module, names in (
                (
                    storage,
                    (
                        "encode_metadata",
                        "encode_supplemental",
                        "refresh_gallery_projection",
                    ),
                ),
                (
                    projection,
                    (
                        "searchable_parameters",
                        "_search_projection",
                        "project_import_metadata",
                    ),
                ),
                (
                    task_storage,
                    (
                        "compact_request",
                        "compact_result",
                        "compact_outputs",
                        "request_fingerprint",
                    ),
                ),
            ):
                for name in names:
                    patched.setattr(module, name, current_behavior_changed)
            schema.ensure_release_schema(conn, backup_dir=tmp_path / "backups")

        saved_metadata = conn.execute(
            "SELECT metadata_json FROM image_metadata WHERE asset_id='asset'"
        ).fetchone()[0]
        assert storage.decode_metadata(conn, saved_metadata) == metadata
        image = conn.execute(
            "SELECT supplemental_json,generated_at,model,mode "
            "FROM generation_images WHERE id='image'"
        ).fetchone()
        compact = json.loads(image[0])
        assert image[1:] == (123.0, "saved model", "text2img")
        assert compact["overrides"] == supplemental["overrides"]
        assert compact["_image_studio_storage"]["derived_display"] is True
        assert load_payload(conn, compact["comfyui"]) == workflow
        group = conn.execute(
            "SELECT supplemental_json,latest_content_at,search_text FROM generations WHERE id='group'"
        ).fetchone()
        assert json.loads(group[0])["_image_studio_storage"]["group_image"] == "image"
        assert group[1] == 123
        assert "manual prompt" in group[2]
        assert "original evidence" not in group[2]
        assert "class_type" not in group[2]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
