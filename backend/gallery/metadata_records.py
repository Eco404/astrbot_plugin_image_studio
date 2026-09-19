"""Metadata cache reads, parser-version backfill, and derived projection updates."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..media.files import (
    _is_within,
)
from ..metadata.node_rules import get_rules, load_rules, use_rules
from .context import GalleryContext
from .projection import (
    _load_json,
)
from .storage import decode_metadata, encode_metadata


@dataclass(frozen=True)
class MetadataServices:
    """Explicit cross-repository operations; connections stay with their caller."""

    refresh_import_projection: Callable[..., Any]
    refresh_search: Callable[..., Any]


class MetadataRecords:
    def __init__(self, context: GalleryContext, services: MetadataServices) -> None:
        self.context = context
        self.services = services

    def metadata_for_asset(
        self, asset_id: str, data: bytes, *, strict: bool = False, rules=None
    ) -> dict[str, Any]:
        with use_rules(
            rules if rules is not None else load_rules(self.context.data_dir)
        ):
            return self._metadata_for_asset(asset_id, data, strict=strict)

    def _metadata_for_asset(
        self, asset_id: str, data: bytes, *, strict: bool = False
    ) -> dict[str, Any]:
        from ..metadata.parser import PARSER_VERSION, parse_image_metadata

        with self.context.connect() as conn:
            row = conn.execute(
                "SELECT metadata_json FROM image_metadata WHERE asset_id = ?",
                (asset_id,),
            ).fetchone()
            if row is not None:
                cached = _load_json(str(row["metadata_json"]))
                if int(cached.get("parser_version", 0)) >= PARSER_VERSION and (
                    cached.get("format") != "comfyui"
                    or cached.get("rules_fingerprint") == get_rules().fingerprint
                ):
                    return decode_metadata(conn, cached)
        try:
            return parse_image_metadata(data)
        except ValueError as exc:
            if strict:
                raise
            return {
                "format": "unknown",
                "parser_version": 0,
                "raw": {},
                "normalized": {},
                "warnings": [str(exc)],
            }

    @staticmethod
    def save_metadata(
        conn: sqlite3.Connection, metadata: dict[str, dict[str, Any]]
    ) -> None:
        for asset_id, item in metadata.items():
            old = conn.execute(
                "SELECT parser_version,json_extract(metadata_json,'$.rules_fingerprint') "
                "FROM image_metadata WHERE asset_id=?",
                (asset_id,),
            ).fetchone()
            version = int(item.get("parser_version", 1))
            if old is not None and (
                old[0] > version
                or old[0] == version
                and (old[1] or "") == (item.get("rules_fingerprint") or "")
            ):
                continue
            compact = encode_metadata(conn, asset_id, item)
            conn.execute(
                "INSERT INTO image_metadata (asset_id, format, parser_version, metadata_json) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(asset_id) DO UPDATE SET "
                "format=excluded.format, parser_version=excluded.parser_version, "
                "metadata_json=excluded.metadata_json "
                "WHERE image_metadata.parser_version < excluded.parser_version OR "
                "(image_metadata.parser_version = excluded.parser_version AND "
                "COALESCE(json_extract(image_metadata.metadata_json, '$.rules_fingerprint'), '') "
                "!= COALESCE(json_extract(excluded.metadata_json, '$.rules_fingerprint'), ''))",
                (
                    asset_id,
                    str(item.get("format") or "unknown"),
                    version,
                    json.dumps(compact, ensure_ascii=False, separators=(",", ":")),
                ),
            )

    def backfill_metadata(self, *, rules=None) -> None:
        """Parse old asset metadata outside the schema migration transaction."""

        with use_rules(
            rules if rules is not None else load_rules(self.context.data_dir)
        ):
            self._backfill_metadata()

    def _backfill_metadata(self) -> None:

        from ..metadata.parser import PARSER_VERSION

        with self.context.connect() as conn:
            rows = conn.execute(
                "SELECT a.id, a.path FROM image_assets a LEFT JOIN image_metadata m "
                "ON m.asset_id = a.id WHERE m.asset_id IS NULL OR m.parser_version < ? "
                "OR (m.format = 'comfyui' AND "
                "COALESCE(json_extract(m.metadata_json, '$.rules_fingerprint'), '') != ?)",
                (PARSER_VERSION, get_rules().fingerprint),
            ).fetchall()
        for row in rows:
            path = self.context.data_dir / str(row["path"])
            if not _is_within(path, self.context.assets_dir) or not path.is_file():
                continue
            try:
                metadata = self._metadata_for_asset(str(row["id"]), path.read_bytes())
            except (OSError, ValueError):
                continue
            with self.context.connect() as conn:
                self.save_metadata(conn, {str(row["id"]): metadata})
                generation_ids = conn.execute(
                    "SELECT generation_id FROM generation_images WHERE asset_id = ?",
                    (row["id"],),
                ).fetchall()
                for item in generation_ids:
                    generation_id = str(item["generation_id"])
                    self.services.refresh_import_projection(conn, generation_id)
                    self.services.refresh_search(conn, generation_id)
