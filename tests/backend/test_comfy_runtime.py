"""Offline integration coverage for durable ComfyUI jobs and the real gallery."""

from __future__ import annotations

import asyncio
import copy
import io
import json
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from astrbot_plugin_image_studio.backend.providers.comfyui import (
    runtime as comfyui_runtime,
)
from astrbot_plugin_image_studio.backend.providers.comfyui.client import (
    ComfyExecutionError,
)
from astrbot_plugin_image_studio.backend.providers.comfyui.runtime import ComfyRuntime
from astrbot_plugin_image_studio.backend.config import HistorySettings, RuntimeSettings
from astrbot_plugin_image_studio.backend.models import (
    GeneratedImage,
    ImageProvider,
    InvocationSource,
    ReferenceImage,
)
from astrbot_plugin_image_studio.backend.providers.executor import ProviderError
from astrbot_plugin_image_studio.backend.generation.service import (
    ImageGenerationService,
)
from astrbot_plugin_image_studio.backend.gallery.store import GenerationStore
from PIL import Image


def image(color):
    output = io.BytesIO()
    Image.new("RGB", (24, 32), color).save(output, "PNG")
    return GeneratedImage(output.getvalue(), "image/png")


def workflow(*, reference_count=0, prompt_bound=False, count_bound=False):
    graph = {
        "1": {"class_type": "CLIPTextEncode", "inputs": {"text": "fixed prompt"}},
        "2": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": 512, "height": 512, "batch_size": 3},
        },
        "3": {
            "class_type": "KSampler",
            "inputs": {"seed": 42, "positive": ["1", 0], "latent_image": ["2", 0]},
        },
        "4": {
            "class_type": "SaveImage",
            "inputs": {"images": ["3", 0], "filename_prefix": "result"},
        },
    }
    bindings = {}
    if prompt_bound:
        bindings["prompt"] = {
            "source": "prompt",
            "type": "text",
            "node_id": "1",
            "input_name": "text",
            "required": True,
        }
    if count_bound:
        bindings["count"] = {
            "source": "count",
            "type": "number",
            "node_id": "2",
            "input_name": "batch_size",
        }
    for index in range(reference_count):
        node_id = str(index + 10)
        graph[node_id] = {"class_type": "LoadImage", "inputs": {"image": "old.png"}}
        bindings[f"reference_{index}"] = {
            "source": "reference",
            "type": "image",
            "node_id": node_id,
            "input_name": "image",
            "reference_index": index,
        }
    return {
        "api_graph": graph,
        "bindings": bindings,
        "outputs": ["4"],
        "workflow": {"nodes": [], "links": []},
    }


def configured_provider(config=None, **changes):
    return ImageProvider.from_mapping(
        {
            "id": "comfy",
            "name": "Local Comfy",
            "kind": "comfyui",
            "base_url": "http://comfy.invalid:8188",
            "api_key": "secret-not-in-jobs",
            "models": [
                {
                    "id": "workflow",
                    "name": "Saved workflow",
                    "comfyui": config or workflow(),
                    "tool": {"enabled": True, "max_reference_images": 8},
                }
            ],
            **changes,
        }
    )


class FakeComfyClient:
    def __init__(self):
        self.calls = []
        self.images = (image("red"), image("blue"), image("green"))
        self.history = {
            "status": {"completed": True, "status_str": "success"},
            "outputs": {"4": {"images": [{"filename": "result.png"}]}},
        }
        self.wait_started = asyncio.Event()
        self.release_wait = asyncio.Event()
        self.release_wait.set()
        self.wait_error = None
        self.download_error = None
        self.submit_error = None
        self.cancel_result = {"cancelled": True}
        self.cancel_hook = None
        self.issues = []
        self.upload_started = asyncio.Event()
        self.release_upload = asyncio.Event()
        self.release_upload.set()

    async def inspect(self, provider, config):
        self.calls.append(("inspect", copy.deepcopy(config)))
        return {"issues": copy.deepcopy(self.issues)}

    async def upload_references(self, provider, references, *, config):
        self.calls.append(("upload", tuple(references)))
        self.upload_started.set()
        await self.release_upload.wait()
        return [f"uploaded-{index}.png" for index in range(len(references))]

    async def submit(self, provider, graph, ui_workflow, *, client_id):
        self.calls.append(("submit", copy.deepcopy(graph), client_id))
        if self.submit_error:
            raise self.submit_error
        return {"prompt_id": f"remote-{client_id}", "node_errors": {}}

    async def wait(self, provider, remote_id, *, on_progress, client_id):
        self.calls.append(("wait", remote_id, client_id))
        await on_progress({"status": "running", "value": 1, "max": 10})
        self.wait_started.set()
        await self.release_wait.wait()
        if self.wait_error:
            raise self.wait_error
        return copy.deepcopy(self.history)

    async def download_outputs(self, provider, history, outputs, *, output_limit=None):
        self.calls.append(
            ("download", copy.deepcopy(history), list(outputs), output_limit)
        )
        if self.download_error:
            raise self.download_error
        return self.images[:output_limit] if output_limit is not None else self.images

    async def cancel(self, provider, remote_id):
        self.calls.append(("cancel", remote_id))
        if self.cancel_hook:
            await self.cancel_hook()
        return self.cancel_result


async def runtime_fixture(
    tmp_path, monkeypatch, *, config=None, history=True, native=1, count_default=1
):
    client = FakeComfyClient()
    monkeypatch.setattr(comfyui_runtime, "ComfyClient", lambda _: client)
    provider = configured_provider(config)
    if native != 1 or count_default != 1:
        raw = provider.public_dict()
        raw["models"][0]["native_batch_size"] = native
        raw["models"][0]["native_batch_size_source"] = "manual"
        raw["models"][0]["parameters"]["count"]["default"] = count_default
        provider = ImageProvider.from_mapping(raw)
    settings = RuntimeSettings(
        enable_llm_tool=True,
        providers=(provider,),
        history=HistorySettings(history, 100, 0, True, True),
        revision=0,
        default_page_text2img_model_ref="comfy:workflow",
        default_page_img2img_model_ref="comfy:workflow",
        default_tool_text2img_model_ref="comfy:workflow",
        default_tool_img2img_model_ref="comfy:workflow",
    )
    store = GenerationStore(tmp_path)
    await store.initialize()
    service = ImageGenerationService(
        settings=settings, executor=SimpleNamespace(session=object()), store=store
    )
    runtime = ComfyRuntime(service)
    service.comfy_runtime = runtime
    await runtime.start()
    return runtime, service, provider, client


async def submit(runtime, provider, **changes):
    return await runtime.submit(
        provider=provider,
        model=provider.models[0],
        mode="text2img",
        prompt="",
        **changes,
    )


@pytest.mark.parametrize("source", ["webui", "command", "llm_tool"])
def test_default_workflow_trims_excess_outputs_and_needs_no_prompt(
    tmp_path, monkeypatch, source
):
    async def run():
        runtime, service, _, client = await runtime_fixture(tmp_path, monkeypatch)
        try:
            result = await service.generate(
                mode="text2img",
                provider_id="comfy",
                model="workflow",
                prompt="",
                source=source,
            )
            assert len(result.images) == 1
            assert result.warning == ""
            jobs = await runtime.store.list_jobs()
            assert len(jobs) == 1 and jobs[0]["status"] == "succeeded"
            assert len([call for call in client.calls if call[0] == "submit"]) == 1
            with service.store._connect() as conn:
                assert (
                    conn.execute("SELECT COUNT(*) FROM generations").fetchone()[0] == 1
                )
                assert (
                    conn.execute("SELECT COUNT(*) FROM generation_images").fetchone()[0]
                    == 1
                )
                assert (
                    conn.execute("SELECT source FROM generations").fetchone()[0]
                    == source
                )
            assert b"secret-not-in-jobs" not in runtime.store.db_path.read_bytes()
        finally:
            await runtime.close()

    asyncio.run(run())


def test_bound_prompt_fails_before_remote_submission(tmp_path, monkeypatch):
    async def run():
        runtime, service, _, client = await runtime_fixture(
            tmp_path, monkeypatch, config=workflow(prompt_bound=True)
        )
        try:
            with pytest.raises(ProviderError, match="提示词"):
                await service.generate(mode="text2img", provider_id="comfy", prompt="")
            assert not [call for call in client.calls if call[0] == "submit"]
        finally:
            await runtime.close()

    asyncio.run(run())


