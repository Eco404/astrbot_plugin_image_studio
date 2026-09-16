"""Asynchronous gallery facade and the shared mutation boundary."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..config import HistorySettings
from ..database.schema import ensure_release_schema
from ..media.display import DisplayImageCache, display_file_version, display_max_edge
from ..media.files import (
    _atomic_write,
    _is_within,
)
from ..media.images import (
    _image_dimensions,
    _image_suffix,
    _safe_filename,
    _validate_import_content,
    detect_mime_type,
    image_data_url,
)
from ..models import (
    GeneratedImage,
    GenerationRequest,
    ImageProvider,
    ReferenceImage,
    WorkflowImageAsset,
    WorkflowImageLoadResult,
)
from .assets import AssetRepository
from .constants import (
    _SAFE_ID_RE,
    _SHA256_RE,
    WORKFLOW_ASSET_RETENTION_SECONDS,
)
from .context import GalleryContext
from .external_records import ExternalRecords, ExternalServices
from .errors import _ExternalFileChangedError
from .imports import ImportRepository, ImportServices
from .maintenance import GalleryMaintenance, MaintenanceServices
from .metadata_records import MetadataRecords, MetadataServices
from .projection import (
    _canonical_supplemental,
    _known_merge_engine,
    _load_json,
    _search_projection,
    _validate_generation_selection,
    _validate_import_edit_overrides,
    _validate_import_hashes,
    _validate_import_overrides,
)
from .queries import GalleryQueries, QueryServices
from .records import GenerationRecords, RecordServices

_LOGGER = logging.getLogger(__name__)


class GenerationStore:
    """Compose gallery repositories while preserving public async operations and locks."""

    def __init__(self, data_dir: Path) -> None:
        self._context = GalleryContext(data_dir)
        self.data_dir = self._context.data_dir
        self.history_dir = self._context.history_dir
        self.assets_dir = self._context.assets_dir
        self.thumbnails_dir = self._context.thumbnails_dir
        self.staging_dir = self._context.staging_dir
        self.imports_dir = self._context.imports_dir
        self.exports_dir = self._context.exports_dir
        self.delivery_dir = self._context.delivery_dir
        self.db_path = self._context.db_path
        self._lock = asyncio.Lock()
        self._display_cache = DisplayImageCache()
        self._revision_lock = threading.Lock()
        self._revision_connection: sqlite3.Connection | None = None
        self._revision_identity: tuple[int, int] | None = None
        self._revision_instance = ""
        self._retention_cache_lock = asyncio.Lock()
        self._retention_cache: tuple[Any, float, float, dict[str, Any]] | None = None
        self._last_maintenance_report: dict[str, Any] = {
            "status": "never",
            "running": False,
            "checked_at": 0,
            "duration_ms": 0,
            "deep": False,
            "stats": {},
            "repaired": {},
            "errors": [],
        }
        # Dependencies are explicit and resolved against this store's repositories.
        self.external_records = ExternalRecords(
            self._context,
            ExternalServices(
                metadata_for_asset=lambda *args, **kwargs: (
                    self.metadata_records.metadata_for_asset(*args, **kwargs)
                ),
                prepare_thumbnail=lambda *args, **kwargs: self.assets.prepare_thumbnail(
                    *args, **kwargs
                ),
                purge_unreferenced_assets=lambda *args, **kwargs: (
                    self.maintenance.purge_unreferenced_assets(*args, **kwargs)
                ),
                refresh_search=lambda *args, **kwargs: self._refresh_search_sync(
                    *args, **kwargs
                ),
                save_metadata=lambda *args, **kwargs: (
                    self.metadata_records.save_metadata(*args, **kwargs)
                ),
                upsert_thumbnails=lambda *args, **kwargs: self.assets.upsert_thumbnails(
                    *args, **kwargs
                ),
            ),
        )
        self.imports = ImportRepository(
            self._context,
            ImportServices(
                cleanup_orphaned_asset_files=lambda *args, **kwargs: (
                    self.maintenance.cleanup_orphaned_asset_files(*args, **kwargs)
                ),
                cleanup_orphaned_thumbnails=lambda *args, **kwargs: (
                    self.maintenance.cleanup_orphaned_thumbnails(*args, **kwargs)
                ),
                metadata_for_asset=lambda *args, **kwargs: (
                    self.metadata_records.metadata_for_asset(*args, **kwargs)
                ),
                prepare_asset=lambda *args, **kwargs: self.assets.prepare_asset(
                    *args, **kwargs
                ),
                prepare_thumbnail=lambda *args, **kwargs: self.assets.prepare_thumbnail(
                    *args, **kwargs
                ),
                refresh_search=lambda *args, **kwargs: self._refresh_search_sync(
                    *args, **kwargs
                ),
                save_metadata=lambda *args, **kwargs: (
                    self.metadata_records.save_metadata(*args, **kwargs)
                ),
                upsert_thumbnails=lambda *args, **kwargs: self.assets.upsert_thumbnails(
                    *args, **kwargs
                ),
            ),
        )
        self.queries = GalleryQueries(
            self._context,
            QueryServices(
                allowed_actions_for_generation=lambda *args, **kwargs: (
                    self.external_records.allowed_actions_for_generation(
                        *args, **kwargs
                    )
                ),
                assert_external_action=lambda *args, **kwargs: (
                    self.external_records.assert_external_action(*args, **kwargs)
                ),
                external_display=lambda *args, **kwargs: (
                    self.external_records.external_display(*args, **kwargs)
                ),
                external_generation_enabled=lambda *args, **kwargs: (
                    self.external_records.external_generation_enabled(*args, **kwargs)
                ),
                gallery_data_url=lambda *args, **kwargs: (
                    self.external_records.gallery_data_url(*args, **kwargs)
                ),
                gallery_revision=lambda *args, **kwargs: self._gallery_revision_sync(
                    *args, **kwargs
                ),
                gallery_thumbnail=lambda *args, **kwargs: (
                    self.external_records.gallery_thumbnail(*args, **kwargs)
                ),
                resolve_image_asset=lambda *args, **kwargs: (
                    self.external_records.resolve_image_asset(*args, **kwargs)
                ),
                report_external_failure=lambda *args, **kwargs: (
                    self.external_records.report_external_failure(*args, **kwargs)
                ),
            ),
        )
        self.assets = AssetRepository(self._context)
        self.maintenance = GalleryMaintenance(
            self._context,
            MaintenanceServices(
                backfill_metadata=lambda *args, **kwargs: (
                    self.metadata_records.backfill_metadata(*args, **kwargs)
                ),
                delete_expired_import_batches=lambda *args, **kwargs: (
                    self.imports.delete_expired_import_batches(*args, **kwargs)
                ),
                delete_generation=lambda *args, **kwargs: (
                    self.records.delete_generation(*args, **kwargs)
                ),
                expire_asset_retention=lambda *args, **kwargs: (
                    self.assets.expire_asset_retention(*args, **kwargs)
                ),
                external_sources_status=lambda *args, **kwargs: (
                    self.external_records.external_sources_status(*args, **kwargs)
                ),
                gallery_revision=lambda *args, **kwargs: self._gallery_revision_sync(
                    *args, **kwargs
                ),
                prepare_thumbnail=lambda *args, **kwargs: self.assets.prepare_thumbnail(
                    *args, **kwargs
                ),
                repair_derived_fields=lambda *args, **kwargs: (
                    self._repair_derived_fields_sync(*args, **kwargs)
                ),
                trim_disabled_external_thumbnails=lambda *args, **kwargs: (
                    self.external_records.trim_disabled_external_thumbnails(
                        *args, **kwargs
                    )
                ),
                upsert_thumbnails=lambda *args, **kwargs: self.assets.upsert_thumbnails(
                    *args, **kwargs
                ),
            ),
        )
        self.records = GenerationRecords(
            self._context,
            RecordServices(
                assert_external_action=lambda *args, **kwargs: (
                    self.external_records.assert_external_action(*args, **kwargs)
                ),
                cleanup_orphaned_asset_files=lambda *args, **kwargs: (
                    self.maintenance.cleanup_orphaned_asset_files(*args, **kwargs)
                ),
                cleanup_orphaned_thumbnails=lambda *args, **kwargs: (
                    self.maintenance.cleanup_orphaned_thumbnails(*args, **kwargs)
                ),
                delete_external_original=lambda *args, **kwargs: (
                    self.external_records.delete_external_original(*args, **kwargs)
                ),
                generation_detail=lambda *args, **kwargs: (
                    self.queries.generation_detail(*args, **kwargs)
                ),
                metadata_for_asset=lambda *args, **kwargs: (
                    self.metadata_records.metadata_for_asset(*args, **kwargs)
                ),
                prepare_asset=lambda *args, **kwargs: self.assets.prepare_asset(
                    *args, **kwargs
                ),
                prepare_thumbnail=lambda *args, **kwargs: self.assets.prepare_thumbnail(
                    *args, **kwargs
                ),
                purge_unreferenced_assets=lambda *args, **kwargs: (
                    self.maintenance.purge_unreferenced_assets(*args, **kwargs)
                ),
                refresh_search=lambda *args, **kwargs: self._refresh_search_sync(
                    *args, **kwargs
                ),
                report_external_failure=lambda *args, **kwargs: (
                    self.external_records.report_external_failure(*args, **kwargs)
                ),
                resolve_image_asset=lambda *args, **kwargs: (
                    self.external_records.resolve_image_asset(*args, **kwargs)
                ),
                save_metadata=lambda *args, **kwargs: (
                    self.metadata_records.save_metadata(*args, **kwargs)
                ),
                upsert_thumbnails=lambda *args, **kwargs: self.assets.upsert_thumbnails(
                    *args, **kwargs
                ),
            ),
        )
        self.metadata_records = MetadataRecords(
            self._context,
            MetadataServices(
                refresh_import_projection=lambda *args, **kwargs: (
                    self.imports.refresh_import_projection(*args, **kwargs)
                ),
                refresh_search=lambda *args, **kwargs: self._refresh_search_sync(
                    *args, **kwargs
                ),
            ),
        )

    async def initialize(self) -> None:
        """Create directories and database tables."""

        await asyncio.to_thread(self._initialize_sync)

    async def close(self) -> None:
        """Release the read-only database observer used by gallery caches."""
        await self._display_cache.close()
        await asyncio.to_thread(self._close_revision_connection_sync)

    def set_external_issue_handler(
        self, handler: Callable[[str, str], None] | None
    ) -> None:
        """Attach the manager's thread-safe recovery scheduler while it is running."""
        self._context.external_issue_handler = handler

    def _close_revision_connection_sync(self) -> None:
        with self._revision_lock:
            if self._revision_connection is not None:
                self._revision_connection.close()
                self._revision_connection = None
            self._retention_cache = None

    async def gallery_revision(self) -> str:
        """Identify committed database changes without scanning gallery records."""
        return await asyncio.to_thread(self._gallery_revision_sync)

    def _gallery_revision_sync(self) -> str:
        with self._revision_lock:
            stat = self.db_path.stat()
            identity = (stat.st_dev, stat.st_ino)
            if self._revision_connection is None or identity != self._revision_identity:
                if self._revision_connection is not None:
                    self._revision_connection.close()
                self._revision_connection = sqlite3.connect(
                    self.db_path.as_uri() + "?mode=ro",
                    uri=True,
                    check_same_thread=False,
                )
                self._revision_identity = identity
                self._revision_instance = uuid.uuid4().hex
            version = self._revision_connection.execute(
                "PRAGMA data_version"
            ).fetchone()[0]
            return f"{self._revision_instance}:{version}"

    def _initialize_sync(self) -> None:
        for directory in (
            self.data_dir,
            self.assets_dir,
            self.thumbnails_dir,
            self.staging_dir,
            self.imports_dir,
            self.exports_dir,
            self.delivery_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            ensure_release_schema(conn, backup_dir=self.data_dir / "backups")
            # Keep existing session grants, but cap legacy configurable retention
            # at one hour after the last access. Never extend it on restart.
            conn.execute(
                "UPDATE agent_asset_leases SET "
                "expires_at = MIN(expires_at, hard_expires_at, last_accessed_at + ?), "
                "hard_expires_at = MIN(expires_at, hard_expires_at, last_accessed_at + ?) "
                "WHERE expires_at != hard_expires_at OR expires_at > last_accessed_at + ?",
                (WORKFLOW_ASSET_RETENTION_SECONDS,) * 3,
            )
        self._repair_derived_fields_sync()
        self.metadata_records.backfill_metadata()
        self.assets.expire_asset_retention(time.time())
        self.imports.delete_expired_import_batches(time.time())
        self.maintenance.purge_unreferenced_assets()
        self.maintenance.cleanup_orphaned_asset_files()
        self.maintenance.cleanup_orphaned_thumbnails()

    def _repair_derived_fields_sync(self) -> None:
        """Repair incomplete dimensions and indexed aliases independently of schema upgrades."""

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, path FROM image_assets WHERE width <= 0 OR height <= 0"
            ).fetchall()
        for row in rows:
            path = self.data_dir / str(row["path"])
            if not _is_within(path, self.assets_dir) or not path.is_file():
                continue
            try:
                dimensions = _image_dimensions(path.read_bytes())
            except OSError:
                continue
            with self._connect() as conn:
                conn.execute(
                    "UPDATE image_assets SET width = ?, height = ? WHERE id = ? "
                    "AND (width <= 0 OR height <= 0)",
                    (*dimensions, str(row["id"])),
                )
        with self._connect() as conn:
            conn.execute(
                "UPDATE generations SET generation_engine = CASE provider_kind "
                "WHEN 'nai_direct' THEN 'novelai' WHEN 'novelai_official' THEN 'novelai' WHEN '' THEN 'unknown' ELSE provider_kind END "
                "WHERE generation_engine = 'unknown' AND source != 'import' "
                "AND provider_kind != ''"
            )
            # Preserve raw requests and per-image metadata; normalize only the gallery projection.
            conn.execute(
                "UPDATE generations SET generation_engine = 'novelai' "
                "WHERE lower(trim(generation_engine)) IN ('nai', 'novelai') "
                "AND generation_engine != 'novelai'"
            )
            conn.execute(
                "UPDATE generations SET generation_engine = 'novelai' "
                "WHERE generation_engine = 'mixed' AND EXISTS ("
                "SELECT 1 FROM generation_images i WHERE i.generation_id = generations.id) "
                "AND NOT EXISTS (SELECT 1 FROM generation_images i "
                "WHERE i.generation_id = generations.id AND "
                "lower(trim(COALESCE(json_extract(i.supplemental_json, '$.generation_engine'), 'unknown'))) "
                "NOT IN ('nai', 'novelai'))"
            )

    async def maintenance_report(self) -> dict[str, Any]:
        """Return the latest maintenance report plus current lightweight stats."""

        async with self._lock:
            report = dict(self._last_maintenance_report)
            report["stats"] = await asyncio.to_thread(self.maintenance.storage_stats)
        return report

    async def configure_external_source(
        self,
        source_id: str,
        name: str,
        root_path: str | Path,
        enabled: bool,
        *,
        source_type: str = "nai",
        recursive: bool = False,
        permissions: dict[str, bool] | None = None,
    ) -> dict[str, Any]:
        await self._external_mutation(
            self.external_records.configure_external_source,
            source_id,
            name,
            root_path,
            enabled,
            source_type,
            recursive,
            permissions,
        )
        return next(
            item
            for item in await self.external_sources_status()
            if item["id"] == source_id
        )

    async def _external_mutation(self, function, *args):
        # A cancelled scanner must not release the store lock while its worker still commits.
        async with self._lock:
            task = asyncio.create_task(asyncio.to_thread(function, *args))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                try:
                    await task
                except Exception:
                    _LOGGER.exception(
                        "External gallery mutation failed while cancellation was pending"
                    )
                raise

    async def remove_external_source(self, source_id: str) -> None:
        """Remove source registration/cache only; never delete source-owned originals."""
        await self._external_mutation(
            self.external_records.remove_external_source, source_id
        )

    async def external_scan_snapshot(self, source_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(
            self.external_records.external_scan_snapshot, source_id
        )

    async def set_external_status(self, source_id: str, status: dict[str, Any]) -> None:
        await self._external_mutation(
            self.external_records.set_external_status, source_id, status
        )

    async def external_sources_status(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.external_records.external_sources_status)

    async def upsert_external_image(
        self,
        source_id: str,
        relative_path: str,
        data: bytes,
        *,
        fingerprint: str,
        sidecar_fingerprint: str,
        size_bytes: int,
        mtime_ns: int,
        parameters: dict[str, Any] | None = None,
        created_at: float | None = None,
        preview_max_edge: int = 1024,
        preview_quality: int = 80,
        generation_engine: str = "unknown",
        time_source: str = "",
        metadata_created_at: float | None = None,
        file_birthtime: float | None = None,
        time_policy_version: int = 0,
    ) -> dict[str, str] | None:
        return await self._external_mutation(
            self.external_records.upsert_external_image,
            source_id,
            relative_path,
            data,
            fingerprint,
            sidecar_fingerprint,
            size_bytes,
            mtime_ns,
            parameters or {},
            created_at,
            preview_max_edge,
            preview_quality,
            generation_engine,
            time_source,
            metadata_created_at,
            file_birthtime,
            time_policy_version,
        )

    async def reconcile_external_source(self, source_id: str, seen_paths) -> int:
        return await self._external_mutation(
            self.external_records.reconcile_external_source, source_id, set(seen_paths)
        )

    async def stage_gallery_reference(self, image_id: str) -> dict[str, str]:
        # Resolve and read while holding the same lock as scanning and explicit deletion.
        async with self._lock:
            if not _SAFE_ID_RE.fullmatch(str(image_id or "")):
                raise ValueError("图片 ID 无效")
            resolved = await asyncio.to_thread(
                self.queries.gallery_image_file, image_id, "reference"
            )
            if resolved is None:
                raise ValueError("图片不存在、已被删除或外部图库已关闭")
            path, mime, filename = resolved
            content = await asyncio.to_thread(
                self.external_records.read_gallery_file, image_id, path
            )
            return await self.stage_reference(
                filename=filename, content=content, mime_type=mime
            )

    async def external_delete_preview(
        self, generation_ids: list[str]
    ) -> dict[str, Any]:
        ids = _validate_generation_selection(generation_ids)
        return await asyncio.to_thread(
            self.external_records.external_delete_preview, ids
        )

    async def external_action_preview(
        self, generation_ids: list[str], action: str
    ) -> dict[str, Any]:
        ids = _validate_generation_selection(generation_ids)
        return await asyncio.to_thread(
            self.external_records.external_action_preview, ids, action
        )

    async def assert_external_action(
        self, generation_ids: list[str], action: str
    ) -> None:
        ids = _validate_generation_selection(generation_ids)
        await asyncio.to_thread(
            self.external_records.assert_external_action, ids, action
        )

    @staticmethod
    def _refresh_search_sync(conn: sqlite3.Connection, generation_id: str) -> None:
        row = conn.execute(
            "SELECT parameters_json, supplemental_json FROM generations WHERE id = ?",
            (generation_id,),
        ).fetchone()
        if row is None:
            return
        values: list[Any] = [
            _load_json(row["parameters_json"]),
            _canonical_supplemental(_load_json(row["supplemental_json"])),
        ]
        for item in conn.execute(
            "SELECT m.metadata_json, i.supplemental_json FROM generation_images i LEFT JOIN image_metadata m "
            "ON m.asset_id = i.asset_id WHERE i.generation_id = ? ORDER BY i.ordinal",
            (generation_id,),
        ).fetchall():
            values.append(_load_json(item["metadata_json"]).get("normalized", {}))
            values.append(
                _canonical_supplemental(_load_json(item["supplemental_json"]))
            )
        # Only normalized fields are searchable; entire workflow graphs stay out.
        conn.execute(
            "UPDATE generations SET search_text = ? WHERE id = ?",
            (_search_projection(values), generation_id),
        )

    async def run_maintenance(
        self,
        history: HistorySettings,
        *,
        preview_max_edge: int,
        preview_quality: int,
        deep: bool = False,
    ) -> dict[str, Any]:
        """Check and repair the complete plugin-owned storage graph."""

        async with self._lock:
            self._last_maintenance_report = {
                **self._last_maintenance_report,
                "running": True,
            }
            try:
                report = await asyncio.to_thread(
                    self.maintenance.run_maintenance,
                    history,
                    preview_max_edge,
                    preview_quality,
                    bool(deep),
                )
            except Exception as exc:
                report = {
                    "status": "error",
                    "running": False,
                    "checked_at": time.time(),
                    "duration_ms": 0,
                    "deep": bool(deep),
                    "stats": self.maintenance.storage_stats(),
                    "repaired": {},
                    "errors": [f"存储维护失败：{type(exc).__name__}"],
                }
            self._last_maintenance_report = report
            return dict(report)

    async def stage_reference(
        self,
        *,
        filename: str,
        content: bytes,
        mime_type: str,
    ) -> dict[str, Any]:
        """Persist a WebUI upload until the generation request consumes it."""

        if not content:
            raise ValueError("参考图为空")
        if len(content) > 20 * 1024 * 1024:
            raise ValueError("参考图不能超过 20 MB")
        ref_id = uuid.uuid4().hex
        suffix = _image_suffix(mime_type, content)
        target = self.staging_dir / f"{ref_id}{suffix}"
        await asyncio.to_thread(_atomic_write, target, content)
        width, height = await asyncio.to_thread(_image_dimensions, content)
        return {
            "id": ref_id,
            "filename": _safe_filename(filename) or f"reference{suffix}",
            "mime_type": detect_mime_type(content, mime_type),
            "width": width,
            "height": height,
            "preview_data_url": image_data_url(
                content, detect_mime_type(content, mime_type)
            ),
        }

    async def load_staged_references(
        self,
        reference_ids: list[str],
        *,
        max_images: int = 8,
        reject_excess: bool = False,
    ) -> tuple[ReferenceImage, ...]:
        """Load selected plugin-owned staged references by opaque IDs."""

        if reject_excess and len(reference_ids) > max_images:
            raise ValueError(
                f"当前模型/工具最多允许 {max_images} 张参考图；请减少输入，避免底图、蒙版与角色参考编号错位"
            )
        return await asyncio.to_thread(
            self.assets.load_staged_references, reference_ids[:max_images]
        )

    async def discard_staged_references(
        self, reference_ids: tuple[ReferenceImage, ...]
    ) -> None:
        """Remove request staging files after a request reaches a terminal state."""

        await asyncio.to_thread(
            self.assets.discard_staged_references,
            [reference.id for reference in reference_ids],
        )

    async def lease_agent_images(
        self,
        images: tuple[GeneratedImage, ...],
        *,
        scope_id: str,
        create_preview: bool,
        preview_max_edge: int,
        preview_quality: int,
    ) -> tuple[WorkflowImageAsset, ...]:
        """Grant session access and protect generated assets for one idle hour."""

        async with self._lock:
            return await asyncio.to_thread(
                self.assets.lease_agent_images,
                images,
                scope_id,
                create_preview,
                preview_max_edge,
                preview_quality,
            )

    async def load_workflow_image(
        self,
        asset_id: str,
        *,
        scope_id: str,
        detail: str,
        preview_max_edge: int,
        preview_quality: int,
    ) -> tuple[GeneratedImage, str] | None:
        """Read a session-authorized asset and renew its temporary protection."""

        result = await self.load_workflow_image_detailed(
            asset_id,
            scope_id=scope_id,
            detail=detail,
            preview_max_edge=preview_max_edge,
            preview_quality=preview_quality,
        )
        if not result.ok or result.image is None:
            return None
        return result.image, result.internal_path

    async def load_workflow_image_detailed(
        self,
        asset_id: str,
        *,
        scope_id: str,
        detail: str,
        preview_max_edge: int,
        preview_quality: int,
    ) -> WorkflowImageLoadResult:
        """Load a scoped workflow asset while preserving its failure reason."""

        async with self._lock:
            return await asyncio.to_thread(
                self.assets.load_workflow_image_detailed,
                asset_id,
                scope_id,
                detail,
                preview_max_edge,
                preview_quality,
            )

    async def record_success(
        self,
        *,
        provider: ImageProvider,
        request: GenerationRequest,
        images: tuple[GeneratedImage, ...],
        elapsed_ms: int,
        history: HistorySettings,
        preview_max_edge: int = 768,
        preview_quality: int = 80,
        batch_failures: tuple[tuple[int, str], ...] = (),
    ) -> str:
        """Persist a successful generation when history is enabled.

        Returns:
            A gallery generation ID, or an empty string when retention is off.
        """

        if not history.enabled:
            return ""
        async with self._lock:
            generation_id = await asyncio.to_thread(
                self.records.record_success,
                provider,
                request,
                images,
                elapsed_ms,
                history.retain_reference_images,
                history.record_invocation_identity,
                preview_max_edge,
                preview_quality,
                batch_failures,
            )
            await asyncio.to_thread(self.maintenance.cleanup, history)
        return generation_id

    async def import_edit_snapshot(
        self,
        generation_id: str,
        *,
        light: bool = False,
        image_id: str = "",
        item_revision: str = "",
        include_preview: bool = True,
    ) -> dict[str, Any]:
        """Read an edit snapshot without changing files, metadata, or import receipts."""
        if image_id:
            if not _SAFE_ID_RE.fullmatch(image_id):
                raise ValueError("导入图片 ID 无效")
            if not _SHA256_RE.fullmatch(item_revision):
                raise ValueError("图片编辑版本无效，请重新打开编辑窗口")
        # The read transaction provides a coherent snapshot. Projection and
        # thumbnail IO must not hold the store's mutation lock.
        return await asyncio.to_thread(
            self.imports.import_edit_snapshot,
            generation_id,
            light,
            image_id,
            item_revision,
            include_preview,
        )

    async def project_import_edit_image(
        self, generation_id: str, image_id: str, item_revision: str, output_node_id: str
    ) -> dict[str, Any]:
        """Project a stored workflow after validating the editor's item snapshot."""
        if not isinstance(image_id, str) or not _SAFE_ID_RE.fullmatch(image_id):
            raise ValueError("导入图片 ID 无效")
        if not isinstance(item_revision, str) or not _SHA256_RE.fullmatch(
            item_revision
        ):
            raise ValueError("图片编辑版本无效，请重新打开编辑窗口")
        if not isinstance(output_node_id, str):
            raise ValueError("ComfyUI 输出节点 ID 必须是字符串")
        return await asyncio.to_thread(
            self.imports.project_import_edit_image,
            generation_id,
            image_id,
            item_revision,
            output_node_id,
        )

    async def edit_import(
        self, generation_id: str, revision: str, items: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Atomically update supplemental parameters and the complete image order."""
        if not isinstance(revision, str) or not _SHA256_RE.fullmatch(revision):
            raise ValueError("编辑版本无效，请重新打开编辑窗口")
        if not isinstance(items, list) or not 1 <= len(items) <= 100:
            raise ValueError("编辑记录需要 1 至 100 张图片")
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict) or set(item) - {"image_id", "overrides"}:
                raise ValueError("编辑项目格式无效")
            image_id = item.get("image_id")
            if (
                not isinstance(image_id, str)
                or not _SAFE_ID_RE.fullmatch(image_id)
                or image_id in seen
            ):
                raise ValueError("编辑图片 ID 无效或重复")
            seen.add(image_id)
            _validate_import_edit_overrides(item.get("overrides", {}))
        if len(json.dumps(items, ensure_ascii=False).encode()) > 4 * 1024 * 1024:
            raise ValueError("编辑参数不能超过 4 MB")
        async with self._lock:
            return await asyncio.to_thread(
                self.imports.edit_import, generation_id, revision, items
            )

    async def import_image(
        self,
        data: bytes,
        filename: str,
        overrides: dict[str, Any],
        *,
        import_key: str = "",
        preview_max_edge: int = 768,
        preview_quality: int = 80,
    ) -> dict[str, Any]:
        """Archive one image independently of automatic generation retention."""

        _validate_import_content(data)
        _validate_import_overrides(overrides)
        async with self._lock:
            return await asyncio.to_thread(
                self.imports.import_image,
                data,
                filename,
                overrides,
                str(import_key or "").strip()[:160],
                preview_max_edge,
                preview_quality,
            )

    async def stage_import_file(self, path: Path, data: bytes) -> None:
        """Validate and stage an import while excluding concurrent maintenance."""

        async with self._lock:
            await asyncio.to_thread(self.imports.stage_import_file, path, data)

    async def import_group(
        self,
        entries: list[dict[str, Any]],
        *,
        import_key: str,
        preview_max_edge: int = 768,
        preview_quality: int = 80,
    ) -> dict[str, Any]:
        """Atomically archive an ordered group of staged images of the same model."""

        if not isinstance(entries, list) or not 1 <= len(entries) <= 100:
            raise ValueError("每个图组需要 1 至 100 张图片")
        if (
            not isinstance(import_key, str)
            or not import_key.strip()
            or len(import_key) > 160
        ):
            raise ValueError("图组导入请求标识无效")
        async with self._lock:
            return await asyncio.to_thread(
                self.imports.import_group,
                entries,
                import_key.strip(),
                preview_max_edge,
                preview_quality,
            )

    async def list_import_merge_targets(
        self,
        engine: str,
        *,
        limit: int = 24,
        offset: int = 0,
        query: str = "",
        sort: str = "created",
    ) -> dict[str, Any]:
        """List imported records whose surviving images all have the requested source."""

        engine = _known_merge_engine(engine)
        async with self._lock:
            return await asyncio.to_thread(
                self.queries.list_generations,
                {
                    "source": "import",
                    "_merge_target_engine": engine,
                    "limit": limit,
                    "offset": offset,
                    "query": query,
                    "sort": sort,
                },
            )

    async def append_import_group(
        self,
        entries: list[dict[str, Any]],
        target_id: str,
        *,
        import_key: str,
        expected_engine: str,
        preview_max_edge: int = 768,
        preview_quality: int = 80,
    ) -> dict[str, Any]:
        """Append a same-source batch to an existing import, with durable retry receipts."""

        if not isinstance(entries, list) or not 1 <= len(entries) <= 100:
            raise ValueError("每次合并需要 1 至 100 张图片")
        if not isinstance(target_id, str) or not _SAFE_ID_RE.fullmatch(target_id):
            raise ValueError("目标导入图组 ID 无效")
        if (
            not isinstance(import_key, str)
            or not import_key.strip()
            or len(import_key) > 160
        ):
            raise ValueError("合并导入请求标识无效")
        engine = _known_merge_engine(expected_engine)
        async with self._lock:
            return await asyncio.to_thread(
                self.imports.append_import_group,
                entries,
                target_id,
                import_key.strip(),
                engine,
                preview_max_edge,
                preview_quality,
            )

    async def check_import_hashes(self, hashes: list[str]) -> dict[str, Any]:
        """Check exact gallery membership and duplicates within an import selection."""

        hashes = _validate_import_hashes(hashes)
        async with self._lock:
            return await asyncio.to_thread(self.imports.check_import_hashes, hashes)

    async def commit_import_batch(
        self,
        entries: list[dict[str, Any]],
        *,
        import_key: str,
        mode: str = "separate",
        target_id: str = "",
        expected_engine: str = "",
        preview_max_edge: int = 768,
        preview_quality: int = 80,
    ) -> dict[str, Any]:
        """Commit separate imports, a new group, or an append in one database transaction."""

        if not isinstance(entries, list) or not 1 <= len(entries) <= 100:
            raise ValueError("每次导入需要 1 至 100 张图片")
        for entry in entries:
            if not isinstance(entry, dict) or "data" in entry:
                raise ValueError("批量导入项目必须使用已暂存的图片")
            path = entry.get("path")
            if not isinstance(path, (str, Path)) or not _is_within(
                Path(path), self.imports_dir
            ):
                raise ValueError("待导入图片路径不属于导入暂存目录")
        async with self._lock:
            return await asyncio.to_thread(
                self.imports.commit_import_batch,
                entries,
                import_key,
                mode,
                target_id,
                expected_engine,
                preview_max_edge,
                preview_quality,
            )

    async def get_import_batch_result(self, import_key: str) -> dict[str, Any] | None:
        """Recover a successful import response after an API process restart."""

        if not isinstance(import_key, str) or not import_key or len(import_key) > 160:
            return None
        async with self._lock:
            return await asyncio.to_thread(
                self.imports.get_import_batch_result, import_key
            )

    async def list_generations(self, filters: dict[str, Any]) -> dict[str, Any]:
        """Return a paginated gallery collection and aggregate filter values."""

        return await asyncio.to_thread(self.queries.list_generations, filters)

    async def generation_detail(
        self, generation_id: str, *, include_assets: bool = True, light: bool = False
    ) -> dict[str, Any] | None:
        """Return a generation plus its result and reference assets."""

        if light:
            return await asyncio.to_thread(
                self.queries.generation_manifest, generation_id
            )
        return await asyncio.to_thread(
            self.queries.generation_detail, generation_id, include_assets
        )

    async def gallery_image_info(
        self, image_id: str, *, include_preview: bool = True
    ) -> dict[str, Any] | None:
        if not _SAFE_ID_RE.fullmatch(image_id):
            return None
        return await asyncio.to_thread(
            self.queries.gallery_image_info, image_id, include_preview
        )

    async def generation_image_context(
        self, generation_id: str, image_id: str = ""
    ) -> dict[str, Any] | None:
        """Read one image's parameter context without image or thumbnail bytes."""
        if not isinstance(generation_id, str) or not _SAFE_ID_RE.fullmatch(
            generation_id
        ):
            return None
        if image_id and (
            not isinstance(image_id, str) or not _SAFE_ID_RE.fullmatch(image_id)
        ):
            raise ValueError("所选图片不属于当前生成记录或已被删除")
        return await asyncio.to_thread(
            self.queries.generation_image_context, generation_id, image_id
        )

    async def gallery_reference_image(self, reference_id: str) -> dict[str, Any] | None:
        if not _SAFE_ID_RE.fullmatch(reference_id):
            return None
        return await asyncio.to_thread(
            self.queries.gallery_reference_image, reference_id
        )

    async def gallery_image_sequence(
        self, filters: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Return lightweight image cursors in the active gallery order."""

        return await asyncio.to_thread(self.queries.gallery_image_sequence, filters)

    async def gallery_image_data(
        self, image_id: str, *, detail: str, max_edge: object = 1536
    ) -> dict[str, Any] | None:
        """Load one gallery result without decoding originals on the event loop."""

        edge = display_max_edge(max_edge) if detail == "display" else 0
        if not _SAFE_ID_RE.fullmatch(image_id):
            return None
        if detail == "display":
            return await self._gallery_display_data(image_id, edge)
        return await asyncio.to_thread(
            self.queries.gallery_image_data, image_id, detail
        )

    async def _gallery_display_data(
        self, image_id: str, max_edge: int
    ) -> dict[str, Any] | None:
        source = await asyncio.to_thread(self.queries.gallery_display_source, image_id)
        if source is None:
            return None
        item, path, version = source
        key = (str(path), version, max_edge)

        def read_source() -> bytes:
            # An encode may wait for a slot: source settings and identity can
            # change meanwhile. Resolve again before opening source bytes.
            current = self.queries.gallery_display_source(image_id)
            if current is None or current[1:] != source[1:]:
                raise FileNotFoundError("生成图片已不可读取")
            content = self.external_records.read_gallery_file(image_id, path)
            try:
                if display_file_version(path.stat()) != version:
                    raise _ExternalFileChangedError("图片在读取时发生变化")
            except (OSError, _ExternalFileChangedError) as exc:
                self.external_records.report_external_failure(
                    str(item["generation_id"]), exc
                )
                raise
            return content

        try:
            display = await self._display_cache.get(key, read_source, max_edge)
        except (OSError, _ExternalFileChangedError):
            return None
        current = await asyncio.to_thread(self.queries.gallery_display_source, image_id)
        if (
            current is None
            or current[1:] != source[1:]
            or current[0]["sha256"] != item["sha256"]
        ):
            return None
        actions, data_url = await asyncio.gather(
            asyncio.to_thread(
                self.external_records.allowed_actions_for_generation,
                str(item["generation_id"]),
            ),
            asyncio.to_thread(image_data_url, display.data, "image/webp"),
        )
        return {
            "image_id": str(item["image_id"]),
            "allowed_actions": actions,
            "sha256": str(item["sha256"]),
            "thumbnail_revision": str(item["thumbnail_revision"]),
            "generation_id": str(item["generation_id"]),
            "image_index": int(item["image_index"]),
            "mime_type": "image/webp",
            "size_bytes": len(display.data),
            "width": max(1, int(item.get("width") or 1)),
            "height": max(1, int(item.get("height") or 1)),
            "display_width": display.width,
            "display_height": display.height,
            "max_edge": max_edge,
            "data_url": data_url,
        }

    async def gallery_image_file(self, image_id: str) -> tuple[Path, str, str] | None:
        """Resolve one generated image for an authenticated download."""

        if not _SAFE_ID_RE.fullmatch(image_id):
            return None
        return await asyncio.to_thread(self.queries.gallery_image_file, image_id)

    async def read_workflow_image(self, image_id: str) -> bytes:
        """Read fallback workflow metadata under the gallery reuse permission."""
        if not _SAFE_ID_RE.fullmatch(str(image_id or "")):
            raise ValueError("图片 ID 无效")
        async with self._lock:
            resolved = await asyncio.to_thread(
                self.queries.gallery_image_file, image_id, "reference"
            )
            if resolved is None:
                raise ValueError("原图已不可读取，无法重新解析工作流")
            return await asyncio.to_thread(
                self.external_records.read_gallery_file,
                image_id,
                resolved[0],
                30 * 1024 * 1024,
            )

    async def stage_generation_references(
        self, generation_id: str, *, strict: bool = False
    ) -> list[dict[str, Any]]:
        """Copy retained references into the transient input area for reproduction."""

        if not _SAFE_ID_RE.fullmatch(generation_id):
            return []
        return await asyncio.to_thread(
            self.queries.stage_generation_references, generation_id, strict
        )

    async def set_favorite(self, generation_id: str, favorite: bool) -> dict[str, Any]:
        """Protect the complete generation or grant 24 hours after unfavoriting."""

        if not _SAFE_ID_RE.fullmatch(generation_id):
            raise ValueError("生成记录 ID 无效")
        return await self._external_mutation(
            self.records.set_favorite, generation_id, bool(favorite)
        )

    async def favorite_status(self, generation_ids: list[str]) -> dict[str, Any]:
        """Read favorite states for a selection spanning gallery pages."""

        ids = _validate_generation_selection(generation_ids)
        async with self._lock:
            return await asyncio.to_thread(self.records.favorite_status, ids)

    async def toggle_favorites(self, generation_ids: list[str]) -> dict[str, Any]:
        """Favorite missing selections, or unfavorite when all are already favorites."""

        ids = _validate_generation_selection(generation_ids)
        return await self._external_mutation(self.records.toggle_favorites, ids)

    async def delete_images(
        self, generation_id: str, image_ids: list[str]
    ) -> dict[str, Any]:
        """Atomically delete selected result links without changing the request snapshot."""

        if not _SAFE_ID_RE.fullmatch(generation_id):
            raise ValueError("生成记录 ID 无效")
        if not isinstance(image_ids, list) or not image_ids or len(image_ids) > 100:
            raise ValueError("请选择要删除的图片")
        if any(
            not isinstance(item, str) or not _SAFE_ID_RE.fullmatch(item)
            for item in image_ids
        ):
            raise ValueError("图片 ID 无效")
        return await self._external_mutation(
            self.records.delete_images, generation_id, list(dict.fromkeys(image_ids))
        )

    async def delete_generation(self, generation_id: str) -> bool:
        """Delete one generation and every plugin-owned result/reference file."""

        if not _SAFE_ID_RE.fullmatch(generation_id):
            return False
        return await self._external_mutation(
            self.records.delete_generation, generation_id
        )

    async def delete_generations(self, generation_ids: list[str]) -> dict[str, Any]:
        """Validate all source permissions before deleting any selected record."""
        ids = _validate_generation_selection(generation_ids)
        return await self._external_mutation(self.records.delete_generations, ids)

    async def delete_reference(self, reference_id: str) -> bool:
        """Delete one retained reference without deleting its parent history record."""

        if not _SAFE_ID_RE.fullmatch(reference_id):
            return False
        async with self._lock:
            return await asyncio.to_thread(self.records.delete_reference, reference_id)

    async def export_generations(self, generation_ids: list[str]) -> Path:
        """Build a flat ZIP containing each result and its own metadata JSON."""

        valid_ids = [
            item for item in generation_ids if _SAFE_ID_RE.fullmatch(str(item or ""))
        ][:200]
        if not valid_ids:
            raise ValueError("没有可导出的生成记录")
        return await self._external_mutation(self.records.export_generations, valid_ids)

    async def cleanup_exports(self) -> None:
        """Remove stale export archives after one hour."""

        await asyncio.to_thread(self.records.cleanup_exports)

    async def retention_status(self, history: HistorySettings) -> dict[str, Any]:
        """Return global quota accounting and the same candidates used by cleanup."""

        async with self._lock:
            return await asyncio.to_thread(self.maintenance.retention_status, history)

    async def gallery_retention_status(
        self, history: HistorySettings
    ) -> dict[str, Any]:
        """Briefly reuse display-only quota accounting across gallery requests."""
        async with self._retention_cache_lock:
            revision = await self.gallery_revision()
            now = time.time()
            key = (history, revision)
            cached = self._retention_cache
            if cached is not None and cached[0] == key and cached[1] <= now < cached[2]:
                return copy.deepcopy(cached[3])
            async with self._lock:
                status, started_at, expires_at, revision = await asyncio.to_thread(
                    self.maintenance.gallery_retention_snapshot, history
                )
            if revision:
                self._retention_cache = (
                    (history, revision),
                    started_at,
                    expires_at,
                    status,
                )
            else:
                self._retention_cache = None
            return copy.deepcopy(status)

    def _connect(self) -> sqlite3.Connection:
        return self._context.connect()
