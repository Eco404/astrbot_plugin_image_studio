"""Durable ComfyUI execution snapshots and detached, restartable local jobs.

Provider credentials are resolved by the runner at execution time. Only workflow
definitions, request parameters and private staged image references belong here.
Remote submissions with an uncertain outcome are never automatically repeated.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
import sqlite3
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ...database.schema import ensure_release_schema
from ...models import GeneratedImage, ReferenceImage

TERMINAL_STATUSES = frozenset(
    {"succeeded", "partial", "failed", "cancelled", "unknown"}
)
JOB_STATUSES = TERMINAL_STATUSES | {
    "queued",
    "preparing",
    "submitting",
    "submitted",
    "running",
    "downloading",
    "finalizing",
}
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SECRET_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "proxy_authorization",
        "password",
        "access_token",
        "refresh_token",
        "client_secret",
        "auth_headers",
    }
)
_LOGGER = logging.getLogger(__name__)


def _json_snapshot(value: Any) -> str:
    def check(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                if str(key).lower().replace("-", "_") in _SECRET_KEYS and child:
                    raise ValueError("ComfyUI 任务快照不能包含密钥或认证信息")
                check(child)
        elif isinstance(item, (tuple, list)):
            for child in item:
                check(child)

    check(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _job_id(value: str) -> str:
    if not _SAFE_ID.fullmatch(value):
        raise ValueError("ComfyUI 任务编号无效")
    return value


class ComfyJobStore:
    """Use the gallery database, with one short-lived connection per operation."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.data_dir = self.db_path.parent
        self.inputs_dir = self.data_dir / "comfyui_inputs"
        self.outputs_dir = self.data_dir / "comfyui_outputs"

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
            connection.execute(
                "INSERT OR IGNORE INTO comfy_workflow_revisions(id,fingerprint,config_json,created_at) VALUES (?,?,?,?)",
                (revision_id, revision_id, payload, time.time()),
            )

    async def get_revision(self, revision_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_revision_sync, revision_id)

    def _get_revision_sync(self, revision_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM comfy_workflow_revisions WHERE id=?", (revision_id,)
            ).fetchone()
        if row is None:
            return None
        return json.loads(row["config_json"])

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
        created_directory = False
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
                    previous["request_json"],
                ) != (provider_id, model_id, revision_id, request_json):
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
                return self._decode(previous)
            try:
                if references:
                    directory.mkdir(parents=True, exist_ok=False)
                    created_directory = True
                    directory.chmod(0o700)
                for index, reference in enumerate(references):
                    relative_path = f"{job_id}/{index}.image"
                    path = self.inputs_dir / relative_path
                    with path.open("xb") as handle:
                        handle.write(reference.data)
                    path.chmod(0o600)
                    staged.append(
                        {
                            "id": reference.id,
                            "filename": reference.filename,
                            "mime_type": reference.mime_type,
                            "path": relative_path,
                            "sha256": hashlib.sha256(reference.data).hexdigest(),
                            "size_bytes": len(reference.data),
                        }
                    )
                now = time.time()
                connection.execute(
                    "INSERT INTO comfy_jobs(id,provider_id,model_id,revision_id,status,request_json,input_refs_json,created_at,updated_at) VALUES (?,?,?,?,'queued',?,?,?,?)",
                    (
                        job_id,
                        provider_id,
                        model_id,
                        revision_id,
                        request_json,
                        _json_snapshot(staged),
                        now,
                        now,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM comfy_jobs WHERE id=?", (job_id,)
                ).fetchone()
            except Exception:
                # This directory was exclusively created for this request above.
                if created_directory:
                    shutil.rmtree(directory)
                raise
        return self._decode(row)

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for column, name in (
            ("request_json", "request"),
            ("input_refs_json", "references"),
            ("output_refs_json", "outputs"),
            ("result_json", "result"),
        ):
            result[name] = json.loads(result.pop(column))
        return result

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_job_sync, job_id)

    def _get_job_sync(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM comfy_jobs WHERE id=?", (job_id,)
            ).fetchone()
        return self._decode(row) if row else None

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
            clauses.append(
                "COALESCE(json_extract(request_json, '$.parent_job_id'), '') = ''"
            )
        if provider_id is not None:
            clauses.append("provider_id=?")
            values.append(provider_id)
        if queue_only:
            # Filter before LIMIT so recent successful runs cannot displace old
            # failures or active jobs from the recoverable WebUI queue.
            clauses.extend(
                (
                    "status != 'succeeded'",
                    "COALESCE(json_extract(result_json, '$.queue_dismissed'), 0) != 1",
                )
            )
        if pending:
            clauses.append(
                "status NOT IN (" + ",".join("?" for _ in TERMINAL_STATUSES) + ")"
            )
            values.extend(sorted(TERMINAL_STATUSES))
        query = "SELECT * FROM comfy_jobs" + (
            " WHERE " + " AND ".join(clauses) if clauses else ""
        )
        query += " ORDER BY created_at " + ("ASC" if pending else "DESC")
        if limit is not None:
            query += " LIMIT ?"
            values.append(limit)
        with self._connect() as connection:
            return [
                self._decode(row)
                for row in connection.execute(query, values).fetchall()
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
                    return self._decode(row)
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
            connection.execute(
                "UPDATE comfy_jobs SET "
                + ",".join(f"{key}=?" for key in updates)
                + " WHERE id=?",
                (*updates.values(), job_id),
            )
            return self._decode(
                connection.execute(
                    "SELECT * FROM comfy_jobs WHERE id=?", (job_id,)
                ).fetchone()
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
            if json.loads(row["result_json"]).get("queue_dismissed") is True:
                return self._decode(row)
            connection.execute(
                "UPDATE comfy_jobs SET result_json=json_set(result_json, '$.queue_dismissed', json('true')),updated_at=? WHERE id=?",
                (time.time(), job_id),
            )
            return self._decode(
                connection.execute(
                    "SELECT * FROM comfy_jobs WHERE id=?", (job_id,)
                ).fetchone()
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
            if not row["remote_id"]:
                child_ids = json.loads(row["result_json"]).get("child_ids") or []
                associated = []
                for child_id in child_ids:
                    child = connection.execute(
                        "SELECT request_json FROM comfy_jobs WHERE id=?", (child_id,)
                    ).fetchone()
                    if child is not None:
                        associated.append(
                            json.loads(child["request_json"]).get("parent_job_id")
                            == job_id
                        )
                if not associated or not all(associated):
                    raise ValueError(
                        "ComfyUI 任务没有已确认的远端编号或子任务，不能恢复或重新提交"
                    )
            if row["status"] not in {"failed", "unknown"}:
                if row["status"] not in TERMINAL_STATUSES:
                    return self._decode(row)
                raise ValueError("ComfyUI 任务已经结束，不需要恢复")
            connection.execute(
                "UPDATE comfy_jobs SET status='submitted',error='',finished_at=NULL,"
                "result_json=json_remove(result_json, '$.queue_dismissed'),updated_at=? WHERE id=?",
                (time.time(), job_id),
            )
            return self._decode(
                connection.execute(
                    "SELECT * FROM comfy_jobs WHERE id=?", (job_id,)
                ).fetchone()
            )

    async def load_references(self, job_id: str) -> tuple[ReferenceImage, ...]:
        return await asyncio.to_thread(self._load_references_sync, _job_id(job_id))

    def _load_references_sync(self, job_id: str) -> tuple[ReferenceImage, ...]:
        job = self._get_job_sync(job_id)
        if job is None:
            raise ValueError("ComfyUI 任务不存在")
        images = []
        directory = (self.inputs_dir / job_id).resolve()
        for metadata in job["references"]:
            path = (self.inputs_dir / metadata["path"]).resolve()
            if path.parent != directory:
                raise ValueError("ComfyUI 任务参考图路径无效")
            try:
                data = path.read_bytes()
            except OSError as exc:
                raise ValueError(
                    f"ComfyUI 任务参考图不可用：{metadata['filename']}"
                ) from exc
            if (
                len(data) != metadata["size_bytes"]
                or hashlib.sha256(data).hexdigest() != metadata["sha256"]
            ):
                raise ValueError(
                    f"ComfyUI 任务参考图内容发生变化：{metadata['filename']}"
                )
            images.append(
                ReferenceImage(
                    metadata["id"], metadata["filename"], data, metadata["mime_type"]
                )
            )
        return tuple(images)

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
        directory = self.outputs_dir / job_id
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
                return previous
            directory.mkdir(parents=True, exist_ok=True)
            directory.chmod(0o700)
            for index, image in enumerate(images):
                digest = hashlib.sha256(image.data).hexdigest()
                filename = f"{index}-{digest}.image"
                destination = directory / filename
                if not destination.exists():
                    temporary = directory / f"{uuid.uuid4().hex}.tmp"
                    try:
                        with temporary.open("xb") as handle:
                            handle.write(image.data)
                        temporary.chmod(0o600)
                        temporary.replace(destination)
                    finally:
                        temporary.unlink(missing_ok=True)
                manifest.append(
                    {
                        "path": f"{job_id}/{filename}",
                        "sha256": digest,
                        "mime_type": image.mime_type,
                        "size_bytes": len(image.data),
                        "effective_parameters": image.effective_parameters,
                        "response_index": image.response_index,
                    }
                )
            connection.execute(
                "UPDATE comfy_jobs SET output_refs_json=?,updated_at=? WHERE id=?",
                (_json_snapshot(manifest), time.time(), job_id),
            )
        return manifest

    async def load_outputs(self, job_id: str) -> tuple[GeneratedImage, ...]:
        return await asyncio.to_thread(self._load_outputs_sync, _job_id(job_id))

    def _load_outputs_sync(self, job_id: str) -> tuple[GeneratedImage, ...]:
        job = self._get_job_sync(job_id)
        if job is None:
            raise ValueError("ComfyUI 任务不存在")
        directory = (self.outputs_dir / job_id).resolve()
        outputs = []
        for index, item in enumerate(job["outputs"]):
            path = (self.outputs_dir / item["path"]).resolve()
            if path.parent != directory:
                raise ValueError("ComfyUI 输出图片路径无效")
            try:
                data = path.read_bytes()
            except OSError as exc:
                raise ValueError(f"ComfyUI 第 {index + 1} 张输出图片已不可用") from exc
            if (
                len(data) != item["size_bytes"]
                or hashlib.sha256(data).hexdigest() != item["sha256"]
            ):
                raise ValueError(f"ComfyUI 第 {index + 1} 张输出图片内容发生变化")
            outputs.append(
                GeneratedImage(
                    data,
                    item["mime_type"],
                    item["effective_parameters"],
                    item["response_index"],
                )
            )
        return tuple(outputs)

    async def cleanup_terminal_files(self, *, terminal_before: float) -> int:
        """Remove expired terminal job staging, never gallery assets or revisions."""
        return await asyncio.to_thread(self._cleanup_inputs_sync, terminal_before)

    def _cleanup_inputs_sync(self, terminal_before: float) -> int:
        removed = 0
        with self._connect() as connection:
            # Explicit resume also takes this lock. It cannot turn a selected
            # terminal job active between the eligibility check and file removal.
            connection.execute("BEGIN IMMEDIATE")
            jobs = connection.execute(
                "SELECT id,request_json FROM comfy_jobs WHERE status IN ('succeeded','partial','failed','cancelled') AND finished_at < ?",
                (terminal_before,),
            ).fetchall()
            for row in jobs:
                parent_id = json.loads(row["request_json"]).get("parent_job_id")
                if parent_id:
                    parent = connection.execute(
                        "SELECT status FROM comfy_jobs WHERE id=?", (parent_id,)
                    ).fetchone()
                    if parent is not None and parent["status"] not in TERMINAL_STATUSES:
                        continue
                for root in (self.inputs_dir, self.outputs_dir):
                    directory = root / _job_id(row["id"])
                    if directory.is_dir():
                        shutil.rmtree(directory)
                        removed += 1
        return removed


class ComfyJobManager:
    """Keep execution alive across browser disconnects; callbacks own transport."""

    def __init__(
        self,
        store: ComfyJobStore,
        run: Callable[[dict[str, Any]], Awaitable[Mapping[str, Any]]],
    ):
        self.store = store
        self.run = run
        self._tasks: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    async def submit(
        self,
        *,
        provider_id: str,
        model_id: str,
        workflow: Mapping[str, Any],
        request: Mapping[str, Any],
        references: Sequence[ReferenceImage] = (),
        job_id: str | None = None,
    ) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("ComfyUI 任务管理器已关闭")
        revision_id = await self.store.save_revision(workflow)
        job = await self.store.create_job(
            provider_id=provider_id,
            model_id=model_id,
            revision_id=revision_id,
            request=request,
            references=references,
            job_id=job_id,
        )
        await self._start(job)
        return job

    async def _start(self, job: dict[str, Any]) -> None:
        async with self._lock:
            if (
                self._closed
                or job["status"] in TERMINAL_STATUSES
                or job["id"] in self._tasks
            ):
                return
            task = asyncio.create_task(
                self._execute(job["id"]), name=f"comfyui-{job['id']}"
            )
            self._tasks[job["id"]] = task
            task.add_done_callback(
                lambda finished, identifier=job["id"]: self._task_finished(
                    identifier, finished
                )
            )

    def _task_finished(self, job_id: str, task: asyncio.Task) -> None:
        if self._tasks.get(job_id) is task:
            self._tasks.pop(job_id)
        if not task.cancelled() and (error := task.exception()) is not None:
            _LOGGER.error(
                "Could not persist ComfyUI task %s outcome",
                job_id,
                exc_info=(type(error), error, error.__traceback__),
            )

    async def _execute(self, job_id: str) -> None:
        job = await self.store.get_job(job_id)
        if job is None or job["status"] in TERMINAL_STATUSES:
            return
        try:
            result = dict(await self.run(job))
            current = await self.store.get_job(job_id)
            if current and current["status"] not in TERMINAL_STATUSES:
                status = "partial" if result.get("status") == "partial" else "succeeded"
                await self.store.update_job(
                    job_id,
                    status=status,
                    result=result,
                    generation_id=str(result.get("generation_id") or ""),
                    error="",
                )
        except asyncio.CancelledError:
            # Closing the plugin cancels its waiter, not the remote generation.
            # The persisted phase determines safe recovery on the next startup.
            raise
        except Exception as exc:  # noqa: BLE001 - detached runner must persist unexpected failures
            current = await self.store.get_job(job_id)
            if current and current["status"] not in TERMINAL_STATUSES:
                uncertain = (
                    current["status"] == "submitting" and not current["remote_id"]
                )
                await self.store.update_job(
                    job_id, status="unknown" if uncertain else "failed", error=str(exc)
                )

    async def wait(self, job_id: str) -> dict[str, Any]:
        task = self._tasks.get(job_id)
        if task is not None:
            await asyncio.shield(task)
        job = await self.store.get_job(job_id)
        if job is None:
            raise ValueError("ComfyUI 任务不存在")
        return job

    async def resume_pending(self) -> list[dict[str, Any]]:
        pending = await self.store.get_pending()
        for job in pending:
            if job["id"] in self._tasks:
                continue
            if job["status"] == "submitting" and not job["remote_id"]:
                await self.store.update_job(
                    job["id"],
                    status="unknown",
                    expected_status="submitting",
                    error="上次提交中断，无法确认 ComfyUI 是否已经接收任务；未自动重复提交，请检查服务端队列。",
                )
            else:
                await self._start(job)
        return pending

    async def resume(self, job_id: str) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("ComfyUI 任务管理器已关闭")
        active = self._tasks.get(job_id)
        if active is not None:
            current = await self.store.get_job(job_id)
            if current and current["status"] not in TERMINAL_STATUSES:
                return current
            # A terminal DB write can become visible before the old callback has
            # returned. Finish retiring that callback before scheduling recovery.
            await asyncio.shield(active)
            if self._tasks.get(job_id) is active:
                self._tasks.pop(job_id)
        job = await self.store.resume_job(job_id)
        await self._start(job)
        return job

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            tasks = tuple(self._tasks.values())
            for task in tasks:
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
