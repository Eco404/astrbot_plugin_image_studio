from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from contextlib import closing

import pytest

from astrbot_plugin_image_studio.backend.database import schema as schema
from astrbot_plugin_image_studio.backend.config import HistorySettings
from astrbot_plugin_image_studio.backend.models import (
    GeneratedImage,
    GenerationRequest,
    ImageProvider,
    ReferenceImage,
)
from astrbot_plugin_image_studio.backend.gallery.store import GenerationStore
from astrbot_plugin_image_studio.tests.support.schema_upgrade_fixtures import create_v1
from astrbot_plugin_image_studio.tests.backend.test_import_groups import image


def mark_development(conn, revision=3, target=1):
    conn.execute("PRAGMA user_version = 0")
    conn.execute(
        "CREATE TABLE schema_meta (id INTEGER PRIMARY KEY CHECK(id = 1), "
        "target_version INTEGER NOT NULL, dev_revision INTEGER NOT NULL)"
    )
    conn.execute("INSERT INTO schema_meta VALUES (1, ?, ?)", (target, revision))


def snapshot(conn):
    return conn.execute("PRAGMA user_version").fetchone()[0], tuple(conn.iterdump())


def business_rows(conn):
    return {
        table: [
            tuple(row) for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY 1')
        ]
        for table in (
            "generations",
            "generation_images",
            "generation_references",
            "image_assets",
            "image_thumbnails",
            "agent_asset_leases",
            "image_metadata",
            "import_batches",
        )
    }


async def populated_store(directory):
    store = GenerationStore(directory)
    await store.initialize()
    provider = ImageProvider.from_mapping(
        {"id": "nai", "kind": "nai_direct", "model": "model"}
    )
    raw = image("red")
    generated = await store.record_success(
        provider=provider,
        request=GenerationRequest(
            mode="text2img",
            provider_id="nai",
            prompt="saved prompt",
            model="model",
            negative_prompt="saved negative",
            parameters={"cfg": 0.3, "steps": 26},
            references=(
                ReferenceImage("reference", "reference.png", raw, "image/png"),
            ),
            source="command",
        ),
        images=(
            GeneratedImage(raw, "image/png"),
            GeneratedImage(image("blue"), "image/png"),
        ),
        elapsed_ms=123,
        history=HistorySettings(True, 100, 0, True),
    )
    await store.set_favorite(generated, True)
    imported = await store.import_image(
        image("green"), "manual.png", {"prompt": "manual override"}
    )
    await store.lease_agent_images(
        (GeneratedImage(raw, "image/png"),),
        scope_id="scope",
        create_preview=True,
        preview_max_edge=768,
        preview_quality=80,
    )
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO import_batches VALUES (?, ?, ?, ?)",
            (
                "receipt",
                "fingerprint",
                json.dumps({"generation_id": imported["generation_id"]}),
                time.time() + 3600,
            ),
        )
    return store


def test_new_release_creates_complete_schema_once_without_development_metadata(
    tmp_path,
):
    with closing(sqlite3.connect(tmp_path / "history.sqlite3")) as conn:
        assert (
            schema.ensure_release_schema(conn, backup_dir=tmp_path / "backups") is None
        )
        assert schema.RELEASE_VERSION == 3
        assert schema.DATABASE_VERSION == 3
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='schema_meta'"
            ).fetchone()
            is None
        )
        assert "supplemental_json" in {
            row[1] for row in conn.execute("PRAGMA table_info(generation_images)")
        }
        assert {"width", "height", "file_state"} <= {
            row[1] for row in conn.execute("PRAGMA table_info(image_assets)")
        }
        assert {
            "is_favorite",
            "cleanup_protected_until",
            "import_key",
            "generated_at",
            "search_text",
        } <= {row[1] for row in conn.execute("PRAGMA table_info(generations)")}
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name='import_batches'"
        ).fetchone()
        before = snapshot(conn)
        statements = []
        conn.set_trace_callback(statements.append)
        schema.ensure_release_schema(conn, backup_dir=tmp_path / "backups")
        assert not any(
            statement.lstrip()
            .upper()
            .startswith(
                ("CREATE", "ALTER", "DROP", "UPDATE", "INSERT", "DELETE", "BEGIN")
            )
            for statement in statements
        )
        assert snapshot(conn) == before
    assert not (tmp_path / "backups").exists()


