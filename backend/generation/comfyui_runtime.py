"""Durable ComfyUI generation shared by WebUI, commands and Agent tools."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
import uuid
from dataclasses import replace

from ..models import (
    PARAMETER_POLICY_FIELDS,
    GenerationRequest,
    GenerationResult,
    ImageProvider,
    InvocationSource,
    ReferenceImage,
)
from ..providers.comfyui.client import (
    ComfyClient,
    ComfyExecutionError,
    normalize_workflow,
    prepare_submission_workflow,
)
from ..providers.comfyui.job_manager import ComfyJobManager
from ..providers.comfyui.job_store import ComfyJobStore
from ..providers.comfyui.job_types import RECOVERY_SECONDS
from ..providers.comfyui.output_metadata import prepare_output
from ..providers.comfyui.storage import compact_request
from ..providers.comfyui.workflows import FIXED_OUTPUT_POLICY, migrate_fixed_outputs
from ..providers.executor import ProviderError, ProviderPartialResponseError
from .service import ImageGenerationService


def connection_fingerprint(provider: ImageProvider) -> str:
    # Credentials can rotate; an address change must never recover a task from
    # a different ComfyUI instance just because its provider ID was reused.
    return hashlib.sha256(provider.base_url.rstrip("/").encode()).hexdigest()


class ComfyRuntime:
    def __init__(self, service: ImageGenerationService):
        self.service = service
        self.store = ComfyJobStore(service.store.db_path)
        self.manager = ComfyJobManager(self.store, self._run)

    async def start(self):
        await self.store.initialize()
        self.service.store.on_results_removed = self.store.reconcile_gallery_outputs
        await self.manager.resume_pending()

    async def close(self):
        await self.manager.close()
        if (
            self.service.store.on_results_removed
            == self.store.reconcile_gallery_outputs
        ):
            self.service.store.on_results_removed = None

    async def submit(
        self,
        *,
        provider,
        model,
        references=(),
        comfyui=None,
        temporary: bool = False,
        **values,
    ):
        config = normalize_workflow(comfyui or model.comfyui)
        snapshot = copy.deepcopy(model.public_dict())
        snapshot["comfyui"] = config
        if isinstance(config.get("parameters_schema"), dict):
            parameters = copy.deepcopy(config["parameters_schema"])
            for key, descriptor in parameters.items():
                current = model.parameters.get(key)
                if isinstance(descriptor, dict) and isinstance(current, dict):
                    for flag in PARAMETER_POLICY_FIELDS:
                        descriptor[flag] = current.get(flag, True)
            snapshot["parameters"] = parameters
        migrated = migrate_fixed_outputs(
            config, snapshot.get("parameters"), snapshot.get("tool")
        )
        config = migrated["comfyui"]
        snapshot.update(
            comfyui=config, parameters=migrated["parameters"], tool=migrated["tool"]
        )
        identity = values.get("invocation_source")
        if isinstance(identity, InvocationSource):
            values["invocation_source"] = identity.public_dict()
        request = {
            "values": values,
            "model": snapshot,
            "provider_name": provider.name,
            "connection": connection_fingerprint(provider),
            "temporary": bool(temporary),
        }
        return await self.manager.submit(
            provider_id=provider.id,
            model_id=model.id,
            workflow=config,
            request=request,
            references=references,
        )

    async def generate(self, **values) -> GenerationResult:
        job = await self.submit(**values)
        job = await self.manager.wait(job["id"])
        if job["status"] not in {"succeeded", "partial"}:
            raise ProviderError(
                job.get("error") or f"ComfyUI 任务状态：{job['status']}"
            )
        return await self.result(job)

    async def _provider(self, job):
        current = self.service.settings.provider(job["provider_id"])
        if current is None or current.kind != "comfyui":
            raise ProviderError(
                "任务所属 ComfyUI 已删除或停用，请恢复该服务商后检查远端任务"
            )
        if connection_fingerprint(current) != job["request"]["connection"]:
            raise ProviderError("ComfyUI 地址已改变，无法在新服务器上恢复旧任务")
        raw = current.public_dict()
        raw["models"] = [job["request"]["model"]]
        # Discovery is live configuration; execution limits belong to the job's
        # resolved model snapshot, including legacy remote capability values.
        raw["discovered_models"] = []
        return ImageProvider.from_mapping(raw, migrate_comfyui=False)

    async def _run(self, job):
        if job["request"].get("parent_job_id"):
            parent_state = await self.store.get_job(job["request"]["parent_job_id"])
            if parent_state and parent_state["status"] == "cancelled":
                await self.store.update_job(job["id"], status="cancelled")
                raise asyncio.CancelledError
        provider = await self._provider(job)
        runtime = self
        client = ComfyClient(self.service.executor.session)
        config = await self.store.get_revision(job["revision_id"])
        fixed_outputs = config.get("execution_policy") == FIXED_OUTPUT_POLICY
        missing_reference_warning = ""
        try:
            references = await self.store.load_references(job["id"])
        except (OSError, ValueError):
            if not (
                job.get("remote_id") or (job.get("result") or {}).get("child_ids")
            ) or job.get("status") in {
                "succeeded",
                "partial",
                "cancelled",
            }:
                raise
            # Monitoring an acknowledged remote prompt no longer needs uploaded
            # inputs. Preserve their count for request validation but never save
            # empty placeholders as gallery reference assets or upload them again.
            references = tuple(
                ReferenceImage(item["id"], item["filename"], b"", item["mime_type"])
                for item in job.get("references", ())
            )
            missing_reference_warning = (
                "原任务参考图已不可用；已恢复远端结果，未保留缺失的参考图。"
            )
        values = dict(job["request"]["values"])
        identity = values.pop("invocation_source", None) or {}
        values.pop("comfyui", None)
        is_child = bool(job["request"].get("parent_job_id"))

        class ChildGalleryStore:
            """A child owns staging only; its parent writes the final gallery group."""

            def __getattr__(self, name):
                return getattr(runtime.service.store, name)

            async def record_success(self, **_kwargs):
                return ""

            async def discard_staged_references(self, _references):
                return None

        class RecoveryGalleryStore:
            def __getattr__(self, name):
                return getattr(runtime.service.store, name)

            async def record_success(self, **kwargs):
                kwargs["request"] = replace(kwargs["request"], references=())
                return await runtime.service.store.record_success(**kwargs)

        class Executor:
            async def generate_batch(self, selected_provider, request):
                return await runtime._run_children(
                    job, selected_provider, request, config
                )

            async def generate(self, selected_provider, request):
                saved = await runtime.store.get_job(job["id"])
                if saved["status"] == "cancelled":
                    raise asyncio.CancelledError
                if saved["status"] in {"submitting", "unknown"} and not saved.get(
                    "remote_id"
                ):
                    # Recovery can reach a child through its parent before the
                    # manager has classified interrupted submissions as unknown.
                    # An unacknowledged POST must never be sent a second time.
                    await runtime.store.update_job(
                        job["id"],
                        status="unknown",
                        error="提交结果未知，未重复提交 ComfyUI 任务",
                    )
                    raise ProviderError(
                        "提交结果未知，请先核实远端任务，不能自动重复提交"
                    )
                output_limit = None
                if fixed_outputs:
                    output_limit = request.count
                    previous_limit = (saved.get("result") or {}).get("collection_limit")
                    if previous_limit is not None and previous_limit != output_limit:
                        raise ProviderError("ComfyUI 图片收集额度与保存的任务不一致")
                    if previous_limit is None:
                        await runtime.store.update_job(
                            job["id"],
                            result={
                                **(saved.get("result") or {}),
                                "collection_limit": output_limit,
                            },
                        )
                cached = await runtime.store.load_outputs(job["id"])
                if cached:
                    warning = (saved.get("result") or {}).get("partial_warning")
                    if warning:
                        raise ProviderPartialResponseError(cached, ((1, warning),))
                    return cached[:output_limit] if output_limit is not None else cached
                remote_id = saved.get("remote_id", "")
                graph = (saved.get("result") or {}).get("api_graph")
                if not remote_id:
                    from ..providers.comfyui.client import prepare_graph

                    placeholders = [
                        f"reference-{index}.png"
                        for index in range(len(request.references))
                    ]
                    prepared = prepare_graph(config, request, placeholders)
                    report = await client.inspect(
                        selected_provider,
                        {
                            **config,
                            "api_graph": prepared,
                            "api_graph_json": json.dumps(prepared, ensure_ascii=False),
                            "bindings": {
                                key: item
                                for key, item in config["bindings"].items()
                                if item["source"] != "reference"
                                or item["reference_index"] < len(request.references)
                            },
                        },
                    )
                    issues = [
                        item
                        for item in report.get("issues", [])
                        if item.get("severity", "error") == "error"
                    ]
                    if issues:
                        raise ProviderError(
                            "；".join(
                                " ".join(
                                    filter(
                                        None,
                                        (
                                            f"节点 #{item['node_id']}"
                                            if item.get("node_id")
                                            else "",
                                            str(item.get("class_type") or ""),
                                            str(item.get("input_name") or ""),
                                            item.get("message", str(item)),
                                        ),
                                    )
                                )
                                for item in issues
                            )
                        )
                    uploaded = await client.upload_references(
                        selected_provider, request.references, config=config
                    )
                    written_inputs = set()
                    graph = prepare_graph(
                        config, request, uploaded, written_inputs=written_inputs
                    )
                    synchronized = prepare_submission_workflow(
                        config, graph, targets=written_inputs
                    )
                    if (await runtime.store.get_job(job["id"], light=True))[
                        "status"
                    ] == "cancelled":
                        raise asyncio.CancelledError
                    prepared_state = await runtime.store.get_job(job["id"])
                    submitted_result = {
                        **(prepared_state.get("result") or {}),
                        "api_graph": graph,
                        "workflow": synchronized["workflow"],
                        "workflow_json": json.dumps(
                            synchronized["workflow"], ensure_ascii=False
                        )
                        if synchronized["workflow"] is not None
                        else None,
                        "workflow_sync_warnings": synchronized["warnings"],
                    }
                    await runtime.store.update_job(
                        job["id"],
                        status="submitting",
                        result=submitted_result,
                    )
                    try:
                        submitted = await client.submit(
                            selected_provider,
                            graph,
                            synchronized["workflow"],
                            client_id=job["id"],
                        )
                    except ComfyExecutionError as exc:
                        if not exc.unknown_submission:
                            await runtime.store.update_job(
                                job["id"], status="failed", error=str(exc)
                            )
                        raise
                    except (asyncio.CancelledError, Exception):
                        # A lost response can hide a successful remote submission.
                        # Keep this phase durable; it must never be auto-resubmitted.
                        raise
                    remote_id = submitted["prompt_id"]
                    await runtime.store.update_job(
                        job["id"],
                        status="submitted",
                        remote_id=remote_id,
                        result={
                            **submitted_result,
                            "node_errors": submitted.get("node_errors", {}),
                        },
                    )

                async def progress(value):
                    state = await runtime.store.update_progress(
                        job["id"],
                        value,
                        status="running"
                        if value.get("status") == "running"
                        else "submitted",
                    )
                    if state == "cancelled":
                        raise asyncio.CancelledError

                latest = await runtime.store.get_job(job["id"])
                node_errors = (latest.get("result") or {}).get("node_errors")
                failure = (
                    "ComfyUI 部分节点未通过校验："
                    + json.dumps(node_errors, ensure_ascii=False)[:2000]
                    if node_errors
                    else None
                )
                try:
                    history = await client.wait(
                        selected_provider,
                        remote_id,
                        on_progress=progress,
                        client_id=job["id"],
                    )
                except ComfyExecutionError as exc:
                    history = getattr(exc, "history", None)
                    if not history:
                        raise ProviderError(str(exc)) from exc
                    failure = str(exc)
                try:
                    images = await client.download_outputs(
                        selected_provider,
                        history,
                        config["outputs"],
                        **(
                            {"output_limit": output_limit}
                            if output_limit is not None
                            else {}
                        ),
                    )
                except ComfyExecutionError as exc:
                    images = getattr(exc, "images", ())
                    if not images:
                        raise ProviderError(str(exc)) from exc
                    failure = str(exc)
                if output_limit is not None:
                    images = images[:output_limit]
                if not images:
                    raise ProviderError("ComfyUI 本轮未取得可用图片，未追加执行")
                actual_graph = graph or config["api_graph"]
                execution_state = (await runtime.store.get_job(job["id"])).get(
                    "result"
                ) or {}
                submitted_workflow = execution_state.get(
                    "workflow", config.get("workflow")
                )
                snapshot = {
                    **config,
                    "api_graph": actual_graph,
                    "api_graph_json": json.dumps(actual_graph, ensure_ascii=False),
                    "workflow": copy.deepcopy(submitted_workflow),
                    "workflow_json": json.dumps(submitted_workflow, ensure_ascii=False)
                    if submitted_workflow is not None
                    else None,
                    "workflow_sync_warnings": execution_state.get(
                        "workflow_sync_warnings", []
                    ),
                    "parameters_schema": copy.deepcopy(
                        selected_provider.get_model(request.model).parameters
                    ),
                    **(
                        {
                            "temporary": True,
                            "workflow_name": selected_provider.get_model(
                                request.model
                            ).name,
                        }
                        if job["request"].get("temporary")
                        else {}
                    ),
                }
                if (await runtime.store.get_job(job["id"], light=True))[
                    "status"
                ] == "cancelled":
                    raise asyncio.CancelledError
                images = await asyncio.to_thread(
                    lambda: tuple(prepare_output(image, snapshot) for image in images)
                )
                images = tuple(
                    replace(
                        image,
                        effective_parameters={
                            **image.effective_parameters,
                            "comfy_job_id": job["id"],
                        },
                    )
                    for image in images
                )
                await runtime.store.save_outputs(job["id"], images)
                if failure:
                    current = await runtime.store.get_job(job["id"])
                    await runtime.store.update_job(
                        job["id"],
                        result={
                            **(current.get("result") or {}),
                            "partial_warning": failure,
                        },
                    )
                    raise ProviderPartialResponseError(images, ((1, failure),))
                return images

        isolated = ImageGenerationService(
            settings=replace(self.service.settings, providers=(provider,)),
            executor=Executor(),
            store=(
                ChildGalleryStore()
                if is_child
                else RecoveryGalleryStore()
                if missing_reference_warning
                else self.service.store
            ),
            concurrency=self.service.concurrency,
        )
        result = await isolated.generate(
            **values,
            provider_id=provider.id,
            model=job["model_id"],
            references=references,
            invocation_source=InvocationSource(**identity),
            _comfy_job_id=job["id"],
            _comfy_child_count=values.get("count") if is_child else None,
        )
        persisted = (await self.store.get_job(job["id"])).get("result") or {}
        sync_notice = "；".join(
            dict.fromkeys(
                message
                for image in result.images
                for message in image.effective_parameters.get("_comfyui", {}).get(
                    "workflow_sync_warnings", []
                )
                if isinstance(message, str) and message
            )
        )
        return {
            **{
                key: persisted[key]
                for key in (
                    "batch_plan",
                    "collection_limit",
                    "api_graph",
                    "workflow",
                    "workflow_json",
                    "workflow_sync_warnings",
                )
                if key in persisted
            },
            **(
                {
                    "child_ids": persisted["child_ids"],
                    "progress": persisted.get("progress"),
                }
                if persisted.get("child_ids")
                else {}
            ),
            "generation_id": result.generation_id,
            "resolved_request": {
                "mode": result.request.mode,
                "prompt": result.request.prompt,
                "negative_prompt": result.request.negative_prompt,
                "size": result.request.size,
                "count": result.request.count,
                "parameters": result.request.parameters,
                "source": result.request.source,
                "invocation_source": result.request.invocation_source.public_dict(),
            },
            "warning": "；".join(
                filter(None, (result.warning, missing_reference_warning))
            ),
            "elapsed_ms": result.elapsed_ms,
            "provider_name": provider.name,
            "model": job["model_id"],
            "status": "partial"
            if (
                persisted.get("partial_warning")
                if fixed_outputs
                else result.warning and result.warning != sync_notice
            )
            else "succeeded",
        }

    async def _run_children(self, parent, provider, request, config):
        """Persist fixed execution quotas, or resume a legacy native-batch plan."""
        saved = await self.store.get_job(parent["id"])
        if saved["status"] == "cancelled":
            raise asyncio.CancelledError
        cached = await self.store.load_outputs(parent["id"])
        if cached:
            warning = (saved.get("result") or {}).get("partial_warning")
            if warning:
                raise ProviderPartialResponseError(cached, ((1, warning),))
            return cached
        model = provider.get_model(request.model)
        native = max(1, model.native_batch_size)
        allowed = (
            model.llm_exposed_parameter_names if request.source == "llm_tool" else None
        )
        public_keys = {
            str(descriptor.get("request_key") or name): name
            for name, descriptor in model.active_parameters.items()
            if not descriptor.get("ui_only") and (allowed is None or name in allowed)
        }
        child_parameters = {}
        for wire_key, value in request.parameters.items():
            public_key = public_keys.get(wire_key)
            if public_key is not None:
                child_parameters[public_key] = value
            elif allowed is None:
                child_parameters[wire_key] = value
        sizes = [
            min(native, request.count - offset)
            for offset in range(0, request.count, native)
        ]
        fixed_outputs = config.get("execution_policy") == FIXED_OUTPUT_POLICY
        batch_plan = {
            "execution_policy": config.get("execution_policy", "legacy_count_binding"),
            "count": request.count,
            "images_per_run": native,
            "quotas": sizes,
        }
        previous_plan = (saved.get("result") or {}).get("batch_plan")
        if previous_plan is not None and previous_plan != batch_plan:
            raise ProviderError("ComfyUI 执行计划与保存的任务不一致，未重新提交")
        child_ids = [
            uuid.uuid5(
                uuid.NAMESPACE_URL, f"image-studio-comfy:{parent['id']}:{index}"
            ).hex
            for index in range(len(sizes))
        ]
        previous_ids = (saved.get("result") or {}).get("child_ids")
        if previous_ids and previous_ids != child_ids:
            raise ProviderError("ComfyUI 批次计划与保存的任务不一致，未重新提交")
        await self.store.update_job(
            parent["id"],
            status="running",
            result={
                **(saved.get("result") or {}),
                "child_ids": child_ids,
                "request_sizes": sizes,
                "batch_plan": batch_plan,
                "progress": {"status": "running", "completed": 0, "total": len(sizes)},
            },
        )
        revision_id = parent["revision_id"]
        children = []
        for index, (child_id, size) in enumerate(zip(child_ids, sizes)):
            if (await self.store.get_job(parent["id"], light=True))[
                "status"
            ] == "cancelled":
                raise asyncio.CancelledError
            values = {
                "mode": request.mode,
                "prompt": request.prompt,
                "negative_prompt": request.negative_prompt,
                "size": request.size,
                "count": size,
                "parameters": child_parameters,
                "source": request.source,
                "invocation_source": request.invocation_source.public_dict(),
            }
            child_request = {
                **parent["request"],
                "values": values,
                "parent_job_id": parent["id"],
                "chunk_index": index,
            }
            child = await self.store.get_job(child_id)
            if child is None:
                if any(not item.data for item in request.references):
                    raise ProviderError(
                        "任务参考图已不可用，不能提交尚未创建的子任务；已确认的远端任务可单独恢复"
                    )
                child = await self.store.create_job(
                    provider_id=provider.id,
                    model_id=request.model,
                    revision_id=revision_id,
                    request=child_request,
                    references=request.references,
                    job_id=child_id,
                )
            elif (
                child.get("archive_state")
                or child["provider_id"] != provider.id
                or child["model_id"] != request.model
                or child["revision_id"] != revision_id
                or compact_request(child["request"]) != compact_request(child_request)
            ):
                raise ProviderError(
                    "ComfyUI 子任务与保存的批次请求不一致或已归档，不能重复提交"
                )
            if (await self.store.get_job(parent["id"], light=True))[
                "status"
            ] == "cancelled":
                if child["status"] not in {
                    "succeeded",
                    "partial",
                    "failed",
                    "cancelled",
                    "unknown",
                }:
                    await self.cancel(child_id)
                raise asyncio.CancelledError
            if child["status"] in {"failed", "unknown"} and child.get("remote_id"):
                child = await self.manager.resume(child_id)
            else:
                await self.manager._start(child)
            children.append(child)

        progress_lock = asyncio.Lock()
        completed_count = 0

        async def wait_child(child):
            nonlocal completed_count
            try:
                completed_child = await self.manager.wait(child["id"])
            except asyncio.CancelledError:
                completed_child = await self.store.get_job(child["id"])
                parent_state = await self.store.get_job(parent["id"])
                if (
                    completed_child["status"] != "cancelled"
                    or parent_state["status"] == "cancelled"
                ):
                    raise
                # A cancelled child is a terminal outcome to aggregate. A
                # plugin shutdown still propagates cancellation through waits.
                if self.manager._closed:
                    raise
            async with progress_lock:
                completed_count += 1
                current = await self.store.get_job(parent["id"], light=True)
                if current["status"] not in {
                    "succeeded",
                    "partial",
                    "failed",
                    "cancelled",
                    "unknown",
                }:
                    await self.store.update_progress(
                        parent["id"],
                        {
                            "status": "running",
                            "completed": completed_count,
                            "total": len(sizes),
                        },
                    )
            return completed_child

        completed = await asyncio.gather(*(wait_child(child) for child in children))
        if (await self.store.get_job(parent["id"], light=True))[
            "status"
        ] == "cancelled":
            raise asyncio.CancelledError
        images, failures = [], []
        for index, child in enumerate(completed):
            if child["status"] in {"succeeded", "partial"}:
                try:
                    outputs = await self.store.load_outputs(child["id"])
                except (ValueError, OSError) as exc:
                    failures.append((index + 1, str(exc)))
                    continue
                if fixed_outputs:
                    outputs = outputs[: sizes[index]]
                start_index = len(images)
                images.extend(
                    replace(
                        image,
                        response_index=start_index + offset + 1,
                        effective_parameters={
                            **image.effective_parameters,
                            "comfy_job_id": parent["id"],
                            "comfy_child_job_id": child["id"],
                        },
                    )
                    for offset, image in enumerate(outputs)
                )
                if child["status"] == "partial":
                    failures.append(
                        (
                            index + 1,
                            (child.get("result") or {}).get("warning")
                            or "部分输出失败",
                        )
                    )
            else:
                failures.append(
                    (index + 1, child.get("error") or f"子任务状态：{child['status']}")
                )
        current = await self.store.get_job(parent["id"])
        warning = "；".join(
            f"第 {index} 批（计划 {sizes[index - 1]} 张）：{reason}"
            for index, reason in failures
        )
        await self.store.update_job(
            parent["id"],
            result={
                **(current.get("result") or {}),
                "partial_warning": warning if images else "",
                "progress": {
                    "status": "completed",
                    "completed": len(completed),
                    "total": len(sizes),
                },
            },
        )
        if not images:
            raise ProviderError("ComfyUI 批次没有返回图片：" + warning)
        if (await self.store.get_job(parent["id"], light=True))[
            "status"
        ] == "cancelled":
            raise asyncio.CancelledError
        await self.store.save_outputs(parent["id"], tuple(images))
        if failures:
            raise ProviderPartialResponseError(tuple(images), tuple(failures))
        return tuple(images)

    async def result(self, job) -> GenerationResult:
        current = next(
            (
                provider
                for provider in self.service.settings.providers
                if provider.id == job["provider_id"]
            ),
            None,
        )
        raw = (
            current.public_dict()
            if current
            else {
                "id": job["provider_id"],
                "kind": "comfyui",
                "name": job["request"].get("provider_name", "ComfyUI"),
            }
        )
        raw["models"] = [job["request"]["model"]]
        raw["discovered_models"] = []
        provider = ImageProvider.from_mapping(raw, migrate_comfyui=False)
        details = job.get("result") or {}
        values = details.get("resolved_request") or job["request"]["values"]
        terminal = job.get("status") in {"succeeded", "partial"}
        warning = details.get("warning", "")
        generation_id = str(
            details.get("generation_id") or job.get("generation_id") or ""
        )
        try:
            # Once published, gallery links define which images still exist.
            # A protected sibling cache or pre-migration cache must not undo a
            # user's deletion from this generation.
            images = (
                ()
                if terminal and generation_id
                else await self.store.load_outputs(job["id"])
            )
        except (OSError, ValueError):
            if not terminal:
                raise
            images = ()
        if terminal and not images:
            from ..models import GeneratedImage

            gallery = (
                await self.service.store.generation_detail(generation_id, light=True)
                if generation_id
                else None
            )
            descriptors = job.get("outputs") or []
            recovered = []
            # The gallery can lose individual links after a user deletion. Match
            # both the original ordinal and digest, never restore deleted images
            # through another generation that happens to share the same asset.
            for item in (gallery or {}).get("images", []):
                ordinal = item.get("ordinal", -1)
                if not isinstance(ordinal, int) or not 0 <= ordinal < len(descriptors):
                    continue
                descriptor = descriptors[ordinal]
                if item.get("sha256") != descriptor.get("sha256"):
                    continue
                resolved = await self.service.store.gallery_image_file(item["id"])
                if resolved is None:
                    continue
                path, mime_type, _ = resolved
                try:
                    content = await asyncio.to_thread(path.read_bytes)
                except OSError:
                    continue
                if len(content) != descriptor.get("size_bytes") or hashlib.sha256(
                    content
                ).hexdigest() != descriptor.get("sha256"):
                    continue
                recovered.append(
                    GeneratedImage(
                        content,
                        mime_type,
                        await self.store.output_parameters(
                            {
                                **descriptor,
                                "storage": "gallery",
                                "gallery_image_id": item["id"],
                                "gallery_generation_id": generation_id,
                            }
                        ),
                        descriptor.get("response_index"),
                    )
                )
            images = tuple(recovered)
            if not images:
                raise ValueError(
                    "任务临时图片已过期或不可用，画廊中也没有保留的原图；"
                    "请检查画廊记录，或重新运行工作流。"
                )
            if len(images) < len(descriptors):
                warning = "；".join(
                    filter(
                        None,
                        [
                            warning,
                            f"任务原有 {len(descriptors)} 张图片，画廊现有 {len(images)} 张可读取原图；已返回仍保留的图片。",
                        ],
                    )
                )
        try:
            references = await self.store.load_references(job["id"])
        except (OSError, ValueError):
            if not terminal:
                raise
            # Result viewing never restores expired or explicitly deleted
            # reference links. Execution/recovery above still requires all inputs.
            references = ()
        request = GenerationRequest(
            mode=values.get("mode", "text2img"),
            provider_id=provider.id,
            model=job["model_id"],
            prompt=values.get("prompt", ""),
            negative_prompt=values.get("negative_prompt") or "",
            size=values.get("size", ""),
            count=values.get("count") or 1,
            parameters=values.get("parameters") or {},
            references=references,
            source=values.get("source", "webui"),
            invocation_source=InvocationSource(
                **(values.get("invocation_source") or {})
            ),
        )
        return GenerationResult(
            provider,
            request,
            images,
            details.get("elapsed_ms", 0),
            generation_id=details.get("generation_id", ""),
            warning=warning,
        )

    async def cancel(self, job_id):
        job = await self.store.get_job(job_id)
        if not job:
            raise ValueError("ComfyUI 任务不存在")
        if job["status"] in {"succeeded", "partial", "failed", "cancelled", "unknown"}:
            return job
        child_ids = (job.get("result") or {}).get("child_ids", [])
        if child_ids:
            unresolved = []
            for child_id in child_ids:
                child = await self.store.get_job(child_id)
                if child is None:
                    continue
                if child["request"].get("parent_job_id") != job_id:
                    raise ValueError("ComfyUI 子任务关联无效，未取消其他任务")
                try:
                    outcome = await self.cancel(child_id)
                    confirmed = outcome["status"] in {
                        "succeeded",
                        "partial",
                        "cancelled",
                    } or (
                        outcome["status"] == "failed" and not outcome.get("remote_id")
                    )
                    if not confirmed:
                        unresolved.append(
                            f"{child_id}（提交结果未知）"
                            if outcome["status"] == "unknown"
                            else f"{child_id}（远端任务尚未确认结束）"
                        )
                except (ValueError, ProviderError, ComfyExecutionError) as exc:
                    unresolved.append(str(exc))
            if unresolved:
                raise ValueError(
                    "部分 ComfyUI 子任务尚未确认取消，请稍后重试："
                    + "；".join(unresolved)
                )
        if job.get("remote_id"):
            provider = await self._provider(job)
            outcome = await ComfyClient(self.service.executor.session).cancel(
                provider, job["remote_id"]
            )
            if (
                outcome.get("status") == "queued_removed_running_unchanged"
                or outcome.get("cancelled") is False
            ):
                return await self.store.update_job(
                    job_id,
                    error="已移除等待中的任务；旧版服务器不能确认运行中任务已取消",
                    expected_status={
                        "queued",
                        "preparing",
                        "submitting",
                        "submitted",
                        "running",
                        "downloading",
                        "finalizing",
                    },
                )
        elif job["status"] == "submitting":
            raise ValueError(
                "正在提交，提交结果未知，远端任务编号尚未确定，请稍后再试；不会发送全局中断"
            )
        await self.store.update_job(
            job_id,
            status="cancelled",
            expected_status={
                "queued",
                "preparing",
                "submitted",
                "running",
                "downloading",
                "finalizing",
            },
        )
        return await self.store.get_job(job_id)

    @staticmethod
    def public_job(job):
        if not job:
            return None
        return {
            key: job.get(key)
            for key in (
                "id",
                "provider_id",
                "model_id",
                "status",
                "remote_id",
                "error",
                "created_at",
                "updated_at",
                "generation_id",
            )
        } | {
            "temporary": bool(
                job.get("temporary", job.get("request", {}).get("temporary"))
            ),
            "progress": (job.get("result") or {}).get("progress"),
            "can_resume": job.get("status") in {"failed", "unknown"}
            and not job.get("archive_state")
            and (
                job.get("status") == "unknown"
                or not job.get("finished_at")
                or job.get("recovery_protected")
                or job["finished_at"] >= time.time() - RECOVERY_SECONDS
            )
            and bool(
                job.get("remote_id") or (job.get("result") or {}).get("child_ids")
            ),
            "parent_job_id": job.get(
                "parent_job_id", job.get("request", {}).get("parent_job_id", "")
            ),
            "model_name": job.get("model_name")
            or job.get("request", {}).get("model", {}).get("name")
            or job.get("model_id", ""),
            "result_available": job.get("status") in {"succeeded", "partial"}
            and bool(
                job.get("has_result", job.get("outputs") or job.get("generation_id"))
            ),
        }
