"""Exercise image caption adaptation at the real host's next model request."""

import asyncio
import base64
import copy
from dataclasses import replace
from functools import wraps
from types import SimpleNamespace
from uuid import uuid4

import mcp.types
import pytest
from astrbot.core.agent.hooks import BaseAgentRunHooks
from astrbot.core.agent.message import Message
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.provider.entities import LLMResponse, ProviderRequest
from astrbot_plugin_image_studio.backend.tools.image_context import (
    ImageContextAdapter,
    STATE_KEY,
    track_image_result,
)
from astrbot_plugin_image_studio.tests.backend.integration.test_agent_delivery_scope import (
    plugin,
)
from astrbot_plugin_image_studio.tests.backend.integration.test_service_and_tool import (
    PNG,
    ToolEvent,
)


@pytest.fixture
def adapter():
    warnings = []
    adapter = ImageContextAdapter(
        SimpleNamespace(warning=lambda *args: warnings.append(args))
    )
    adapter.warnings = warnings
    adapter.install()
    assert adapter.active
    try:
        yield adapter
    finally:
        adapter.restore()


async def make_runner(
    adapter,
    names,
    *,
    mode="preview",
    streaming=False,
    schema_mode="full",
    image_support=True,
    bind=True,
    history=(),
    results_per_call=1,
):
    instance = plugin()
    instance._settings = replace(instance._settings, llm_image_return_mode=mode)
    instance._image_context_adapter = adapter
    event = ToolEvent()
    observations = []
    tool_outputs = []
    token = uuid4().hex
    response = LLMResponse(
        role="assistant",
        tools_call_name=list(names),
        tools_call_args=[{} for _ in names],
        tools_call_ids=[f"call_{token}_{i}" for i in range(len(names))],
    )
    replies = [response, LLMResponse(role="assistant", completion_text="完成")]
    if schema_mode == "skills_like":
        replies.insert(1, copy.deepcopy(response))

    async def chat(**kwargs):
        observations.append(
            [
                copy.deepcopy(message)
                if isinstance(message, dict)
                else message.model_dump()
                for message in kwargs["contexts"]
            ]
        )
        return replies.pop(0)

    async def stream(**kwargs):
        yield await chat(**kwargs)

    class Hooks(BaseAgentRunHooks):
        async def on_agent_begin(self, context):
            if bind:
                await instance.prepare_agent_image_context(event, context)

    class Executor:
        async def execute(self, tool, **kwargs):
            if tool.name == "image_studio_view_asset":
                result = await instance.image_studio_view_asset(
                    event, asset_ids=[f"{1:064x}"], detail=mode
                )
            elif tool.name in {"image_studio_generate", "image_studio_task"}:
                generated = await instance._service.generate(mode="text2img")
                generated = replace(
                    generated, images=generated.images * results_per_call
                )
                result = await instance._agent_generation_result(event, generated)
            else:
                result = mcp.types.CallToolResult(
                    content=[
                        mcp.types.TextContent(type="text", text="其他工具的结果"),
                        mcp.types.ImageContent(
                            type="image",
                            data=base64.b64encode(PNG).decode(),
                            mimeType="image/png",
                        ),
                    ]
                )
            tool_outputs.append(result)
            yield result

    request = ProviderRequest(
        contexts=[m.model_dump() for m in history],
        system_prompt="人格与其他插件说明保持原样",
        func_tool=ToolSet(
            [FunctionTool(name=name, description=name, parameters={}) for name in names]
        ),
    )
    event.set_extra("provider_request", request)
    runner = ToolLoopAgentRunner()
    await runner.reset(
        provider=SimpleNamespace(
            provider_config={
                "modalities": [] if image_support else ["text", "tool_use"]
            },
            text_chat=chat,
            text_chat_stream=stream,
        ),
        request=request,
        run_context=ContextWrapper(context=SimpleNamespace(event=event)),
        tool_executor=Executor(),
        agent_hooks=Hooks(),
        streaming=streaming,
        tool_schema_mode=schema_mode,
    )
    return runner, event, observations, tool_outputs


