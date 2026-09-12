"""Upgrade fixtures for the v1 -> v2 dev gallery schema."""

import sqlite3
from contextlib import closing

import pytest

from astrbot_plugin_image_studio import database_schema as schema


def dump(conn):
    return tuple(conn.iterdump()), conn.execute("PRAGMA user_version").fetchone()[0]


@pytest.mark.parametrize("existing", [False, True])
def test_create_and_upgrade_share_layout_and_repeat_without_writes(tmp_path, existing):
    with closing(sqlite3.connect(tmp_path / "history.sqlite3")) as conn:
        if existing:
            schema.ensure_release_schema(conn, backup_dir=tmp_path / "backups")
            conn.execute(
                "INSERT INTO image_assets VALUES ('asset', 'owned.png', 'image/png', 42, 1, 1, 10, 'available')"
            )
            conn.commit()
        before = dump(conn)
        backup = schema.ensure_development_schema(conn, backup_dir=tmp_path / "backups")
        assert schema.DATABASE_VERSION == "2-dev.2"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert conn.execute("SELECT * FROM schema_meta").fetchall() == [(1, 2, 2)]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        if existing:
            assert backup and backup.is_file()
            with closing(sqlite3.connect(backup)) as old:
                assert dump(old) == before
            assert (
                conn.execute(
                    "SELECT size_bytes FROM image_assets WHERE id='asset'"
                ).fetchone()[0]
                == 42
            )
        else:
            assert backup is None
        after = dump(conn)
        statements = []
        conn.set_trace_callback(statements.append)
        assert (
            schema.ensure_development_schema(conn, backup_dir=tmp_path / "backups")
            is None
        )
        assert dump(conn) == after
        assert not any(
            s.startswith(("CREATE", "DROP", "ALTER", "INSERT", "UPDATE", "BEGIN"))
            for s in statements
        )
        with pytest.raises(RuntimeError, match="开发修订"):
            schema.ensure_release_schema(conn, backup_dir=tmp_path / "backups")


@pytest.mark.parametrize("corruption", ["future", "columns", "index", "marker", "fk"])
def test_unknown_or_malformed_dev_database_is_never_repaired_blindly(
    tmp_path, corruption
):
    with closing(sqlite3.connect(tmp_path / "history.sqlite3")) as conn:
        schema.ensure_development_schema(conn, backup_dir=tmp_path / "backups")
        if corruption == "future":
            conn.execute("UPDATE schema_meta SET dev_revision=99")
        elif corruption == "columns":
            conn.execute("ALTER TABLE external_sources DROP COLUMN name")
        elif corruption == "index":
            conn.execute("DROP INDEX idx_external_records_asset")
        elif corruption == "marker":
            conn.execute("DROP TABLE schema_meta")
        else:
            conn.execute("DROP TABLE external_records")
        conn.commit()
        before = dump(conn)
        with pytest.raises(RuntimeError):
            schema.ensure_development_schema(conn, backup_dir=tmp_path / "backups")
        assert dump(conn) == before
        assert not (tmp_path / "backups").exists()


@pytest.mark.parametrize("existing", [False, True])
def test_failed_upgrade_rolls_back_all_development_tables(
    tmp_path, monkeypatch, existing
):
    with closing(sqlite3.connect(tmp_path / "history.sqlite3")) as conn:
        if existing:
            schema.ensure_release_schema(conn, backup_dir=tmp_path / "backups")
        before = dump(conn)

        def fail(target):
            target.execute("INSERT INTO schema_meta VALUES (1, 2, 1)")
            raise RuntimeError("simulated disk error")

        monkeypatch.setattr(schema, "_stamp_development", fail)
        with pytest.raises(RuntimeError, match="disk error"):
            schema.ensure_development_schema(conn, backup_dir=tmp_path / "backups")
        assert dump(conn) == before
        assert not conn.in_transaction
        assert len(list((tmp_path / "backups").glob("*.sqlite3"))) == int(existing)


def test_backup_failure_blocks_dev_upgrade(tmp_path, monkeypatch):
    with closing(sqlite3.connect(tmp_path / "history.sqlite3")) as conn:
        schema.ensure_release_schema(conn, backup_dir=tmp_path / "backups")
        before = dump(conn)

        def fail(*args):
            raise OSError("full disk")

        monkeypatch.setattr(schema, "_backup_database", fail)
        with pytest.raises(OSError, match="full disk"):
            schema.ensure_development_schema(conn, backup_dir=tmp_path / "backups")
        assert dump(conn) == before


def old_external_database(conn):
    for statement in (*schema.SCHEMA_STATEMENTS, *schema.DEVELOPMENT_V1_STATEMENTS):
        conn.execute(statement)
    conn.execute("PRAGMA user_version=1")
    conn.execute("INSERT INTO schema_meta VALUES (1,2,1)")
    conn.execute(
        "INSERT INTO external_sources VALUES ('nai','NAI 插件图库','/fixture/nai',1,'{}')"
    )
    conn.execute(
        "INSERT INTO generations(id,created_at,source,status,mode,provider_id,provider_name,provider_kind,model,original_prompt,final_prompt,parameters_json,elapsed_ms,is_favorite) VALUES ('g',123,'external','succeeded','text2img','','','','m','p','p','{}',0,1)"
    )
    conn.execute(
        "INSERT INTO image_assets VALUES ('a','external/a.png','image/png',10,1,1,123,'available')"
    )
    conn.execute("INSERT INTO generation_images VALUES ('i','g',0,'a','{}')")
    conn.execute(
        "INSERT INTO external_records VALUES ('g','nai','nai_123.png','a','{}','{}',10,123,1)"
    )
    conn.commit()


def test_previous_dev_upgrade_preserves_favorites_links_and_schedules_time_backfill(
    tmp_path,
):
    with closing(sqlite3.connect(tmp_path / "history.sqlite3")) as conn:
        old_external_database(conn)
        before = dump(conn)
        backup = schema.ensure_development_schema(conn, backup_dir=tmp_path / "backups")
        assert backup
        with closing(sqlite3.connect(backup)) as old:
            assert dump(old) == before
        assert conn.execute("SELECT * FROM schema_meta").fetchall() == [(1, 2, 2)]
        assert conn.execute(
            "SELECT created_at,is_favorite FROM generations WHERE id='g'"
        ).fetchone() == (123, 1)
        assert conn.execute(
            "SELECT type,recursive FROM external_sources WHERE id='nai'"
        ).fetchone() == ("nai", 0)
        assert conn.execute(
            "SELECT time_source,time_policy_version,metadata_created_at,file_birthtime FROM external_records"
        ).fetchone() == ("", 0, None, None)
        assert conn.execute(
            "SELECT generation_id,asset_id FROM generation_images"
        ).fetchone() == ("g", "a")
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            schema.ensure_development_schema(conn, backup_dir=tmp_path / "backups")
            is None
        )


def test_previous_dev_failed_upgrade_rolls_back_columns_and_version(
    tmp_path, monkeypatch
):
    with closing(sqlite3.connect(tmp_path / "history.sqlite3")) as conn:
        old_external_database(conn)
        before = dump(conn)

        def fail(target):
            target.execute("UPDATE schema_meta SET dev_revision=2")
            raise RuntimeError("interrupted upgrade")

        monkeypatch.setattr(schema, "_stamp_development", fail)
        with pytest.raises(RuntimeError, match="interrupted upgrade"):
            schema.ensure_development_schema(conn, backup_dir=tmp_path / "backups")
        assert dump(conn) == before
        assert len(list((tmp_path / "backups").glob("*.sqlite3"))) == 1
