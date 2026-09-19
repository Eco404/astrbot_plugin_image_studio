"""Frozen SQL from published baselines and accepted final development builds.

Independent of current schema definitions so upgrades cannot hide drift.
"""

V1_STATEMENTS = (
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


def create_v1(conn):
    for statement in V1_STATEMENTS:
        conn.execute(statement)
    conn.execute("PRAGMA user_version = 1")
    conn.commit()


def create_final_dev(conn, revision=2):
    create_v1(conn)
    for statement in DEVELOPMENT_V1_STATEMENTS:
        conn.execute(statement)
    if revision == 2:
        for statement in DEVELOPMENT_V2_UPGRADE:
            conn.execute(statement)
    conn.execute("INSERT INTO schema_meta VALUES (1, 2, ?)", (revision,))
    conn.commit()


# Frozen published v2 and final 1.3 development SQL; never import runtime constants.
V2_PUBLISHED_MIGRATION = (
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
V3_FINAL_DEVELOPMENT_STATEMENTS = (
    """CREATE TABLE comfy_workflow_revisions (
        id TEXT PRIMARY KEY,
        fingerprint TEXT NOT NULL UNIQUE,
        config_json TEXT NOT NULL,
        created_at REAL NOT NULL
    )""",
    """CREATE TABLE comfy_jobs (
        id TEXT PRIMARY KEY,
        remote_id TEXT NOT NULL DEFAULT '',
        provider_id TEXT NOT NULL,
        model_id TEXT NOT NULL,
        revision_id TEXT NOT NULL REFERENCES comfy_workflow_revisions(id),
        status TEXT NOT NULL,
        request_json TEXT NOT NULL,
        input_refs_json TEXT NOT NULL DEFAULT '[]',
        output_refs_json TEXT NOT NULL DEFAULT '[]',
        result_json TEXT NOT NULL DEFAULT '{}',
        error TEXT NOT NULL DEFAULT '',
        generation_id TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        finished_at REAL
    )""",
    "CREATE INDEX idx_comfy_jobs_status ON comfy_jobs(status, created_at)",
    "CREATE INDEX idx_comfy_jobs_provider ON comfy_jobs(provider_id, created_at)",
    "CREATE UNIQUE INDEX idx_comfy_jobs_remote ON comfy_jobs(provider_id, remote_id) WHERE remote_id != ''",
)
DEVELOPMENT_META_STATEMENT = """CREATE TABLE schema_meta (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    target_version INTEGER NOT NULL,
    dev_revision INTEGER NOT NULL
)"""


def create_v2(conn):
    create_v1(conn)
    for statement in V2_PUBLISHED_MIGRATION:
        conn.execute(statement)
    conn.execute("PRAGMA user_version = 2")
    conn.commit()


def create_v3_dev(conn):
    create_v2(conn)
    for statement in V3_FINAL_DEVELOPMENT_STATEMENTS:
        conn.execute(statement)
    conn.execute(DEVELOPMENT_META_STATEMENT)
    conn.execute("INSERT INTO schema_meta VALUES (1, 3, 1)")
    conn.commit()


# Frozen from 6153f83: final 1.4 development layout, independent of runtime SQL.
V4_FINAL_DEVELOPMENT_STATEMENTS = (
    """CREATE TABLE storage_payloads (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        codec TEXT NOT NULL,
        data BLOB NOT NULL,
        size_bytes INTEGER NOT NULL,
        created_at REAL NOT NULL
    )""",
    """CREATE TABLE storage_payload_refs (
        owner_table TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        slot TEXT NOT NULL,
        payload_id TEXT NOT NULL REFERENCES storage_payloads(id),
        PRIMARY KEY(owner_table,owner_id,slot)
    )""",
    "CREATE INDEX idx_storage_payload_refs_payload ON storage_payload_refs(payload_id)",
    "ALTER TABLE comfy_jobs ADD COLUMN parent_job_id TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE comfy_jobs ADD COLUMN queue_dismissed INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE comfy_jobs ADD COLUMN temporary INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE comfy_jobs ADD COLUMN model_name TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE comfy_jobs ADD COLUMN archive_state TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE comfy_jobs ADD COLUMN request_fingerprint TEXT NOT NULL DEFAULT ''",
    "CREATE INDEX idx_comfy_jobs_parent ON comfy_jobs(parent_job_id)",
    "CREATE INDEX idx_comfy_jobs_queue ON comfy_jobs(parent_job_id,queue_dismissed,created_at DESC)",
    "CREATE INDEX idx_comfy_jobs_expiry ON comfy_jobs(status,finished_at)",
    "CREATE INDEX idx_comfy_jobs_revision ON comfy_jobs(revision_id)",
    "CREATE INDEX idx_comfy_jobs_generation ON comfy_jobs(generation_id)",
    "ALTER TABLE generation_images ADD COLUMN generated_at REAL",
    "ALTER TABLE generation_images ADD COLUMN model TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE generation_images ADD COLUMN mode TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE generations ADD COLUMN latest_content_at REAL",
    "CREATE INDEX idx_generations_latest_content ON generations(COALESCE(latest_content_at,created_at) DESC,created_at DESC,id DESC)",
    "CREATE TRIGGER storage_refs_delete_comfy_workflow_revisions AFTER DELETE ON comfy_workflow_revisions BEGIN DELETE FROM storage_payload_refs WHERE owner_table='comfy_workflow_revisions' AND owner_id=OLD.id; END",
    "CREATE TRIGGER storage_refs_delete_comfy_jobs AFTER DELETE ON comfy_jobs BEGIN DELETE FROM storage_payload_refs WHERE owner_table='comfy_jobs' AND owner_id=OLD.id; END",
    "CREATE TRIGGER storage_refs_delete_image_metadata AFTER DELETE ON image_metadata BEGIN DELETE FROM storage_payload_refs WHERE owner_table='image_metadata' AND owner_id=OLD.asset_id; END",
    "CREATE TRIGGER storage_refs_delete_generation_images AFTER DELETE ON generation_images BEGIN DELETE FROM storage_payload_refs WHERE owner_table='generation_images' AND owner_id=OLD.id; END",
    "CREATE TRIGGER storage_refs_delete_generations AFTER DELETE ON generations BEGIN DELETE FROM storage_payload_refs WHERE owner_table='generations' AND owner_id=OLD.id; END",
    "ALTER TABLE generations ADD COLUMN title TEXT NOT NULL DEFAULT ''",
)


def create_v4_dev(conn, revision=2):
    create_v3_dev(conn)
    for statement in V4_FINAL_DEVELOPMENT_STATEMENTS:
        if revision == 1 and statement.startswith(
            "ALTER TABLE generations ADD COLUMN title"
        ):
            continue
        conn.execute(statement)
    conn.execute("PRAGMA user_version = 3")
    conn.execute(
        "UPDATE schema_meta SET target_version=4, dev_revision=? WHERE id=1",
        (revision,),
    )
    conn.commit()