async def step(runner):
    return [response async for response in runner.step()]


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("mode", ["preview", "original"])
@pytest.mark.parametrize("name", ["image_studio_generate", "image_studio_task"])
def test_next_request_receives_owned_caption_and_no_native_sender_hint(
    adapter, streaming, mode, name
):
    async def run():
        runner, event, observations, outputs = await make_runner(
            adapter, [name], mode=mode, streaming=streaming, results_per_call=2
        )
        await step(runner)
        tool = next(m for m in runner.run_context.messages if m.role == "tool")
        assert tool.content == outputs[0].content[0].text
        assert not event.get_extra(STATE_KEY).pending
        image_parts = runner.run_context.messages[-1].content
        for index, part in enumerate(image_parts[1::2]):
            assert part.image_url.url.endswith(outputs[0].content[index + 1].data)
        first_caption = image_parts[0].text
        assert "由你调用生图工具生成" in first_caption
        assert "本次工具调用未向用户发送图片" in first_caption
        assert ("预览" in first_caption) is (mode == "preview")
        assert "asset_id=" in first_caption
        await step(runner)
        assert "Use send_message_to_user" not in str(observations[1])
        assert observations[1][-1]["content"][0]["text"] == first_caption
        assert not adapter.warnings

    asyncio.run(run())


def test_mixed_tools_repeated_view_results_and_previous_history_are_preserved(adapter):
    async def run():
        history = [
            Message(role="user", content="[Image from tool 'old', path='old.png']"),
            Message(role="assistant", content="此前的正文与图片说明"),
        ]
        runner, _, observations, _ = await make_runner(
            adapter,
            ["image_studio_view_asset", "other_image_tool", "image_studio_view_asset"],
            history=history,
        )
        await step(runner)
        parts = runner.run_context.messages[-1].content
        assert "本次只查看" in parts[0].text
        assert "由你调用生图工具生成" not in parts[0].text
        assert parts[2].text.startswith("[Image from tool 'other_image_tool'")
        assert "Image Studio" not in parts[2].text
        assert "本次只查看" in parts[4].text
        assert parts[0].text != parts[4].text  # Two distinct call IDs/paths.
        await step(runner)
        original_history = [m.model_dump() for m in history]
        assert observations[0][1:3] == observations[1][1:3] == original_history
        assert str(observations[1]).count("Use send_message_to_user") == 1
        assert not adapter.warnings

    asyncio.run(run())


@pytest.mark.parametrize("image_support", [False, True])
@pytest.mark.parametrize("mode", ["preview", "asset"])
def test_no_image_and_text_only_provider_paths(adapter, image_support, mode):
    async def run():
        runner, _, _, _ = await make_runner(
            adapter, ["image_studio_generate"], mode=mode, image_support=image_support
        )
        await step(runner)
        assert "Use send_message_to_user" not in str(runner.run_context.messages)
        if not image_support or mode == "asset":
            assert runner.run_context.messages[-1].role == "tool"
        assert not adapter.warnings

    asyncio.run(run())


def test_other_events_and_unregistered_plugin_results_remain_unchanged(adapter):
    async def run():
        first, _, _, _ = await make_runner(adapter, ["image_studio_generate"])
        second, _, _, _ = await make_runner(
            adapter, ["image_studio_generate"], bind=False
        )
        await asyncio.gather(step(first), step(second))
        assert "Use send_message_to_user" not in str(first.run_context.messages)
        assert "Use send_message_to_user" in str(second.run_context.messages)
        assert (
            "由你调用生图工具生成"
            not in second.run_context.messages[-1].content[0].text
        )

    asyncio.run(run())


def test_unknown_host_layout_falls_back_without_partial_rewrite(adapter, monkeypatch):
    original_finish = adapter._finish_step
    snapshots = []

    def altered_layout(runner, *, completed):
        if completed and runner.run_context.messages[-1].role == "user":
            runner.run_context.messages[-1].content[-2].text = "Changed host caption"
            snapshots.extend(m.model_dump() for m in runner.run_context.messages)
        original_finish(runner, completed=completed)

    monkeypatch.setattr(adapter, "_finish_step", altered_layout)

    async def run():
        runner, event, _, _ = await make_runner(
            adapter, ["image_studio_generate"], results_per_call=2
        )
        await step(runner)
        assert [m.model_dump() for m in runner.run_context.messages] == snapshots
        assert "Use send_message_to_user" in str(runner.run_context.messages)
        assert not event.get_extra(STATE_KEY).pending
        assert len(adapter.warnings) == 1

    asyncio.run(run())


