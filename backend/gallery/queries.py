"""Gallery filters, ordered manifests, and lazy per-image projections."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..media.display import display_file_version
from ..media.files import (
    _atomic_write,
    _is_within,
)
from ..media.images import (
    _image_suffix,
    _path_data_url,
    detect_mime_type,
    export_image_filename,
    image_data_url,
)
from .constants import (
    _SAFE_ID_RE,
    _THUMBNAIL_REVISION_SQL,
)
from .context import GalleryContext
from .projection import (
    _as_int,
    _canonical_engine,
    _canonical_supplemental,
    _load_json,
    project_import_metadata,
)


@dataclass(frozen=True)
class QueryServices:
    """Explicit cross-repository operations; connections stay with their caller."""

    allowed_actions_for_generation: Callable[..., Any]
    assert_external_action: Callable[..., Any]
    external_display: Callable[..., Any]
    external_generation_enabled: Callable[..., Any]
    gallery_data_url: Callable[..., Any]
    gallery_revision: Callable[..., Any]
    gallery_thumbnail: Callable[..., Any]
    resolve_image_asset: Callable[..., Any]
    report_external_failure: Callable[..., Any]


class GalleryQueries:
    def __init__(self, context: GalleryContext, services: QueryServices) -> None:
        self.context = context
        self.services = services

    @staticmethod
    def gallery_filters(filters: dict[str, Any]) -> tuple[str, list[Any]]:
        where: list[str] = [
            "EXISTS (SELECT 1 FROM generation_images visible_image "
            "JOIN image_assets visible_asset ON visible_asset.id=visible_image.asset_id "
            "WHERE visible_image.generation_id=g.id)",
            "(g.source != 'external' OR EXISTS (SELECT 1 FROM external_records e "
            "JOIN external_sources s ON s.id=e.source_id WHERE e.generation_id=g.id "
            "AND s.enabled=1 AND e.available=1 AND NOT EXISTS (SELECT 1 FROM generation_images local_image "
            "JOIN generations local_generation ON local_generation.id=local_image.generation_id "
            "WHERE local_image.asset_id=e.asset_id AND local_generation.source != 'external')))",
        ]
        args: list[Any] = []
        query = str(filters.get("query") or "").strip()[:240]
        for key, plural, limit in (
            ("provider_id", "provider_ids", 64),
            ("mode", "modes", 20),
            ("source", "sources", 30),
            ("generation_engine", "generation_engines", 80),
        ):
            if plural in filters:
                values = filters[plural]
                if isinstance(values, str):
                    if len(values) > 32768:
                        raise ValueError(f"{plural} 筛选内容过长")
                    try:
                        values = json.loads(values)
                    except (ValueError, RecursionError) as exc:
                        raise ValueError(f"{plural} 必须是 JSON 字符串数组") from exc
                if not isinstance(values, list) or len(values) > 256:
                    raise ValueError(f"{plural} 必须是最多 256 项的字符串数组")
                if any(
                    not isinstance(value, str) or len(value) > limit for value in values
                ):
                    raise ValueError(f"{plural} 每项必须是最多 {limit} 字符的字符串")
                values = list(dict.fromkeys(value.strip() for value in values))
            else:
                value = str(filters.get(key) or "").strip()[:limit]
                if (
                    not value
                    or key == "mode"
                    and value not in {"text2img", "img2img", "unknown"}
                ):
                    continue
                values = [value]
            if not values:
                where.append("0 = 1")
                continue
            column = f"trim(g.{key})"
            if key == "generation_engine":
                values = list(
                    dict.fromkeys(_canonical_engine(value) for value in values)
                )
                column = (
                    "CASE WHEN lower(trim(g.generation_engine)) IN ('nai', 'novelai') "
                    "THEN 'novelai' ELSE COALESCE(NULLIF(trim(g.generation_engine), ''), 'unknown') END"
                )
            elif key in {"mode", "source"}:
                values = list(dict.fromkeys(value or "unknown" for value in values))
                column = f"COALESCE(NULLIF(trim(g.{key}), ''), 'unknown')"
            where.append(f"{column} IN ({','.join('?' for _ in values)})")
            args.extend(values)
        if filters.get("_merge_target_engine"):
            where.append(
                "EXISTS (SELECT 1 FROM generation_images selected WHERE selected.generation_id = g.id) "
                "AND NOT EXISTS (SELECT 1 FROM generation_images selected WHERE selected.generation_id = g.id "
                "AND (CASE lower(trim(COALESCE(json_extract(selected.supplemental_json, '$.generation_engine'), g.generation_engine))) "
                "WHEN 'nai' THEN 'novelai' WHEN 'novelai' THEN 'novelai' "
                "ELSE trim(COALESCE(json_extract(selected.supplemental_json, '$.generation_engine'), g.generation_engine)) END) != ?)"
            )
            args.append(filters["_merge_target_engine"])
        if filters.get("favorite") in (True, 1, "1", "true"):
            where.append("g.is_favorite = 1")
        if query:
            where.append(
                "(g.original_prompt LIKE ? OR g.final_prompt LIKE ? OR g.provider_name LIKE ? OR g.model LIKE ? "
                "OR g.platform_name LIKE ? OR g.platform_id LIKE ? OR g.group_id LIKE ? "
                "OR g.group_name LIKE ? OR g.user_id LIKE ? OR g.user_name LIKE ? "
                "OR g.search_text LIKE ? OR g.generation_engine LIKE ? "
                "OR g.context_type LIKE ? "
                "OR (CASE g.context_type WHEN 'group' THEN '群聊' WHEN 'private' THEN '私聊' ELSE '' END) LIKE ? "
                "OR (CASE lower(g.platform_name) WHEN 'aiocqhttp' THEN 'OneBot v11 OneBotV11' "
                "WHEN 'qq_official' THEN 'QQ 官方 QQ官方' ELSE '' END) LIKE ?)"
            )
            token = f"%{query}%"
            args.extend([token] * 15)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        return clause, args

    @staticmethod
    def gallery_order(filters: dict[str, Any]) -> tuple[str, str]:
        sort = filters.get("sort", "created")
        if sort not in ("created", "latest_content"):
            raise ValueError("画廊排序方式必须是 created 或 latest_content")
        if sort == "created":
            return "", "g.created_at"
        # Calculate each group's image time once, including for the flat image
        # cursor. Asset creation times cannot be used: content hashes are shared
        # across records. External records already use their selected source time.
        image_time = "json_extract(latest_image.supplemental_json, '$.generated_at')"
        join = (
            "LEFT JOIN (SELECT latest_image.generation_id, MAX(CASE "
            "WHEN latest_generation.source = 'external' THEN latest_generation.created_at "
            "WHEN json_type(latest_image.supplemental_json, '$.generated_at') IN ('integer', 'real') "
            f"AND {image_time} >= 0 AND {image_time} < 253402300800 THEN {image_time} "
            "ELSE latest_generation.created_at END) AS sort_time "
            "FROM generation_images latest_image JOIN generations latest_generation "
            "ON latest_generation.id=latest_image.generation_id "
            "GROUP BY latest_image.generation_id) gallery_order ON gallery_order.generation_id=g.id "
        )
        return join, "COALESCE(gallery_order.sort_time, g.created_at)"

    @staticmethod
    def gallery_facet_order(value: str) -> tuple[bool, str]:
        return (
            value.strip().lower()
            in {"", "unknown", "unspecified", "未知", "未指定", "未记录"},
            value.casefold(),
        )

    def gallery_facets(self, conn: sqlite3.Connection) -> dict[str, Any]:
        # Facets describe the entire visible gallery, not the current selection.
        # Disabled/duplicate external entries and image-less records must not
        # keep otherwise empty options alive.
        clause, args = self.gallery_filters({})
        rows = conn.execute(
            "SELECT DISTINCT g.provider_id, g.provider_name, g.mode, g.source, g.generation_engine "
            f"FROM generations g {clause}",
            args,
        ).fetchall()
        providers: dict[str, str] = {}
        modes, sources, engines = set(), set(), set()
        for row in rows:
            provider_id = str(row["provider_id"] or "").strip()
            name = str(row["provider_name"] or "").strip() if provider_id else "未指定"
            providers[provider_id] = max(providers.get(provider_id, ""), name)
            modes.add(str(row["mode"] or "").strip() or "unknown")
            sources.add(str(row["source"] or "").strip() or "unknown")
            engines.add(_canonical_engine(row["generation_engine"]))
        return {
            "providers": [
                {"id": provider_id, "name": name or provider_id}
                for provider_id, name in sorted(
                    providers.items(),
                    key=lambda item: (
                        self.gallery_facet_order(item[0])[0]
                        or self.gallery_facet_order(item[1])[0],
                        (item[1] or item[0]).casefold(),
                        item[0],
                    ),
                )
            ],
            "modes": sorted(modes, key=self.gallery_facet_order),
            "sources": sorted(sources, key=self.gallery_facet_order),
            "generation_engines": sorted(engines, key=self.gallery_facet_order),
        }

    def list_generations(self, filters: dict[str, Any]) -> dict[str, Any]:
        revision = self.services.gallery_revision()
        light = filters.get("light") is True
        limit = max(1, min(60, _as_int(filters.get("limit"), 24)))
        offset = max(0, _as_int(filters.get("offset"), 0))
        clause, args = self.gallery_filters(filters)
        order_join, sort_time = self.gallery_order(filters)
        with self.context.connect() as conn:
            total = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM generations g {clause}", args
                ).fetchone()[0]
            )
            rows = conn.execute(
                f"SELECT g.id, g.created_at, {sort_time} AS sort_time, g.source, g.mode, g.provider_id, g.provider_name, "
                f"g.model, g.original_prompt, g.elapsed_ms, g.context_type, g.platform_name, "
                f"g.platform_id, g.group_id, g.group_name, g.user_id, g.user_name, "
                f"g.is_favorite, g.cleanup_protected_until, g.generation_engine, g.generated_at, "
                f"i.id AS image_id, t.path AS thumbnail_path, "
                f"a.id AS sha256, {_THUMBNAIL_REVISION_SQL} AS thumbnail_revision, "
                f"t.mime_type AS thumbnail_mime_type, a.size_bytes, a.mime_type, a.file_state, "
                f"(SELECT COUNT(*) FROM generation_images counted WHERE counted.generation_id = g.id) AS image_count "
                f"FROM generations g JOIN generation_images i ON i.generation_id = g.id "
                f"AND i.id = (SELECT cover.id FROM generation_images cover WHERE cover.generation_id = g.id ORDER BY cover.ordinal, cover.id LIMIT 1) "
                f"JOIN image_assets a ON a.id = i.asset_id "
                f"LEFT JOIN image_thumbnails t ON t.asset_id = a.id "
                f"{order_join}{clause} ORDER BY sort_time DESC, g.created_at DESC, g.id DESC LIMIT ? OFFSET ?",
                [*args, limit, offset],
            ).fetchall()
            facets = self.gallery_facets(conn)
        items = [
            self.gallery_item(dict(row), include_thumbnail=not light) for row in rows
        ]
        return {
            **({"lightweight": True} if light else {}),
            "items": items,
            "revision": revision,
            "total": total,
            "offset": offset,
            "limit": limit,
            "filters": facets,
        }

    def generation_manifest(self, generation_id: str) -> dict[str, Any] | None:
        if not _SAFE_ID_RE.fullmatch(generation_id):
            return None
        if not self.services.external_generation_enabled(generation_id):
            return None
        with self.context.connect() as conn:
            conn.execute("BEGIN")
            record = conn.execute(
                "SELECT id, created_at, source, status, mode, provider_id, provider_name, "
                "provider_kind, model, elapsed_ms, error_message, is_favorite, cleanup_protected_until, "
                "generation_engine, generated_at, context_type, platform_name, platform_id, "
                "group_id, group_name, user_id, user_name FROM generations WHERE id = ?",
                (generation_id,),
            ).fetchone()
            if record is None:
                return None
            rows = conn.execute(
                "SELECT i.id, i.generation_id, i.ordinal, a.mime_type, a.size_bytes, "
                "a.id AS sha256, a.width, a.height, a.file_state, a.path, "
                f"{_THUMBNAIL_REVISION_SQL} AS thumbnail_revision, "
                "json_extract(i.supplemental_json, '$.model') AS model, "
                "json_extract(i.supplemental_json, '$.mode') AS mode "
                "FROM generation_images i JOIN image_assets a ON a.id = i.asset_id "
                "LEFT JOIN image_thumbnails t ON t.asset_id = a.id "
                "WHERE i.generation_id = ? ORDER BY i.ordinal, i.id",
                (generation_id,),
            ).fetchall()
            references = conn.execute(
                "SELECT r.id, r.filename, r.mime_type, r.size_bytes, r.available, r.deleted_at, "
                "a.file_state FROM generation_references r LEFT JOIN image_assets a ON a.id = r.asset_id "
                "WHERE r.generation_id = ? ORDER BY r.ordinal, r.id",
                (generation_id,),
            ).fetchall()
        result = self.generation_header(dict(record))
        result["lightweight"] = True
        result["images"] = []
        used_stems: set[str] = set()
        for index, row in enumerate(rows, start=1):
            item = dict(row)
            item["allowed_actions"] = dict(result["allowed_actions"])
            for key in ("model", "mode"):
                if item[key] is None:
                    item[key] = result[key]
            item["download_filename"] = export_image_filename(
                {**result, **item},
                image_index=index,
                image_count=len(rows),
                mime_type=item["mime_type"],
                suffix=Path(item.pop("path")).suffix,
                used_stems=used_stems,
            )
            result["images"].append(item)
        result["references"] = [
            {
                **dict(row),
                "available": bool(row["available"])
                and row["file_state"] == "available",
            }
            for row in references
        ]
        return result

    def generation_header(self, result: dict[str, Any]) -> dict[str, Any]:
        result.update(
            self.services.external_display(
                str(result.get("id") or ""), result.get("source")
            )
        )
        if "parameters_json" in result:
            result["parameters"] = _load_json(result.pop("parameters_json"))
        if "supplemental_json" in result:
            result["supplemental"] = _canonical_supplemental(
                _load_json(result.pop("supplemental_json"))
            )
        result["generation_engine"] = _canonical_engine(result["generation_engine"])
        result["is_favorite"] = bool(result["is_favorite"])
        result.pop("search_text", None)
        result.pop("import_key", None)
        result["invocation_source"] = {
            key: str(result.pop(key, "") or "")
            for key in (
                "context_type",
                "platform_name",
                "platform_id",
                "group_id",
                "group_name",
                "user_id",
                "user_name",
            )
        }
        return result

    def gallery_image_info(
        self,
        image_id: str,
        include_preview: bool = True,
        connection: sqlite3.Connection | None = None,
        *,
        include_group_manifest: bool = False,
    ) -> dict[str, Any] | None:
        with (
            nullcontext(connection)
            if connection is not None
            else self.context.connect() as conn
        ):
            if connection is None:
                conn.execute("BEGIN")
            row = conn.execute(
                "SELECT i.*, a.path, a.mime_type, a.size_bytes, a.id AS sha256, "
                "a.width, a.height, a.file_state, m.metadata_json, "
                "t.path AS thumbnail_path, t.mime_type AS thumbnail_mime_type, "
                f"{_THUMBNAIL_REVISION_SQL} AS thumbnail_revision "
                "FROM generation_images i JOIN image_assets a ON a.id = i.asset_id "
                "LEFT JOIN image_metadata m ON m.asset_id = i.asset_id "
                "LEFT JOIN image_thumbnails t ON t.asset_id = i.asset_id WHERE i.id = ?",
                (image_id,),
            ).fetchone()
            if row is None:
                return None
            if not self.services.external_generation_enabled(row["generation_id"]):
                return None
            record = conn.execute(
                "SELECT id, created_at, source, status, mode, provider_id, provider_name, "
                "provider_kind, model, original_prompt, final_prompt, parameters_json, "
                "supplemental_json, elapsed_ms, error_message, is_favorite, "
                "cleanup_protected_until, generation_engine, generated_at, context_type, "
                "platform_name, platform_id, group_id, group_name, user_id, user_name "
                "FROM generations WHERE id = ?",
                (row["generation_id"],),
            ).fetchone()
            position = conn.execute(
                "SELECT COUNT(*) AS image_count, COALESCE(SUM(ordinal < ?), 0) + 1 AS image_index "
                "FROM generation_images WHERE generation_id = ?",
                (row["ordinal"], row["generation_id"]),
            ).fetchone()
        detail = self.generation_header(dict(record))
        if not include_group_manifest:
            detail["supplemental"].pop("group_manifest", None)
        image = self.asset_item(
            dict(row), preview_full=False, include_preview=include_preview
        )
        if detail["source"] == "import" and not image["supplemental"]:
            image["supplemental"] = detail["supplemental"]
        image["generation_id"] = row["generation_id"]
        image["allowed_actions"] = dict(detail["allowed_actions"])
        image["ordinal"] = row["ordinal"]
        image["download_filename"] = export_image_filename(
            {
                **detail,
                **{
                    key: image["supplemental"].get(key, detail[key])
                    for key in ("mode", "model")
                },
            },
            image_index=position["image_index"],
            image_count=position["image_count"],
            mime_type=image["mime_type"],
            suffix=Path(image["path"]).suffix,
            used_stems=set(),
        )
        return {"image": image, "detail_fields": detail}

    def generation_image_context(
        self, generation_id: str, image_id: str
    ) -> dict[str, Any] | None:
        with self.context.connect() as conn:
            conn.execute("BEGIN")
            if (
                conn.execute(
                    "SELECT 1 FROM generations WHERE id = ?", (generation_id,)
                ).fetchone()
                is None
            ):
                return None
            selected = conn.execute(
                "SELECT id FROM generation_images WHERE generation_id = ? "
                + ("AND id = ? " if image_id else "")
                + "ORDER BY ordinal, id LIMIT 1",
                (generation_id, image_id) if image_id else (generation_id,),
            ).fetchone()
            if selected is None:
                raise ValueError("所选图片不属于当前生成记录或已被删除")
            payload = self.gallery_image_info(
                str(selected["id"]), False, conn, include_group_manifest=True
            )
            if payload is None:
                raise ValueError("所选图片不属于当前生成记录或已被删除")
            references = conn.execute(
                "SELECT r.*, a.path, a.file_state FROM generation_references r "
                "LEFT JOIN image_assets a ON a.id = r.asset_id "
                "WHERE r.generation_id = ? ORDER BY r.ordinal, r.id",
                (generation_id,),
            ).fetchall()
        return {
            **payload["detail_fields"],
            "images": [payload["image"]],
            "references": [
                self.reference_item(dict(row), include_data=False) for row in references
            ],
        }

    def gallery_reference_image(self, reference_id: str) -> dict[str, Any] | None:
        with self.context.connect() as conn:
            row = conn.execute(
                "SELECT r.*, a.path, a.file_state, t.path AS thumbnail_path, "
                "t.mime_type AS thumbnail_mime_type FROM generation_references r "
                "LEFT JOIN image_assets a ON a.id = r.asset_id "
                "LEFT JOIN image_thumbnails t ON t.asset_id = r.asset_id WHERE r.id = ?",
                (reference_id,),
            ).fetchone()
        return self.reference_item(dict(row)) if row is not None else None

    def gallery_image_sequence(self, filters: dict[str, Any]) -> list[dict[str, Any]]:
        clause, args = self.gallery_filters(filters)
        order_join, sort_time = self.gallery_order(filters)
        with self.context.connect() as conn:
            rows = conn.execute(
                f"SELECT g.id AS generation_id, g.created_at, {sort_time} AS sort_time, g.source, "
                "COALESCE(json_extract(i.supplemental_json, '$.mode'), g.mode) AS mode, g.provider_id, "
                "COALESCE(json_extract(i.supplemental_json, '$.model'), g.model) AS model, "
                "i.id AS image_id, i.ordinal, a.mime_type, a.size_bytes, "
                f"a.id AS sha256, {_THUMBNAIL_REVISION_SQL} AS thumbnail_revision, "
                "a.width, a.height, a.path, a.file_state, "
                "COUNT(*) OVER (PARTITION BY g.id) AS image_count "
                "FROM generations g JOIN generation_images i ON i.generation_id = g.id "
                "JOIN image_assets a ON a.id = i.asset_id "
                "LEFT JOIN image_thumbnails t ON t.asset_id = a.id "
                f"{order_join}{clause} ORDER BY sort_time DESC, g.created_at DESC, g.id DESC, i.ordinal ASC, i.id ASC",
                args,
            ).fetchall()
        used_stems: dict[str, set[str]] = {}
        generation_positions: dict[str, int] = {}
        image_positions: dict[str, int] = {}
        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            generation_id = str(item["generation_id"])
            item.update(
                self.services.external_display(generation_id, item.pop("source"))
            )
            if generation_id not in generation_positions:
                generation_positions[generation_id] = len(generation_positions)
            used = used_stems.setdefault(generation_id, set())
            position = image_positions.get(generation_id, 0)
            image_positions[generation_id] = position + 1
            item["download_filename"] = export_image_filename(
                item,
                image_index=position + 1,
                image_count=int(item["image_count"]),
                mime_type=str(item.get("mime_type") or ""),
                suffix=Path(str(item.pop("path", ""))).suffix,
                used_stems=used,
            )
            item.pop("ordinal")
            item["image_index"] = position
            item["generation_position"] = generation_positions[generation_id]
            item["width"] = max(1, int(item.get("width") or 1))
            item["height"] = max(1, int(item.get("height") or 1))
            items.append(item)
        return items

    def gallery_image_file(
        self, image_id: str, action: str = "download"
    ) -> tuple[Path, str, str] | None:
        with self.context.connect() as conn:
            row = conn.execute(
                "SELECT a.path, a.mime_type, i.generation_id, g.created_at, g.provider_id, "
                "COALESCE(json_extract(i.supplemental_json, '$.model'), g.model) AS model, "
                "COALESCE(json_extract(i.supplemental_json, '$.mode'), g.mode) AS mode, "
                "(SELECT COUNT(*) FROM generation_images n WHERE n.generation_id = i.generation_id) AS image_count, "
                "(SELECT COUNT(*) + 1 FROM generation_images n WHERE n.generation_id = i.generation_id "
                "AND n.ordinal < i.ordinal) AS image_index "
                "FROM generation_images i JOIN generations g ON g.id = i.generation_id "
                "JOIN image_assets a ON a.id = i.asset_id WHERE i.id = ?",
                (image_id,),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        if not self.services.external_generation_enabled(str(item["generation_id"])):
            return None
        self.services.assert_external_action([str(item["generation_id"])], action)
        path = self.services.resolve_image_asset(item)
        if path is None:
            return None
        return (
            path,
            str(item["mime_type"]),
            export_image_filename(
                item,
                image_index=item["image_index"],
                image_count=item["image_count"],
                mime_type=item["mime_type"],
                suffix=path.suffix,
                used_stems=set(),
            ),
        )

    def gallery_image_source(self, image_id: str) -> dict[str, Any] | None:
        with self.context.connect() as conn:
            row = conn.execute(
                "SELECT i.id AS image_id, i.generation_id, i.ordinal, a.path, "
                "(SELECT COUNT(*) FROM generation_images previous WHERE previous.generation_id = i.generation_id "
                "AND previous.ordinal < i.ordinal) AS image_index, "
                "a.mime_type, a.size_bytes, a.width, a.height, t.path AS thumbnail_path, "
                f"a.id AS sha256, {_THUMBNAIL_REVISION_SQL} AS thumbnail_revision, "
                "t.mime_type AS thumbnail_mime_type, t.size_bytes AS thumbnail_size_bytes "
                "FROM generation_images i JOIN image_assets a ON a.id = i.asset_id "
                "LEFT JOIN image_thumbnails t ON t.asset_id = a.id WHERE i.id = ?",
                (image_id,),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        if not self.services.external_generation_enabled(item["generation_id"]):
            return None
        return item

    def gallery_display_source(
        self, image_id: str
    ) -> tuple[dict[str, Any], Path, tuple[int, ...]] | None:
        """Recheck browsing access and actual file readability on every cache use."""
        item = self.gallery_image_source(image_id)
        if item is None:
            return None
        path = self.services.resolve_image_asset(item)
        if path is None:
            return None
        try:
            with path.open("rb") as stream:
                version = display_file_version(os.fstat(stream.fileno()))
        except OSError as exc:
            self.services.report_external_failure(str(item["generation_id"]), exc)
            return None
        return item, path, version

    def gallery_image_data(self, image_id: str, detail: str) -> dict[str, Any] | None:
        item = self.gallery_image_source(image_id)
        if item is None:
            return None
        original = self.services.resolve_image_asset(item)
        thumbnail = self.context.data_dir / str(item["thumbnail_path"] or "")
        use_original = detail == "original"
        path = original if use_original else thumbnail
        mime_type = str(
            item["mime_type"]
            if use_original
            else item["thumbnail_mime_type"] or "image/webp"
        )
        size_bytes = int(
            item["size_bytes"] if use_original else item["thumbnail_size_bytes"] or 0
        )
        if path is None:
            return None
        data_url = (
            self.services.gallery_data_url(path, mime_type, str(item["generation_id"]))
            if use_original
            else self.services.gallery_thumbnail(item, str(item["generation_id"]))
        )
        if not data_url or (
            not use_original and not _is_within(path, self.context.thumbnails_dir)
        ):
            return None
        return {
            "image_id": str(item["image_id"]),
            "allowed_actions": self.services.allowed_actions_for_generation(
                str(item["generation_id"])
            ),
            "sha256": str(item["sha256"]),
            "thumbnail_revision": str(item["thumbnail_revision"]),
            "generation_id": str(item["generation_id"]),
            "image_index": int(item["image_index"]),
            "mime_type": mime_type,
            "size_bytes": size_bytes,
            "width": max(1, int(item.get("width") or 1)),
            "height": max(1, int(item.get("height") or 1)),
            "data_url": data_url,
        }

    def stage_generation_references(
        self, generation_id: str, strict: bool = False
    ) -> list[dict[str, Any]]:
        with self.context.connect() as conn:
            rows = conn.execute(
                "SELECT r.id, r.filename, a.path, a.mime_type, a.width, a.height "
                "FROM generation_references r JOIN image_assets a ON a.id = r.asset_id "
                "WHERE r.generation_id = ? AND r.available = 1 "
                "AND a.file_state = 'available' ORDER BY r.ordinal",
                (generation_id,),
            ).fetchall()
            if strict:
                expected = conn.execute(
                    "SELECT COUNT(*) FROM generation_references WHERE generation_id=?",
                    (generation_id,),
                ).fetchone()[0]
                if expected != len(rows):
                    return []
        if strict and any(
            not (self.context.data_dir / str(row["path"])).is_file()
            or not _is_within(
                self.context.data_dir / str(row["path"]), self.context.assets_dir
            )
            for row in rows
        ):
            return []
        staged: list[dict[str, str]] = []
        for row in rows:
            source = self.context.data_dir / str(row["path"])
            if not source.is_file() or not _is_within(source, self.context.assets_dir):
                continue
            raw = source.read_bytes()
            staging_id = uuid.uuid4().hex
            mime_type = detect_mime_type(raw, str(row["mime_type"]))
            suffix = _image_suffix(mime_type, raw)
            _atomic_write(self.context.staging_dir / f"{staging_id}{suffix}", raw)
            staged.append(
                {
                    "id": staging_id,
                    "filename": str(row["filename"]),
                    "mime_type": mime_type,
                    "width": row["width"],
                    "height": row["height"],
                    "preview_data_url": image_data_url(raw, mime_type),
                }
            )
        return staged

    def generation_detail(
        self, generation_id: str, include_assets: bool = True
    ) -> dict[str, Any] | None:
        if not _SAFE_ID_RE.fullmatch(generation_id):
            return None
        if not self.services.external_generation_enabled(generation_id):
            return None
        with self.context.connect() as conn:
            row = conn.execute(
                "SELECT * FROM generations WHERE id = ?", (generation_id,)
            ).fetchone()
            if row is None:
                return None
            image_rows = conn.execute(
                "SELECT i.*, a.path, a.mime_type, a.size_bytes, a.id AS sha256, "
                "a.width, a.height, a.file_state, m.metadata_json, "
                "t.path AS thumbnail_path, t.mime_type AS thumbnail_mime_type, "
                f"{_THUMBNAIL_REVISION_SQL} AS thumbnail_revision "
                "FROM generation_images i "
                "JOIN image_assets a ON a.id = i.asset_id "
                "LEFT JOIN image_thumbnails t ON t.asset_id = a.id "
                "LEFT JOIN image_metadata m ON m.asset_id = a.id "
                "WHERE i.generation_id = ? ORDER BY i.ordinal",
                (generation_id,),
            ).fetchall()
            reference_rows = conn.execute(
                "SELECT r.*, a.path, a.file_state, t.path AS thumbnail_path, "
                "t.mime_type AS thumbnail_mime_type FROM generation_references r "
                "LEFT JOIN image_assets a ON a.id = r.asset_id "
                "LEFT JOIN image_thumbnails t ON t.asset_id = a.id "
                "WHERE r.generation_id = ? ORDER BY r.ordinal",
                (generation_id,),
            ).fetchall()
        result = self.generation_header(dict(row))
        result["images"] = [
            self.asset_item(dict(item), preview_full=include_assets)
            for item in image_rows
        ]
        used_stems: set[str] = set()
        for image_index, image in enumerate(result["images"], start=1):
            image["allowed_actions"] = dict(result["allowed_actions"])
            if result["source"] == "import" and not image["supplemental"]:
                image["supplemental"] = result["supplemental"]
            image["download_filename"] = export_image_filename(
                {
                    **result,
                    "mode": image["supplemental"].get("mode", result["mode"]),
                    "model": image["supplemental"].get("model", result["model"]),
                },
                image_index=image_index,
                image_count=len(result["images"]),
                mime_type=str(image.get("mime_type") or ""),
                suffix=Path(str(image.get("path") or "")).suffix,
                used_stems=used_stems,
            )
        result["references"] = [
            self.reference_item(dict(item), include_data=include_assets)
            for item in reference_rows
        ]
        return result

    def gallery_item(
        self, row: dict[str, Any], *, include_thumbnail: bool = True
    ) -> dict[str, Any]:
        invocation_source = {
            key: str(row.get(key) or "")
            for key in (
                "context_type",
                "platform_name",
                "platform_id",
                "group_id",
                "group_name",
                "user_id",
                "user_name",
            )
        }
        item = {
            **self.services.external_display(row["id"], row["source"]),
            "id": row["id"],
            "created_at": row["created_at"],
            "sort_time": row["sort_time"],
            "source": row["source"],
            "generation_engine": _canonical_engine(row["generation_engine"]),
            "is_favorite": bool(row["is_favorite"]),
            "cleanup_protected_until": row["cleanup_protected_until"],
            "generated_at": row["generated_at"],
            "file_state": row["file_state"],
            "image_count": row["image_count"],
            "mode": row["mode"],
            "provider_id": row["provider_id"],
            "provider_name": row["provider_name"],
            "model": row["model"],
            "prompt_preview": row["original_prompt"][:180],
            "elapsed_ms": row["elapsed_ms"],
            "image_id": row["image_id"],
            "sha256": row["sha256"],
            "thumbnail_revision": row["thumbnail_revision"],
            "mime_type": row["mime_type"],
            "size_bytes": row["size_bytes"],
            "invocation_source": invocation_source,
        }
        if include_thumbnail:
            item["thumbnail_data_url"] = self.services.gallery_thumbnail(
                row, str(row["id"])
            )
        return item

    def asset_item(
        self, row: dict[str, Any], *, preview_full: bool, include_preview: bool = True
    ) -> dict[str, Any]:
        path = self.services.resolve_image_asset(row) if preview_full else None
        generation_id = str(row.get("generation_id") or "")
        preview = (
            self.services.gallery_data_url(path, row["mime_type"], generation_id)
            if path is not None
            else ""
        )
        supplemental = _canonical_supplemental(
            _load_json(row.get("supplemental_json") or "{}")
        )
        metadata = _load_json(row.get("metadata_json") or "{}")
        try:
            metadata = project_import_metadata(
                metadata, supplemental.get("overrides") or {}
            )
        except ValueError as exc:
            metadata = {
                **metadata,
                "warnings": [*(metadata.get("warnings") or []), str(exc)],
            }
        return {
            "id": row["id"],
            "path": row["path"],
            "mime_type": row["mime_type"],
            "size_bytes": row["size_bytes"],
            "sha256": row["sha256"],
            "thumbnail_revision": row.get("thumbnail_revision", ""),
            "width": row.get("width", 1),
            "height": row.get("height", 1),
            "file_state": row.get("file_state", "available"),
            "metadata": metadata,
            "supplemental": supplemental,
            "data_url": preview,
            # Keep summaries lightweight while allowing the carousel to render
            # every result before original assets arrive.
            "thumbnail_data_url": (
                self.services.gallery_thumbnail(row, generation_id)
                if not preview_full and include_preview
                else ""
            ),
        }

    def reference_item(
        self, row: dict[str, Any], *, include_data: bool = True
    ) -> dict[str, Any]:
        path = self.context.data_dir / str(row.get("path") or "")
        thumbnail = self.context.data_dir / str(row.get("thumbnail_path") or "")
        available = (
            bool(row["available"])
            and row.get("file_state") != "unavailable"
            and path.is_file()
            and _is_within(path, self.context.assets_dir)
        )
        return {
            "id": row["id"],
            "filename": row["filename"],
            "mime_type": row["mime_type"],
            "size_bytes": row["size_bytes"],
            "available": available,
            "deleted_at": row["deleted_at"],
            "data_url": (
                _path_data_url(
                    thumbnail if thumbnail.is_file() else path,
                    str(row.get("thumbnail_mime_type") or row["mime_type"]),
                )
                if available and include_data
                else ""
            ),
        }
