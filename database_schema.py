"""The published gallery schema and transaction boundary for release upgrades."""

from __future__ import annotations

import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

RELEASE_VERSION = 2
DATABASE_VERSION = "2"

# Published v1 is immutable; future releases retain this upgrade starting point.
V1_SCHEMA_STATEMENTS = (
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

_V1_TABLES = (
    "generations",
    "image_assets",
    "image_thumbnails",
    "generation_images",
    "generation_references",
    "agent_asset_leases",
    "image_metadata",
    "import_batches",
)

# One published v1 -> v2 migration, also used when creating a fresh v2 database.
# Intermediate development CREATE/ALTER chains belong only in upgrade fixtures.
V2_MIGRATION_STATEMENTS = (
    """CREATE TABLE external_sources (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        type TEXT NOT NULL DEFAULT 'nai',
        root_path TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 0,
        recursive INTEGER NOT NULL DEFAULT 0,
        permissions_json TEXT NOT NULL DEFAULT '{"favorite":true,"delete":true,"download":true,"reference":true}',
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
        time_source TEXT NOT NULL DEFAULT '',
        metadata_created_at REAL,
        file_birthtime REAL,
        time_policy_version INTEGER NOT NULL DEFAULT 0,
        UNIQUE(source_id, relative_path)
    )""",
    "CREATE INDEX idx_external_records_asset ON external_records(asset_id)",
)
SCHEMA_STATEMENTS = (*V1_SCHEMA_STATEMENTS, *V2_MIGRATION_STATEMENTS)
_TABLES = (*_V1_TABLES, "external_sources", "external_records")
_DEVELOPMENT_META_STATEMENT = """CREATE TABLE schema_meta (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    target_version INTEGER NOT NULL,
    dev_revision INTEGER NOT NULL
)"""


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
        # Development ALTERs appended columns. Compare logical index columns, not
        # their physical column IDs, against the final release CREATE statements.
        columns_in_index = tuple(
            (
                ("column", item[2]) if item[1] >= 0 else ("special", item[1]),
                *item[3:],
            )
            for item in conn.execute(f"PRAGMA index_xinfo({_identifier(row[1])})")
        )
        partial_sql = ""
        if row[4]:
            partial_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
                (row[1],),
            ).fetchone()[0]
        indexes.append((row[2], row[3], row[4], columns_in_index, partial_sql))
    return columns, foreign_keys, sorted(indexes)


@lru_cache(maxsize=2)
def _release_shapes(version: int = RELEASE_VERSION) -> dict[str, tuple[Any, ...]]:
    with closing(sqlite3.connect(":memory:")) as conn:
        statements = V1_SCHEMA_STATEMENTS if version == 1 else SCHEMA_STATEMENTS
        tables = _V1_TABLES if version == 1 else _TABLES
        for statement in statements:
            conn.execute(statement)
        return {table: _table_shape(conn, table) for table in tables}


def _validate_release_layout(
    conn: sqlite3.Connection, version: int = RELEASE_VERSION
) -> None:
    for table, expected in _release_shapes(version).items():
        if _table_shape(conn, table) != expected:
            raise RuntimeError(f"图库数据库结构与正式基线不符：{table}；未执行升级")
    if version == 1 and any(
        conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone()
        for table in ("external_sources", "external_records")
    ):
        raise RuntimeError("图库数据库存在未登记的外部图库结构；未执行升级")


@lru_cache(maxsize=1)
def _development_meta_shape() -> tuple[Any, ...]:
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute(_DEVELOPMENT_META_STATEMENT)
        return _table_shape(conn, "schema_meta")


def _development_marker(conn: sqlite3.Connection) -> tuple[int, int, int]:
    if _table_shape(conn, "schema_meta") != _development_meta_shape():
        raise RuntimeError("图库数据库开发版本信息结构无效；未执行升级")
    declaration = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='schema_meta'"
    ).fetchone()[0]
    if "CHECK(ID=1)" not in "".join(declaration.upper().split()):
        raise RuntimeError("图库数据库开发版本信息约束无效；未执行升级")
    rows = conn.execute(
        "SELECT id, target_version, dev_revision FROM schema_meta"
    ).fetchall()
    if len(rows) != 1:
        raise RuntimeError("图库数据库开发版本信息无效；未执行升级")
    return tuple(rows[0])


def _database_kind(conn: sqlite3.Connection) -> str:
    release = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if release not in {0, 1, RELEASE_VERSION}:
        raise RuntimeError(f"不支持的图库数据库正式版本：{release}；请使用对应插件版本")
    has_marker = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_meta'"
    ).fetchone()
    if has_marker:
        marker = _development_marker(conn)
        if release == 0 and marker == (1, 1, 3):
            _validate_release_layout(conn, 1)
            return "promotion_v1"
        if release == 1 and marker == (1, 2, 2):
            _validate_release_layout(conn)
            return "promotion_v2"
        raise RuntimeError(
            "不支持此图库数据库开发修订；1-dev 系列请先用末版开发版升级，"
            "2-dev 系列请先升级到 1.1.0-dev.2 的最终数据库 2-dev.2 后再转换"
        )
    if release in {1, RELEASE_VERSION}:
        _validate_release_layout(conn, release)
        return "release" if release == RELEASE_VERSION else "upgrade_v1"
    if not conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone():
        return "empty"
    raise RuntimeError("图库数据库没有受支持的版本信息；请先用末版开发版升级后再转换")


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


def _change_token(conn: sqlite3.Connection) -> tuple[int, int, int]:
    return (
        conn.execute("PRAGMA data_version").fetchone()[0],
        conn.execute("PRAGMA schema_version").fetchone()[0],
        conn.total_changes,
    )


def _create_search_index(conn: sqlite3.Connection) -> None:
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE generation_search USING fts5("
            "generation_id UNINDEXED, original_prompt, final_prompt, provider_name, model)"
        )
    except sqlite3.OperationalError as exc:
        if "no such module: fts5" not in str(exc).lower():
            raise


def ensure_release_schema(conn: sqlite3.Connection, *, backup_dir: Path) -> Path | None:
    """Create v2, upgrade published v1 once, or promote a supported final dev layout.

    Backups precede every existing-database upgrade. Version/structure and commit
    tokens are checked again under the write lock, keeping backup and migration
    tied to the same state. Published v2 startup only validates the schema.
    """
    if conn.in_transaction:
        raise RuntimeError("图库数据库升级必须在独立事务中执行")
    initial_token = _change_token(conn)
    kind = _database_kind(conn)
    if kind == "release":
        return None
    backup = _backup_database(conn, backup_dir) if kind != "empty" else None
    try:
        conn.execute("BEGIN IMMEDIATE")
        if _database_kind(conn) != kind or _change_token(conn) != initial_token:
            raise RuntimeError("图库数据库在备份后发生变化，请重新启动插件重试")
        if kind == "empty":
            for statement in SCHEMA_STATEMENTS:
                conn.execute(statement)
            _create_search_index(conn)
        else:
            if kind in {"promotion_v1", "promotion_v2"}:
                conn.execute("DROP TABLE schema_meta")
            if kind in {"promotion_v1", "upgrade_v1"}:
                for statement in V2_MIGRATION_STATEMENTS:
                    conn.execute(statement)
        _validate_release_layout(conn)
        _stamp_release(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return backup
