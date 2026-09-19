"""Shared gallery paths and database connection policy.

Repositories use this one context and never create an independent mutation lock.
GenerationStore owns asynchronous scheduling and retains the existing lock boundary.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class GalleryContext:
    data_dir: Path
    external_issue_handler: Callable[[str, str], None] | None = None
    images_dir: Path = field(init=False)
    assets_dir: Path = field(init=False)
    thumbnails_dir: Path = field(init=False)
    staging_dir: Path = field(init=False)
    imports_dir: Path = field(init=False)
    exports_dir: Path = field(init=False)
    delivery_dir: Path = field(init=False)
    db_path: Path = field(init=False)

    def __post_init__(self) -> None:
        self.data_dir = self.data_dir.resolve()
        self.images_dir = self.data_dir / "images"
        self.assets_dir = self.images_dir / "assets"
        self.thumbnails_dir = self.images_dir / "thumbnails"
        self.staging_dir = self.data_dir / "staging_references"
        self.imports_dir = self.data_dir / "import_staging"
        self.exports_dir = self.data_dir / "exports"
        self.delivery_dir = self.data_dir / "delivery_staging"
        self.db_path = self.data_dir / "history.sqlite3"

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn
