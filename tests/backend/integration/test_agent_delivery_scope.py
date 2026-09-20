"""Generated-image delivery routing stays within the current user message."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from astrbot.core.agent.hooks import BaseAgentRunHooks
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.provider.entities import LLMResponse, ProviderRequest
from astrbot_plugin_image_studio.main import ImageStudioPlugin
from astrbot_plugin_image_studio.backend.models import GeneratedImage, GenerationResult
from astrbot_plugin_image_studio.backend.providers.executor import ProviderError
from astrbot_plugin_image_studio.tests.backend.integration.test_service_and_tool import (
    PNG,
    FakeAgentAssetStore,
    ToolEvent,
    settings,
)
from astrbot_plugin_image_studio.tests.support.comfy_runtime import runtime_fixture

BLOCKED = {"send_message_to_user", "pc_send_current_media"}
ALLOWED = {
    "image_studio_generate",
    "image_studio_task",
    "image_studio_send_output",
    "web_search",
}


def tools():
    return ToolSet(
        [
            FunctionTool(name=name, description=name, parameters={})
            for name in BLOCKED | ALLOWED
        ]
    )


class Service:
    fail = False

    async def generate(self, **kwargs):
        if self.fail:
            raise ProviderError("upstream rejected")
        return GenerationResult(
            settings().providers[0],
            SimpleNamespace(mode=kwargs["mode"]),
            (GeneratedImage(PNG, "image/png"),),
            1,
        )


def plugin():
    instance = object.__new__(ImageStudioPlugin)
    instance._settings = settings()
    instance._service = Service()
    instance.store = FakeAgentAssetStore()
    return instance


async def prepare(instance, master):
    event = ToolEvent()
    request = ProviderRequest(func_tool=master, system_prompt="原有人格", contexts=[])
    event.set_extra("provider_request", request)
    await instance.prepare_agent_delivery_tools(event, request)
    return event, request


@pytest.mark.parametrize("schema_mode", ["full", "skills_like"])
def test_success_filters_host_schema_and_execution_only_for_this_message(schema_mode):
    async def run():
        instance = plugin()
        master = tools()
        event, req = await prepare(instance, master)
        assert set(req.func_tool.names()) == BLOCKED | ALLOWED
        executed = []

        class Executor:
            async def execute(self, tool, **kwargs):
                executed.append(tool.name)
                yield "unexpected execution"

        runner = ToolLoopAgentRunner()
        await runner.reset(
            provider=SimpleNamespace(provider_config={}),
            request=req,
            run_context=ContextWrapper(context=None),
            tool_executor=Executor(),
            agent_hooks=BaseAgentRunHooks(),
            tool_schema_mode=schema_mode,
        )
        full_before = runner._skill_like_raw_tool_set or req.func_tool
        second_event, second_req = await prepare(instance, master)
        await instance.image_studio_get_capabilities(event, mode="text2img")
        await instance.image_studio_get_capabilities(event, query_type="search")
        await instance.image_studio_view_asset(event, asset_ids=[f"{1:064x}"])
        assert set(req.func_tool.names()) == BLOCKED | ALLOWED
        instance._service.fail = True
        failed = await instance.image_studio_generate(
            event, prompt="tree", mode="text2img"
        )
        assert failed.isError and set(req.func_tool.names()) == BLOCKED | ALLOWED
        instance._service.fail = False
        result = await instance.image_studio_generate(
            event, prompt="tree", mode="text2img"
        )
        assert not result.isError
        assert set(runner._func_tool_for_provider().names()) == ALLOWED
        assert set(full_before.names()) == ALLOWED
        if schema_mode == "skills_like":
            assert set(runner._tool_schema_param_set.names()) == ALLOWED
        # Even a stale/hallucinated call from the previous schema cannot execute.
        for name in BLOCKED:
            response = LLMResponse(
                role="assistant",
                tools_call_name=[name],
                tools_call_args=[{}],
                tools_call_ids=["blocked-" + name],
            )
            outputs = [
                part async for part in runner._handle_function_tools(req, response)
            ]
            assert outputs
        assert executed == []
        instance._service.fail = True
        assert (
            await instance.image_studio_generate(event, prompt="later", mode="text2img")
        ).isError
        assert set(req.func_tool.names()) == ALLOWED
        assert set(master.names()) == BLOCKED | ALLOWED
        assert set(second_req.func_tool.names()) == BLOCKED | ALLOWED
        assert all(tool.active for tool in master.tools)
        assert req.system_prompt == second_req.system_prompt == "原有人格"
        assert not req.extra_user_content_parts
        assert second_event.unified_msg_origin == event.unified_msg_origin

    asyncio.run(run())


@pytest.mark.parametrize("schema_mode", ["full", "skills_like"])
def test_same_response_cannot_send_through_old_route_after_generating(schema_mode):
    async def run():
        instance = plugin()
        instance._settings = replace(instance._settings, llm_image_return_mode="asset")
        event, req = await prepare(instance, tools())
        executed = []
        parameter_queries = []

        class Executor:
            async def execute(self, tool, **kwargs):
                executed.append(tool.name)
                if tool.name == "image_studio_generate":
                    yield await instance.image_studio_generate(
                        event, prompt="tree", mode="text2img"
                    )
                else:
                    yield "incorrectly sent"

        async def text_chat(**kwargs):
            parameter_queries.append(set(kwargs["func_tool"].names()))
            return LLMResponse(
                role="assistant",
                tools_call_name=["web_search"],
                tools_call_args=[{}],
                tools_call_ids=["search"],
            )

        runner = ToolLoopAgentRunner()
        await runner.reset(
            provider=SimpleNamespace(provider_config={}, text_chat=text_chat),
            request=req,
            run_context=ContextWrapper(context=None),
            tool_executor=Executor(),
            agent_hooks=BaseAgentRunHooks(),
            tool_schema_mode=schema_mode,
        )
        await instance.image_studio_get_capabilities(event, mode="text2img")
        names = ["image_studio_generate", *sorted(BLOCKED)]
        response = LLMResponse(
            role="assistant",
            tools_call_name=names,
            tools_call_args=[{} for _ in names],
            tools_call_ids=["call-" + n for n in names],
        )
        outputs = [part async for part in runner._handle_function_tools(req, response)]
        assert executed == ["image_studio_generate"]
        blocks = [
            block
            for output in outputs
            for block in (output.tool_call_result_blocks or [])
        ]
        assert len(blocks) == 3
        assert all("not found" in block.content for block in blocks[1:])
        if schema_mode == "skills_like":
            # A mixed selection must not re-expose blocked parameter schemas.
            mixed = LLMResponse(
                role="assistant",
                tools_call_name=["web_search", "send_message_to_user"],
                tools_call_args=[{}, {}],
                tools_call_ids=["search", "send"],
            )
            await runner._resolve_tool_exec(mixed)
            assert parameter_queries == [{"web_search"}]

    asyncio.run(run())


@pytest.mark.parametrize("return_mode", ["preview", "original", "asset"])
def test_partial_success_with_deliverable_assets_triggers_routing(return_mode):
    async def run():
        instance = plugin()
        instance._settings = replace(
            instance._settings, llm_image_return_mode=return_mode
        )
        event, req = await prepare(instance, tools())
        result = await instance._service.generate(mode="text2img")
        response = await instance._agent_generation_result(
            event, result, generation_status="partial"
        )
        assert json.loads(response.content[0].text)["status"] == "partial"
        assert set(req.func_tool.names()) == ALLOWED

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["raise", "incomplete"])
def test_failed_asset_registration_defers_routing_until_successful_recovery(failure):
    async def run():
        instance = plugin()
        event, req = await prepare(instance, tools())
        store = instance.store

        async def fail(*args, **kwargs):
            if failure == "raise":
                raise OSError("unavailable")
            return ()

        instance.store = SimpleNamespace(lease_agent_images=fail)
        result = await instance._service.generate(mode="text2img")
        response = await instance._agent_generation_result(event, result)
        assert response.isError
        assert set(req.func_tool.names()) == BLOCKED | ALLOWED
        instance.store = store
        recovered = await instance.image_studio_task(
            event, task_id=json.loads(response.content[0].text)["task_id"]
        )
        assert not recovered.isError and set(req.func_tool.names()) == ALLOWED

    asyncio.run(run())


@pytest.mark.parametrize("sender_state", ["missing", "inactive"])
def test_no_sender_available_keeps_other_delivery_routes(sender_state):
    async def run():
        instance = plugin()
        master = tools()
        if sender_state == "missing":
            master.remove_tool("image_studio_send_output")
        else:
            master.get_tool("image_studio_send_output").active = False
        event, req = await prepare(instance, master)
        response = await instance._agent_generation_result(
            event, await instance._service.generate(mode="text2img")
        )
        assert not response.isError
        assert BLOCKED <= set(req.func_tool.names())

    asyncio.run(run())


def test_comfy_pending_does_not_block_until_task_returns_images(tmp_path, monkeypatch):
    async def run():
        runtime, service, _, client = await runtime_fixture(tmp_path, monkeypatch)
        client.release_wait.clear()
        instance = plugin()
        instance._settings = service.settings
        instance._service = service
        instance.store = service.store
        event, req = await prepare(instance, tools())
        try:
            await instance.image_studio_get_capabilities(event, mode="text2img")
            response = await instance.image_studio_generate(event, mode="text2img")
            pending = json.loads(response.content[0].text)
            assert pending["status"] == "pending"
            assert BLOCKED <= set(req.func_tool.names())
            client.release_wait.set()
            completed = await instance.image_studio_task(
                event, task_id=pending["task_id"], wait_seconds=5
            )
            assert not completed.isError
            assert set(req.func_tool.names()) == ALLOWED
        finally:
            client.release_wait.set()
            await runtime.close()
            await service.store.close()

    asyncio.run(run())


@pytest.mark.parametrize("with_caption", [False, True])
def test_delivery_notice_matches_successfully_sent_content(with_caption):
    async def run():
        instance = plugin()
        instance.context = SimpleNamespace(get_config=lambda **kwargs: {})
        event = ToolEvent()
        messages = [{"type": "image", "asset_id": f"{1:064x}"}]
        if with_caption:
            messages.append({"type": "plain", "text": "必要说明"})
        result = await instance.image_studio_send_output(
            event, destination="session", messages=messages
        )
        body = json.loads(result.content[0].text)
        assert not result.isError and len(event.sent) == 1
        assert ("随附文字" in body["notice"]) is with_caption
        assert ("不要再次发送这些文字" in body["notice"]) is with_caption
        assert ("无需再次发送相同内容" in body["notice"]) is (not with_caption)
        assert "最终回复" not in body["notice"]
        assert "结束" not in body["notice"]

    asyncio.run(run())


def test_failed_or_workspace_delivery_does_not_claim_user_received_content():
    async def run():
        instance = plugin()

        async def fail(*args):
            raise TimeoutError("no acknowledgment")

        instance._send_output_to_session = fail
        response = await instance.image_studio_send_output(
            ToolEvent(),
            destination="session",
            messages=[{"type": "plain", "text": "附言"}],
        )
        body = json.loads(response.content[0].text)
        assert response.isError and body["status"] == "delivery_unknown"
        assert "notice" not in body

        async def copy_assets(*args):
            return {
                "status": "succeeded",
                "destination": "workspace",
                "workspace_paths": ["image.png"],
            }

        instance._materialize_assets_to_workspace = copy_assets
        copied = await instance.image_studio_send_output(
            ToolEvent(), destination="workspace", messages=[{"asset_id": f"{1:064x}"}]
        )
        assert "已发送" not in copied.content[0].text
        assert "notice" not in json.loads(copied.content[0].text)

    asyncio.run(run())
