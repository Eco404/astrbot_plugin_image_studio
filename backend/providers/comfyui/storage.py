"""Compact durable ComfyUI records without changing their public snapshots."""

from __future__ import annotations

import copy
import hashlib
import json

from ...database.payloads import load_payload, release_payloads, store_payload


def dumps(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def compact_request(request):
    value = copy.deepcopy(request)
    model = value.get("model")
    if isinstance(model, dict):
        model.pop("comfyui", None)
    return value


def request_fingerprint(request, references):
    canonical = {
        "request": compact_request(request),
        "references": [
            [item["sha256"], item["filename"], item["mime_type"]] for item in references
        ],
    }
    return hashlib.sha256(dumps(canonical).encode()).hexdigest()


def compact_result(connection, job_id, result):
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


def expand_result(connection, result):
    value = dict(result)
    marker = value.pop("_execution_payload", None)
    if marker is not None:
        value.update(load_payload(connection, marker))
    return value


def compact_outputs(connection, job_id, outputs):
    release_payloads(connection, "comfy_jobs", job_id, slot_prefix="output:")
    value = copy.deepcopy(outputs)
    for index, item in enumerate(value):
        effective = item.get("effective_parameters") or {}
        if isinstance(effective.get("_comfyui"), dict):
            # Rewriting a task also accepts already compact output snapshots.
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


def expand_outputs(connection, outputs):
    for item in outputs:
        effective = item.get("effective_parameters") or {}
        if isinstance(effective.get("_comfyui"), dict):
            effective["_comfyui"] = load_payload(connection, effective["_comfyui"])
    return outputs
