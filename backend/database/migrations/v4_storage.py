"""Frozen v3 -> 4-dev.1 body conversion, independent of live repositories.

The SQL layout and transaction belong to database.schema. This module describes
the exact v4 storage projection and reference slots; later repository/parser
changes must not alter how an old database is upgraded. Future storage formats
need a new migration instead of changing these conversion rules. Only the shared
payload store's existing ``json``/``workflow`` formats are used here.

No image files are accessed, and no metadata is reparsed during this conversion.
Search columns are rebuildable projections, but their initial migration values
are kept stable as well. All original heavy bodies remain in shared payloads.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import sqlite3
from typing import Any

from ..payloads import link_payload, load_payload, release_payloads, store_payload

_STORAGE = "_image_studio_storage"
_NORMALIZED_LIGHT = frozenset(
    {
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
)


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


def _canonical_dump(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _searchable_parameters(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    excluded = {
        "raw",
        "workflow",
        "workflow_json",
        "api_graph",
        "api_graph_json",
        "api_prompt",
        "nodes",
        "links",
        "comfyui",
        "fingerprint",
        "rules_fingerprint",
        "request_fingerprint",
        "sha256",
        "asset_id",
    }
    result = {}
    for key, item in value.items():
        if not isinstance(key, str) or key.startswith("_") or key.lower() in excluded:
            continue
        if isinstance(item, (str, int, float, bool)):
            result[key] = item
        elif isinstance(item, list) and all(
            isinstance(entry, (str, int, float, bool)) for entry in item
        ):
            result[key] = item[:1000]
    return result


def _search_projection(value: Any) -> str:
    values: list[str] = []
    seen: set[str] = set()
    fields = {
        "original_filename",
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
        "noise_schedule",
        "cfg",
        "width",
        "height",
        "size",
        "count",
        "strength",
        "denoise",
        "output_format",
        "artist",
        "style",
        "loras",
    }
    containers = {
        "parameters",
        "overrides",
        "effective_request",
        "display_parameters",
        "external_parameters",
    }

    def add(item: Any) -> None:
        if isinstance(item, (str, int, float, bool)):
            text = str(item).strip()
            if text and text not in seen:
                seen.add(text)
                values.append(text)

    def collect(item: Any, depth: int = 0) -> None:
        if depth > 5:
            return
        if isinstance(item, list):
            for child in item[:1000]:
                collect(child, depth + 1)
        elif isinstance(item, dict):
            for key, child in item.items():
                if key in fields:
                    if isinstance(child, list):
                        for entry in child[:100]:
                            if isinstance(entry, dict):
                                add(entry.get("name"))
                                add(entry.get("model"))
                            else:
                                add(entry)
                    else:
                        add(child)
                elif key in {"parameters", "external_parameters"}:
                    for entry in _searchable_parameters(child).values():
                        if isinstance(entry, list):
                            for scalar in entry:
                                add(scalar)
                        else:
                            add(entry)
                elif key in containers:
                    collect(child, depth + 1)

    collect(value)
    return " ".join(values)[:200000]


def _compact_metadata(conn: sqlite3.Connection, asset_id: str, value: Any) -> dict:
    result = _object(value)
    previous = result.pop(_STORAGE, {})
    if isinstance(previous, dict) and previous.get("raw_payload"):
        result["raw"] = load_payload(conn, result.get("raw"))
    if isinstance(previous, dict) and "normalized" in previous:
        details = load_payload(conn, previous["normalized"])
        if isinstance(details, dict):
            result["normalized"] = {**result.get("normalized", {}), **details}
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
                result["normalized"]["parameters"] = _searchable_parameters(
                    normalized["parameters"]
                )
            internal["normalized"] = store_payload(
                conn, "image_metadata", asset_id, "normalized", details
            )
    if internal:
        result[_STORAGE] = internal
    return result


def _compact_supplemental(
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
            result["display_parameters"] = {
                key: item for key, item in display.items() if key in _NORMALIZED_LIGHT
            }
            internal["derived_display"] = True
        else:
            # Missing provenance never permits discarding old manual values.
            result["display_parameters"] = store_payload(
                conn, owner_table, owner_id, "display_parameters", display
            )
            internal["display_payload"] = True
    result[_STORAGE] = internal
    return result


def _time(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and 0 <= value < 253402300800 else None


def _migrate_generation(conn: sqlite3.Connection, generation_id: str) -> None:
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
        compact = _compact_supplemental(
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
    group = _compact_supplemental(conn, "generations", generation_id, group)
    conn.execute(
        "UPDATE generations SET supplemental_json=?,latest_content_at=?,search_text=? WHERE id=?",
        (
            _dump(group),
            max(times, default=row[1]),
            _search_projection(values),
            generation_id,
        ),
    )


def migrate_gallery_storage(conn: sqlite3.Connection) -> None:
    """Convert gallery bodies with the fixed v4 projection, without parsing images."""
    for table, column in (
        ("image_metadata", "metadata_json"),
        ("generation_images", "supplemental_json"),
        ("generations", "supplemental_json"),
        ("generations", "parameters_json"),
    ):
        for (value,) in conn.execute(f"SELECT {column} FROM {table}"):
            try:
                parsed = json.loads(value)
            except (ValueError, TypeError, RecursionError) as exc:
                raise ValueError(
                    f"{table}.{column} 包含无效 JSON，已停止存储迁移"
                ) from exc
            if not isinstance(parsed, dict):
                raise ValueError(f"{table}.{column} 不是 JSON 对象，已停止存储迁移")
    for row in conn.execute(
        "SELECT asset_id,metadata_json FROM image_metadata"
    ).fetchall():
        compact = _compact_metadata(conn, row[0], row[1])
        conn.execute(
            "UPDATE image_metadata SET metadata_json=? WHERE asset_id=?",
            (_dump(compact), row[0]),
        )
    for row in conn.execute("SELECT id FROM generations").fetchall():
        _migrate_generation(conn, row[0])


def _compact_request(request):
    value = copy.deepcopy(request)
    model = value.get("model")
    if isinstance(model, dict):
        model.pop("comfyui", None)
    return value


def _request_fingerprint(request, references):
    canonical = {
        "request": _compact_request(request),
        "references": [
            [item["sha256"], item["filename"], item["mime_type"]] for item in references
        ],
    }
    return hashlib.sha256(_canonical_dump(canonical).encode()).hexdigest()


def _compact_result(connection, job_id, result):
    value = copy.deepcopy(result)
    execution = {
        key: value.pop(key)
        for key in ("api_graph", "api_graph_json", "workflow", "workflow_json")
        if key in value
    }
    if execution:
        value["_execution_payload"] = store_payload(
            connection, "comfy_jobs", job_id, "execution", execution, kind="workflow"
        )
    elif "_execution_payload" not in value:
        release_payloads(connection, "comfy_jobs", job_id, slot_prefix="execution")
    return value


def _compact_outputs(connection, job_id, outputs):
    release_payloads(connection, "comfy_jobs", job_id, slot_prefix="output:")
    value = copy.deepcopy(outputs)
    for index, item in enumerate(value):
        effective = item.get("effective_parameters") or {}
        if isinstance(effective.get("_comfyui"), dict):
            snapshot = load_payload(connection, effective["_comfyui"])
            effective["_comfyui"] = store_payload(
                connection,
                "comfy_jobs",
                job_id,
                f"output:{index}",
                snapshot,
                kind="workflow",
            )
    return value


def migrate_comfy_storage(connection: sqlite3.Connection) -> None:
    """Convert tasks only when their embedded config agrees with the saved revision."""
    revision_configs = {}
    for row in connection.execute(
        "SELECT id,config_json FROM comfy_workflow_revisions"
    ).fetchall():
        revision_id, encoded = row
        config = load_payload(connection, json.loads(encoded))
        revision_configs[revision_id] = config
        marker = store_payload(
            connection,
            "comfy_workflow_revisions",
            revision_id,
            "config",
            config,
            kind="workflow",
        )
        connection.execute(
            "UPDATE comfy_workflow_revisions SET config_json=? WHERE id=?",
            (_canonical_dump(marker), revision_id),
        )
    for row in connection.execute(
        "SELECT id,revision_id,request_json,input_refs_json,output_refs_json,result_json FROM comfy_jobs"
    ).fetchall():
        job_id, revision_id, request_json, input_json, output_json, result_json = row
        original_request = json.loads(request_json)
        model = original_request.get("model")
        if isinstance(model, dict) and "comfyui" in model:
            if revision_id not in revision_configs or _canonical_dump(
                model["comfyui"]
            ) != _canonical_dump(revision_configs[revision_id]):
                raise ValueError(
                    f"ComfyUI 任务 {job_id} 的工作流快照与修订不一致，已中止存储迁移；原始数据与备份保持完整"
                )
        request = _compact_request(original_request)
        references = json.loads(input_json)
        result = _compact_result(connection, job_id, json.loads(result_json))
        outputs = _compact_outputs(connection, job_id, json.loads(output_json))
        connection.execute(
            "UPDATE comfy_jobs SET request_json=?,output_refs_json=?,result_json=?,"
            "parent_job_id=?,temporary=?,model_name=?,queue_dismissed=?,request_fingerprint=? WHERE id=?",
            (
                _canonical_dump(request),
                _canonical_dump(outputs),
                _canonical_dump(result),
                str(request.get("parent_job_id") or ""),
                int(bool(request.get("temporary"))),
                str((request.get("model") or {}).get("name") or ""),
                int(result.get("queue_dismissed") is True),
                _request_fingerprint(request, references),
                job_id,
            ),
        )
