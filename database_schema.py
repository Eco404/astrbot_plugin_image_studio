"""Release baselines and disposable development revisions for gallery storage."""

from __future__ import annotations

import sqlite3

RELEASE_VERSION = 0
TARGET_VERSION = 1
DEV_REVISION = 2
DATABASE_VERSION = "1-dev.2"


def check_database_version(conn: sqlite3.Connection) -> None:
    """Reject unknown layouts before executing any schema or data writes."""

    release = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if release != RELEASE_VERSION:
        raise RuntimeError(f"不支持的图库数据库正式版本：{release}")
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_meta'"
    ).fetchone()
    if exists:
        rows = conn.execute(
            "SELECT target_version, dev_revision FROM schema_meta"
        ).fetchall()
        if len(rows) != 1 or tuple(rows[0]) not in {
            (TARGET_VERSION, 1),
            (TARGET_VERSION, DEV_REVISION),
        }:
            raise RuntimeError("不支持的图库数据库开发修订，请使用对应开发版本")


def finish_development_schema(conn: sqlite3.Connection) -> None:
    """Stamp the version only after every schema change in the transaction succeeds."""

    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_meta ("
        "id INTEGER PRIMARY KEY CHECK(id = 1), target_version INTEGER NOT NULL, "
        "dev_revision INTEGER NOT NULL)"
    )
    conn.execute(
        "INSERT INTO schema_meta (id, target_version, dev_revision) VALUES (1, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET target_version=excluded.target_version, "
        "dev_revision=excluded.dev_revision",
        (TARGET_VERSION, DEV_REVISION),
    )
    conn.execute(f"PRAGMA user_version = {RELEASE_VERSION}")
