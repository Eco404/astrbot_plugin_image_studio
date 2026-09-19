"""Independent upgrade fixtures for the published v1 -> v2 migration."""

import sqlite3
from contextlib import closing

import pytest

from astrbot_plugin_image_studio.backend.database import schema as schema
from astrbot_plugin_image_studio.tests.support.schema_upgrade_fixtures import (
    V1_STATEMENTS,
    create_final_dev,
    create_v1,
)


def dump(conn):
    return tuple(conn.iterdump()), conn.execute("PRAGMA user_version").fetchone()[0]


def populate_external(conn):
    conn.execute(
        "INSERT INTO external_sources(id,name,root_path,enabled,type,recursive,permissions_json,status_json) "
        "VALUES ('nai','NAI 插件图库','/fixture/nai',1,'nai',1,'{\"delete\":false}','{\"status\":\"completed\"}')"
    )
    conn.execute(
        "INSERT INTO generations(id,created_at,source,status,mode,provider_id,provider_name,provider_kind,model,original_prompt,final_prompt,parameters_json,elapsed_ms,is_favorite) VALUES ('g',123,'external','succeeded','text2img','','','','m','p','p','{}',0,1)"
    )
    conn.execute(
        "INSERT INTO image_assets VALUES ('a','external/a.png','image/png',10,1,1,123,'available')"
    )
    conn.execute("INSERT INTO generation_images VALUES ('i','g',0,'a','{}')")
    conn.execute(
        "INSERT INTO external_records(generation_id,source_id,relative_path,asset_id,fingerprint,sidecar_fingerprint,size_bytes,mtime_ns,available,time_source,metadata_created_at,file_birthtime,time_policy_version) "
        "VALUES ('g','nai','nai_123.png','a','{}','{}',10,123,1,'metadata',122,121,1)"
    )
    conn.commit()


def test_published_v1_definition_has_not_changed():
    assert schema.V1_SCHEMA_STATEMENTS == V1_STATEMENTS


@pytest.mark.parametrize("existing", ["empty", "v1", "final_dev"])
def test_create_and_upgrade_share_layout_and_repeat_without_writes(tmp_path, existing):
    with closing(sqlite3.connect(tmp_path / "history.sqlite3")) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        if existing == "final_dev":
            create_final_dev(conn)
            populate_external(conn)
        elif existing == "v1":
            create_v1(conn)
            conn.execute(
                "INSERT INTO image_assets VALUES ('asset', 'owned.png', 'image/png', 42, 1, 1, 10, 'available')"
            )
            conn.commit()
        before = dump(conn)
        migration_statements = []
        conn.set_trace_callback(migration_statements.append)
        backup = schema.ensure_release_schema(conn, backup_dir=tmp_path / "backups")
        conn.set_trace_callback(None)
        assert not any(
            s.startswith("ALTER TABLE external_") for s in migration_statements
        )
        if existing == "final_dev":
            assert not any(
                s.startswith(
                    (
                        "ALTER TABLE external_",
                        "DROP TABLE external_",
                        "UPDATE external_",
                        "DELETE FROM external_",
                    )
                )
                for s in migration_statements
            ), (
                "Final dev promotion must preserve its original external tables and rows."
            )
        assert schema.DATABASE_VERSION == 4
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='schema_meta'"
        ).fetchone()
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        if existing != "empty":
            assert backup and backup.is_file()
            with closing(sqlite3.connect(backup)) as old:
                assert dump(old) == before
        else:
            assert backup is None
        if existing == "v1":
            assert conn.execute(
                "SELECT size_bytes FROM image_assets WHERE id='asset'"
            ).fetchone() == (42,)
        if existing == "final_dev":
            assert conn.execute(
                "SELECT created_at,is_favorite FROM generations WHERE id='g'"
            ).fetchone() == (123, 1)
            assert conn.execute(
                "SELECT type,recursive,permissions_json,status_json FROM external_sources"
            ).fetchone() == ("nai", 1, '{"delete":false}', '{"status":"completed"}')
            assert conn.execute(
                "SELECT time_source,metadata_created_at,file_birthtime,time_policy_version FROM external_records"
            ).fetchone() == ("metadata", 122, 121, 1)
            assert conn.execute(
                "SELECT generation_id,asset_id FROM generation_images"
            ).fetchone() == ("g", "a")
        with closing(sqlite3.connect(":memory:")) as fresh:
            schema.ensure_release_schema(fresh, backup_dir=tmp_path / "unused")
            assert {
                table: schema._table_shape(conn, table) for table in schema._TABLES
            } == {table: schema._table_shape(fresh, table) for table in schema._TABLES}
            if existing == "final_dev":
                assert list(
                    conn.execute("PRAGMA table_info(external_sources)")
                ) != list(fresh.execute("PRAGMA table_info(external_sources)")), (
                    "The frozen ALTER fixture must have a different physical column order."
                )
        after = dump(conn)
        statements = []
        conn.set_trace_callback(statements.append)
        assert (
            schema.ensure_release_schema(conn, backup_dir=tmp_path / "backups") is None
        )
        assert dump(conn) == after
        assert not any(
            s.startswith(("CREATE", "DROP", "ALTER", "INSERT", "UPDATE", "BEGIN"))
            for s in statements
        )


