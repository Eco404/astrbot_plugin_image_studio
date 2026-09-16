"""Quota cleanup, orphan repair, and retained-asset health accounting."""

from __future__ import annotations

import hashlib
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..config import HistorySettings
from ..database.schema import DATABASE_VERSION
from ..media.files import (
    _delete_unreferenced_files,
    _is_within,
    _remove_empty_directories,
    _unlink_if_owned,
)
from ..media.images import _IMAGE_SUFFIXES
from .constants import (
    _GALLERY_RETENTION_CACHE_SECONDS,
    _SHA256_RE,
)
from .context import GalleryContext


@dataclass(frozen=True)
class MaintenanceServices:
    """Explicit cross-repository operations; connections stay with their caller."""

    backfill_metadata: Callable[..., Any]
    delete_expired_import_batches: Callable[..., Any]
    delete_generation: Callable[..., Any]
    expire_asset_retention: Callable[..., Any]
    external_sources_status: Callable[..., Any]
    gallery_revision: Callable[..., Any]
    prepare_thumbnail: Callable[..., Any]
    repair_derived_fields: Callable[..., Any]
    trim_disabled_external_thumbnails: Callable[..., Any]
    upsert_thumbnails: Callable[..., Any]


class GalleryMaintenance:
    def __init__(self, context: GalleryContext, services: MaintenanceServices) -> None:
        self.context = context
        self.services = services

    def run_maintenance(
        self,
        history: HistorySettings,
        preview_max_edge: int,
        preview_quality: int,
        deep: bool,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        checked_at = time.time()
        errors: list[str] = []
        repaired = {
            "expired_leases": 0,
            "expired_import_batches": 0,
            "broken_assets": 0,
            "rebuilt_thumbnails": 0,
            "unreferenced_assets": 0,
            "orphan_files": 0,
            "stale_temporary_files": 0,
        }

        try:
            with self.context.connect() as conn:
                quick = str(conn.execute("PRAGMA quick_check").fetchone()[0])
                if quick != "ok":
                    errors.append(f"SQLite quick_check：{quick}")
                foreign_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
                if foreign_rows:
                    errors.append(f"SQLite 外键异常：{len(foreign_rows)} 项")
        except sqlite3.Error as exc:
            errors.append(f"SQLite 检查失败：{type(exc).__name__}")

        repaired["expired_leases"] = self.services.expire_asset_retention(checked_at)
        repaired["expired_import_batches"] = (
            self.services.delete_expired_import_batches(checked_at)
        )
        self.services.repair_derived_fields()
        self.services.backfill_metadata()
        broken_ids: list[str] = []
        with self.context.connect() as conn:
            asset_rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT id, path, mime_type, size_bytes FROM image_assets WHERE path NOT LIKE 'external/%'"
                ).fetchall()
            ]
        for asset in asset_rows:
            path = self.context.data_dir / str(asset["path"])
            try:
                valid = (
                    _is_within(path, self.context.assets_dir)
                    and path.is_file()
                    and path.stat().st_size == int(asset["size_bytes"])
                )
                if valid and deep:
                    valid = hashlib.sha256(path.read_bytes()).hexdigest() == asset["id"]
            except OSError:
                valid = False
            if not valid:
                broken_ids.append(str(asset["id"]))
                continue
            thumbnail_missing = not (
                self.context.thumbnails_dir / f"{asset['id']}.webp"
            ).is_file()
            thumbnail = self.services.prepare_thumbnail(
                asset,
                max_edge=preview_max_edge,
                quality=preview_quality,
            )
            with self.context.connect() as conn:
                previous = conn.execute(
                    "SELECT path, size_bytes, max_edge, quality FROM image_thumbnails "
                    "WHERE asset_id = ?",
                    (asset["id"],),
                ).fetchone()
                self.services.upsert_thumbnails(conn, [thumbnail])
                conn.execute(
                    "UPDATE image_assets SET file_state = 'available' WHERE id = ?",
                    (asset["id"],),
                )
            if (
                thumbnail_missing
                or previous is None
                or int(previous["size_bytes"]) != thumbnail["size_bytes"]
                or int(previous["max_edge"]) != thumbnail["max_edge"]
                or int(previous["quality"]) != thumbnail["quality"]
            ):
                repaired["rebuilt_thumbnails"] += 1

        for asset_id in broken_ids:
            repaired["broken_assets"] += self.remove_broken_asset(asset_id)
        if broken_ids:
            errors.append(f"原图不可用：{len(broken_ids)} 项，已有历史和元数据已保留")

        self.cleanup(history)
        before_assets = self.asset_count()
        self.purge_unreferenced_assets()
        repaired["unreferenced_assets"] = max(0, before_assets - self.asset_count())
        grace_cutoff = checked_at - 600
        repaired["orphan_files"] += self.cleanup_orphaned_asset_files(
            older_than=grace_cutoff
        )
        repaired["orphan_files"] += self.cleanup_orphaned_thumbnails(
            older_than=grace_cutoff
        )
        repaired["stale_temporary_files"] = self.cleanup_stale_files(checked_at)
        try:
            with self.context.connect() as conn:
                conn.execute("PRAGMA optimize")
        except sqlite3.Error as exc:
            errors.append(f"SQLite optimize 失败：{type(exc).__name__}")
        return {
            "status": "warning" if errors else "healthy",
            "running": False,
            "checked_at": checked_at,
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "deep": deep,
            "stats": self.storage_stats(),
            "repaired": repaired,
            "errors": errors,
        }

    def cleanup(self, settings: HistorySettings) -> None:
        while True:
            status = self.retention_status(settings)
            if not status["over_limit"] or not status["candidate_ids"]:
                break
            if not self.services.delete_generation(status["candidate_ids"][0]):
                break

    def gallery_retention_snapshot(
        self, history: HistorySettings
    ) -> tuple[dict[str, Any], float, float, str]:
        revision = self.services.gallery_revision()
        now = time.time()
        status = self.retention_status(history)
        with self.context.connect() as conn:
            protected_until = conn.execute(
                "SELECT MIN(cleanup_protected_until) FROM generations "
                "WHERE source NOT IN ('import','external') AND is_favorite = 0 AND cleanup_protected_until > ?",
                (now,),
            ).fetchone()[0]
        expires_at = now + _GALLERY_RETENTION_CACHE_SECONDS
        if protected_until is not None:
            expires_at = min(expires_at, float(protected_until))
        # An external writer can commit despite the store's asyncio lock.
        # Never retain a calculation that straddled such a database change.
        if self.services.gallery_revision() != revision:
            revision = ""
        return status, now, expires_at, revision

    def retention_status(self, history: HistorySettings) -> dict[str, Any]:
        now = time.time()
        with self.context.connect() as conn:
            count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM generations WHERE source NOT IN ('import','external') AND is_favorite = 0"
                ).fetchone()[0]
            )
            protected = int(
                conn.execute(
                    "SELECT COUNT(*) FROM generations WHERE source NOT IN ('import','external') "
                    "AND is_favorite = 0 AND cleanup_protected_until > ?",
                    (now,),
                ).fetchone()[0]
            )
            candidates = [
                str(row[0])
                for row in conn.execute(
                    "SELECT id FROM generations WHERE source NOT IN ('import','external') AND is_favorite = 0 "
                    "AND cleanup_protected_until <= ? ORDER BY created_at ASC, id ASC LIMIT 10",
                    (now,),
                ).fetchall()
            ]
            exempt = conn.execute(
                "SELECT SUM(CASE WHEN source = 'import' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN is_favorite = 1 THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN source = 'import' OR is_favorite = 1 THEN 1 ELSE 0 END) FROM generations WHERE source != 'external'"
            ).fetchone()
        usage = self.history_asset_usage()
        size_bytes = usage["counted_size_bytes"]
        limit_records = max(0, history.max_records)
        limit_bytes = max(0, history.max_megabytes) * 1024 * 1024
        near = (limit_records > 0 and count >= limit_records * 0.9) or (
            limit_bytes > 0 and size_bytes >= limit_bytes * 0.9
        )
        over = (limit_records > 0 and count > limit_records) or (
            limit_bytes > 0 and size_bytes > limit_bytes
        )
        return {
            "record_count": count,
            "limit_records": limit_records,
            "size_bytes": size_bytes,
            "limit_bytes": limit_bytes,
            "near_limit": bool(near),
            "over_limit": bool(over),
            "candidate_ids": candidates if near else [],
            "protected_records": protected,
            "imported_records": int(exempt[0] or 0),
            "favorite_records": int(exempt[1] or 0),
            "exempt_record_count": int(exempt[2] or 0),
            "exempt_size_bytes": usage["exempt_size_bytes"],
            "total_records": count + int(exempt[2] or 0),
            "gallery_size_bytes": size_bytes + usage["exempt_size_bytes"],
            "total_size_bytes": self.storage_stats()["size_bytes"],
        }

    def history_asset_bytes(self) -> int:
        """Count automatic-history assets once, excluding imported/favorite shares."""

        return self.history_asset_usage()["counted_size_bytes"]

    def history_asset_usage(self) -> dict[str, int]:
        """Assign each gallery asset to counted or exempt ownership exactly once."""

        with self.context.connect() as conn:
            row = conn.execute(
                "WITH links AS ("
                "SELECT asset_id, generation_id FROM generation_images UNION "
                "SELECT asset_id, generation_id FROM generation_references WHERE asset_id IS NOT NULL"
                "), ownership AS ("
                "SELECT l.asset_id, MAX(CASE WHEN g.source = 'import' OR g.is_favorite = 1 THEN 1 ELSE 0 END) AS exempt "
                "FROM links l JOIN generations g ON g.id = l.generation_id WHERE g.source != 'external' GROUP BY l.asset_id"
                ") SELECT "
                "COALESCE(SUM(CASE WHEN o.exempt = 0 THEN a.size_bytes + COALESCE(t.size_bytes, 0) ELSE 0 END), 0), "
                "COALESCE(SUM(CASE WHEN o.exempt = 1 THEN a.size_bytes + COALESCE(t.size_bytes, 0) ELSE 0 END), 0) "
                "FROM ownership o JOIN image_assets a ON a.id = o.asset_id "
                "LEFT JOIN image_thumbnails t ON t.asset_id = a.id"
            ).fetchone()
        return {"counted_size_bytes": int(row[0]), "exempt_size_bytes": int(row[1])}

    def asset_count(self) -> int:
        with self.context.connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM image_assets").fetchone()[0])

    def storage_stats(self) -> dict[str, Any]:
        now = time.time()
        with self.context.connect() as conn:
            row = conn.execute(
                "SELECT "
                "(SELECT COUNT(*) FROM generations WHERE source != 'external'), "
                "(SELECT COUNT(*) FROM image_assets WHERE path NOT LIKE 'external/%'), "
                "(SELECT COUNT(*) FROM image_thumbnails), "
                "(SELECT COUNT(*) FROM agent_asset_leases "
                " WHERE expires_at > ? AND hard_expires_at > ?), "
                "COALESCE((SELECT SUM(size_bytes) FROM image_assets WHERE path NOT LIKE 'external/%'), 0) + "
                "COALESCE((SELECT SUM(size_bytes) FROM image_thumbnails), 0)",
                (now, now),
            ).fetchone()
        return {
            "generations": int(row[0]),
            "assets": int(row[1]),
            "thumbnails": int(row[2]),
            "active_leases": int(row[3]),
            "size_bytes": int(row[4]),
            "database_version": DATABASE_VERSION,
            "external_sources": [
                item
                for item in self.services.external_sources_status()
                if item["enabled"]
            ],
        }

    def remove_broken_asset(self, asset_id: str) -> int:
        if not _SHA256_RE.fullmatch(asset_id):
            return 0
        with self.context.connect() as conn:
            row = conn.execute(
                "SELECT path FROM image_assets WHERE id = ?", (asset_id,)
            ).fetchone()
            if row is None:
                return 0
            conn.execute(
                "UPDATE image_assets SET file_state = 'unavailable' WHERE id = ?",
                (asset_id,),
            )
            conn.execute(
                "UPDATE agent_asset_leases SET expires_at = 0, hard_expires_at = 0 "
                "WHERE asset_id = ?",
                (asset_id,),
            )
        return 1

    def purge_unreferenced_assets(self, asset_ids: list[str] | None = None) -> None:
        candidates = list(
            dict.fromkeys(
                asset_id
                for asset_id in (asset_ids or [])
                if _SHA256_RE.fullmatch(asset_id)
            )
        )
        restriction = ""
        args: list[str] = []
        if asset_ids is not None:
            if not candidates:
                return
            restriction = f"AND a.id IN ({','.join('?' for _ in candidates)})"
            args = candidates
        with self.context.connect() as conn:
            # An external association must not keep an unreferenced owned copy alive.
            demoted = conn.execute(
                "SELECT a.id,a.path,a.mime_type FROM image_assets a WHERE a.path NOT LIKE 'external/%' "
                "AND EXISTS (SELECT 1 FROM external_records e WHERE e.asset_id=a.id) "
                "AND NOT EXISTS (SELECT 1 FROM generation_images i JOIN generations g ON g.id=i.generation_id WHERE i.asset_id=a.id AND g.source!='external') "
                "AND NOT EXISTS (SELECT 1 FROM generation_references r WHERE r.asset_id=a.id) "
                "AND NOT EXISTS (SELECT 1 FROM agent_asset_leases l WHERE l.asset_id=a.id AND l.expires_at>? AND l.hard_expires_at>?) "
                f"{restriction}",
                [time.time(), time.time(), *args],
            ).fetchall()
            conn.executemany(
                "UPDATE image_assets SET path=? WHERE id=?",
                [
                    (
                        f"external/{row['id']}{_IMAGE_SUFFIXES.get(row['mime_type'], '.img')}",
                        row["id"],
                    )
                    for row in demoted
                ],
            )
            rows = conn.execute(
                "SELECT a.id, a.path, t.path AS thumbnail_path FROM image_assets a "
                "LEFT JOIN image_thumbnails t ON t.asset_id = a.id "
                "WHERE NOT EXISTS (SELECT 1 FROM generation_images i WHERE i.asset_id = a.id) "
                "AND NOT EXISTS (SELECT 1 FROM generation_references r WHERE r.asset_id = a.id) "
                "AND NOT EXISTS (SELECT 1 FROM agent_asset_leases l WHERE l.asset_id = a.id "
                "AND l.expires_at > ? AND l.hard_expires_at > ?) "
                f"{restriction}",
                [time.time(), time.time(), *args],
            ).fetchall()
            conn.executemany(
                "DELETE FROM image_assets WHERE id = ?",
                [(str(row["id"]),) for row in rows],
            )
        for row in demoted:
            _unlink_if_owned(
                self.context.data_dir / str(row["path"]), self.context.assets_dir
            )
        for row in rows:
            _unlink_if_owned(
                self.context.data_dir / str(row["path"]), self.context.assets_dir
            )
            if row["thumbnail_path"]:
                _unlink_if_owned(
                    self.context.data_dir / str(row["thumbnail_path"]),
                    self.context.thumbnails_dir,
                )
        _remove_empty_directories(self.context.assets_dir)
        self.services.trim_disabled_external_thumbnails()

    def cleanup_orphaned_asset_files(self, *, older_than: float | None = None) -> int:
        """Delete content-addressed files that have no database asset row."""

        with self.context.connect() as conn:
            rows = conn.execute("SELECT path FROM image_assets").fetchall()
        referenced = {
            (self.context.data_dir / str(row["path"])).resolve()
            for row in rows
            if _is_within(
                self.context.data_dir / str(row["path"]), self.context.assets_dir
            )
        }
        removed = _delete_unreferenced_files(
            self.context.assets_dir, referenced, older_than=older_than
        )
        _remove_empty_directories(self.context.assets_dir)
        return removed

    def cleanup_orphaned_thumbnails(self, *, older_than: float | None = None) -> int:
        """Delete thumbnail files no longer registered to a shared asset."""

        with self.context.connect() as conn:
            rows = conn.execute("SELECT path FROM image_thumbnails").fetchall()
        referenced = {
            (self.context.data_dir / str(row["path"])).resolve()
            for row in rows
            if _is_within(
                self.context.data_dir / str(row["path"]), self.context.thumbnails_dir
            )
        }
        return _delete_unreferenced_files(
            self.context.thumbnails_dir, referenced, older_than=older_than
        )

    def cleanup_stale_files(self, now: float) -> int:
        removed = 0
        for root, cutoff in (
            (self.context.staging_dir, now - 24 * 3600),
            (self.context.imports_dir, now - 3600),
            (self.context.exports_dir, now - 3600),
            (self.context.delivery_dir, now - 3600),
        ):
            if not root.is_dir():
                continue
            for path in root.rglob("*"):
                try:
                    if (
                        path.is_file()
                        and _is_within(path, root)
                        and path.stat().st_mtime < cutoff
                    ):
                        path.unlink()
                        removed += 1
                except OSError:
                    continue
            _remove_empty_directories(root)
        return removed
