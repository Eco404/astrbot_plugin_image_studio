"""Bounded database housekeeping and read-only owned-directory accounting."""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import stat
from contextlib import closing
from pathlib import Path
from typing import Any

_BACKUP_NAME = re.compile(
    r"history-pre-v(?P<version>[1-9]\d*)(?:-dev\.(?P<revision>[1-9]\d*))?"
    r"-(?P<stamp>\d{8}T\d{12}Z)-[A-Za-z0-9_-]+\.sqlite3"
)
_SUPPORTED_DEVELOPMENT_BACKUPS = {(1, 3), (2, 2), (3, 1), (4, 1), (4, 2)}
_MIN_VACUUM_BYTES = 16 * 1024 * 1024
_CATEGORIES = (
    "originals",
    "thumbnails",
    "comfy_inputs",
    "comfy_outputs",
    "comfy_blobs",
    "database",
    "backups",
    "temporary",
    "other",
)


def database_space(conn: sqlite3.Connection) -> dict[str, int]:
    """Free pages remain reusable by SQLite even before physical compaction."""

    page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
    pages = int(conn.execute("PRAGMA page_count").fetchone()[0])
    free_pages = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    return {
        "page_size": page_size,
        "pages": pages,
        "free_pages": free_pages,
        "size_bytes": page_size * pages,
        "reusable_bytes": page_size * free_pages,
    }


def _category(relative: Path) -> str:
    parts = relative.parts
    # Count both layouts while an interrupted images migration is resumed.
    if len(parts) >= 2 and parts[0] in {"images", "history"}:
        if parts[1] == "assets":
            return "originals"
        if parts[1] == "thumbnails":
            return "thumbnails"
    if parts and parts[0] in {"comfyui_inputs", "comfyui_outputs", "comfyui_blobs"}:
        return {
            "comfyui_inputs": "comfy_inputs",
            "comfyui_outputs": "comfy_outputs",
            "comfyui_blobs": "comfy_blobs",
        }[parts[0]]
    if len(parts) == 1 and parts[0] in {
        "history.sqlite3",
        "history.sqlite3-wal",
        "history.sqlite3-shm",
        "history.sqlite3-journal",
    }:
        return "database"
    if parts and parts[0] == "backups":
        return "backups"
    if parts and parts[0] in {
        "staging_references",
        "import_staging",
        "exports",
        "delivery_staging",
    }:
        return "temporary"
    return "other"


def directory_space(data_dir: Path) -> dict[str, Any]:
    """Do not follow symlinks; hard-linked files use one allocation on disk.

    File bytes count directory entries (useful to expose duplicate hard links),
    while allocated bytes count each inode once and include directory blocks.
    This is a point-in-time observation, not a lock over concurrent file writes.
    """

    categories = {
        key: {"file_bytes": 0, "allocated_bytes": 0, "file_count": 0}
        for key in _CATEGORIES
    }
    seen: set[tuple[int, int]] = set()
    result: dict[str, Any] = {
        "file_bytes": 0,
        "allocated_bytes": 0,
        "file_count": 0,
        "skipped_symlinks": 0,
        "unreadable_entries": 0,
        "categories": categories,
    }
    stack = [data_dir]
    while stack:
        path = stack.pop()
        try:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                result["skipped_symlinks"] += 1
                continue
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                continue
            bucket = categories[_category(path.relative_to(data_dir))]
            inode = (info.st_dev, info.st_ino)
            allocated = 0
            if inode not in seen:
                allocated = int(getattr(info, "st_blocks", 0)) * 512
                seen.add(inode)
            bucket["allocated_bytes"] += allocated
            result["allocated_bytes"] += allocated
            if stat.S_ISDIR(info.st_mode):
                # lstat above and scandir below never intentionally descend links.
                with os.scandir(path) as entries:
                    stack.extend(Path(entry.path) for entry in entries)
            else:
                bucket["file_bytes"] += info.st_size
                bucket["file_count"] += 1
                result["file_bytes"] += info.st_size
                result["file_count"] += 1
        except OSError:
            result["unreadable_entries"] += 1
    return result


def rotate_backups(
    backup_dir: Path,
    *,
    database_version: int,
    protected: Path | None = None,
    keep: int = 3,
) -> dict[str, int]:
    """Rotate only verified plugin migration backups, never arbitrary files."""

    result = {"backups_removed": 0, "backup_bytes_reclaimed": 0}
    if backup_dir.is_symlink() or not backup_dir.is_dir():
        return result
    candidates: list[Path] = []
    for path in backup_dir.iterdir():
        match = _BACKUP_NAME.fullmatch(path.name)
        if not match or int(match["version"]) > database_version:
            continue
        if (
            match["revision"] is not None
            and (int(match["version"]), int(match["revision"]))
            not in _SUPPORTED_DEVELOPMENT_BACKUPS
        ):
            continue
        try:
            if not stat.S_ISREG(path.lstat().st_mode):
                continue
            with closing(
                sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
            ) as conn:
                if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    continue
                # A filename alone must not turn unrelated SQLite into our backup.
                if not conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='generations'"
                ).fetchone():
                    continue
            candidates.append(path)
        except (OSError, sqlite3.Error):
            continue
    candidates.sort(
        key=lambda path: (_BACKUP_NAME.fullmatch(path.name)["stamp"], path.name),
        reverse=True,
    )
    retained = set(candidates[: max(1, keep)])
    if protected is not None:
        retained.add(protected)
    for path in candidates:
        if path in retained:
            continue
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                continue
            path.unlink()
            result["backups_removed"] += 1
            result["backup_bytes_reclaimed"] += info.st_size
        except OSError:
            continue
    return result


def compact_database(
    conn: sqlite3.Connection, db_path: Path, *, deep: bool
) -> dict[str, Any]:
    """Only explicit deep maintenance may rebuild a substantially empty file."""

    before = database_space(conn)
    result: dict[str, Any] = {
        "compacted": False,
        "bytes_reclaimed": 0,
        "reusable_bytes": before["reusable_bytes"],
    }
    if not deep:
        result["reason"] = "manual_deep_only"
        return result
    if (
        before["reusable_bytes"] < _MIN_VACUUM_BYTES
        or before["free_pages"] < before["pages"] * 0.2
    ):
        result["reason"] = "below_threshold"
        return result
    if (
        conn.execute("SELECT 1 FROM sqlite_master WHERE name='comfy_jobs'").fetchone()
        and conn.execute(
            "SELECT 1 FROM comfy_jobs WHERE status NOT IN ('succeeded','partial','failed','cancelled') LIMIT 1"
        ).fetchone()
    ):
        result["reason"] = "comfy_tasks_pending"
        return result
    if shutil.disk_usage(db_path.parent).free < before["size_bytes"] * 2:
        result["reason"] = "insufficient_working_space"
        return result
    if conn.in_transaction:
        raise RuntimeError(
            "Database compaction requires a committed maintenance transaction"
        )
    conn.execute("PRAGMA busy_timeout=1000")
    try:
        conn.execute("VACUUM")
    except sqlite3.OperationalError as exc:
        if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
            raise
        result["reason"] = "database_busy"
        return result
    after = database_space(conn)
    result.update(
        compacted=True,
        bytes_reclaimed=max(0, before["size_bytes"] - after["size_bytes"]),
        reusable_bytes=after["reusable_bytes"],
        reason="completed",
    )
    return result