@pytest.mark.parametrize("promotion", [False, True])
def test_changed_partial_index_predicate_is_rejected_without_mutations(
    tmp_path, promotion
):
    with closing(sqlite3.connect(tmp_path / "history.sqlite3")) as conn:
        if promotion:
            create_v1(conn)
            mark_development(conn)
        else:
            schema.ensure_release_schema(conn, backup_dir=tmp_path / "backups")
        conn.execute("DROP INDEX idx_generations_import_key")
        conn.execute(
            "CREATE UNIQUE INDEX idx_generations_import_key "
            "ON generations(import_key) WHERE import_key IS NULL"
        )
        conn.commit()
        before = snapshot(conn)
        with pytest.raises(RuntimeError, match="结构与正式基线不符"):
            schema.ensure_release_schema(conn, backup_dir=tmp_path / "backups")
        assert snapshot(conn) == before
    assert not (tmp_path / "backups").exists()


def test_development_if_not_exists_partial_index_matches_release(tmp_path):
    with closing(sqlite3.connect(":memory:")) as conn:
        create_v1(conn)
        mark_development(conn)
        conn.execute("DROP INDEX idx_generations_import_key")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_generations_import_key "
            "ON generations(import_key) WHERE import_key IS NOT NULL"
        )
        conn.commit()
        assert schema.ensure_release_schema(conn, backup_dir=tmp_path / "backups")
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3


@pytest.mark.parametrize("promotion", [False, True])
def test_v1_upgrade_preserves_all_business_rows_files_and_restore_backup(
    tmp_path,
    promotion,
):
    async def run():
        store = await populated_store(tmp_path)
        files = {
            str(path.relative_to(tmp_path)): path.read_bytes()
            for path in store.history_dir.rglob("*")
            if path.is_file()
        }
        with store._connect() as conn:
            for table in (
                "comfy_jobs",
                "comfy_workflow_revisions",
                "external_records",
                "external_sources",
            ):
                conn.execute(f"DROP TABLE {table}")
            if promotion:
                mark_development(conn)
            else:
                conn.execute("PRAGMA user_version = 1")
        with store._connect() as conn:
            before = business_rows(conn)
            old_snapshot = snapshot(conn)
        promoted = GenerationStore(tmp_path)
        await promoted.initialize()
        with promoted._connect() as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
            assert (
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='schema_meta'"
                ).fetchone()
                is None
            )
            assert business_rows(conn) == before
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        backups = list((tmp_path / "backups").glob("history-pre-v3-*.sqlite3"))
        assert len(backups) == 1
        with closing(sqlite3.connect(backups[0])) as backup:
            assert snapshot(backup) == old_snapshot
            with closing(sqlite3.connect(tmp_path / "restored.sqlite3")) as restored:
                backup.backup(restored)
                assert snapshot(restored) == old_snapshot
        await promoted.initialize()
        with promoted._connect() as conn:
            assert business_rows(conn) == before
        assert list((tmp_path / "backups").glob("*.sqlite3")) == backups
        assert {
            str(path.relative_to(tmp_path)): path.read_bytes()
            for path in store.history_dir.rglob("*")
            if path.is_file()
        } == files
        await promoted.run_maintenance(
            HistorySettings(False, 0, 0, False),
            preview_max_edge=768,
            preview_quality=80,
        )
        assert backups[0].is_file(), (
            "maintenance must not delete release conversion backups"
        )

    asyncio.run(run())


@pytest.mark.parametrize("version", [1, 2, 99])
def test_older_or_unknown_development_revision_is_rejected_without_mutations(
    tmp_path, version
):
    with closing(sqlite3.connect(tmp_path / "history.sqlite3")) as conn:
        create_v1(conn)
        mark_development(conn, revision=version)
        conn.commit()
        before = snapshot(conn)
        with pytest.raises(RuntimeError, match="末版开发版"):
            schema.ensure_release_schema(conn, backup_dir=tmp_path / "backups")
        assert snapshot(conn) == before
    assert not (tmp_path / "backups").exists()


@pytest.mark.parametrize(
    "case",
    [
        "future",
        "unversioned",
        "future_dev",
        "target",
        "meta_shape",
        "meta_rows",
        "missing_column",
        "missing_index",
        "missing_table",
        "foreign_key",
    ],
)
def test_unsupported_or_malformed_database_is_rejected_before_backup_or_changes(
    tmp_path, case
):
    with closing(sqlite3.connect(tmp_path / "history.sqlite3")) as conn:
        create_v1(conn)
        if case == "future":
            conn.execute("PRAGMA user_version = 4")
        elif case == "unversioned":
            conn.execute("PRAGMA user_version = 0")
        else:
            mark_development(conn, target=2 if case == "target" else 1)
            if case == "future_dev":
                conn.execute("PRAGMA user_version = 1")
            elif case == "meta_shape":
                conn.execute("ALTER TABLE schema_meta ADD COLUMN user_note TEXT")
            elif case == "meta_rows":
                conn.execute("DELETE FROM schema_meta")
            elif case == "missing_column":
                conn.execute(
                    "ALTER TABLE generation_images DROP COLUMN supplemental_json"
                )
            elif case == "missing_index":
                conn.execute("DROP INDEX idx_generations_import_key")
            elif case == "missing_table":
                conn.execute("DROP TABLE import_batches")
            elif case == "foreign_key":
                conn.execute("DROP TABLE image_metadata")
                conn.execute(
                    "CREATE TABLE image_metadata(asset_id TEXT PRIMARY KEY, format TEXT NOT NULL, parser_version INTEGER NOT NULL, metadata_json TEXT NOT NULL)"
                )
        conn.commit()
        before = snapshot(conn)
        with pytest.raises(RuntimeError):
            schema.ensure_release_schema(conn, backup_dir=tmp_path / "backups")
        assert snapshot(conn) == before
    assert not (tmp_path / "backups").exists()


@pytest.mark.parametrize("promotion", [False, True])
def test_failed_transaction_rolls_back_schema_and_version_stamp(
    tmp_path, monkeypatch, promotion
):
    with closing(sqlite3.connect(tmp_path / "history.sqlite3")) as conn:
        if promotion:
            create_v1(conn)
            mark_development(conn)
            conn.commit()
        before = snapshot(conn)

        def fail_stamp(target):
            target.execute("PRAGMA user_version = 1")
            raise RuntimeError("simulated stamp failure")

        monkeypatch.setattr(schema, "_stamp_release", fail_stamp)
        with pytest.raises(RuntimeError, match="stamp failure"):
            schema.ensure_release_schema(conn, backup_dir=tmp_path / "backups")
        assert snapshot(conn) == before
        assert not conn.in_transaction
        backups = list((tmp_path / "backups").glob("*.sqlite3"))
        assert len(backups) == int(promotion)
        if backups:
            with closing(sqlite3.connect(backups[0])) as backup:
                assert snapshot(backup) == before


def test_backup_failure_prevents_promotion(tmp_path, monkeypatch):
    with closing(sqlite3.connect(tmp_path / "history.sqlite3")) as conn:
        create_v1(conn)
        mark_development(conn)
        conn.commit()
        before = snapshot(conn)

        def fail_backup(*_args):
            raise OSError("simulated full disk")

        monkeypatch.setattr(schema, "_backup_database", fail_backup)
        with pytest.raises(OSError, match="full disk"):
            schema.ensure_release_schema(conn, backup_dir=tmp_path / "backups")
        assert snapshot(conn) == before


def test_promotion_rechecks_version_after_backup_before_schema_changes(
    tmp_path, monkeypatch
):
    with closing(sqlite3.connect(tmp_path / "history.sqlite3")) as conn:
        create_v1(conn)
        mark_development(conn)
        conn.commit()
        original_backup = schema._backup_database

        def changed_after_backup(target, directory):
            backup = original_backup(target, directory)
            target.execute("UPDATE schema_meta SET dev_revision = 99")
            target.commit()
            return backup

        monkeypatch.setattr(schema, "_backup_database", changed_after_backup)
        with pytest.raises(RuntimeError, match="开发修订"):
            schema.ensure_release_schema(conn, backup_dir=tmp_path / "backups")
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert conn.execute("SELECT dev_revision FROM schema_meta").fetchone()[0] == 99


def test_dimension_repairs_are_independent_and_retry_on_later_maintenance(tmp_path):
    async def run():
        store = await populated_store(tmp_path)
        with store._connect() as conn:
            asset = conn.execute("SELECT id, path FROM image_assets LIMIT 1").fetchone()
            conn.execute(
                "UPDATE image_assets SET width=0,height=0 WHERE id=?", (asset["id"],)
            )
        original = tmp_path / asset["path"]
        held = tmp_path / "temporarily-held.png"
        original.rename(held)
        await store.initialize()
        with store._connect() as conn:
            assert tuple(
                conn.execute(
                    "SELECT width,height FROM image_assets WHERE id=?", (asset["id"],)
                ).fetchone()
            ) == (0, 0)
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        original.parent.mkdir(parents=True, exist_ok=True)
        held.rename(original)
        await store.run_maintenance(
            HistorySettings(False, 0, 0, False),
            preview_max_edge=768,
            preview_quality=80,
        )
        with store._connect() as conn:
            assert tuple(
                conn.execute(
                    "SELECT width,height FROM image_assets WHERE id=?", (asset["id"],)
                ).fetchone()
            ) == (24, 32)
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 3

    asyncio.run(run())
