"""Compact gallery persistence with explicit, lossless payload boundaries.

Only known internal slots contain payload references. User parameter dictionaries
are never recursively interpreted as references. Card/manifest queries can read
the small JSON projections without fetching raw metadata or parser evidence.
"""

from __future__ import annotations

import json
import math
import sqlite3
from typing import Any

from ..database.payloads import (
    link_payload,
    load_payload,
    release_payloads,
    store_payload,
)

_STORAGE = "_image_studio_storage"
_NORMALIZED_LIGHT = {
    "generation_engine",
    "model",
    "models",
    "mode",
    "prompt",
    "negative_prompt",
    "seed",
    "steps",
    "sampler",
    "scheduler",
    "cfg",
    "width",
    "height",
    "size",
    "count",
    "generated_at",
    "file_dimensions",
    "dimensions",
    "strength",
    "denoise",
    "selected_output_node",
    "selected_output_ref",
    "requires_output_selection",
    "output_selection_reason",
    "prompt_status",
    "negative_prompt_status",
    "output_dimensions_status",
    "loras",
    "artist",
    "style",
    "noise_schedule",
    "output_format",
}


def _object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, RecursionError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def encode_metadata(conn: sqlite3.Connection, asset_id: str, value: Any) -> dict:
    from .projection import searchable_parameters

    result = decode_metadata(conn, value)
    release_payloads(conn, "image_metadata", asset_id)
    internal = {}
    if "raw" in result:
        result["raw"] = store_payload(
            conn, "image_metadata", asset_id, "raw", result["raw"]
        )
        internal["raw_payload"] = True
    normalized = result.get("normalized")
    if isinstance(normalized, dict):
        details = {
            key: item
            for key, item in normalized.items()
            if key not in _NORMALIZED_LIGHT
        }
        if details:
            result["normalized"] = {
                key: item
                for key, item in normalized.items()
                if key in _NORMALIZED_LIGHT
            }
            if "parameters" in normalized:
                result["normalized"]["parameters"] = searchable_parameters(
                    normalized["parameters"]
                )
            internal["normalized"] = store_payload(
                conn, "image_metadata", asset_id, "normalized", details
            )
    if internal:
        result[_STORAGE] = internal
    return result


def decode_metadata(conn: sqlite3.Connection, value: Any) -> dict:
    result = _object(value)
    internal = result.pop(_STORAGE, {})
    if isinstance(internal, dict) and internal.get("raw_payload"):
        result["raw"] = load_payload(conn, result.get("raw"))
    if isinstance(internal, dict) and "normalized" in internal:
        details = load_payload(conn, internal["normalized"])
        if isinstance(details, dict):
            result["normalized"] = {**result.get("normalized", {}), **details}
    return result


def encode_supplemental(
    conn: sqlite3.Connection,
    owner_table: str,
    owner_id: str,
    value: Any,
    metadata: dict | None = None,
) -> dict:
    result = _object(value)
    if not result:
        release_payloads(conn, owner_table, owner_id)
        return {}
    internal = result.get(_STORAGE)
    # A persisted value is already compact; preserve its live reference rows.
    if isinstance(internal, dict) and internal.get("version") == 1:
        release_payloads(conn, owner_table, owner_id)
        for key, flag in (
            ("comfyui", "comfyui_payload"),
            ("display_parameters", "display_payload"),
        ):
            if internal.get(flag):
                link_payload(conn, owner_table, owner_id, key, result.get(key))
        return result
    release_payloads(conn, owner_table, owner_id)
    internal = {"version": 1}
    if isinstance(result.get("comfyui"), dict):
        result["comfyui"] = store_payload(
            conn, owner_table, owner_id, "comfyui", result["comfyui"], kind="workflow"
        )
        internal["comfyui_payload"] = True
    display = result.get("display_parameters")
    if isinstance(display, dict):
        if (
            isinstance(result.get("overrides"), dict)
            and metadata
            and int(metadata.get("parser_version", 0)) > 0
            and (
                metadata.get("format") != "comfyui" or metadata.get("rules_fingerprint")
            )
        ):
            # The display is a parser projection plus explicit overrides. Keep
            # its small display fields, reconstruct evidence only on detail reads.
            result["display_parameters"] = {
                key: item for key, item in display.items() if key in _NORMALIZED_LIGHT
            }
            internal["derived_display"] = True
        else:
            # Old records have no reliable manual provenance; retain every value.
            result["display_parameters"] = store_payload(
                conn, owner_table, owner_id, "display_parameters", display
            )
            internal["display_payload"] = True
    result[_STORAGE] = internal
    return result


