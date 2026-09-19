"""Durable job snapshots, transactions, manifests and owner-aware retention."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
import uuid
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ...database.payloads import load_payload, release_payloads, store_payload
from ...database.schema import ensure_release_schema
from ...models import GeneratedImage, ReferenceImage
from .job_files import ComfyJobFiles
from .job_types import (
    JOB_STATUSES,
    RECOVERY_SECONDS,
    TERMINAL_STATUSES,
    _job_id,
    _json_snapshot,
)
from .storage import (
    compact_outputs,
    compact_request,
    compact_result,
    expand_outputs,
    expand_result,
    request_fingerprint,
)


class ComfyJobStore:
    """Use the gallery database, with one short-lived connection per operation."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.data_dir = self.db_path.parent
        self.files = ComfyJobFiles(self.data_dir)
        self.inputs_dir = self.files.inputs_dir
        self.outputs_dir = self.files.outputs_dir
        self.blobs_dir = self.files.blobs_dir

    def _owned_root(self, root):
        return self.files.owned_root(root)

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            ensure_release_schema(
                connection, backup_dir=self.db_path.parent / "backups"
            )

    async def save_revision(self, config: Mapping[str, Any]) -> str:
        payload = _json_snapshot(config)
        revision_id = hashlib.sha256(payload.encode()).hexdigest()
        await asyncio.to_thread(self._save_revision_sync, revision_id, payload)
        return revision_id

    def _save_revision_sync(self, revision_id: str, payload: str) -> None:
        with self._connect() as connection:
            marker = store_payload(
                connection,
                "comfy_workflow_revisions",
                revision_id,
                "config",
                json.loads(payload),
                kind="workflow",
            )
            connection.execute(
                "INSERT INTO comfy_workflow_revisions(id,fingerprint,config_json,created_at) VALUES (?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET config_json=excluded.config_json,created_at=excluded.created_at",
                (revision_id, revision_id, _json_snapshot(marker), time.time()),
            )

    async def get_revision(self, revision_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_revision_sync, revision_id)

    def _get_revision_sync(self, revision_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT * FROM comfy_workflow_revisions WHERE id=?", (revision_id,)
            ).fetchone()
            return (
                load_payload(connection, json.loads(row["config_json"]))
                if row
                else None
            )

    async def create_job(
        self,
        *,
        provider_id: str,
        model_id: str,
        revision_id: str,
        request: Mapping[str, Any],
        references: Sequence[ReferenceImage] = (),
        job_id: str | None = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._create_job_sync,
            _job_id(job_id or uuid.uuid4().hex),
            provider_id,
            model_id,
            revision_id,
            _json_snapshot(request),
            tuple(references),
        )

    def _create_job_sync(
        self, job_id, provider_id, model_id, revision_id, request_json, references
    ):
        directory = self.inputs_dir / job_id
        staged = []
        request = compact_request(json.loads(request_json))
        request_json = _json_snapshot(request)
        reference_descriptors = [
            {
                "sha256": hashlib.sha256(item.data).hexdigest(),
                "filename": item.filename,
                "mime_type": item.mime_type,
            }
            for item in references
        ]
        fingerprint = request_fingerprint(request, reference_descriptors)
        # Reserve the identifier in a write transaction before touching files, so
        # concurrent retries cannot overwrite another task's protected inputs.
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT * FROM comfy_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if previous:
                if (
                    previous["provider_id"],
                    previous["model_id"],
                    previous["revision_id"],
                ) != (provider_id, model_id, revision_id):
                    raise ValueError("ComfyUI 任务编号已用于不同请求")
                expected = [
                    (
                        hashlib.sha256(item.data).hexdigest(),
                        item.filename,
                        item.mime_type,
                    )
                    for item in references
                ]
                persisted = [
                    (item["sha256"], item["filename"], item["mime_type"])
                    for item in json.loads(previous["input_refs_json"])
                ]
                if expected != persisted:
                    raise ValueError("ComfyUI 任务编号已用于不同参考图")
                if previous["request_fingerprint"] != fingerprint:
                    raise ValueError("ComfyUI 任务编号已用于不同请求")
                return self._decode(previous, connection)
            if references:
                self.files.owned_root(self.inputs_dir)
                if directory.exists() or directory.is_symlink():
                    raise FileExistsError(str(directory))
                self.files.owned_root(self.blobs_dir).mkdir(
                    parents=True, exist_ok=True, mode=0o700
                )
            # Shared blobs created before a rolled-back insert have no owner
            # and are collected by the next maintenance transaction.
            for reference in references:
                digest = hashlib.sha256(reference.data).hexdigest()
                relative_path = f"{digest}.image"
                path = self.blobs_dir / relative_path
                self.files.write_blob(path, reference.data)
                staged.append(
                    {
                        "id": reference.id,
                        "filename": reference.filename,
                        "mime_type": reference.mime_type,
                        "path": relative_path,
                        "storage": "blob",
                        "sha256": digest,
                        "size_bytes": len(reference.data),
                    }
                )
            now = time.time()
            connection.execute(
                "INSERT INTO comfy_jobs(id,provider_id,model_id,revision_id,status,request_json,input_refs_json,created_at,updated_at,parent_job_id,temporary,model_name,request_fingerprint) VALUES (?,?,?,?,'queued',?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    provider_id,
                    model_id,
                    revision_id,
                    request_json,
                    _json_snapshot(staged),
                    now,
                    now,
                    str(request.get("parent_job_id") or ""),
                    int(bool(request.get("temporary"))),
                    str((request.get("model") or {}).get("name") or ""),
                    fingerprint,
                ),
            )
            row = connection.execute(
                "SELECT * FROM comfy_jobs WHERE id=?", (job_id,)
            ).fetchone()
            return self._decode(row, connection)

    def _decode(
        self, row: sqlite3.Row, connection, *, light=False, protected_jobs=None
    ) -> dict[str, Any]:
        result = dict(row)
        if (
            result.get("status") == "failed"
            and result.get("finished_at")
            and result["finished_at"] < time.time() - RECOVERY_SECONDS
        ):
            result["recovery_protected"] = (
                result["id"] in protected_jobs
                if protected_jobs is not None
                else self._protected_by_parent(
                    connection, result, time.time() - RECOVERY_SECONDS
                )
            )
        if light:
            result["request"] = {
                "temporary": bool(result["temporary"]),
                "parent_job_id": result["parent_job_id"],
                "model": {"name": result["model_name"]},
            }
            result["result"] = json.loads(result.pop("summary_json"))
            return result
        for column, name in (
            ("request_json", "request"),
            ("input_refs_json", "references"),
            ("output_refs_json", "outputs"),
            ("result_json", "result"),
        ):
            result[name] = json.loads(result.pop(column))
        result["result"] = expand_result(connection, result["result"])
        result["outputs"] = expand_outputs(connection, result["outputs"])
        if result.get("generation_id"):
            result["has_result"] = (
                connection.execute(
                    "SELECT 1 FROM generation_images i JOIN image_assets a ON a.id=i.asset_id "
                    "WHERE i.generation_id=? AND a.file_state='available' LIMIT 1",
                    (result["generation_id"],),
                ).fetchone()
                is not None
            )
        else:
            result["has_result"] = any(
                item.get("storage") != "expired" for item in result["outputs"]
            )
        if not result.get("archive_state") and isinstance(
            result["request"].get("model"), dict
        ):
            revision = connection.execute(
                "SELECT config_json FROM comfy_workflow_revisions WHERE id=?",
                (result["revision_id"],),
            ).fetchone()
            if revision:
                result["request"]["model"]["comfyui"] = load_payload(
                    connection, json.loads(revision[0])
                )
        return result

    async def get_job(self, job_id: str, *, light=False) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_job_sync, job_id, light)

    def _get_job_sync(self, job_id: str, light=False) -> dict[str, Any] | None:
        with self._connect() as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                f"SELECT {self._summary_columns() if light else '*'} FROM comfy_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            return self._decode(row, connection, light=light) if row else None

    @staticmethod
    def _summary_columns():
        return (
            "id,provider_id,model_id,model_name,parent_job_id,temporary,queue_dismissed,"
            "status,remote_id,error,generation_id,created_at,updated_at,finished_at,archive_state,"
            "json_object('progress',json_extract(result_json,'$.progress'),"
            "'child_ids',json_extract(result_json,'$.child_ids')) AS summary_json,"
            "CASE WHEN generation_id!='' THEN EXISTS(SELECT 1 FROM generation_images gi "
            "JOIN image_assets a ON a.id=gi.asset_id WHERE gi.generation_id=comfy_jobs.generation_id "
            "AND a.file_state='available') ELSE "
            "archive_state='' AND EXISTS(SELECT 1 FROM json_each(output_refs_json) "
            "WHERE COALESCE(json_extract(value,'$.storage'),'')!='expired') END AS has_result"
        )

    async def list_jobs(
        self,
        *,
        provider_id: str | None = None,
        limit: int = 100,
        include_children: bool = True,
        queue_only: bool = False,
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self._list_jobs_sync,
            provider_id,
            max(1, min(int(limit), 1000)),
            False,
            include_children,
            queue_only,
        )

    async def get_pending(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_jobs_sync, None, None, True)

    def _list_jobs_sync(
        self, provider_id, limit, pending, include_children=True, queue_only=False
    ):
        clauses, values = [], []
        if not include_children:
            clauses.append("parent_job_id = ''")
        if provider_id is not None:
            clauses.append("provider_id=?")
            values.append(provider_id)
        if queue_only:
            # Filter before LIMIT so recent successful runs cannot displace old
            # failures or active jobs from the recoverable WebUI queue.
            clauses.extend(
                (
                    "status != 'succeeded'",
                    "queue_dismissed != 1",
                )
            )
        if pending:
            clauses.append(
                "status NOT IN (" + ",".join("?" for _ in TERMINAL_STATUSES) + ")"
            )
            values.extend(sorted(TERMINAL_STATUSES))
        light = queue_only or pending
        query = (
            f"SELECT {self._summary_columns() if light else '*'} FROM comfy_jobs"
            + (" WHERE " + " AND ".join(clauses) if clauses else "")
        )
        query += " ORDER BY created_at " + ("ASC" if pending else "DESC")
        if limit is not None:
            query += " LIMIT ?"
            values.append(limit)
        with self._connect() as connection:
            connection.execute("BEGIN")
            rows = connection.execute(query, values).fetchall()
            cutoff = time.time() - RECOVERY_SECONDS
            protected = (
                self._protected_jobs(connection, cutoff)
                if any(
                    row["status"] == "failed"
                    and row["finished_at"]
                    and row["finished_at"] < cutoff
                    for row in rows
                )
                else set()
            )
            return [
                self._decode(row, connection, light=light, protected_jobs=protected)
                for row in rows
            ]

    async def update_job(
        self,
        job_id: str,
        *,
        status: str | None = None,
        remote_id: str | None = None,
        result: Mapping[str, Any] | None = None,
        error: str | None = None,
        generation_id: str | None = None,
        expected_status: str | Sequence[str] | None = None,
    ) -> dict[str, Any]:
        if status is not None and status not in JOB_STATUSES:
            raise ValueError(f"ComfyUI 任务状态无效：{status}")
        updates: dict[str, Any] = {"updated_at": time.time()}
        for name, value in (
            ("status", status),
            ("remote_id", remote_id),
            ("error", error),
            ("generation_id", generation_id),
        ):
            if value is not None:
                updates[name] = value
        if result is not None:
            updates["result_json"] = _json_snapshot(result)
        if status in TERMINAL_STATUSES:
            updates["finished_at"] = updates["updated_at"]
        return await asyncio.to_thread(
            self._update_job_sync, job_id, updates, expected_status
        )

    def _update_job_sync(self, job_id, updates, expected_status):
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM comfy_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise ValueError("ComfyUI 任务不存在")
            if expected_status is not None:
                expected = (
                    {expected_status}
                    if isinstance(expected_status, str)
                    else set(expected_status)
                )
                if row["status"] not in expected:
                    return self._decode(row, connection)
            if (
                row["remote_id"]
                and "remote_id" in updates
                and row["remote_id"] != updates["remote_id"]
            ):
                raise ValueError("ComfyUI 任务已有远端编号，不能重新绑定")
            if (
                row["status"] in TERMINAL_STATUSES
                and updates.get("status", row["status"]) != row["status"]
            ):
                raise ValueError("ComfyUI 任务已经结束，不能重复执行")
            if "result_json" in updates:
                payload = json.loads(updates["result_json"])
                updates["result_json"] = _json_snapshot(
                    compact_result(connection, job_id, payload)
                )
                updates["queue_dismissed"] = int(payload.get("queue_dismissed") is True)
            connection.execute(
                "UPDATE comfy_jobs SET "
                + ",".join(f"{key}=?" for key in updates)
                + " WHERE id=?",
                (*updates.values(), job_id),
            )
            self._refresh_parent_progress(connection, row["parent_job_id"])
            return self._decode(
                connection.execute(
                    "SELECT * FROM comfy_jobs WHERE id=?", (job_id,)
                ).fetchone(),
                connection,
            )

    async def update_progress(self, job_id: str, progress, *, status=None):
        """Frequent websocket events must not inflate and rewrite frozen graphs."""
        return await asyncio.to_thread(
            self._update_progress_sync, job_id, progress, status
        )

    def _update_progress_sync(self, job_id, progress, status):
        if status is not None and status not in JOB_STATUSES - TERMINAL_STATUSES:
            raise ValueError("ComfyUI 任务进度状态无效")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status,parent_job_id,json_extract(result_json,'$.progress') AS progress "
                "FROM comfy_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise ValueError("ComfyUI 任务不存在")
            if row["status"] in TERMINAL_STATUSES:
                return row["status"]
            previous = json.loads(row["progress"] or "{}")
            # Queue polling reports only running/queued state. A running poll
            # must not erase the sampler's more precise websocket progress.
            # Node changes and reconnect/queued events intentionally reset it.
            if (
                previous.get("status") == progress.get("status") == "running"
                and not progress.get("event")
                and "value" not in progress
                and "max" not in progress
            ):
                progress = {**previous, **progress}
            updated = connection.execute(
                "UPDATE comfy_jobs SET result_json=json_set(result_json,'$.progress',json(?)),"
                "status=?,updated_at=? WHERE id=? AND status=?",
                (
                    _json_snapshot(progress),
                    status or row["status"],
                    time.time(),
                    job_id,
                    row["status"],
                ),
            )
            if not updated.rowcount:
                latest = connection.execute(
                    "SELECT status FROM comfy_jobs WHERE id=?", (job_id,)
                ).fetchone()
                return latest["status"] if latest else "cancelled"
            self._refresh_parent_progress(connection, row["parent_job_id"])
            return status or row["status"]

    @staticmethod
    def _refresh_parent_progress(connection, parent_id):
        """Combine light child progress and completion counts atomically.

        Concurrent children may finish in any order; the current workflow is
        identified by its saved plan index, never inferred from completed count.
        """
        if not parent_id:
            return
        parent = connection.execute(
            "SELECT status,json_extract(result_json,'$.progress') AS progress "
            "FROM comfy_jobs WHERE id=?",
            (parent_id,),
        ).fetchone()
        if parent is None or parent["status"] in TERMINAL_STATUSES:
            return
        previous = json.loads(parent["progress"] or "{}")
        children = connection.execute(
            "SELECT id,status,json_extract(request_json,'$.chunk_index') AS chunk_index,"
            "json_extract(result_json,'$.progress') AS progress FROM comfy_jobs "
            "WHERE parent_job_id=? ORDER BY updated_at DESC,id",
            (parent_id,),
        ).fetchall()
        progress = {
            "status": "running",
            "completed": sum(
                child["status"] in TERMINAL_STATUSES for child in children
            ),
            "total": previous.get("total") or len(children),
        }
        for child in children:
            if child["status"] != "running":
                continue
            current = json.loads(child["progress"] or "{}")
            if current.get("status") != "running":
                continue
            progress.update(
                {
                    key: current[key]
                    for key in ("value", "max", "node")
                    if key in current
                }
            )
            if isinstance(child["chunk_index"], int):
                progress["current"] = child["chunk_index"] + 1
            break
        connection.execute(
            "UPDATE comfy_jobs SET result_json=json_set(result_json,'$.progress',json(?)),"
            "updated_at=? WHERE id=?",
            (_json_snapshot(progress), time.time(), parent_id),
        )

    async def dismiss_job(self, job_id: str) -> dict[str, Any]:
        """Hide a terminal task from the WebUI queue without deleting its data."""
        return await asyncio.to_thread(self._dismiss_job_sync, job_id)

    def _dismiss_job_sync(self, job_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM comfy_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise ValueError("ComfyUI 任务不存在")
            if row["status"] not in TERMINAL_STATUSES:
                raise ValueError(
                    "任务仍在进行中，不能从队列清除；请等待结束或先取消任务"
                )
            if row["queue_dismissed"]:
                return self._decode(row, connection)
            connection.execute(
                "UPDATE comfy_jobs SET queue_dismissed=1,result_json=json_set(result_json, '$.queue_dismissed', json('true')),updated_at=? WHERE id=?",
                (time.time(), job_id),
            )
            return self._decode(
                connection.execute(
                    "SELECT * FROM comfy_jobs WHERE id=?", (job_id,)
                ).fetchone(),
                connection,
            )

    async def resume_job(self, job_id: str) -> dict[str, Any]:
        """Explicitly retry monitoring an existing remote task, never submit again."""
        return await asyncio.to_thread(self._resume_job_sync, job_id)

    def _resume_job_sync(self, job_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM comfy_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise ValueError("ComfyUI 任务不存在")
            if row["archive_state"] or (
                row["status"] == "failed"
                and row["finished_at"] is not None
                and row["finished_at"] < time.time() - RECOVERY_SECONDS
                and not self._protected_by_parent(
                    connection, row, time.time() - RECOVERY_SECONDS
                )
            ):
                raise ValueError(
                    "ComfyUI 任务已超过 24 小时恢复期限，不能恢复；请重新运行工作流"
                )
            if not row["remote_id"]:
                child_ids = json.loads(row["result_json"]).get("child_ids") or []
                associated = []
                for child_id in child_ids:
                    child = connection.execute(
                        "SELECT parent_job_id FROM comfy_jobs WHERE id=?", (child_id,)
                    ).fetchone()
                    if child is not None:
                        associated.append(child["parent_job_id"] == job_id)
                if not associated or not all(associated):
                    raise ValueError(
                        "ComfyUI 任务没有已确认的远端编号或子任务，不能恢复或重新提交"
                    )
            if row["status"] not in {"failed", "unknown"}:
                if row["status"] not in TERMINAL_STATUSES:
                    return self._decode(row, connection)
                raise ValueError("ComfyUI 任务已经结束，不需要恢复")
            connection.execute(
                "UPDATE comfy_jobs SET status='submitted',error='',finished_at=NULL,"
                "queue_dismissed=0,result_json=json_remove(result_json, '$.queue_dismissed'),updated_at=? WHERE id=?",
                (time.time(), job_id),
            )
            return self._decode(
                connection.execute(
                    "SELECT * FROM comfy_jobs WHERE id=?", (job_id,)
                ).fetchone(),
                connection,
            )

    async def load_references(self, job_id: str) -> tuple[ReferenceImage, ...]:
        return await asyncio.to_thread(self._load_references_sync, _job_id(job_id))

    def _load_references_sync(self, job_id: str) -> tuple[ReferenceImage, ...]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT input_refs_json FROM comfy_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise ValueError("ComfyUI 任务不存在")
            references = json.loads(row[0])
        return self.files.load_references(job_id, references)

    async def cleanup_inputs(self, *, terminal_before: float) -> int:
        """Explicit maintenance only; pending/uncertain tasks keep their input files."""
        return await self.cleanup_terminal_files(terminal_before=terminal_before)

    async def save_outputs(
        self, job_id: str, images: Sequence[GeneratedImage]
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self._save_outputs_sync, _job_id(job_id), tuple(images)
        )

    def _save_outputs_sync(
        self, job_id: str, images: tuple[GeneratedImage, ...]
    ) -> list[dict[str, Any]]:
        if not images:
            raise ValueError("ComfyUI 没有可保存的输出图片")
        directory = self.files.owned_root(self.blobs_dir)
        manifest = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT output_refs_json FROM comfy_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise ValueError("ComfyUI 任务不存在")
            previous = json.loads(row["output_refs_json"])
            if previous:
                if [item["sha256"] for item in previous] != [
                    hashlib.sha256(item.data).hexdigest() for item in images
                ]:
                    raise ValueError("ComfyUI 任务已经保存了不同的输出图片")
                return expand_outputs(connection, previous)
            directory.mkdir(parents=True, exist_ok=True)
            directory.chmod(0o700)
            for index, image in enumerate(images):
                digest = hashlib.sha256(image.data).hexdigest()
                filename = f"{digest}.image"
                destination = directory / filename
                self.files.write_blob(destination, image.data)
                manifest.append(
                    {
                        "storage": "blob",
                        "path": filename,
                        "sha256": digest,
                        "mime_type": image.mime_type,
                        "size_bytes": len(image.data),
                        "effective_parameters": image.effective_parameters,
                        "response_index": image.response_index,
                    }
                )
            connection.execute(
                "UPDATE comfy_jobs SET output_refs_json=?,updated_at=? WHERE id=?",
                (
                    _json_snapshot(compact_outputs(connection, job_id, manifest)),
                    time.time(),
                    job_id,
                ),
            )
        return manifest

    @staticmethod
    def _write_blob(destination, data):
        if destination.is_symlink():
            raise ValueError("ComfyUI 共享图片缓存不能是符号链接")
        if destination.exists():
            if destination.stat().st_size != len(data):
                raise ValueError("ComfyUI 共享图片缓存大小不一致")
            if hashlib.sha256(destination.read_bytes()).hexdigest() != destination.stem:
                raise ValueError("ComfyUI 共享图片缓存内容不一致")
            return
        temporary = destination.parent / f"{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("xb") as handle:
                handle.write(data)
            temporary.chmod(0o600)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)

    async def load_outputs(self, job_id: str) -> tuple[GeneratedImage, ...]:
        return await asyncio.to_thread(self._load_outputs_sync, _job_id(job_id))

    def _load_outputs_sync(self, job_id: str) -> tuple[GeneratedImage, ...]:
        with self._connect() as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT output_refs_json FROM comfy_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise ValueError("ComfyUI 任务不存在")
            manifest = expand_outputs(connection, json.loads(row[0]))
        outputs = []
        for index, item in enumerate(manifest):
            path = self._output_path(job_id, item)
            try:
                data = path.read_bytes()
            except OSError as exc:
                raise ValueError(f"ComfyUI 第 {index + 1} 张输出图片已不可用") from exc
            if (
                len(data) != item["size_bytes"]
                or hashlib.sha256(data).hexdigest() != item["sha256"]
            ):
                raise ValueError(f"ComfyUI 第 {index + 1} 张输出图片内容发生变化")
            effective = self._output_parameters(item)
            outputs.append(
                GeneratedImage(
                    data,
                    item["mime_type"],
                    effective,
                    item["response_index"],
                )
            )
        return tuple(outputs)

    async def output_parameters(self, descriptor):
        return await asyncio.to_thread(self._output_parameters, descriptor)

    def _output_parameters(self, item):
        effective = dict(item.get("effective_parameters") or {})
        if item.get("storage") == "gallery" and "_comfyui" not in effective:
            with self._connect() as connection:
                connection.execute("BEGIN")
                record = connection.execute(
                    "SELECT supplemental_json FROM generation_images WHERE id=? AND generation_id=? AND asset_id=?",
                    (
                        item.get("gallery_image_id"),
                        item.get("gallery_generation_id"),
                        item["sha256"],
                    ),
                ).fetchone()
                if record:
                    snapshot = json.loads(record[0]).get("comfyui")
                    if snapshot is not None:
                        effective["_comfyui"] = load_payload(connection, snapshot)
        return effective

    def _output_path(self, job_id, item):
        if item.get("storage") == "expired":
            raise ValueError("ComfyUI 任务临时输出已过期")
        if item.get("storage") == "gallery":
            # Never resurrect an explicitly deleted generation image through a
            # matching asset retained by another generation.
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT a.path FROM generation_images i JOIN image_assets a ON a.id=i.asset_id "
                    "WHERE i.id=? AND i.generation_id=? AND a.id=? AND a.file_state='available'",
                    (
                        item.get("gallery_image_id"),
                        item.get("gallery_generation_id"),
                        item["sha256"],
                    ),
                ).fetchone()
            if row is None:
                raise ValueError("ComfyUI 输出图片已从画廊删除或不可用")
            path = (self.data_dir / row["path"]).resolve()
            if not path.is_relative_to(self.data_dir.resolve()):
                raise ValueError("ComfyUI 输出图片路径无效")
            return path
        return self.files.output_path(job_id, item)

    async def release_gallery_outputs(self, job_id, generation_id):
        if generation_id:
            return await asyncio.to_thread(
                self._release_gallery_outputs_sync, job_id, generation_id
            )
        return 0

    async def reconcile_gallery_outputs(self, generation_ids=None):
        """Release published result caches after deletion or during maintenance."""
        return await asyncio.to_thread(
            self._reconcile_gallery_outputs_sync, generation_ids
        )

    def _reconcile_gallery_outputs_sync(self, generation_ids):
        query = (
            "SELECT id,generation_id FROM comfy_jobs "
            "WHERE status IN ('succeeded','partial') AND generation_id!=''"
        )
        args = tuple(dict.fromkeys(generation_ids or ()))
        if generation_ids is not None:
            if not args:
                return 0
            query += " AND generation_id IN (" + ",".join("?" for _ in args) + ")"
        with self._connect() as connection:
            completed = connection.execute(query, args).fetchall()
        return sum(
            self._release_gallery_outputs_sync(row["id"], row["generation_id"])
            for row in completed
        )

    def _release_gallery_outputs_sync(self, job_id, generation_id):
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            owner = connection.execute(
                "SELECT status,generation_id FROM comfy_jobs WHERE id=?", (job_id,)
            ).fetchone()
            # A gallery ID alone is insufficient: publication and terminal state
            # must have committed before absent links can mean deleted results.
            if (
                owner is None
                or owner["status"] not in {"succeeded", "partial"}
                or owner["generation_id"] != generation_id
            ):
                return 0
            if job_id in self._protected_jobs(
                connection, time.time() - RECOVERY_SECONDS
            ):
                return 0
            rows = connection.execute(
                "SELECT i.id,i.ordinal,a.id AS sha256,a.path,a.size_bytes,a.file_state FROM generation_images i "
                "JOIN image_assets a ON a.id=i.asset_id WHERE i.generation_id=?",
                (generation_id,),
            ).fetchall()
            by_ordinal = {row["ordinal"]: row for row in rows}
            by_digest = {row["sha256"]: row for row in rows}
            jobs = connection.execute(
                "SELECT id,output_refs_json FROM comfy_jobs WHERE id=? OR parent_job_id=?",
                (job_id, job_id),
            ).fetchall()
            for job in jobs:
                manifest = json.loads(job["output_refs_json"])
                changed = False
                for index, item in enumerate(manifest):
                    if item.get("storage") == "expired":
                        continue
                    asset = (
                        by_ordinal.get(index)
                        if job["id"] == job_id
                        else by_digest.get(item["sha256"])
                    )
                    if (
                        asset is None
                        or asset["sha256"] != item["sha256"]
                        or asset["size_bytes"] != item["size_bytes"]
                    ):
                        # Keep descriptor positions and digests, but do not retain
                        # pixels or workflow snapshots for removed gallery links.
                        item.update(storage="expired", path="")
                        (item.get("effective_parameters") or {}).pop("_comfyui", None)
                        connection.execute(
                            "DELETE FROM storage_payload_refs WHERE owner_table='comfy_jobs' "
                            "AND owner_id=? AND slot=?",
                            (job["id"], f"output:{index}"),
                        )
                        changed = True
                        continue
                    # A retained link with a temporarily unavailable file is not
                    # proof of deletion; keep its recovery bytes conservatively.
                    if asset["file_state"] != "available":
                        continue
                    path = (self.data_dir / asset["path"]).resolve()
                    if (
                        not path.is_relative_to(self.data_dir.resolve())
                        or not path.is_file()
                    ):
                        continue
                    item.update(
                        storage="gallery",
                        path=asset["path"],
                        gallery_image_id=asset["id"],
                        gallery_generation_id=generation_id,
                    )
                    changed = True
                if changed:
                    connection.execute(
                        "UPDATE comfy_jobs SET output_refs_json=? WHERE id=?",
                        (_json_snapshot(manifest), job["id"]),
                    )
        # The committed manifests now own the gallery association. Collection is
        # serialized against save_outputs and never deletes gallery originals.
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._collect_unreferenced_blobs(connection)

    def _collect_unreferenced_blobs(self, connection):
        retained = set()
        for row in connection.execute(
            "SELECT output_refs_json,input_refs_json FROM comfy_jobs"
        ):
            retained.update(
                item.get("path")
                for item in (*json.loads(row[0]), *json.loads(row[1]))
                if item.get("storage") == "blob"
            )
        return self.files.remove_unreferenced_blobs(
            retained, time.time() - RECOVERY_SECONDS
        )

    async def cleanup_terminal_files(self, *, terminal_before: float) -> int:
        """Archive safe tasks; count removed cache files or whole legacy task dirs.

        Individual migrated files count once; their now-empty parent directories
        are housekeeping, not additional items. Recursive orphan task-directory
        removal counts once per directory. Database-only archival counts zero.
        """
        removed = await asyncio.to_thread(self._migrate_legacy_files_sync)
        removed += await self.reconcile_gallery_outputs()
        return removed + await asyncio.to_thread(
            self._cleanup_inputs_sync, terminal_before
        )

    def _migrate_legacy_files_sync(self):
        obsolete = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for column, root in (
                ("output_refs_json", self.outputs_dir),
                ("input_refs_json", self.inputs_dir),
            ):
                self.files.owned_root(root)
                for row in connection.execute(
                    f"SELECT id,{column} FROM comfy_jobs WHERE {column}!='[]'"
                ).fetchall():
                    manifest = json.loads(row[column])
                    changed = False
                    for item in manifest:
                        if item.get("storage"):
                            continue
                        staged = self.files.stage_legacy_blob(row["id"], root, item)
                        if staged is None:
                            continue
                        old, destination = staged
                        item.update(storage="blob", path=destination.name)
                        obsolete.append(old)
                        changed = True
                    if changed:
                        connection.execute(
                            f"UPDATE comfy_jobs SET {column}=? WHERE id=?",
                            (_json_snapshot(manifest), row["id"]),
                        )
        # Commit the new references before removing any old directory entries.
        for old in obsolete:
            old.unlink(missing_ok=True)
            try:
                old.parent.rmdir()
            except OSError:
                pass
        return len(obsolete)

    def _cleanup_inputs_sync(self, terminal_before: float) -> int:
        removed = 0
        with self._connect() as connection:
            # Explicit resume also takes this lock. It cannot turn a selected
            # terminal job active between the eligibility check and file removal.
            connection.execute("BEGIN IMMEDIATE")
            protected = self._protected_jobs(connection, terminal_before)
            jobs = connection.execute(
                "SELECT * FROM comfy_jobs WHERE archive_state='' AND status IN ('succeeded','partial','failed','cancelled') AND finished_at < ?",
                (terminal_before,),
            ).fetchall()
            for row in jobs:
                if row["id"] in protected:
                    continue
                result = json.loads(row["result_json"])
                result.pop("_execution_payload", None)
                for name in (
                    "api_graph",
                    "api_graph_json",
                    "workflow",
                    "workflow_json",
                ):
                    result.pop(name, None)
                outputs = json.loads(row["output_refs_json"])
                references = json.loads(row["input_refs_json"])
                for item in references:
                    item.update(storage="expired", path="")
                for item in outputs:
                    (item.get("effective_parameters") or {}).pop("_comfyui", None)
                    if item.get("storage") != "gallery":
                        item.update(storage="expired", path="")
                request = compact_request(json.loads(row["request_json"]))
                fingerprint = row["request_fingerprint"] or request_fingerprint(
                    request, json.loads(row["input_refs_json"])
                )
                # Execution needs caller context while recoverable. Archived
                # tasks retain only identity that the gallery policy elected to
                # record, rather than a second permanent private request copy.
                identity_fields = (
                    "context_type",
                    "platform_name",
                    "platform_id",
                    "group_id",
                    "group_name",
                    "user_id",
                    "user_name",
                )
                gallery_identity = (
                    connection.execute(
                        "SELECT "
                        + ",".join(identity_fields)
                        + " FROM generations WHERE id=?",
                        (row["generation_id"],),
                    ).fetchone()
                    if row["generation_id"]
                    else None
                )
                identity = (
                    {key: gallery_identity[key] for key in identity_fields}
                    if gallery_identity
                    else {}
                )
                for values in (request.get("values"), result.get("resolved_request")):
                    if isinstance(values, dict) and "invocation_source" in values:
                        values["invocation_source"] = identity
                connection.execute(
                    "UPDATE comfy_jobs SET request_json=?,result_json=?,output_refs_json=?,input_refs_json=?,archive_state='archived',request_fingerprint=? WHERE id=?",
                    (
                        _json_snapshot(request),
                        _json_snapshot(result),
                        _json_snapshot(outputs),
                        _json_snapshot(references),
                        fingerprint,
                        row["id"],
                    ),
                )
                release_payloads(connection, "comfy_jobs", row["id"])
            # Keep a tiny revision tombstone for immutable task foreign keys and
            # idempotency. A later new request can repopulate its shared body.
            for row in connection.execute(
                "SELECT id FROM comfy_workflow_revisions r WHERE created_at<? AND EXISTS("
                "SELECT 1 FROM comfy_jobs j WHERE j.revision_id=r.id) AND NOT EXISTS("
                "SELECT 1 FROM comfy_jobs j WHERE j.revision_id=r.id AND j.archive_state='')",
                (terminal_before,),
            ).fetchall():
                release_payloads(connection, "comfy_workflow_revisions", row["id"])
                connection.execute(
                    "UPDATE comfy_workflow_revisions SET config_json='{}' WHERE id=?",
                    (row["id"],),
                )
            # save_revision and create_job are intentionally separate short
            # transactions. Only old unowned revisions can be failed-create
            # leftovers; recent ones may still be awaiting their job insert.
            connection.execute(
                "DELETE FROM comfy_workflow_revisions WHERE created_at<? AND NOT EXISTS("
                "SELECT 1 FROM comfy_jobs j WHERE j.revision_id=comfy_workflow_revisions.id)",
                (min(terminal_before, time.time() - RECOVERY_SECONDS),),
            )
        # The archival transaction has committed. File deletion cannot roll
        # back manifests to live references if a later cleanup step fails.
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            archived = {
                row[0]
                for row in connection.execute(
                    "SELECT id FROM comfy_jobs WHERE archive_state='archived'"
                )
            }
            known_ids = {
                row[0] for row in connection.execute("SELECT id FROM comfy_jobs")
            }
            removed += self.files.remove_terminal_directories(
                archived, known_ids, terminal_before
            )
            removed += self._collect_unreferenced_blobs(connection)
        return removed

    @staticmethod
    def _protected_jobs(connection, terminal_before):
        rows = connection.execute(
            "SELECT id,parent_job_id,status,finished_at,archive_state FROM comfy_jobs"
        ).fetchall()
        protected = {
            row["id"]
            for row in rows
            if not row["archive_state"]
            and (
                row["status"] not in TERMINAL_STATUSES
                or row["status"] == "unknown"
                or (
                    row["status"] == "failed"
                    and (
                        row["finished_at"] is None
                        or row["finished_at"] >= terminal_before
                    )
                )
            )
        }
        # Protect both directions: an unknown child also prevents throwing away
        # its parent's aggregation plan; a recoverable parent retains siblings.
        while True:
            previous = len(protected)
            for row in rows:
                parent_id = row["parent_job_id"]
                if parent_id and (row["id"] in protected or parent_id in protected):
                    protected.update((row["id"], parent_id))
            if len(protected) == previous:
                return protected

    def _protected_by_parent(self, connection, row, terminal_before):
        return row["id"] in self._protected_jobs(connection, terminal_before)
