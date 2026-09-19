"""Agent-facing task handoff, provenance and delivery failure boundaries."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import mcp.types
import pytest

from astrbot_plugin_image_studio.main import ImageStudioPlugin, _agent_wait_budget
from astrbot_plugin_image_studio.backend.models import GeneratedImage, GenerationResult
from astrbot_plugin_image_studio.tests.backend.integration.test_service_and_tool import (
    PNG,
    FakeAgentAssetStore,
    ToolEvent,
    settings,
)
from astrbot_plugin_image_studio.tests.support.comfy_runtime import runtime_fixture
from astrbot_plugin_image_studio.backend.generation.service import (
    ImageGenerationService,
)
from astrbot_plugin_image_studio.backend.gallery.store import GenerationStore


def payload(result):
    return json.loads(result.content[0].text)


def test_comfy_tool_returns_before_image_ready_and_retrieves_same_scoped_task(
    tmp_path, monkeypatch
):
    async def run():
        runtime, service, _, client = await runtime_fixture(tmp_path, monkeypatch)
        client.release_wait.clear()
        plugin = object.__new__(ImageStudioPlugin)
        plugin._settings = service.settings
        plugin._service = service
        plugin.store = service.store
        event = ToolEvent()
        try:
            await plugin.image_studio_get_capabilities(event, mode="text2img")
            response = await asyncio.wait_for(
                plugin.image_studio_generate(
                    event, mode="text2img", request_id="first-drawing"
                ),
                timeout=5,
            )
            task = payload(response)
            assert not response.isError and task["status"] == "pending"
            assert task["request_id"] == "first-drawing"
            await asyncio.wait_for(client.wait_started.wait(), timeout=5)
            # A new event needs no capability discovery to read existing output.
            next_event = ToolEvent()
            recent = payload(await plugin.image_studio_task(next_event))
            assert recent["tasks"][0]["task_id"] == task["task_id"]
            assert "request" not in recent["tasks"][0]
            outsider = ToolEvent()
            outsider.unified_msg_origin = "private:other"
            denied = await plugin.image_studio_task(outsider, task_id=task["task_id"])
            assert denied.isError
            assert not payload(await plugin.image_studio_task(outsider))["tasks"]
            client.release_wait.set()
            result = await plugin.image_studio_task(
                next_event, task_id=task["task_id"], wait_seconds=5
            )
            assert not result.isError, result
            assert isinstance(result.content[0], mcp.types.TextContent)
            assert payload(result)["status"] == "succeeded"
            assert payload(result)["origin"] == "tool_generated"
            assert payload(result)["assets"]
            repeated = await plugin.image_studio_generate(
                event, mode="text2img", request_id="first-drawing"
            )
            assert payload(repeated)["task_id"] == task["task_id"]
            assert len([call for call in client.calls if call[0] == "submit"]) == 1
        finally:
            client.release_wait.set()
            await runtime.close()
            await service.store.close()

    asyncio.run(run())


def test_registration_retry_uses_existing_bytes_and_is_event_scoped():
    class Store(FakeAgentAssetStore):
        fail = True

        async def lease_agent_images(self, images, **kwargs):
            if self.fail:
                raise OSError("disk unavailable")
            return await super().lease_agent_images(images, **kwargs)

    async def run():
        plugin = object.__new__(ImageStudioPlugin)
        plugin._settings = settings()
        plugin.store = Store()
        calls = []

        async def generate(**kwargs):
            calls.append(kwargs)
            return GenerationResult(
                settings().providers[0],
                SimpleNamespace(count=1),
                (GeneratedImage(PNG, "image/png"),),
                5,
                warning="普通说明",
            )

        plugin._service = SimpleNamespace(generate=generate)
        event = ToolEvent()
        await plugin.image_studio_get_capabilities(event, mode="text2img")
        failed = await plugin.image_studio_generate(
            event, mode="text2img", prompt="tree"
        )
        assert failed.isError
        assert payload(failed)["status"] == "asset_registration_failed"
        assert payload(failed)["generation_status"] == "succeeded"
        task_id = payload(failed)["task_id"]
        assert task_id.startswith("result_")
        assert not any(
            isinstance(item, mcp.types.ImageContent) for item in failed.content
        )
        denied = await plugin.image_studio_task(ToolEvent(), task_id=task_id)
        assert denied.isError
        plugin.store.fail = False
        recovered = await plugin.image_studio_task(event, task_id=task_id)
        assert not recovered.isError
        assert payload(recovered)["status"] == "succeeded"
        assert payload(recovered)["warning"] == "普通说明"
        assert len(calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize("stage", ["record_success", "discard_staged_references"])
def test_post_generation_storage_failure_keeps_original_output(
    tmp_path, monkeypatch, stage
):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        calls = []

        async def generate(*args):
            calls.append(args)
            return (GeneratedImage(PNG, "image/png"),)

        async def fail(*args, **kwargs):
            raise OSError("storage unavailable")

        monkeypatch.setattr(store, stage, fail)
        service = ImageGenerationService(
            settings=settings(),
            executor=SimpleNamespace(generate=generate),
            store=store,
        )
        plugin = object.__new__(ImageStudioPlugin)
        plugin._settings = service.settings
        plugin._service = service
        plugin.store = store
        event = ToolEvent()
        try:
            await plugin.image_studio_get_capabilities(event, mode="text2img")
            result = await plugin.image_studio_generate(
                event, mode="text2img", prompt="tree"
            )
            body = payload(result)
            assert not result.isError and body["status"] == "succeeded"
            assert "未完成" in body["warning"]
            image, _ = await store.load_workflow_image(
                body["assets"][0]["asset_id"],
                scope_id=event.unified_msg_origin,
                detail="original",
                preview_max_edge=768,
                preview_quality=80,
            )
            assert image.data == PNG and len(calls) == 1
        finally:
            await store.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "status,can_resume",
    [("running", False), ("unknown", False), ("unknown", True), ("failed", True)],
)
def test_task_states_never_request_blind_regeneration(status, can_resume):
    async def run():
        plugin = object.__new__(ImageStudioPlugin)
        runtime = SimpleNamespace(public_job=lambda _: {"can_resume": can_resume})
        result = await plugin._agent_comfy_result(
            ToolEvent(), runtime, {"id": "task", "status": status}
        )
        body = payload(result)
        assert body["status"] == ("pending" if status == "running" else status)
        assert result.isError == (status != "running")
        assert body["task_id"] == "task"
        assert (
            "不要重复提交" in body["next_action"]
            or "不要自动重新提交" in body["next_action"]
            or "resume=true" in body["next_action"]
        )

    asyncio.run(run())


def test_partial_comfy_result_and_unavailable_output_are_not_execution_failure():
    async def run():
        plugin = object.__new__(ImageStudioPlugin)
        plugin._settings = settings()
        plugin.store = FakeAgentAssetStore()
        job = {"id": "task", "status": "partial", "generation_id": "generation"}

        async def result(_):
            return GenerationResult(
                settings().providers[0],
                SimpleNamespace(count=2),
                (GeneratedImage(PNG, "image/png"),),
                5,
                warning="一批失败",
            )

        runtime = SimpleNamespace(result=result)
        response = await plugin._agent_comfy_result(ToolEvent(), runtime, job)
        assert payload(response)["status"] == "partial" and not response.isError

        async def missing(_):
            raise OSError("missing")

        runtime.result = missing
        response = await plugin._agent_comfy_result(ToolEvent(), runtime, job)
        assert response.isError
        assert payload(response)["status"] == "result_unavailable"
        assert payload(response)["generation_status"] == "partial"

    asyncio.run(run())


def test_workspace_batch_validates_first_and_preserves_partial_success(
    tmp_path, monkeypatch
):
    async def run():
        plugin = object.__new__(ImageStudioPlugin)
        plugin._settings = settings()
        plugin.store = FakeAgentAssetStore()
        plugin.context = SimpleNamespace(
            get_config=lambda **_: {
                "provider_settings": {"computer_use_runtime": "local"}
            }
        )
        workspace = tmp_path / "workspace"

        async def root(*_):
            return workspace

        monkeypatch.setattr(
            "astrbot_plugin_image_studio.main._event_workspace_root", root
        )
        item = {"asset_id": f"{1:064x}", "name": "first.png"}
        rejected = await plugin.image_studio_send_output(
            ToolEvent(),
            destination="workspace",
            messages=[item, {"asset_id": f"{2:064x}"}],
        )
        assert rejected.isError and not workspace.exists()

        from astrbot_plugin_image_studio.main import _write_unique_workspace_file

        def write(directory, name, data):
            if name == "second.png":
                raise OSError("disk failure")
            return _write_unique_workspace_file(directory, name, data)

        monkeypatch.setattr(
            "astrbot_plugin_image_studio.main._write_unique_workspace_file", write
        )
        result = await plugin.image_studio_send_output(
            ToolEvent(),
            destination="workspace",
            messages=[item, {**item, "name": "second.png"}],
        )
        body = payload(result)
        assert result.isError and body["status"] == "partial"
        assert body["workspace_paths"] == ["image-studio-assets/first.png"]
        assert body["results"][0]["index"] == 0
        assert body["failures"][0]["index"] == 1
        assert (workspace / body["workspace_paths"][0]).read_bytes() == PNG

    asyncio.run(run())


def test_failed_session_delivery_does_not_mean_failed_generation():
    async def run():
        plugin = object.__new__(ImageStudioPlugin)
        plugin._settings = settings()

        async def failed(*_):
            raise TimeoutError("delivery acknowledgment lost")

        plugin._send_output_to_session = failed
        result = await plugin.image_studio_send_output(
            ToolEvent(),
            destination="session",
            messages=[{"type": "plain", "text": "here"}],
        )
        assert result.isError
        assert payload(result)["status"] == "delivery_unknown"
        assert "请勿重新生图" in payload(result)["message"]

    asyncio.run(run())


@pytest.mark.parametrize("value", [True, "30", -1, 31, float("nan"), float("inf")])
def test_task_wait_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        _agent_wait_budget(None, ToolEvent(), value)


@pytest.mark.parametrize("timeout,expected", [(120, 30), (20, 15), (4, 0)])
def test_task_wait_leaves_host_timeout_margin(timeout, expected):
    context = SimpleNamespace(
        get_config=lambda **_: {
            "agent_runner": {"config": {"misc": {"tool_call_timeout": timeout}}}
        }
    )
    assert _agent_wait_budget(context, ToolEvent(), 30) == expected


def test_disabled_generation_and_task_are_errors():
    plugin = object.__new__(ImageStudioPlugin)
    plugin._settings = replace(settings(), enable_llm_tool=False)
    assert asyncio.run(plugin.image_studio_generate(ToolEvent())).isError
    assert asyncio.run(plugin.image_studio_task(ToolEvent())).isError