def test_reference_order_and_identity_survive_durable_input_staging(
    tmp_path, monkeypatch
):
    async def run():
        runtime, service, _, client = await runtime_fixture(
            tmp_path, monkeypatch, config=workflow(reference_count=2)
        )
        raw = image("yellow").data
        references = (
            ReferenceImage("first", "base.png", raw, "image/png"),
            ReferenceImage("second", "style.png", raw, "image/png"),
        )
        try:
            result = await service.generate(
                mode="img2img",
                provider_id="comfy",
                model="workflow",
                prompt="",
                source="llm_tool",
                references=references,
                invocation_source=InvocationSource(
                    user_id="owner", platform_name="qq_official"
                ),
            )
            assert (
                next(call for call in client.calls if call[0] == "upload")[1]
                == references
            )
            graph = next(call for call in client.calls if call[0] == "submit")[1]
            assert graph["10"]["inputs"]["image"] == "uploaded-0.png"
            assert graph["11"]["inputs"]["image"] == "uploaded-1.png"
            job = (await runtime.store.list_jobs())[0]
            assert await runtime.store.load_references(job["id"]) == references
            public = runtime.public_job(job)
            assert (
                not {"references", "request", "outputs", "revision_id"} & public.keys()
            )
            with service.store._connect() as conn:
                assert tuple(
                    conn.execute(
                        "SELECT source,user_id,platform_name FROM generations WHERE id=?",
                        (result.generation_id,),
                    ).fetchone()
                ) == ("llm_tool", "owner", "qq_official")
                assert (
                    conn.execute(
                        "SELECT COUNT(*) FROM generation_references WHERE generation_id=?",
                        (result.generation_id,),
                    ).fetchone()[0]
                    == 2
                )
        finally:
            await runtime.close()

    asyncio.run(run())


def test_completed_snapshot_and_gallery_are_independent_of_model_edits(
    tmp_path, monkeypatch
):
    async def run():
        runtime, service, provider, client = await runtime_fixture(
            tmp_path, monkeypatch, native=3, count_default=3
        )
        client.release_wait.clear()
        try:
            job = await submit(runtime, provider)
            await asyncio.wait_for(client.wait_started.wait(), 2)
            changed = workflow()
            changed["api_graph"]["1"]["inputs"]["text"] = "newly edited prompt"
            service.settings = replace(
                service.settings, providers=(configured_provider(changed),)
            )
            client.release_wait.set()
            done = await runtime.manager.wait(job["id"])
            assert done["status"] == "succeeded"
            result = await runtime.result(done)
            assert (
                result.images[0].effective_parameters["_comfyui"]["api_graph"]["1"][
                    "inputs"
                ]["text"]
                == "fixed prompt"
            )
            assert (await runtime.store.get_revision(job["revision_id"]))["api_graph"][
                "1"
            ]["inputs"]["text"] == "fixed prompt"
            again = await runtime._run(done)
            assert again["generation_id"] == done["result"]["generation_id"]
            assert len([call for call in client.calls if call[0] == "submit"]) == 1
            with service.store._connect() as conn:
                assert (
                    conn.execute("SELECT COUNT(*) FROM generations").fetchone()[0] == 1
                )
                assert (
                    conn.execute("SELECT COUNT(*) FROM generation_images").fetchone()[0]
                    == 3
                )
        finally:
            await runtime.close()

    asyncio.run(run())


@pytest.mark.parametrize("failure_phase", ["wait", "download"])
def test_partial_output_retains_gallery_identity_and_exact_effective_parameters(
    tmp_path, monkeypatch, failure_phase
):
    async def run():
        runtime, service, _, client = await runtime_fixture(
            tmp_path,
            monkeypatch,
            config=workflow(prompt_bound=True),
            native=3,
            count_default=3,
        )
        try:
            if failure_phase == "wait":
                client.wait_error = ComfyExecutionError(
                    "node 99 failed", history=client.history
                )
            else:
                client.download_error = ComfyExecutionError(
                    "second output missing", images=client.images[:1]
                )
            result = await service.generate(
                mode="text2img",
                provider_id="comfy",
                prompt="the actual prompt",
                source="llm_tool",
                invocation_source=InvocationSource(
                    user_id="partial-owner", platform_name="qq_official"
                ),
            )
            assert result.warning
            assert len(result.images) == (3 if failure_phase == "wait" else 1)
            job = (await runtime.store.list_jobs())[0]
            assert job["status"] == "partial"
            assert (
                result.images[0].effective_parameters["_comfyui"]["api_graph"]["1"][
                    "inputs"
                ]["text"]
                == "the actual prompt"
            )
            with service.store._connect() as conn:
                row = conn.execute(
                    "SELECT source,user_id,platform_name,original_prompt FROM generations WHERE id=?",
                    (result.generation_id,),
                ).fetchone()
                assert tuple(row) == (
                    "llm_tool",
                    "partial-owner",
                    "qq_official",
                    "the actual prompt",
                )
        finally:
            await runtime.close()

    asyncio.run(run())


