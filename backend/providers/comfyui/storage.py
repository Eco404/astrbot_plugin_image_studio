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
            # Decoding also accepts already compact records during migrations.
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


def migrate_comfy_storage(connection):
    """Called inside the schema migration transaction; never touches image files."""
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
            (dumps(marker), revision_id),
        )
    for row in connection.execute(
        "SELECT id,revision_id,request_json,input_refs_json,output_refs_json,result_json FROM comfy_jobs"
    ).fetchall():
        job_id, revision_id, request_json, input_json, output_json, result_json = row
        original_request = json.loads(request_json)
        model = original_request.get("model")
        if isinstance(model, dict) and "comfyui" in model:
            if revision_id not in revision_configs or dumps(model["comfyui"]) != dumps(
                revision_configs[revision_id]
            ):
                raise ValueError(
                    f"ComfyUI 任务 {job_id} 的工作流快照与修订不一致，已中止存储迁移；原始数据与备份保持完整"
                )
        request = compact_request(original_request)
        references = json.loads(input_json)
        result = compact_result(connection, job_id, json.loads(result_json))
        outputs = compact_outputs(connection, job_id, json.loads(output_json))
        connection.execute(
            "UPDATE comfy_jobs SET request_json=?,output_refs_json=?,result_json=?,"
            "parent_job_id=?,temporary=?,model_name=?,queue_dismissed=?,request_fingerprint=? WHERE id=?",
            (
                dumps(request),
                dumps(outputs),
                dumps(result),
                str(request.get("parent_job_id") or ""),
                int(bool(request.get("temporary"))),
                str((request.get("model") or {}).get("name") or ""),
                int(result.get("queue_dismissed") is True),
                request_fingerprint(request, references),
                job_id,
            ),
        )
