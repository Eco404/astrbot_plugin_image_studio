"""The published gallery schema and transaction boundary for release upgrades."""

from __future__ import annotations

import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

RELEASE_VERSION = 1
DATABASE_VERSION = "2-dev.2"
DEVELOPMENT_TARGET = 2
DEVELOPMENT_REVISION = 2

SCHEMA_STATEMENTS = (
    """CREATE TABLE generations (
        id TEXT PRIMARY KEY,
        created_at REAL NOT NULL,
        source TEXT NOT NULL,
        status TEXT NOT NULL,
        mode TEXT NOT NULL,
        provider_id TEXT NOT NULL,
        provider_name TEXT NOT NULL,
        provider_kind TEXT NOT NULL,
        model TEXT NOT NULL,
        original_prompt TEXT NOT NULL,
        final_prompt TEXT NOT NULL,
        parameters_json TEXT NOT NULL,
        elapsed_ms INTEGER NOT NULL,
        error_message TEXT NOT NULL DEFAULT '',
        context_type TEXT NOT NULL DEFAULT '',
        platform_name TEXT NOT NULL DEFAULT '',
        platform_id TEXT NOT NULL DEFAULT '',
        group_id TEXT NOT NULL DEFAULT '',
        group_name TEXT NOT NULL DEFAULT '',
        user_id TEXT NOT NULL DEFAULT '',
        user_name TEXT NOT NULL DEFAULT '',
        is_favorite INTEGER NOT NULL DEFAULT 0,
        cleanup_protected_until REAL NOT NULL DEFAULT 0,
        generation_engine TEXT NOT NULL DEFAULT 'unknown',
        generated_at REAL,
        supplemental_json TEXT NOT NULL DEFAULT '{}',
        import_key TEXT,
        search_text TEXT NOT NULL DEFAULT ''
    )""",
    """CREATE TABLE image_assets (
        id TEXT PRIMARY KEY,
        path TEXT NOT NULL UNIQUE,
        mime_type TEXT NOT NULL,
        size_bytes INTEGER NOT NULL,
        width INTEGER NOT NULL DEFAULT 0,
        height INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        file_state TEXT NOT NULL DEFAULT 'available'
    )""",
    """CREATE TABLE image_thumbnails (
        asset_id TEXT PRIMARY KEY REFERENCES image_assets(id) ON DELETE CASCADE,
        path TEXT NOT NULL UNIQUE,
        mime_type TEXT NOT NULL,
        size_bytes INTEGER NOT NULL,
        max_edge INTEGER NOT NULL DEFAULT 0,
        quality INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE generation_images (
        id TEXT PRIMARY KEY,
        generation_id TEXT NOT NULL REFERENCES generations(id) ON DELETE CASCADE,
        ordinal INTEGER NOT NULL,
        asset_id TEXT NOT NULL REFERENCES image_assets(id),
        supplemental_json TEXT NOT NULL DEFAULT '{}'
    )""",
    """CREATE TABLE generation_references (
        id TEXT PRIMARY KEY,
        generation_id TEXT NOT NULL REFERENCES generations(id) ON DELETE CASCADE,
        ordinal INTEGER NOT NULL,
        filename TEXT NOT NULL,
        mime_type TEXT NOT NULL,
        size_bytes INTEGER NOT NULL,
        available INTEGER NOT NULL DEFAULT 1,
        deleted_at REAL,
        asset_id TEXT REFERENCES image_assets(id)
    )""",
    """CREATE TABLE agent_asset_leases (
        id TEXT PRIMARY KEY,
        asset_id TEXT NOT NULL REFERENCES image_assets(id) ON DELETE CASCADE,
        scope_id TEXT NOT NULL,
        created_at REAL NOT NULL,
        last_accessed_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        hard_expires_at REAL NOT NULL,
        UNIQUE(scope_id, asset_id)
    )""",
    """CREATE TABLE image_metadata (
        asset_id TEXT PRIMARY KEY REFERENCES image_assets(id) ON DELETE CASCADE,
        format TEXT NOT NULL,
        parser_version INTEGER NOT NULL,
        metadata_json TEXT NOT NULL
    )""",
    """CREATE TABLE import_batches (
        id TEXT PRIMARY KEY,
        fingerprint TEXT NOT NULL,
        result_json TEXT NOT NULL,
        expires_at REAL NOT NULL
    )""",
    "CREATE INDEX idx_import_batches_expiry ON import_batches(expires_at)",
    "CREATE INDEX idx_generations_created_at ON generations(created_at DESC)",
    "CREATE INDEX idx_generations_provider ON generations(provider_id)",
    "CREATE INDEX idx_generation_images_generation ON generation_images(generation_id)",
    "CREATE INDEX idx_generation_references_generation ON generation_references(generation_id)",
    "CREATE INDEX idx_generation_images_asset ON generation_images(asset_id)",
    "CREATE INDEX idx_generation_references_asset ON generation_references(asset_id)",
    "CREATE INDEX idx_agent_asset_leases_asset ON agent_asset_leases(asset_id)",
    "CREATE INDEX idx_agent_asset_leases_expiry ON agent_asset_leases(expires_at)",
    "CREATE UNIQUE INDEX idx_generations_import_key ON generations(import_key) WHERE import_key IS NOT NULL",
    "CREATE INDEX idx_generations_retention ON generations(source, is_favorite, cleanup_protected_until, created_at)",
    "CREATE INDEX idx_generations_engine ON generations(generation_engine)",
)

_TABLES = (
    "generations",
    "image_assets",
    "image_thumbnails",
    "generation_images",
    "generation_references",
    "agent_asset_leases",
    "image_metadata",
    "import_batches",
)

# Keep the published v1 definition above immutable. At release time this single
# additive migration becomes v1 -> v2; intermediate dev revisions are not releases.
DEVELOPMENT_V1_STATEMENTS = (
    """CREATE TABLE external_sources (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        root_path TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 0,
        status_json TEXT NOT NULL DEFAULT '{}'
    )""",
    """CREATE TABLE external_records (
        generation_id TEXT PRIMARY KEY REFERENCES generations(id) ON DELETE CASCADE,
        source_id TEXT NOT NULL REFERENCES external_sources(id),
        relative_path TEXT NOT NULL,
        asset_id TEXT NOT NULL REFERENCES image_assets(id),
        fingerprint TEXT NOT NULL,
        sidecar_fingerprint TEXT NOT NULL,
        size_bytes INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        available INTEGER NOT NULL DEFAULT 1,
        UNIQUE(source_id, relative_path)
    )""",
    "CREATE INDEX idx_external_records_asset ON external_records(asset_id)",
    """CREATE TABLE schema_meta (
        id INTEGER PRIMARY KEY CHECK(id = 1),
        target_version INTEGER NOT NULL,
        dev_revision INTEGER NOT NULL
    )""",
)
DEVELOPMENT_V2_UPGRADE = (
    "ALTER TABLE external_sources ADD COLUMN type TEXT NOT NULL DEFAULT 'nai'",
    "ALTER TABLE external_sources ADD COLUMN recursive INTEGER NOT NULL DEFAULT 0",
    """ALTER TABLE external_sources ADD COLUMN permissions_json TEXT NOT NULL DEFAULT '{"favorite":true,"delete":true,"download":true,"reference":true}'""",
    "ALTER TABLE external_records ADD COLUMN time_source TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE external_records ADD COLUMN metadata_created_at REAL",
    "ALTER TABLE external_records ADD COLUMN file_birthtime REAL",
    "ALTER TABLE external_records ADD COLUMN time_policy_version INTEGER NOT NULL DEFAULT 0",
)
DEVELOPMENT_STATEMENTS = (*DEVELOPMENT_V1_STATEMENTS, *DEVELOPMENT_V2_UPGRADE)
_DEVELOPMENT_TABLES = ("external_sources", "external_records", "schema_meta")