def test_failed_job_can_resume_known_remote_without_resubmission(tmp_path, monkeypatch):
    async def run():
        runtime, service, provider, client = await runtime_fixture(
            tmp_path, monkeypatch
        )
        try:
            client.wait_error = ComfyExecutionError("polling timed out")
            job = await submit(runtime, provider)
            failed = await runtime.manager.wait(job["id"])
            assert failed["status"] == "failed"
            assert failed["remote_id"]
            with service.store._connect() as conn:
                assert (
                    conn.execute("SELECT COUNT(*) FROM generations").fetchone()[0] == 0
                )
            client.wait_error = None
            await runtime.manager.resume(job["id"])
            succeeded = await runtime.manager.wait(job["id"])
            assert succeeded["status"] == "succeeded"
            assert len([call for call in client.calls if call[0] == "submit"]) == 1
        finally:
            await runtime.close()

    asyncio.run(run())


def test_restart_resumes_remote_and_preserves_outputs_without_history(
    tmp_path, monkeypatch
):
    async def run():
        runtime, service, provider, client = await runtime_fixture(
            tmp_path, monkeypatch, history=False, native=3, count_default=3
        )
        client.release_wait.clear()
        job = await submit(runtime, provider)
        await asyncio.wait_for(client.wait_started.wait(), 2)
        await runtime.close()
        restarted = ComfyRuntime(service)
        service.comfy_runtime = restarted
        try:
            client.release_wait.set()
            await restarted.start()
            result_job = await restarted.manager.wait(job["id"])
            assert result_job["status"] == "succeeded"
            result = await restarted.result(result_job)
            assert len(result.images) == 3
            assert not result.generation_id
            assert len([call for call in client.calls if call[0] == "submit"]) == 1
            assert len([call for call in client.calls if call[0] == "wait"]) == 2
        finally:
            await restarted.close()

    asyncio.run(run())


def test_uncertain_submission_stays_unknown_across_restart(tmp_path, monkeypatch):
    async def run():
        runtime, service, provider, client = await runtime_fixture(
            tmp_path, monkeypatch
        )
        client.submit_error = ComfyExecutionError(
            "response lost", unknown_submission=True
        )
        job = await submit(runtime, provider)
        unknown = await runtime.manager.wait(job["id"])
        assert unknown["status"] == "unknown"
        await runtime.close()
        restarted = ComfyRuntime(service)
        try:
            await restarted.start()
            assert (await restarted.store.get_job(job["id"]))["status"] == "unknown"
            with pytest.raises(ValueError, match="远端编号"):
                await restarted.manager.resume(job["id"])
            assert len([call for call in client.calls if call[0] == "submit"]) == 1
        finally:
            await restarted.close()

    asyncio.run(run())


def test_cancel_remote_running_job_and_keep_unsupported_cancel_resumable(
    tmp_path, monkeypatch
):
    async def run():
        runtime, _, provider, client = await runtime_fixture(tmp_path, monkeypatch)
        client.release_wait.clear()
        try:
            job = await submit(runtime, provider)
            await asyncio.wait_for(client.wait_started.wait(), 2)
            client.cancel_result = {
                "status": "queued_removed_running_unchanged",
                "cancelled": False,
            }
            unchanged = await runtime.cancel(job["id"])
            assert unchanged["status"] == "running"
            assert unchanged["error"]
            client.cancel_result = {"cancelled": True}
            cancelled = await runtime.cancel(job["id"])
            assert cancelled["status"] == "cancelled"
            client.release_wait.set()
            try:
                await runtime.manager.wait(job["id"])
            except asyncio.CancelledError:
                pass
            assert (await runtime.store.get_job(job["id"]))["status"] == "cancelled"
        finally:
            await runtime.close()

    asyncio.run(run())


def test_cancel_completion_race_returns_terminal_success_without_overwriting(
    tmp_path, monkeypatch
):
    async def run():
        runtime, _, provider, client = await runtime_fixture(tmp_path, monkeypatch)
        client.release_wait.clear()
        try:
            job = await submit(runtime, provider)
            await asyncio.wait_for(client.wait_started.wait(), 2)

            async def finish_during_cancel():
                client.release_wait.set()
                await runtime.manager.wait(job["id"])

            client.cancel_hook = finish_during_cancel
            result = await runtime.cancel(job["id"])
            assert result["status"] == "succeeded"
        finally:
            await runtime.close()

    asyncio.run(run())


