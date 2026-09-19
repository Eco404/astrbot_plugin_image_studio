"""Detached ComfyUI task scheduling and restart recovery."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from astrbot.api import logger

from ...models import ReferenceImage
from .job_store import ComfyJobStore
from .job_types import TERMINAL_STATUSES


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
            logger.error(
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
                await self.store.release_gallery_outputs(
                    job_id, str(result.get("generation_id") or "")
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

    async def wait(
        self, job_id: str, *, timeout_seconds: float | None = None
    ) -> dict[str, Any]:
        """Return the current job after a bounded wait without cancelling execution."""
        task = self._tasks.get(job_id)
        if task is not None:
            if timeout_seconds is None:
                await asyncio.shield(task)
            elif timeout_seconds > 0:
                done, _pending = await asyncio.wait({task}, timeout=timeout_seconds)
                if done:
                    # Preserve the existing propagation of runner persistence errors.
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