def decode_supplemental(
    conn: sqlite3.Connection, value: Any, metadata: dict | None = None
) -> dict:
    result = _object(value)
    internal = result.pop(_STORAGE, {})
    if not isinstance(internal, dict):
        return result
    if internal.get("comfyui_payload"):
        result["comfyui"] = load_payload(conn, result.get("comfyui"))
    if internal.get("display_payload"):
        result["display_parameters"] = load_payload(
            conn, result.get("display_parameters")
        )
    if (
        internal.get("derived_display")
        and metadata
        and not (
            metadata.get("normalized", {}).get("requires_output_selection")
            and not result.get("comfy_output_node")
        )
    ):
        from .projection import _import_supplemental

        try:
            projected = _import_supplemental(
                str(result.get("original_filename") or ""),
                result.get("overrides") or {},
                metadata,
                allow_unresolved_output=True,
            )
            result["display_parameters"] = projected["display_parameters"]
        except (TypeError, ValueError):
            # Keep the saved lightweight display if a newer parser cannot
            # reconstruct this old selection; explicit overrides remain intact.
            pass
    image_id = internal.get("group_image")
    if isinstance(image_id, str):
        row = conn.execute(
            "SELECT i.supplemental_json,m.metadata_json FROM generation_images i "
            "LEFT JOIN image_metadata m ON m.asset_id=i.asset_id WHERE i.id=?",
            (image_id,),
        ).fetchone()
        if row is not None:
            first = decode_supplemental(conn, row[0], decode_metadata(conn, row[1]))
            for key in internal.get("group_fields", []):
                if key in first:
                    result[key] = first[key]
    return result


def _time(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and 0 <= value < 253402300800 else None


def refresh_gallery_projection(conn: sqlite3.Connection, generation_id: str) -> None:
    """Compact changed rows and update search/sort projections in one transaction."""
    from .projection import _search_projection

    row = conn.execute(
        "SELECT source,created_at,model,mode,parameters_json,supplemental_json "
        "FROM generations WHERE id=?",
        (generation_id,),
    ).fetchone()
    if row is None:
        return
    group = _object(row[5])
    images = conn.execute(
        "SELECT i.id,i.supplemental_json,m.metadata_json FROM generation_images i "
        "LEFT JOIN image_metadata m ON m.asset_id=i.asset_id "
        "WHERE i.generation_id=? ORDER BY i.ordinal,i.id",
        (generation_id,),
    ).fetchall()
    values = [_object(row[4]), group]
    times = []
    first = None
    for image in images:
        supplemental = _object(image[1])
        metadata = _object(image[2])
        compact = encode_supplemental(
            conn, "generation_images", image[0], supplemental, metadata
        )
        generated_at = _time(supplemental.get("generated_at"))
        times.append(
            row[1]
            if row[0] == "external"
            else generated_at
            if generated_at is not None
            else row[1]
        )
        conn.execute(
            "UPDATE generation_images SET supplemental_json=?,generated_at=?,model=?,mode=? WHERE id=?",
            (
                _dump(compact),
                generated_at,
                str(supplemental.get("model", row[2]) or ""),
                str(supplemental.get("mode", row[3]) or ""),
                image[0],
            ),
        )
        values.extend([metadata.get("normalized", {}), compact])
        if first is None:
            first = (image[0], supplemental, compact)
    internal = group.get(_STORAGE, {})
    if first and row[0] in {"import", "external"}:
        # Only share fields proven to originate from the first image. Legacy
        # group-only edits with different values remain losslessly retained.
        keys = [
            key
            for key in ("display_parameters", "overrides", "comfyui")
            if key in group and key in first[1] and group[key] == first[1][key]
        ]
        if isinstance(internal, dict) and internal.get("group_image"):
            keys = list(dict.fromkeys([*internal.get("group_fields", []), *keys]))
        if keys:
            for key in keys:
                group.pop(key, None)
                if isinstance(internal, dict):
                    internal.pop(
                        {
                            "display_parameters": "display_payload",
                            "comfyui": "comfyui_payload",
                        }.get(key, ""),
                        None,
                    )
            group[_STORAGE] = {
                **internal,
                "version": 1,
                "group_image": first[0],
                "group_fields": keys,
            }
            release_payloads(conn, "generations", generation_id)
    group = encode_supplemental(conn, "generations", generation_id, group)
    conn.execute(
        "UPDATE generations SET supplemental_json=?,latest_content_at=?,search_text=? WHERE id=?",
        (
            _dump(group),
            max(times, default=row[1]),
            _search_projection(values),
            generation_id,
        ),
    )
