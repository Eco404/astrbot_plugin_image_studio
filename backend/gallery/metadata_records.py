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
from .context import GalleryContext
from .projection import (
    _load_json,
)


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
            if int(cached.get("parser_version", 0)) >= PARSER_VERSION:
                return cached
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
        conn.executemany(
            "INSERT INTO image_metadata (asset_id, format, parser_version, metadata_json) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(asset_id) DO UPDATE SET "
            "format=excluded.format, parser_version=excluded.parser_version, "
            "metadata_json=excluded.metadata_json "
            "WHERE image_metadata.parser_version < excluded.parser_version",
            [
                (
                    asset_id,
                    str(item.get("format") or "unknown"),
                    int(item.get("parser_version", 1)),
                    json.dumps(item, ensure_ascii=False, separators=(",", ":")),
                )
                for asset_id, item in metadata.items()
            ],
        )

    def backfill_metadata(self) -> None:
        """Parse old asset metadata outside the schema migration transaction."""

        from ..metadata.parser import PARSER_VERSION

        with self.context.connect() as conn:
            rows = conn.execute(
                "SELECT a.id, a.path FROM image_assets a LEFT JOIN image_metadata m "
                "ON m.asset_id = a.id WHERE m.asset_id IS NULL OR m.parser_version < ?",
                (PARSER_VERSION,),
            ).fetchall()
        for row in rows:
            path = self.context.data_dir / str(row["path"])
            if not _is_within(path, self.context.assets_dir) or not path.is_file():
                continue
            try:
                metadata = self.metadata_for_asset(str(row["id"]), path.read_bytes())
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
