"""Shared bodies preserve exact workflows and independent owner lifetimes."""

import json
import sqlite3
import zlib
from contextlib import contextmanager

import pytest

from astrbot_plugin_image_studio.backend.database import payloads
from astrbot_plugin_image_studio.backend.database.schema import SCHEMA_STATEMENTS


@contextmanager
def database():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    for statement in SCHEMA_STATEMENTS:
        conn.execute(statement)
    for identity in ("one", "two"):
        conn.execute(
            "INSERT INTO comfy_workflow_revisions VALUES(?,?,?,?)",
            (identity, identity, "{}", 1),
        )
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


def save(conn, owner, value, slot="config", kind="json"):
    return payloads.store_payload(
        conn, "comfy_workflow_revisions", owner, slot, value, kind
    )


def test_equal_bodies_share_storage_and_survive_until_last_owner_is_deleted():
    with database() as conn:
        value = {"a": "long repeated content " * 500, "b": [1, 2]}
        first = save(conn, "one", value)
        second = save(conn, "two", dict(reversed(list(value.items()))))
        assert first == second
        assert conn.execute("SELECT count(*) FROM storage_payloads").fetchone()[0] == 1
        assert (
            conn.execute("SELECT count(*) FROM storage_payload_refs").fetchone()[0] == 2
        )
        assert (
            conn.execute("SELECT codec FROM storage_payloads").fetchone()[0] == "zlib"
        )
        conn.execute("DELETE FROM comfy_workflow_revisions WHERE id='one'")
        assert payloads.gc_payloads(conn)["payloads_removed"] == 0
        assert payloads.load_payload(conn, second) == value
        conn.execute("DELETE FROM comfy_workflow_revisions WHERE id='two'")
        assert payloads.gc_payloads(conn)["payloads_removed"] == 1
        with pytest.raises(ValueError, match="缺失"):
            payloads.load_payload(conn, first)


def test_workflow_encoding_retains_exact_json_text_and_uint64_and_nonmatching_values():
    api = {"2": {"inputs": {"seed": (1 << 64) - 1}}, "1": {"text": "中文"}}
    workflow = {"nodes": [{"id": 2, "widgets_values": [(1 << 64) - 1]}]}
    value = {
        "api_graph": api,
        "api_graph_json": json.dumps(api, ensure_ascii=False, indent=2),
        "workflow": workflow,
        "workflow_json": json.dumps(workflow, ensure_ascii=False),
        "bindings": {"prompt": {"target": "1"}},
    }
    with database() as conn:
        marker = save(conn, "one", value, kind="workflow")
        assert payloads.load_payload(conn, marker) == value
        decoded = payloads.load_payload(conn, marker)
        assert decoded["api_graph"]["2"]["inputs"]["seed"] == (1 << 64) - 1
        row = conn.execute("SELECT codec,data FROM storage_payloads").fetchone()
        packed = json.loads(zlib.decompress(row[1]) if row[0] == "zlib" else row[1])
        assert "api_graph" not in packed["value"]
        assert "workflow" not in packed["value"]
        value["api_graph"] = {"actually_different": 1}
        other = save(conn, "two", value, kind="workflow")
        assert other != marker
        assert payloads.load_payload(conn, other) == value


def test_slot_replacement_and_prefix_release_do_not_release_other_owners():
    with database() as conn:
        old = save(conn, "one", {"value": "old"}, slot="output:0")
        save(conn, "two", {"value": "old"}, slot="config")
        new = save(conn, "one", {"value": "new"}, slot="output:0")
        save(conn, "one", {"untouched": True}, slot="input:0")
        assert payloads.gc_payloads(conn)["payloads_removed"] == 0
        payloads.release_payloads(conn, "comfy_workflow_revisions", "one", "output:")
        assert payloads.gc_payloads(conn)["payloads_removed"] == 1
        assert payloads.load_payload(conn, old) == {"value": "old"}
        with pytest.raises(ValueError, match="缺失"):
            payloads.load_payload(conn, new)
        assert (
            conn.execute("SELECT count(*) FROM storage_payload_refs").fetchone()[0] == 2
        )


def test_owner_and_body_writes_rollback_together():
    with database() as conn:
        with pytest.raises(RuntimeError), conn:
            save(conn, "one", {"rollback": True})
            raise RuntimeError("abort")
        assert conn.execute("SELECT count(*) FROM storage_payloads").fetchone()[0] == 0
        assert (
            conn.execute("SELECT count(*) FROM storage_payload_refs").fetchone()[0] == 0
        )


@pytest.mark.parametrize("fault", ["body", "trailing", "size", "codec"])
def test_corrupt_payload_is_rejected_instead_of_silently_losing_history(fault):
    with database() as conn:
        marker = save(conn, "one", {"content": "repeat " * 1000})
        if fault == "body":
            conn.execute("UPDATE storage_payloads SET data=?", (zlib.compress(b"{}"),))
        elif fault == "trailing":
            data = conn.execute("SELECT data FROM storage_payloads").fetchone()[0]
            conn.execute("UPDATE storage_payloads SET data=?", (data + b"trailing",))
        elif fault == "size":
            conn.execute("UPDATE storage_payloads SET size_bytes=1")
        else:
            conn.execute("UPDATE storage_payloads SET codec='unsupported'")
        with pytest.raises(ValueError):
            payloads.load_payload(conn, marker)


def test_legacy_json_and_marker_shaped_user_values_are_not_recursively_expanded():
    with database() as conn:
        original = {"user_value": {payloads.PAYLOAD_KEY: "a" * 64}}
        marker = save(conn, "one", original)
        assert payloads.load_payload(conn, marker) == original
        assert payloads.load_payload(conn, original) == original
        with pytest.raises(ValueError):
            save(conn, "one", {"nan": float("nan")})