def _identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _table_shape(conn: sqlite3.Connection, table: str) -> tuple[Any, ...]:
    columns = {
        row[1]: tuple(row[2:])
        for row in conn.execute(f"PRAGMA table_info({_identifier(table)})")
    }
    foreign_keys = sorted(
        tuple(row[2:])
        for row in conn.execute(f"PRAGMA foreign_key_list({_identifier(table)})")
    )
    indexes = []
    for row in conn.execute(f"PRAGMA index_list({_identifier(table)})"):
        columns_in_index = tuple(
            tuple(item[1:])
            for item in conn.execute(f"PRAGMA index_xinfo({_identifier(row[1])})")
        )
        # PRAGMA exposes the partial flag, but not the predicate guarding uniqueness.
        partial_sql = ""
        if row[4]:
            partial_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
                (row[1],),
            ).fetchone()[0]
        indexes.append((row[2], row[3], row[4], columns_in_index, partial_sql))
    return columns, foreign_keys, sorted(indexes)


@lru_cache(maxsize=1)
def _release_shapes() -> dict[str, tuple[Any, ...]]:
    with closing(sqlite3.connect(":memory:")) as conn:
        for statement in SCHEMA_STATEMENTS:
            conn.execute(statement)
        return {table: _table_shape(conn, table) for table in _TABLES}


def _validate_release_layout(conn: sqlite3.Connection) -> None:
    for table, expected in _release_shapes().items():
        if _table_shape(conn, table) != expected:
            raise RuntimeError(f"图库数据库结构与正式基线不符：{table}；未执行升级")


def _database_kind(conn: sqlite3.Connection) -> str:
    release = int(conn.execute("PRAGMA user_version").fetchone()[0])
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if release not in {0, RELEASE_VERSION}:
        raise RuntimeError(f"不支持的图库数据库正式版本：{release}；请使用对应插件版本")
    if release == RELEASE_VERSION:
        if "schema_meta" in tables:
            raise RuntimeError("数据库包含未发布的开发修订，请使用对应开发版本")
        _validate_release_layout(conn)
        return "release"
    if not conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone():
        return "empty"
    if "schema_meta" not in tables:
        raise RuntimeError(
            "图库数据库没有受支持的版本信息；请先用末版开发版升级后再转换"
        )
    expected_meta = {
        "id": ("INTEGER", 0, None, 1),
        "target_version": ("INTEGER", 1, None, 0),
        "dev_revision": ("INTEGER", 1, None, 0),
    }
    if _table_shape(conn, "schema_meta")[0] != expected_meta:
        raise RuntimeError("图库数据库开发版本信息结构无效；未执行升级")
    try:
        rows = conn.execute(
            "SELECT id, target_version, dev_revision FROM schema_meta"
        ).fetchall()
    except sqlite3.Error as exc:
        raise RuntimeError("图库数据库开发版本信息无效；未执行升级") from exc
    # This is a one-step promotion of the final unpublished layout, not a dev migration chain.
    if len(rows) != 1 or tuple(rows[0]) != (1, 1, 3):
        raise RuntimeError("不支持此图库数据库开发修订；请先用末版开发版升级后再转换")
    _validate_release_layout(conn)
    return "promotion"


