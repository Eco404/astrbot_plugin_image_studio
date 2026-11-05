"""Delivery protection follows real host runners when other hooks rebuild tools."""

import asyncio
import copy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from astrbot.core.agent.hooks import BaseAgentRunHooks
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.agent.tool import ToolSet
from astrbot.core.provider.entities import LLMResponse
from astrbot_plugin_image_studio.tests.backend.integration.test_agent_delivery_scope import (
    ALLOWED,
    BLOCKED,
    plugin,
    prepare,
    tools,
)
from astrbot_plugin_image_studio.tests.backend.integration.test_agent_image_context import (
    adapter as adapter,
    step,
)


def _response(names, prefix):
    return LLMResponse(
        role="assistant",
        tools_call_name=list(names),
        tools_call_args=[{} for _ in names],
        tools_call_ids=[f"{prefix}-{name}" for name in names],
    )


def _replace_runner_tools(runner, shared):
    """Emulate another plugin publishing fresh tool sets shared with a registry."""
    if runner.tool_schema_mode == "skills_like":
        light, parameters = (
            shared.get_light_tool_set(),
            shared.get_param_only_tool_set(),
        )
        runner.req.func_tool = light
        runner._skill_like_raw_tool_set = shared
        runner._tool_schema_param_set = parameters
        return [shared, light, parameters]
    runner.req.func_tool = shared
    return [shared]


def _assert_protected(runner):
    assert set(runner.req.func_tool.names()) == ALLOWED
    if runner.tool_schema_mode == "skills_like":
        assert set(runner._skill_like_raw_tool_set.names()) == ALLOWED
        assert set(runner._tool_schema_param_set.names()) == ALLOWED


async def _runner(
    adapter, *, schema_mode, batches, replacement="before_reset", mode="asset"
):
    instance = plugin()
    instance._settings = replace(instance._settings, llm_image_return_mode=mode)
    instance._image_context_adapter = adapter
    master = tools()
    event, request = await prepare(instance, master)
    _, other_request = await prepare(instance, master)
    external_sets = []
    if replacement == "before_reset":
        request.func_tool = ToolSet(list(request.func_tool.tools))
        external_sets.append(request.func_tool)

    replies = []
    for batch in batches:
        replies.append(batch)
        if schema_mode == "skills_like":
            replies.append(copy.deepcopy(batch))
    observations, executed, results = [], [], []

    async def chat(**kwargs):
        observations.append(set(kwargs["func_tool"].names()))
        return replies.pop(0)

    class Hooks(BaseAgentRunHooks):
        async def on_agent_begin(self, context):
            await instance.prepare_agent_image_context(event, context)
            if replacement == "on_agent_begin":
                external_sets.extend(_replace_runner_tools(runner, master))

    class Executor:
        async def execute(self, tool, **kwargs):
            executed.append(tool.name)
            if tool.name == "image_studio_generate":
                result = await instance.image_studio_generate(
                    event, prompt="tree", mode="text2img"
                )
                results.append(result)
                yield result
            else:
                yield "executed " + tool.name

    runner = ToolLoopAgentRunner()
    await runner.reset(
        provider=SimpleNamespace(provider_config={}, text_chat=chat),
        request=request,
        run_context=ContextWrapper(context=SimpleNamespace(event=event)),
        tool_executor=Executor(),
        agent_hooks=Hooks(),
        tool_schema_mode=schema_mode,
    )
    await instance.image_studio_get_capabilities(event, mode="text2img")
    return SimpleNamespace(
        runner=runner,
        instance=instance,
        event=event,
        master=master,
        other_request=other_request,
        external_sets=external_sets,
        observations=observations,
        executed=executed,
        results=results,
        replies=replies,
    )


@pytest.mark.parametrize("schema_mode", ["full", "skills_like"])
@pytest.mark.parametrize("replacement", ["before_reset", "on_agent_begin"])
def test_rebuilt_sets_reject_same_batch_senders_without_touching_shared_tools(
    adapter, schema_mode, replacement
):
    async def run():
        scope = await _runner(
            adapter,
            schema_mode=schema_mode,
            replacement=replacement,
            batches=[_response(["image_studio_generate", *sorted(BLOCKED)], "first")],
        )
        await step(scope.runner)
        assert scope.executed == ["image_studio_generate"]
        assert not scope.results[0].isError
        assert all(part.type != "image" for part in scope.results[0].content)
        _assert_protected(scope.runner)
        errors = [
            message.content
            for message in scope.runner.run_context.messages
            if message.role == "tool" and "not found" in message.content
        ]
        assert len(errors) == len(BLOCKED)
        assert set(scope.master.names()) == ALLOWED | BLOCKED
        assert set(scope.other_request.func_tool.names()) == ALLOWED | BLOCKED
        assert all(
            set(view.names()) == ALLOWED | BLOCKED for view in scope.external_sets
        )
        assert all(tool.active for tool in scope.master.tools)
        assert scope.runner.req.system_prompt == "原有人格"
        if schema_mode == "skills_like":
            scope.replies.append(_response(["web_search"], "parameter"))
            await scope.runner._resolve_tool_exec(
                _response(["web_search", "send_message_to_user"], "mixed")
            )
            assert scope.observations[-1] == {"web_search"}
        assert not adapter.warnings

    asyncio.run(run())