def test_unload_preserves_later_wrappers_and_install_is_idempotent(
    adapter, monkeypatch
):
    ours = ToolLoopAgentRunner.step
    adapter.install()
    assert ToolLoopAgentRunner.step is ours

    @wraps(ours)
    async def other_wrapper(self):
        async for response in ours(self):
            yield response

    monkeypatch.setattr(ToolLoopAgentRunner, "step", other_wrapper)
    adapter.restore()
    assert ToolLoopAgentRunner.step is other_wrapper
    assert not adapter.active
    # Restore the actual host entry for the next fixture, retaining monkeypatch cleanup.
    monkeypatch.setattr(ToolLoopAgentRunner, "step", adapter._original)


def test_invalid_registration_does_not_break_tool_output(adapter):
    event = ToolEvent()
    adapter.bind(event, ContextWrapper(context=SimpleNamespace(event=event)))
    result = mcp.types.CallToolResult(
        content=[
            mcp.types.TextContent(type="text", text="not json"),
            mcp.types.ImageContent(type="image", data="x", mimeType="image/png"),
        ]
    )
    assert track_image_result(event, result) is result
    assert len(adapter.warnings) == 1


def test_repeated_call_warning_survives_and_two_phase_images_are_adapted(adapter):
    async def run():
        runner, _, _, _ = await make_runner(
            adapter,
            ["image_studio_view_asset"] * 3,
            schema_mode="skills_like",
        )
        await step(runner)
        messages = runner.run_context.messages
        assert "Use send_message_to_user" not in str(messages)
        assert (
            runner.REPEATED_TOOL_NOTICE_L1_TEMPLATE.split("{", 1)[0]
            in messages[-2].content
        )
        assert sum("本次只查看" in p.text for p in messages[-1].content[::2]) == 3
        assert not adapter.warnings

    asyncio.run(run())


def test_truncated_tool_text_keeps_entire_group_unchanged(adapter, monkeypatch):
    async def truncate(self, *, tool_call_id, content):
        return "Result saved to overflow file."

    monkeypatch.setattr(ToolLoopAgentRunner, "_materialize_large_tool_result", truncate)

    async def run():
        runner, event, _, _ = await make_runner(adapter, ["image_studio_generate"])
        await step(runner)
        assert (
            runner.run_context.messages[-2].content == "Result saved to overflow file."
        )
        assert "Image Studio" not in runner.run_context.messages[-1].content[0].text
        assert not event.get_extra(STATE_KEY).pending
        assert len(adapter.warnings) == 1

    asyncio.run(run())


@pytest.mark.parametrize("exit_kind", ["close", "cancel", "error"])
def test_generator_shutdown_clears_pending_and_preserves_exception(
    exit_kind, monkeypatch
):
    closed = []
    warnings = []
    adapter = ImageContextAdapter(
        SimpleNamespace(warning=lambda *x: warnings.append(x))
    )
    event = ToolEvent()
    context = ContextWrapper(context=SimpleNamespace(event=event))

    async def source(self):
        adapter.bind(event, context)
        event.get_extra(STATE_KEY).pending.append("must be discarded")
        try:
            yield "partial step"
            if exit_kind == "cancel":
                raise asyncio.CancelledError
            if exit_kind == "error":
                raise RuntimeError("host failure")
        finally:
            closed.append(True)

    monkeypatch.setattr(ToolLoopAgentRunner, "step", source)
    adapter.install()

    async def run():
        runner = ToolLoopAgentRunner()
        runner.run_context = context
        stream = runner.step()
        assert await anext(stream) == "partial step"
        if exit_kind == "close":
            await stream.aclose()
        else:
            error = asyncio.CancelledError if exit_kind == "cancel" else RuntimeError
            with pytest.raises(error):
                await anext(stream)
        assert closed == [True]
        assert not event.get_extra(STATE_KEY).pending
        assert not warnings

    try:
        asyncio.run(run())
    finally:
        adapter.restore()
    assert ToolLoopAgentRunner.step is source


def test_incompatible_host_is_not_patched(monkeypatch):
    import astrbot

    original = ToolLoopAgentRunner.step
    monkeypatch.setattr(astrbot, "__version__", "5.0.0")
    warnings = []
    adapter = ImageContextAdapter(
        SimpleNamespace(warning=lambda *x: warnings.append(x))
    )
    adapter.install()
    assert not adapter.active
    assert ToolLoopAgentRunner.step is original
    assert len(warnings) == 1
