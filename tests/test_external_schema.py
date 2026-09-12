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
        assert schema.DATABASE_VERSION == "2-dev.1"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert conn.execute("SELECT * FROM schema_meta").fetchall() == [(1, 2, 1)]
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