def test_cancel_before_submission_never_posts_remote_job(tmp_path, monkeypatch):
    async def run():
        runtime, _, provider, client = await runtime_fixture(tmp_path, monkeypatch)
        client.release_upload.clear()
        try:
            job = await submit(runtime, provider)
            await asyncio.wait_for(client.upload_started.wait(), 2)
            cancelled = await runtime.cancel(job["id"])
            assert cancelled["status"] == "cancelled"
            client.release_upload.set()
            try:
                await runtime.manager.wait(job["id"])
            except asyncio.CancelledError:
                pass
            assert not [
                call for call in client.calls if call[0] in {"submit", "cancel"}
            ]
        finally:
            await runtime.close()

    asyncio.run(run())


def test_partial_gallery_retry_preserves_failure_warning_without_redownload(
    tmp_path, monkeypatch
):
    async def run():
        runtime, service, provider, client = await runtime_fixture(
            tmp_path, monkeypatch
        )
        original_record = service.store.record_success
        calls = []

        async def fail_first_record(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise OSError("disk temporarily unavailable")
            return await original_record(**kwargs)

        monkeypatch.setattr(service.store, "record_success", fail_first_record)
        client.download_error = ComfyExecutionError(
            "second branch failed", images=client.images[:1]
        )
        try:
            job = await submit(runtime, provider)
            failed = await runtime.manager.wait(job["id"])
            assert failed["status"] == "failed"
            assert await runtime.store.load_outputs(job["id"])
            await runtime.manager.resume(job["id"])
            completed = await runtime.manager.wait(job["id"])
            assert completed["status"] == "partial"
            assert "second branch failed" in completed["result"]["warning"]
            assert len([call for call in client.calls if call[0] == "download"]) == 1
            assert len([call for call in client.calls if call[0] == "submit"]) == 1
        finally:
            await runtime.close()

    asyncio.run(run())


def test_changed_provider_origin_blocks_recovery_without_contacting_new_server(
    tmp_path, monkeypatch
):
    async def run():
        runtime, service, provider, client = await runtime_fixture(
            tmp_path, monkeypatch
        )
        client.release_wait.clear()
        job = await submit(runtime, provider)
        await asyncio.wait_for(client.wait_started.wait(), 2)
        await runtime.close()
        service.settings = replace(
            service.settings,
            providers=(configured_provider(base_url="http://different.invalid:8188"),),
        )
        restarted = ComfyRuntime(service)
        try:
            before = len(client.calls)
            await restarted.start()
            blocked = await restarted.manager.wait(job["id"])
            assert blocked["status"] == "failed"
            assert "地址已改变" in blocked["error"]
            assert len(client.calls) == before
        finally:
            await restarted.close()

    asyncio.run(run())


def test_canonical_api_json_preserves_uint64_seed_through_snapshot_preflight_and_submit(
    tmp_path, monkeypatch
):
    async def run():
        config = workflow()
        exact_seed = 18446744073709551615
        config["api_graph"]["3"]["inputs"]["seed"] = exact_seed
        config["api_graph_json"] = json.dumps(config["api_graph"])
        # The preview from JavaScript has already rounded this integer. Its
        # canonical JSON source must stay authoritative through every stage.
        config["api_graph"]["3"]["inputs"]["seed"] = 18446744073709552000
        runtime, _, provider, client = await runtime_fixture(
            tmp_path, monkeypatch, config=config
        )
        try:
            job = await submit(runtime, provider)
            done = await runtime.manager.wait(job["id"])
            assert done["status"] == "succeeded"
            revision = await runtime.store.get_revision(job["revision_id"])
            assert revision["api_graph"]["3"]["inputs"]["seed"] == exact_seed
            inspected = next(call[1] for call in client.calls if call[0] == "inspect")
            submitted = next(call[1] for call in client.calls if call[0] == "submit")
            assert inspected["api_graph"]["3"]["inputs"]["seed"] == exact_seed
            assert submitted["3"]["inputs"]["seed"] == exact_seed
        finally:
            await runtime.close()

    asyncio.run(run())


def test_imported_seed_fingerprints_stay_archived_but_never_reach_new_executions(
    tmp_path, monkeypatch
):
    async def run():
        config = workflow()
        config["api_graph"]["91"] = {
            "class_type": "Seed (rgthree)",
            "inputs": {"seed": -1},
            "is_changed": [862058598661582],
        }
        config["api_graph"]["3"]["inputs"]["seed"] = ["91", 0]
        config["api_graph_json"] = json.dumps(config["api_graph"])
        runtime, _, provider, client = await runtime_fixture(
            tmp_path, monkeypatch, config=config
        )
        try:
            for _ in range(2):
                job = await submit(runtime, provider)
                done = await runtime.manager.wait(job["id"])
                assert done["status"] == "succeeded", done
                revision = await runtime.store.get_revision(job["revision_id"])
                assert revision["api_graph"]["91"]["is_changed"] == [862058598661582]
                images = await runtime.store.load_outputs(job["id"])
                actual = images[0].effective_parameters["_comfyui"]
                assert "is_changed" not in actual["api_graph"]["91"]
                assert "is_changed" not in json.loads(actual["api_graph_json"])["91"]
            submitted = [call[1] for call in client.calls if call[0] == "submit"]
            inspected = [
                call[1]["api_graph"] for call in client.calls if call[0] == "inspect"
            ]
            assert len(submitted) == len(inspected) == 2
            for graph in [*submitted, *inspected]:
                assert "is_changed" not in graph["91"]
                assert graph["91"]["inputs"]["seed"] == -1
                assert graph["3"]["inputs"]["seed"] == ["91", 0]
            assert provider.models[0].comfyui["api_graph"]["91"]["is_changed"] == [
                862058598661582
            ]
        finally:
            await runtime.close()

    asyncio.run(run())


def test_preflight_missing_dependencies_blocks_upload_and_submission(
    tmp_path, monkeypatch
):
    async def run():
        runtime, _, provider, client = await runtime_fixture(tmp_path, monkeypatch)
        client.issues = [
            {"severity": "error", "message": "节点 #3 缺少 ThirdPartySampler"}
        ]
        try:
            job = await submit(runtime, provider)
            failed = await runtime.manager.wait(job["id"])
            assert failed["status"] == "failed"
            assert "节点 #3 缺少 ThirdPartySampler" in failed["error"]
            assert not [
                call for call in client.calls if call[0] in {"upload", "submit"}
            ]
        finally:
            await runtime.close()

    asyncio.run(run())


def test_returned_request_uses_resolved_defaults_and_invocation_identity(
    tmp_path, monkeypatch
):
    async def run():
        runtime, service, _, client = await runtime_fixture(
            tmp_path, monkeypatch, config=workflow()
        )
        provider = configured_provider(
            models=[
                {
                    "id": "workflow",
                    "name": "Batch workflow",
                    "comfyui": workflow(),
                    "native_batch_size": 3,
                    "native_batch_size_source": "manual",
                    "parameters": {
                        "count": {"type": "integer", "default": 3, "min": 1, "max": 16}
                    },
                    "tool": {"enabled": True},
                }
            ]
        )
        service.settings = replace(service.settings, providers=(provider,))
        try:
            result = await service.generate(
                mode="text2img",
                provider_id="comfy",
                prompt="",
                source="command",
                invocation_source=InvocationSource(user_id="caller"),
            )
            graph = next(call[1] for call in client.calls if call[0] == "submit")
            assert graph["2"]["inputs"]["batch_size"] == 3
            assert result.request.count == 3
            assert result.request.invocation_source.user_id == "caller"
        finally:
            await runtime.close()

    asyncio.run(run())


async def expire_terminal_staging(runtime, job_id):
    with runtime.store._connect() as connection:
        connection.execute(
            "UPDATE comfy_jobs SET finished_at=? WHERE id=?",
            (time.time() - 8 * 86400, job_id),
        )
    await runtime.store.cleanup_terminal_files(terminal_before=time.time() - 7 * 86400)


def test_expired_terminal_outputs_use_gallery_originals_with_exact_metadata(
    tmp_path, monkeypatch
):
    async def run():
        runtime, service, _, client = await runtime_fixture(
            tmp_path,
            monkeypatch,
            config=workflow(reference_count=1),
            native=3,
            count_default=3,
        )
        client.images = tuple(
            replace(item, response_index=index + 10)
            for index, item in enumerate(client.images)
        )
        reference = ReferenceImage(
            "base", "base.png", image("yellow").data, "image/png"
        )
        try:
            result = await service.generate(
                mode="img2img", provider_id="comfy", prompt="", references=(reference,)
            )
            job = (await runtime.store.list_jobs())[0]
            await expire_terminal_staging(runtime, job["id"])
            calls_before = len(client.calls)
            restored = await runtime.result(await runtime.store.get_job(job["id"]))
            assert restored.images == result.images
            assert restored.generation_id == result.generation_id
            assert restored.request.references == ()
            assert restored.warning == result.warning
            assert len(client.calls) == calls_before
        finally:
            await runtime.close()

    asyncio.run(run())


@pytest.mark.parametrize("remove_gallery", [False, True])
def test_expired_outputs_without_gallery_return_actionable_error(
    tmp_path, monkeypatch, remove_gallery
):
    async def run():
        runtime, service, provider, client = await runtime_fixture(
            tmp_path, monkeypatch, history=remove_gallery
        )
        try:
            job = await submit(runtime, provider)
            done = await runtime.manager.wait(job["id"])
            if remove_gallery:
                await service.store.delete_generation(done["generation_id"])
            await expire_terminal_staging(runtime, job["id"])
            calls_before = len(client.calls)
            with pytest.raises(
                ValueError, match="临时图片已过期.*画廊.*没有保留的原图"
            ):
                await runtime.result(await runtime.store.get_job(job["id"]))
            assert len(client.calls) == calls_before
        finally:
            await runtime.close()

    asyncio.run(run())


def test_expired_output_fallback_does_not_restore_deleted_gallery_images(
    tmp_path, monkeypatch
):
    async def run():
        runtime, service, provider, client = await runtime_fixture(
            tmp_path, monkeypatch, native=3, count_default=3
        )
        client.images = tuple(
            replace(item, response_index=index + 10)
            for index, item in enumerate(client.images)
        )
        try:
            job = await submit(runtime, provider)
            done = await runtime.manager.wait(job["id"])
            original = await runtime.result(done)
            gallery = await service.store.generation_detail(
                done["generation_id"], light=True
            )
            await service.store.delete_images(
                done["generation_id"], [gallery["images"][1]["id"]]
            )
            await expire_terminal_staging(runtime, job["id"])
            restored = await runtime.result(await runtime.store.get_job(job["id"]))
            assert restored.images == (original.images[0], original.images[2])
            assert [item.response_index for item in restored.images] == [10, 12]
            assert "原有 3 张" in restored.warning and "现有 2 张" in restored.warning
            remaining = await service.store.generation_detail(
                done["generation_id"], light=True
            )
            assert len(remaining["images"]) == 2
        finally:
            await runtime.close()

    asyncio.run(run())


def test_deleted_reference_staging_does_not_block_results_or_restore_tombstone(
    tmp_path, monkeypatch
):
    async def run():
        runtime, service, _, _ = await runtime_fixture(
            tmp_path, monkeypatch, config=workflow(reference_count=1)
        )
        try:
            result = await service.generate(
                mode="img2img",
                provider_id="comfy",
                prompt="",
                references=(
                    ReferenceImage(
                        "base", "base.png", image("yellow").data, "image/png"
                    ),
                ),
            )
            job = (await runtime.store.list_jobs())[0]
            detail = await service.store.generation_detail(
                result.generation_id, light=True
            )
            reference_id = detail["references"][0]["id"]
            await service.store.delete_reference(reference_id)
            (runtime.store.blobs_dir / job["references"][0]["path"]).unlink()
            readable = await runtime.result(job)
            assert readable.images == result.images
            assert readable.request.references == ()
            updated = await service.store.generation_detail(
                result.generation_id, light=True
            )
            assert updated["references"][0]["available"] is False
            assert updated["references"][0]["deleted_at"]
            with pytest.raises(ValueError, match="参考图不可用"):
                await runtime._run(job)
        finally:
            await runtime.close()

    asyncio.run(run())


def workflow_with_history_controls():
    config = workflow()
    config["api_graph"]["3"]["inputs"].update(
        steps=20, cfg=5.5, old_only=2, private_value=4
    )
    config["bindings"] = {
        key: {
            "source": "parameter",
            "type": "number",
            "node_id": "3",
            "input_name": target,
        }
        for key, target in (
            ("denoise_steps", "steps"),
            ("guide_value", "cfg"),
            ("old_only", "old_only"),
            ("private_value", "private_value"),
        )
    }
    schema = {
        "sampling_steps": {
            "type": "integer",
            "label": "Historical steps",
            "request_key": "denoise_steps",
            "default": 20,
            "min": 1,
            "max": 80,
        },
        "guide_strength": {
            "type": "number",
            "label": "Historical guidance",
            "request_key": "guide_value",
            "default": 5.5,
            "min": 0,
            "max": 20,
        },
        "old_only": {
            "type": "integer",
            "label": "Historical-only control",
            "default": 2,
        },
        "private_control": {
            "type": "integer",
            "request_key": "private_value",
            "default": 4,
            "record_in_history": False,
        },
    }
    return config, schema


@pytest.mark.parametrize(
    ("policy", "expected_steps"),
    [
        ({}, 37),
        ({"webui_visible": False}, 20),
        ({"refill_from_history": False}, 20),
        ({"record_in_history": False}, 37),
    ],
)
def test_gallery_reproduction_preserves_workflow_schema_after_model_mode_edit(
    tmp_path, monkeypatch, policy, expected_steps
):
    async def run():
        old_config, old_schema = workflow_with_history_controls()
        runtime, service, _, client = await runtime_fixture(
            tmp_path, monkeypatch, config=old_config
        )
        original_provider = configured_provider(
            models=[
                {
                    "id": "workflow",
                    "name": "Historical text workflow",
                    "comfyui": old_config,
                    "parameters": old_schema,
                    "tool": {"enabled": True},
                }
            ]
        )
        service.update_settings(
            replace(service.settings, providers=(original_provider,))
        )
        try:
            first = await service.generate(
                mode="text2img",
                provider_id="comfy",
                prompt="",
                parameters={
                    "sampling_steps": 37,
                    "guide_strength": 7.25,
                    "old_only": 8,
                    "private_control": 9,
                },
            )
            historical_detail = await service.store.generation_detail(
                first.generation_id, include_assets=False
            )
            original_graph = next(
                call[1] for call in client.calls if call[0] == "submit"
            )
            assert original_graph["3"]["inputs"]["steps"] == 37
            assert original_graph["3"]["inputs"]["cfg"] == 7.25

            edited_config = workflow(reference_count=1, prompt_bound=True)
            edited_provider = configured_provider(
                models=[
                    {
                        "id": "workflow",
                        "name": "Now an image workflow",
                        "comfyui": edited_config,
                        "parameters": {
                            "sampling_steps": {
                                "type": "text",
                                "label": "New unrelated choice",
                                "request_key": "new_steps_wire",
                                "default": "replacement",
                                **policy,
                            },
                            "guide_strength": {
                                "type": "select",
                                "label": "New guidance preset",
                                "request_key": "new_guide_wire",
                                "default": "new",
                                "choices": ["new"],
                            },
                            "new_only": {"type": "integer", "default": 99},
                        },
                        "tool": {"enabled": True},
                    }
                ]
            )
            assert edited_provider.models[0].supports("img2img")
            assert not edited_provider.models[0].supports("text2img")
            service.update_settings(
                replace(service.settings, providers=(edited_provider,))
            )

            draft = await service.reproduction_plan(first.generation_id)
            assert draft["mode"] == "text2img"
            assert draft["requires_model_selection"] is False
            assert draft["comfyui"]["api_graph"] == original_graph
            controls = draft["comfyui_model"]["parameters"]
            assert controls["sampling_steps"]["type"] == "integer"
            assert controls["sampling_steps"]["request_key"] == "denoise_steps"
            assert controls["guide_strength"]["type"] == "number"
            assert controls["guide_strength"]["request_key"] == "guide_value"
            assert controls["old_only"]["default"] == 2
            assert "new_only" not in controls
            for key, value in policy.items():
                assert controls["sampling_steps"][key] is value
            assert draft["parameters"]["sampling_steps"] == expected_steps
            assert draft["parameters"]["guide_strength"] == 7.25
            assert draft["parameters"]["old_only"] == 8
            assert draft["parameters"]["private_control"] == 4

            again = await runtime.generate(
                provider=edited_provider,
                model=edited_provider.models[0],
                mode=draft["mode"],
                prompt=draft["prompt"],
                parameters=draft["parameters"],
                comfyui=draft["comfyui"],
            )
            assert again.request.mode == "text2img"
            graphs = [call[1] for call in client.calls if call[0] == "submit"]
            assert len(graphs) == 2
            rerun = graphs[-1]
            assert "10" not in rerun
            assert rerun["3"]["inputs"]["steps"] == expected_steps
            assert rerun["3"]["inputs"]["cfg"] == 7.25
            assert rerun["3"]["inputs"]["old_only"] == 8
            assert rerun["3"]["inputs"]["private_value"] == 4
            assert (
                await service.store.generation_detail(
                    first.generation_id, include_assets=False
                )
                == historical_detail
            )
        finally:
            await runtime.close()

    asyncio.run(run())