@pytest.mark.parametrize(
    "corruption",
    [
        "future",
        "columns",
        "index",
        "marker",
        "fk",
        "default",
        "type",
        "meta_check",
        "release_marker",
    ],
)
def test_unknown_or_malformed_dev_database_is_never_repaired_blindly(
    tmp_path, corruption
):
    with closing(sqlite3.connect(tmp_path / "history.sqlite3")) as conn:
        create_final_dev(conn)
        if corruption == "future":
            conn.execute("UPDATE schema_meta SET dev_revision=99")
        elif corruption == "columns":
            conn.execute("ALTER TABLE external_sources DROP COLUMN name")
        elif corruption == "index":
            conn.execute("DROP INDEX idx_external_records_asset")
        elif corruption == "marker":
            conn.execute("DROP TABLE schema_meta")
        elif corruption == "release_marker":
            conn.execute("PRAGMA user_version = 2")
        elif corruption == "meta_check":
            conn.execute("DROP TABLE schema_meta")
            conn.execute(
                "CREATE TABLE schema_meta(id INTEGER PRIMARY KEY, target_version INTEGER NOT NULL, dev_revision INTEGER NOT NULL)"
            )
            conn.execute("INSERT INTO schema_meta VALUES (1,2,2)")
        else:
            declaration = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='external_records'"
            ).fetchone()[0]
            if corruption == "fk":
                declaration = declaration.replace(
                    "REFERENCES generations(id) ON DELETE CASCADE", ""
                )
            elif corruption == "default":
                declaration = declaration.replace("DEFAULT 1", "DEFAULT 0")
            else:
                declaration = declaration.replace("mtime_ns INTEGER", "mtime_ns REAL")
            conn.execute("DROP TABLE external_records")
            conn.execute(declaration)
            conn.execute(
                "CREATE INDEX idx_external_records_asset ON external_records(asset_id)"
            )
        conn.commit()
        before = dump(conn)
        with pytest.raises(RuntimeError):
            schema.ensure_release_schema(conn, backup_dir=tmp_path / "backups")
        assert dump(conn) == before
        assert not (tmp_path / "backups").exists()


def test_intermediate_development_revision_requires_final_dev_upgrade(tmp_path):
    with closing(sqlite3.connect(tmp_path / "history.sqlite3")) as conn:
        create_final_dev(conn, revision=1)
        before = dump(conn)
        with pytest.raises(RuntimeError, match="1.1.0-dev.2"):
            schema.ensure_release_schema(conn, backup_dir=tmp_path / "backups")
        assert dump(conn) == before
        assert not (tmp_path / "backups").exists()


@pytest.mark.parametrize("existing", ["empty", "v1", "final_dev"])
def test_failed_upgrade_rolls_back_tables_marker_and_version(
    tmp_path, monkeypatch, existing
):
    with closing(sqlite3.connect(tmp_path / "history.sqlite3")) as conn:
        if existing == "v1":
            create_v1(conn)
        elif existing == "final_dev":
            create_final_dev(conn)
            populate_external(conn)
        before = dump(conn)

        def fail(target):
            target.execute("PRAGMA user_version=2")
            raise RuntimeError("simulated disk error")

        monkeypatch.setattr(schema, "_stamp_release", fail)
        with pytest.raises(RuntimeError, match="disk error"):
            schema.ensure_release_schema(conn, backup_dir=tmp_path / "backups")
        assert dump(conn) == before
        assert not conn.in_transaction
        backups = list((tmp_path / "backups").glob("*.sqlite3"))
        assert len(backups) == int(existing != "empty")
        if backups:
            with closing(sqlite3.connect(backups[0])) as backup:
                assert dump(backup) == before


@pytest.mark.parametrize("final_dev", [False, True])
def test_backup_failure_blocks_upgrade(tmp_path, monkeypatch, final_dev):
    with closing(sqlite3.connect(tmp_path / "history.sqlite3")) as conn:
        (create_final_dev if final_dev else create_v1)(conn)
        before = dump(conn)

        def fail(*args):
            raise OSError("full disk")

        monkeypatch.setattr(schema, "_backup_database", fail)
        with pytest.raises(OSError, match="full disk"):
            schema.ensure_release_schema(conn, backup_dir=tmp_path / "backups")
        assert dump(conn) == before


@pytest.mark.parametrize("other_connection", [False, True])
def test_business_write_after_backup_aborts_without_schema_changes(
    tmp_path, monkeypatch, other_connection
):
    database = tmp_path / "history.sqlite3"
    with closing(sqlite3.connect(database)) as conn:
        create_final_dev(conn)
        populate_external(conn)
        original_backup = schema._backup_database

        def concurrent_write(target, directory):
            backup = original_backup(target, directory)
            if other_connection:
                with closing(sqlite3.connect(database)) as other:
                    other.execute("UPDATE generations SET final_prompt='concurrent'")
                    other.commit()
            else:
                target.execute("UPDATE generations SET final_prompt='concurrent'")
                target.commit()
            return backup

        monkeypatch.setattr(schema, "_backup_database", concurrent_write)
        with pytest.raises(RuntimeError, match="备份后发生变化"):
            schema.ensure_release_schema(conn, backup_dir=tmp_path / "backups")
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert conn.execute("SELECT * FROM schema_meta").fetchall() == [(1, 2, 2)]
        assert conn.execute("SELECT final_prompt FROM generations").fetchone() == (
            "concurrent",
        )
        backups = list((tmp_path / "backups").glob("*.sqlite3"))
        assert len(backups) == 1
        with closing(sqlite3.connect(backups[0])) as backup:
            assert backup.execute(
                "SELECT final_prompt FROM generations"
            ).fetchone() == ("p",)