def _backup_database(conn: sqlite3.Connection, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    with tempfile.NamedTemporaryFile(
        prefix=f"history-pre-v{RELEASE_VERSION}-{timestamp}-",
        suffix=".sqlite3",
        dir=backup_dir,
        delete=False,
    ) as temporary:
        path = Path(temporary.name)
    try:
        with closing(sqlite3.connect(path)) as backup:
            conn.backup(backup)
            if backup.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("图库数据库备份校验失败；未执行升级")
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def _stamp_release(conn: sqlite3.Connection) -> None:
    conn.execute(f"PRAGMA user_version = {RELEASE_VERSION}")


def ensure_release_schema(conn: sqlite3.Connection, *, backup_dir: Path) -> Path | None:
    """Create v1 or promote the final dev layout; published v1 is validation-only.

    Each future published schema upgrade belongs at this transaction boundary,
    with one consolidated migration and backup per released version.
    """

    if conn.in_transaction:
        raise RuntimeError("图库数据库升级必须在独立事务中执行")
    kind = _database_kind(conn)
    if kind == "release":
        return None
    backup = _backup_database(conn, backup_dir) if kind == "promotion" else None
    try:
        conn.execute("BEGIN IMMEDIATE")
        if _database_kind(conn) != kind:
            raise RuntimeError("图库数据库在备份后发生变化，请重新启动插件重试")
        if kind == "empty":
            for statement in SCHEMA_STATEMENTS:
                conn.execute(statement)
            try:
                conn.execute(
                    "CREATE VIRTUAL TABLE generation_search USING fts5("
                    "generation_id UNINDEXED, original_prompt, final_prompt, provider_name, model)"
                )
            except sqlite3.OperationalError as exc:
                if "no such module: fts5" not in str(exc).lower():
                    raise
            _validate_release_layout(conn)
        else:
            conn.execute("DROP TABLE schema_meta")
        _stamp_release(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return backup


@lru_cache(maxsize=2)
def _development_shapes(
    revision: int = DEVELOPMENT_REVISION,
) -> dict[str, tuple[Any, ...]]:
    with closing(sqlite3.connect(":memory:")) as conn:
        statements = (
            DEVELOPMENT_V1_STATEMENTS if revision == 1 else DEVELOPMENT_STATEMENTS
        )
        for statement in (*SCHEMA_STATEMENTS, *statements):
            conn.execute(statement)
        return {table: _table_shape(conn, table) for table in _DEVELOPMENT_TABLES}


def _development_kind(conn: sqlite3.Connection) -> str:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version not in {0, RELEASE_VERSION}:
        raise RuntimeError(f"不支持的图库数据库正式版本：{version}；请使用对应插件版本")
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if version == RELEASE_VERSION and "schema_meta" in tables:
        if _table_shape(conn, "schema_meta") != _development_shapes()["schema_meta"]:
            raise RuntimeError("图库数据库开发结构不符：schema_meta；未执行升级")
        rows = conn.execute(
            "SELECT id, target_version, dev_revision FROM schema_meta"
        ).fetchall()
        if (
            len(rows) != 1
            or tuple(rows[0][:2]) != (1, DEVELOPMENT_TARGET)
            or rows[0][2] not in {1, DEVELOPMENT_REVISION}
        ):
            raise RuntimeError("不支持的图库数据库开发修订；请使用对应开发版本")
        revision = int(rows[0][2])
        for table, expected in _development_shapes(revision).items():
            if _table_shape(conn, table) != expected:
                raise RuntimeError(f"图库数据库开发结构不符：{table}；未执行升级")
        _validate_release_layout(conn)
        return "development_upgrade" if revision == 1 else "development"
    if set(_DEVELOPMENT_TABLES[:2]) & tables:
        raise RuntimeError("图库数据库存在未登记的外部图库结构；未执行升级")
    return _database_kind(conn)


def _stamp_development(conn: sqlite3.Connection) -> None:
    conn.execute(f"PRAGMA user_version = {RELEASE_VERSION}")
    conn.execute(
        "INSERT INTO schema_meta(id, target_version, dev_revision) VALUES (1, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET target_version=excluded.target_version,dev_revision=excluded.dev_revision",
        (DEVELOPMENT_TARGET, DEVELOPMENT_REVISION),
    )


def ensure_development_schema(
    conn: sqlite3.Connection, *, backup_dir: Path
) -> Path | None:
    """Create/upgrade to the supported dev layout with a single backup transaction."""
    if conn.in_transaction:
        raise RuntimeError("图库数据库升级必须在独立事务中执行")
    kind = _development_kind(conn)
    if kind == "development":
        return None
    backup = _backup_database(conn, backup_dir) if kind != "empty" else None
    try:
        conn.execute("BEGIN IMMEDIATE")
        if _development_kind(conn) != kind:
            raise RuntimeError("图库数据库在备份后发生变化，请重新启动插件重试")
        if kind == "empty":
            for statement in SCHEMA_STATEMENTS:
                conn.execute(statement)
            try:
                conn.execute(
                    "CREATE VIRTUAL TABLE generation_search USING fts5("
                    "generation_id UNINDEXED, original_prompt, final_prompt, provider_name, model)"
                )
            except sqlite3.OperationalError as exc:
                if "no such module: fts5" not in str(exc).lower():
                    raise
        elif kind == "promotion":
            conn.execute("DROP TABLE schema_meta")
        statements = (
            DEVELOPMENT_V2_UPGRADE
            if kind == "development_upgrade"
            else DEVELOPMENT_STATEMENTS
        )
        for statement in statements:
            conn.execute(statement)
        _stamp_development(conn)
        _development_kind(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return backup
