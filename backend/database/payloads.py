"""Content-addressed JSON bodies, independently referenced by their owners.

Large bodies are losslessly encoded here, never in list/queue projection columns.
Only repository code at an explicit storage boundary resolves a payload marker.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
import time
import zlib
from typing import Any

PAYLOAD_KEY = "__image_studio_payload__"
MAX_PAYLOAD_BYTES = 64 * 1024 * 1024
PAYLOAD_OWNERS = {
    "comfy_workflow_revisions": "id",
    "comfy_jobs": "id",
    "image_metadata": "asset_id",
    "generation_images": "id",
    "generations": "id",
}
_DIGEST = re.compile(r"[a-f0-9]{64}")


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _encode(value: Any, kind: str) -> bytes:
    if kind == "json":
        return _json_bytes(value)
    if kind != "workflow" or not isinstance(value, dict):
        raise ValueError("未知的存储正文类型")
    compact = copy.deepcopy(value)
    restored = []
    for field in ("api_graph", "workflow"):
        source = value.get(field + "_json")
        if field not in value or not isinstance(source, str):
            continue
        try:
            parsed = json.loads(source)
            matches = _json_bytes(parsed) == _json_bytes(value[field])
        except (ValueError, TypeError, RecursionError):
            matches = False
        if matches:
            compact.pop(field)
            restored.append(field)
    # Keep the precise source string, including uint64 seeds and formatting.
    return _json_bytes({"value": compact, "restore_objects": restored})


def _decode(raw: bytes, kind: str) -> Any:
    value = json.loads(raw)
    if kind == "json":
        return value
    if kind != "workflow" or not isinstance(value, dict):
        raise ValueError("未知的存储正文类型")
    result = value["value"]
    for field in value["restore_objects"]:
        if field not in {"api_graph", "workflow"}:
            raise ValueError("工作流正文重建字段无效")
        result[field] = json.loads(result[field + "_json"])
    return result


def is_payload_reference(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {PAYLOAD_KEY}
        and isinstance(value[PAYLOAD_KEY], str)
        and _DIGEST.fullmatch(value[PAYLOAD_KEY]) is not None
    )


def link_payload(
    conn: sqlite3.Connection,
    owner_table: str,
    owner_id: str,
    slot: str,
    marker: dict[str, str],
) -> dict[str, str]:
    """Attach an existing compact value to a new owner without decoding it."""
    if (
        owner_table not in PAYLOAD_OWNERS
        or not owner_id
        or not slot
        or not is_payload_reference(marker)
    ):
        raise ValueError("存储正文引用位置无效")
    identity = marker[PAYLOAD_KEY]
    if (
        conn.execute(
            "SELECT 1 FROM storage_payloads WHERE id=?", (identity,)
        ).fetchone()
        is None
    ):
        raise ValueError("存储正文缺失，无法建立引用")
    conn.execute(
        "INSERT INTO storage_payload_refs(owner_table,owner_id,slot,payload_id) "
        "VALUES (?,?,?,?) ON CONFLICT(owner_table,owner_id,slot) DO UPDATE SET "
        "payload_id=excluded.payload_id",
        (owner_table, str(owner_id), slot, identity),
    )
    return dict(marker)


def store_payload(
    conn: sqlite3.Connection,
    owner_table: str,
    owner_id: str,
    slot: str,
    value: Any,
    kind: str = "json",
) -> dict[str, str]:
    """Write a shared body and attach its owner in the caller's transaction."""
    if owner_table not in PAYLOAD_OWNERS or not owner_id or not slot:
        raise ValueError("存储正文引用位置无效")
    raw = _encode(value, kind)
    if len(raw) > MAX_PAYLOAD_BYTES:
        raise ValueError("存储正文超过大小上限")
    identity = hashlib.sha256(kind.encode() + b"\0" + raw).hexdigest()
    compressed = zlib.compress(raw, 6)
    codec, data = (
        ("zlib", compressed) if len(compressed) + 32 < len(raw) else ("json", raw)
    )
    conn.execute(
        "INSERT OR IGNORE INTO storage_payloads(id,kind,codec,data,size_bytes,created_at) "
        "VALUES (?,?,?,?,?,?)",
        (identity, kind, codec, data, len(raw), time.time()),
    )
    conn.execute(
        "INSERT INTO storage_payload_refs(owner_table,owner_id,slot,payload_id) "
        "VALUES (?,?,?,?) ON CONFLICT(owner_table,owner_id,slot) DO UPDATE SET "
        "payload_id=excluded.payload_id",
        (owner_table, str(owner_id), slot, identity),
    )
    return {PAYLOAD_KEY: identity}


def load_payload(conn: sqlite3.Connection, value: Any) -> Any:
    """Resolve one explicit marker; legacy inline values pass through unchanged."""
    if not is_payload_reference(value):
        return value
    identity = value[PAYLOAD_KEY]
    row = conn.execute(
        "SELECT kind,codec,data,size_bytes FROM storage_payloads WHERE id=?",
        (identity,),
    ).fetchone()
    if row is None:
        raise ValueError("存储正文缺失，无法恢复历史数据")
    kind, codec, data, size = row
    if not isinstance(size, int) or not 0 <= size <= MAX_PAYLOAD_BYTES:
        raise ValueError("存储正文大小无效")
    try:
        if codec == "zlib":
            reader = zlib.decompressobj()
            raw = reader.decompress(data, size + 1)
            if not reader.eof or reader.unused_data or reader.unconsumed_tail:
                raise ValueError("存储正文压缩数据无效")
        elif codec == "json":
            raw = bytes(data)
        else:
            raise ValueError("未知的存储正文编码")
        if (
            len(raw) != size
            or hashlib.sha256(kind.encode() + b"\0" + raw).hexdigest() != identity
        ):
            raise ValueError("存储正文校验失败")
        return _decode(raw, kind)
    except (zlib.error, UnicodeError, KeyError, TypeError, RecursionError) as exc:
        raise ValueError("存储正文无效，无法恢复历史数据") from exc


def release_payloads(
    conn: sqlite3.Connection,
    owner_table: str,
    owner_id: str,
    slot_prefix: str | None = None,
) -> None:
    if owner_table not in PAYLOAD_OWNERS:
        raise ValueError("存储正文引用位置无效")
    query = "DELETE FROM storage_payload_refs WHERE owner_table=? AND owner_id=?"
    args: list[Any] = [owner_table, str(owner_id)]
    if slot_prefix is not None:
        query += " AND substr(slot,1,length(?))=?"
        args.extend((slot_prefix, slot_prefix))
    conn.execute(query, args)


def gc_payloads(conn: sqlite3.Connection) -> dict[str, int]:
    """Collect bodies only after all task/gallery/cache owner references vanish."""
    for table, column in PAYLOAD_OWNERS.items():
        conn.execute(
            f"DELETE FROM storage_payload_refs WHERE owner_table=? AND NOT EXISTS "
            f"(SELECT 1 FROM {table} WHERE {column}=storage_payload_refs.owner_id)",
            (table,),
        )
    count, size = conn.execute(
        "SELECT COUNT(*),COALESCE(SUM(length(data)),0) FROM storage_payloads p "
        "WHERE NOT EXISTS(SELECT 1 FROM storage_payload_refs r WHERE r.payload_id=p.id)"
    ).fetchone()
    conn.execute(
        "DELETE FROM storage_payloads WHERE NOT EXISTS "
        "(SELECT 1 FROM storage_payload_refs r WHERE r.payload_id=storage_payloads.id)"
    )
    return {"payloads_removed": int(count), "bytes_reclaimed": int(size)}