@pytest.mark.parametrize("schema_mode", ["full", "skills_like"])
def test_later_step_rebinds_reintroduced_tools_before_the_model_sees_them(
    adapter, schema_mode
):
    async def run():
        scope = await _runner(
            adapter,
            schema_mode=schema_mode,
            batches=[
                _response(["image_studio_generate"], "first"),
                _response(["web_search", *sorted(BLOCKED)], "second"),
            ],
        )
        await step(scope.runner)
        _assert_protected(scope.runner)
        replaced = _replace_runner_tools(scope.runner, scope.master)
        assert BLOCKED <= set(scope.runner.req.func_tool.names())
        observations_before = len(scope.observations)
        await step(scope.runner)
        assert scope.executed == ["image_studio_generate", "web_search"]
        assert scope.observations[observations_before] == ALLOWED
        if schema_mode == "skills_like":
            assert scope.observations[-1] == {"web_search"}
        _assert_protected(scope.runner)
        assert all(set(view.names()) == ALLOWED | BLOCKED for view in replaced)
        assert set(scope.other_request.func_tool.names()) == ALLOWED | BLOCKED
        assert not adapter.warnings

    asyncio.run(run())


def test_unrecognized_image_caption_does_not_disable_sender_protection(
    adapter, monkeypatch
):
    async def unfamiliar_result(self, *, tool_call_id, content):
        return "Host changed its result layout."

    monkeypatch.setattr(
        ToolLoopAgentRunner, "_materialize_large_tool_result", unfamiliar_result
    )

    async def run():
        scope = await _runner(
            adapter,
            schema_mode="skills_like",
            mode="preview",
            batches=[_response(["image_studio_generate", *sorted(BLOCKED)], "first")],
        )
        await step(scope.runner)
        assert scope.executed == ["image_studio_generate"]
        assert any(part.type == "image" for part in scope.results[0].content)
        _assert_protected(scope.runner)
        assert len(adapter.warnings) == 1
        assert "context adaptation skipped" in adapter.warnings[0][0]
        assert set(scope.master.names()) == ALLOWED | BLOCKED

    asyncio.run(run())


@pytest.mark.parametrize("schema_mode", ["full", "skills_like"])
def test_other_plugin_removal_of_studio_tools_is_not_reversed(adapter, schema_mode):
    async def run():
        scope = await _runner(
            adapter,
            schema_mode=schema_mode,
            batches=[_response(["web_search", "image_studio_generate"], "first")],
        )
        restricted = ToolSet([scope.master.get_tool("web_search")])
        _replace_runner_tools(scope.runner, restricted)
        await step(scope.runner)
        assert scope.executed == ["web_search"]
        assert scope.observations[0] == {"web_search"}
        assert scope.runner.req.func_tool.names() == ["web_search"]
        assert restricted.names() == ["web_search"]
        assert not scope.results

    asyncio.run(run())


@pytest.mark.parametrize("missing_set", ["raw", "parameters"])
def test_missing_executable_studio_sender_keeps_other_senders(adapter, missing_set):
    async def run():
        scope = await _runner(
            adapter,
            schema_mode="skills_like",
            batches=[
                _response(["image_studio_generate", "send_message_to_user"], "first")
            ],
        )
        target = (
            scope.runner._skill_like_raw_tool_set
            if missing_set == "raw"
            else scope.runner._tool_schema_param_set
        )
        target.remove_tool("image_studio_send_output")
        await step(scope.runner)
        assert scope.executed == ["image_studio_generate", "send_message_to_user"]
        assert not scope.results[0].isError
        assert BLOCKED <= set(scope.runner.req.func_tool.names())
        assert set(scope.master.names()) == ALLOWED | BLOCKED

    asyncio.run(run())
