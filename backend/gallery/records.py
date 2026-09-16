"""Generated records, favorites, explicit deletion, and archive exports."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..media.images import (
    _image_suffix,
    _safe_filename,
    export_image_filename,
)
from ..models import (
    GeneratedImage,
    GenerationRequest,
    ImageProvider,
    parameter_flag,
)
from .constants import (
    _SAFE_ID_RE,
)
from .context import GalleryContext
from .errors import (
    ExternalDeleteError,
)
from .projection import (
    _favorite_summary,
    _redact_sensitive,
    compact_comfy_request,
)


@dataclass(frozen=True)
class RecordServices:
    """Explicit cross-repository operations; connections stay with their caller."""

    assert_external_action: Callable[..., Any]
    cleanup_orphaned_asset_files: Callable[..., Any]
    cleanup_orphaned_thumbnails: Callable[..., Any]
    delete_external_original: Callable[..., Any]
    generation_detail: Callable[..., Any]
    metadata_for_asset: Callable[..., Any]
    prepare_asset: Callable[..., Any]
    prepare_thumbnail: Callable[..., Any]
    purge_unreferenced_assets: Callable[..., Any]
    refresh_search: Callable[..., Any]
    report_external_failure: Callable[..., Any]
    resolve_image_asset: Callable[..., Any]
    save_metadata: Callable[..., Any]
    upsert_thumbnails: Callable[..., Any]


class GenerationRecords:
    def __init__(self, context: GalleryContext, services: RecordServices) -> None:
        self.context = context
        self.services = services

    def record_success(
        self,
        provider: ImageProvider,
        request: GenerationRequest,
        images: tuple[GeneratedImage, ...],
        elapsed_ms: int,
        retain_references: bool,
        record_invocation_identity: bool,
        preview_max_edge: int,
        preview_quality: int,
        batch_failures: tuple[tuple[int, str], ...] = (),
    ) -> str:
        job_id = request.local_parameters.get("_comfy_job_id", "")
        generation_id = (
            job_id
            if provider.kind == "comfyui" and _SAFE_ID_RE.fullmatch(job_id)
            else uuid.uuid4().hex
        )
        if job_id:
            with self.context.connect() as conn:
                if conn.execute(
                    "SELECT 1 FROM generations WHERE id = ?", (generation_id,)
                ).fetchone():
                    return generation_id
        created_at = time.time()
        assets: dict[str, dict[str, Any]] = {}
        thumbnails: dict[str, dict[str, Any]] = {}
        image_rows: list[tuple[str, int, str]] = []
        reference_rows: list[tuple[str, int, str, str, int, int, str]] = []
        metadata: dict[str, dict[str, Any]] = {}
        try:
            for ordinal, image in enumerate(images):
                image_id = uuid.uuid4().hex
                asset = self.services.prepare_asset(image.data, image.mime_type)
                assets[asset["id"]] = asset
                metadata[asset["id"]] = self.services.metadata_for_asset(
                    asset["id"], image.data
                )
                thumbnails[asset["id"]] = self.services.prepare_thumbnail(
                    asset,
                    max_edge=preview_max_edge,
                    quality=preview_quality,
                )
                image_rows.append((image_id, ordinal, asset["id"]))
            if retain_references:
                for ordinal, reference in enumerate(request.references):
                    reference_id = uuid.uuid4().hex
                    asset = self.services.prepare_asset(
                        reference.data, reference.mime_type
                    )
                    assets[asset["id"]] = asset
                    metadata[asset["id"]] = self.services.metadata_for_asset(
                        asset["id"], reference.data
                    )
                    thumbnails[asset["id"]] = self.services.prepare_thumbnail(
                        asset,
                        max_edge=preview_max_edge,
                        quality=preview_quality,
                    )
                    suffix = _image_suffix(asset["mime_type"], reference.data)
                    reference_rows.append(
                        (
                            reference_id,
                            ordinal,
                            _safe_filename(reference.filename) or f"reference{suffix}",
                            asset["mime_type"],
                            asset["size_bytes"],
                            1,
                            asset["id"],
                        )
                    )
            parameters = _redact_sensitive(
                {
                    "negative_prompt": request.negative_prompt,
                    "size": request.size,
                    "count": request.count,
                    "native_batch_size": request.native_batch_size,
                    "max_concurrent_requests": request.max_concurrent_requests,
                    "parameters": {**request.parameters, **request.local_parameters},
                    "selection_source": request.selection_source,
                }
            )
            model = provider.get_model(request.model)
            denied: set[str] = set()
            for name, descriptor in model.parameters.items():
                if not parameter_flag(descriptor, "record_in_history"):
                    denied.update({name, str(descriptor.get("request_key") or name)})
            if "n" in denied:
                denied.add("count")
            parameters["parameters"] = {
                name: value
                for name, value in parameters["parameters"].items()
                if name not in denied
            }
            for name in denied & {"size", "count", "negative_prompt"}:
                parameters.pop(name, None)
            if provider.kind == "comfyui":
                parameters = compact_comfy_request(parameters)
            image_supplementals: dict[str, str] = {}
            for (image_id, _, asset_id), image in zip(image_rows, images):
                if not image.effective_parameters:
                    continue
                effective = dict(image.effective_parameters)
                if "seed" not in effective:
                    actual_seed = (
                        metadata.get(asset_id, {}).get("normalized", {}).get("seed")
                    )
                    if actual_seed is not None:
                        effective["seed"] = actual_seed
                allowed = {"seed", "extra_noise_seed", "size", "count"} | {
                    str(descriptor.get("request_key") or name)
                    for name, descriptor in model.active_parameters.items()
                }
                if provider.kind == "novelai_official":
                    allowed.update({"actual_model", "actual_action"})
                effective = _redact_sensitive(
                    {
                        key: value
                        for key, value in effective.items()
                        if key in allowed
                        and key not in denied
                        and key != "params_version"
                    }
                )
                resolved = {
                    **parameters,
                    "parameters": {
                        **parameters.get("parameters", {}),
                        **{
                            key: value
                            for key, value in effective.items()
                            if key not in {"size", "count", "negative_prompt"}
                        },
                    },
                    **{
                        key: effective[key]
                        for key in ("size", "count", "negative_prompt")
                        if key in effective
                    },
                }
                if provider.kind == "comfyui":
                    resolved = compact_comfy_request(resolved)
                image_supplementals[image_id] = json.dumps(
                    {
                        "effective_request": resolved,
                        "response_index": image.response_index,
                        **(
                            {"comfyui": image.effective_parameters["_comfyui"]}
                            if provider.kind == "comfyui"
                            and "_comfyui" in image.effective_parameters
                            else {}
                        ),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            invocation = (
                request.invocation_source.public_dict()
                if record_invocation_identity
                else {
                    "context_type": "",
                    "platform_name": "",
                    "platform_id": "",
                    "group_id": "",
                    "group_name": "",
                    "user_id": "",
                    "user_name": "",
                }
            )
            with self.context.connect() as conn:
                conn.executemany(
                    "INSERT INTO image_assets (id, path, mime_type, size_bytes, width, height, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                    "path=excluded.path, width=excluded.width, height=excluded.height, file_state='available'",
                    [
                        (
                            asset["id"],
                            asset["path"],
                            asset["mime_type"],
                            asset["size_bytes"],
                            asset["width"],
                            asset["height"],
                            created_at,
                        )
                        for asset in assets.values()
                    ],
                )
                self.services.upsert_thumbnails(conn, thumbnails.values())
                self.services.save_metadata(conn, metadata)
                first_metadata = (
                    metadata.get(image_rows[0][2], {}) if image_rows else {}
                )
                final_prompt = str(
                    first_metadata.get("normalized", {}).get("prompt") or request.prompt
                )
                conn.execute(
                    "INSERT INTO generations (id, created_at, source, status, mode, provider_id, provider_name, "
                    "provider_kind, model, original_prompt, final_prompt, parameters_json, elapsed_ms, "
                    "context_type, platform_name, platform_id, group_id, group_name, user_id, user_name) "
                    "VALUES (?, ?, ?, 'succeeded', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        generation_id,
                        created_at,
                        request.source,
                        request.mode,
                        provider.id,
                        provider.name,
                        provider.kind,
                        request.model or provider.model,
                        request.prompt,
                        final_prompt,
                        json.dumps(
                            parameters, ensure_ascii=False, separators=(",", ":")
                        ),
                        elapsed_ms,
                        invocation["context_type"],
                        invocation["platform_name"],
                        invocation["platform_id"],
                        invocation["group_id"],
                        invocation["group_name"],
                        invocation["user_id"],
                        invocation["user_name"],
                    ),
                )
                conn.executemany(
                    "INSERT INTO generation_images (id, generation_id, ordinal, asset_id, supplemental_json) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [
                        (
                            image_id,
                            generation_id,
                            ordinal,
                            asset_id,
                            image_supplementals.get(image_id, "{}"),
                        )
                        for image_id, ordinal, asset_id in image_rows
                    ],
                )
                if reference_rows:
                    conn.executemany(
                        "INSERT INTO generation_references (id, generation_id, ordinal, filename, mime_type, size_bytes, available, asset_id) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        [
                            (reference_id, generation_id, *row)
                            for reference_id, *row in reference_rows
                        ],
                    )
                conn.execute(
                    "UPDATE generations SET generation_engine = ? WHERE id = ?",
                    (
                        "novelai"
                        if provider.kind in {"nai_direct", "novelai_official"}
                        else provider.kind,
                        generation_id,
                    ),
                )
                if (
                    batch_failures
                    or request.count > request.native_batch_size
                    or len(images) != request.count
                ):
                    request_sizes = [
                        min(request.native_batch_size, request.count - offset)
                        for offset in range(0, request.count, request.native_batch_size)
                    ]
                    conn.execute(
                        "UPDATE generations SET supplemental_json = ? WHERE id = ?",
                        (
                            json.dumps(
                                {
                                    "batch": {
                                        "requested": request.count,
                                        "succeeded": len(images),
                                        "failed": len(batch_failures),
                                        "request_count": len(request_sizes),
                                        "request_sizes": request_sizes,
                                        "succeeded_requests": len(request_sizes)
                                        - len(batch_failures),
                                        "failed_requests": len(batch_failures),
                                        "returned_images": len(images),
                                        "failures": [
                                            {"index": index, "error": reason}
                                            for index, reason in batch_failures
                                        ],
                                    }
                                },
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                            generation_id,
                        ),
                    )
                self.services.refresh_search(conn, generation_id)
                try:
                    conn.execute(
                        "INSERT INTO generation_search (generation_id, original_prompt, final_prompt, provider_name, model) VALUES (?, ?, ?, ?, ?)",
                        (
                            generation_id,
                            request.prompt,
                            final_prompt,
                            provider.name,
                            request.model or provider.model,
                        ),
                    )
                except sqlite3.OperationalError:
                    pass
            return generation_id
        except Exception:
            self.services.cleanup_orphaned_asset_files()
            self.services.cleanup_orphaned_thumbnails()
            raise

    def set_favorite(self, generation_id: str, favorite: bool) -> dict[str, Any]:
        self.services.assert_external_action([generation_id], "favorite")
        with self.context.connect() as conn:
            row = conn.execute(
                "SELECT is_favorite, cleanup_protected_until, source FROM generations WHERE id = ?",
                (generation_id,),
            ).fetchone()
            if row is None:
                raise ValueError("生成记录不存在")
            protected = float(row["cleanup_protected_until"])
            if favorite:
                protected = 0.0
            elif row["is_favorite"] and row["source"] not in {"import", "external"}:
                protected = time.time() + 24 * 3600
            conn.execute(
                "UPDATE generations SET is_favorite = ?, cleanup_protected_until = ? WHERE id = ?",
                (int(favorite), protected, generation_id),
            )
        return {
            "id": generation_id,
            "is_favorite": favorite,
            "cleanup_protected_until": protected,
        }

    def favorite_status(self, generation_ids: list[str]) -> dict[str, Any]:
        with self.context.connect() as conn:
            return _favorite_summary(self.selected_favorite_rows(conn, generation_ids))

    @staticmethod
    def selected_favorite_rows(
        conn: sqlite3.Connection, generation_ids: list[str]
    ) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in generation_ids)
        rows = {
            str(row["id"]): dict(row)
            for row in conn.execute(
                "SELECT id, is_favorite, cleanup_protected_until, source FROM generations "
                f"WHERE id IN ({placeholders})",
                generation_ids,
            ).fetchall()
        }
        missing = [
            identifier for identifier in generation_ids if identifier not in rows
        ]
        if missing:
            raise ValueError("以下生成记录不存在或已被删除：" + ", ".join(missing))
        return [rows[identifier] for identifier in generation_ids]

    def toggle_favorites(self, generation_ids: list[str]) -> dict[str, Any]:
        self.services.assert_external_action(generation_ids, "favorite")
        with self.context.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = self.selected_favorite_rows(conn, generation_ids)
            favorite = not all(bool(row["is_favorite"]) for row in rows)
            changed_ids: list[str] = []
            protected_until = time.time() + 24 * 3600
            for row in rows:
                if bool(row["is_favorite"]) == favorite:
                    continue
                protected = (
                    protected_until
                    if not favorite and row["source"] not in {"import", "external"}
                    else 0.0
                )
                conn.execute(
                    "UPDATE generations SET is_favorite = ?, cleanup_protected_until = ? WHERE id = ?",
                    (int(favorite), protected, row["id"]),
                )
                row.update(is_favorite=int(favorite), cleanup_protected_until=protected)
                changed_ids.append(str(row["id"]))
        return {
            **_favorite_summary(rows),
            "action": "favorite" if favorite else "unfavorite",
            "changed_ids": changed_ids,
        }

    def delete_images(self, generation_id: str, image_ids: list[str]) -> dict[str, Any]:
        with self.context.connect() as conn:
            rows = conn.execute(
                "SELECT id, asset_id FROM generation_images WHERE generation_id = ?",
                (generation_id,),
            ).fetchall()
            owned = {str(row["id"]): str(row["asset_id"]) for row in rows}
            invalid = [item for item in image_ids if item not in owned]
            if invalid:
                raise ValueError(
                    "以下图片不存在或不属于本次生成：" + ", ".join(invalid)
                )
            external_error = self.services.delete_external_original(generation_id)
            asset_ids = [owned[item] for item in image_ids]
            conn.executemany(
                "DELETE FROM generation_images WHERE id = ? AND generation_id = ?",
                [(item, generation_id) for item in image_ids],
            )
            remaining = len(owned) - len(image_ids)
            if remaining == 0:
                asset_ids.extend(
                    str(row[0])
                    for row in conn.execute(
                        "SELECT asset_id FROM generation_references WHERE generation_id = ? AND asset_id IS NOT NULL",
                        (generation_id,),
                    ).fetchall()
                )
                conn.execute("DELETE FROM generations WHERE id = ?", (generation_id,))
                try:
                    conn.execute(
                        "DELETE FROM generation_search WHERE generation_id = ?",
                        (generation_id,),
                    )
                except sqlite3.OperationalError:
                    pass
            else:
                self.services.refresh_search(conn, generation_id)
        self.services.purge_unreferenced_assets(asset_ids)
        if external_error is not None:
            raise external_error
        return {
            "deleted": image_ids,
            "remaining": remaining,
            "generation_deleted": remaining == 0,
        }

    def delete_generations(self, generation_ids: list[str]) -> dict[str, Any]:
        self.services.assert_external_action(generation_ids, "delete")
        result: dict[str, Any] = {"deleted": [], "failed": [], "errors": []}
        for generation_id in generation_ids:
            try:
                if self.delete_generation(generation_id):
                    result["deleted"].append(generation_id)
                else:
                    result["failed"].append(generation_id)
                    result["errors"].append(
                        {"id": generation_id, "message": "记录不存在或已删除"}
                    )
            except ExternalDeleteError as exc:
                details = exc.as_dict()
                result["deleted" if details["generation_deleted"] else "failed"].append(
                    generation_id
                )
                result["errors"].append({**details, "id": generation_id})
            except (ValueError, OSError) as exc:
                result["failed"].append(generation_id)
                result["errors"].append({"id": generation_id, "message": str(exc)})
        return result

    def delete_generation(self, generation_id: str) -> bool:
        with self.context.connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM generations WHERE id = ?", (generation_id,)
            ).fetchone()[0]
            if not count:
                return False
            external_error = self.services.delete_external_original(generation_id)
            asset_rows = conn.execute(
                "SELECT asset_id FROM generation_images WHERE generation_id = ? "
                "UNION SELECT asset_id FROM generation_references "
                "WHERE generation_id = ? AND asset_id IS NOT NULL",
                (generation_id, generation_id),
            ).fetchall()
            conn.execute("DELETE FROM generations WHERE id = ?", (generation_id,))
            try:
                conn.execute(
                    "DELETE FROM generation_search WHERE generation_id = ?",
                    (generation_id,),
                )
            except sqlite3.OperationalError:
                pass
        self.services.purge_unreferenced_assets(
            [str(row["asset_id"]) for row in asset_rows if row["asset_id"]]
        )
        if external_error is not None:
            raise external_error
        return True

    def delete_reference(self, reference_id: str) -> bool:
        with self.context.connect() as conn:
            row = conn.execute(
                "SELECT asset_id, available FROM generation_references WHERE id = ?",
                (reference_id,),
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                "UPDATE generation_references SET available = 0, deleted_at = ?, asset_id = NULL WHERE id = ?",
                (time.time(), reference_id),
            )
        if row["asset_id"]:
            self.services.purge_unreferenced_assets([str(row["asset_id"])])
        return True

    def export_generations(self, generation_ids: list[str]) -> Path:
        import zipfile

        self.services.assert_external_action(generation_ids, "download")
        stamp = time.strftime("%Y%m%d_%H%M%S")
        target = (
            self.context.exports_dir
            / f"image_studio_{stamp}_{uuid.uuid4().hex[:8]}.zip"
        )
        used_stems: set[str] = set()
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for generation_id in generation_ids:
                detail = self.services.generation_detail(
                    generation_id, include_assets=False
                )
                if not detail:
                    continue
                images = detail["images"]
                for image_index, image in enumerate(images, start=1):
                    path = self.services.resolve_image_asset(
                        {**image, "generation_id": generation_id}
                    )
                    if path is not None:
                        image_filename = export_image_filename(
                            {
                                **detail,
                                "mode": image.get("supplemental", {}).get(
                                    "mode", detail["mode"]
                                ),
                                "model": image.get("supplemental", {}).get(
                                    "model", detail["model"]
                                ),
                            },
                            image_index=image_index,
                            image_count=len(images),
                            mime_type=image["mime_type"],
                            suffix=path.suffix,
                            used_stems=used_stems,
                        )
                        stem = Path(image_filename).stem
                        try:
                            archive.write(path, arcname=image_filename)
                        except FileNotFoundError as exc:
                            self.services.report_external_failure(generation_id, exc)
                            raise
                        metadata = {
                            key: value
                            for key, value in detail.items()
                            if key not in {"images", "references"}
                        }
                        supplemental = image.get("supplemental") or {}
                        if detail["source"] == "import" and supplemental:
                            metadata.update(
                                {
                                    "model": supplemental.get("model", detail["model"]),
                                    "mode": supplemental.get("mode", detail["mode"]),
                                    "generation_engine": supplemental.get(
                                        "generation_engine", detail["generation_engine"]
                                    ),
                                    "original_prompt": supplemental.get(
                                        "prompt", detail["original_prompt"]
                                    ),
                                    "final_prompt": image.get("metadata", {})
                                    .get("normalized", {})
                                    .get("prompt", supplemental.get("prompt", "")),
                                    "generated_at": supplemental.get("generated_at"),
                                    "supplemental": supplemental,
                                }
                            )
                        metadata["image"] = {
                            "id": image["id"],
                            "filename": image_filename,
                            "mime_type": image["mime_type"],
                            "size_bytes": image["size_bytes"],
                            "sha256": image["sha256"],
                            "metadata": image.get("metadata", {}),
                            "width": image.get("width"),
                            "height": image.get("height"),
                            "supplemental": supplemental,
                        }
                        archive.writestr(
                            f"{stem}.json",
                            json.dumps(metadata, ensure_ascii=False, indent=2),
                        )
        return target

    def cleanup_exports(self) -> None:
        cutoff = time.time() - 3600
        for path in self.context.exports_dir.glob("*.zip"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue
