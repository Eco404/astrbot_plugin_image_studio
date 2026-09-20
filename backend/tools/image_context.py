"""Adapt AstrBot's image captions only for results produced in this agent run."""

from __future__ import annotations

import inspect
import json
import re
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import Any

from mcp.types import CallToolResult, ImageContent, TextContent

STATE_KEY = "_image_studio_image_context"
WRAPPER_KEY = "_image_studio_image_context_adapter"
_CACHE_NOTICE = re.compile(
    r"Image returned and cached at path='([^'\n]+)'\. "
    r"Review the image below\. Use send_message_to_user to send it to the user "
    r"if satisfied, with type='image' and path='\1'\."
)


@dataclass
class _PendingResult:
    text: str
    origin: str
    detail: str
    images: tuple[tuple[int, str], ...]


@dataclass
class _RunState:
    owner: ImageContextAdapter
    context: Any
    pending: list[_PendingResult] = field(default_factory=list)


def track_image_result(event: Any, result: CallToolResult) -> CallToolResult:
    """Register exact plugin output without exposing internal markers to the LLM."""
    getter = getattr(event, "get_extra", None)
    state = getter(STATE_KEY) if callable(getter) else None
    if not isinstance(state, _RunState) or not state.owner.active or result.isError:
        return result
    if not result.content or not isinstance(result.content[0], TextContent):
        return result
    images = [
        index
        for index, part in enumerate(result.content)
        if isinstance(part, ImageContent)
    ]
    if not images:
        return result
    try:
        payload = json.loads(result.content[0].text)
        origin = payload.get("origin")
        detail = payload.get("return_mode", payload.get("detail"))
        assets = payload.get("assets", [])
        if (
            origin not in {"tool_generated", "existing_asset"}
            or detail not in {"preview", "original"}
            or len(assets) != len(images)
        ):
            return result
        state.pending.append(
            _PendingResult(
                result.content[0].text,
                origin,
                detail,
                tuple(
                    (index, asset["asset_id"]) for index, asset in zip(images, assets)
                ),
            )
        )
    except Exception as exc:
        state.owner._warn(f"unrecognized plugin result ({type(exc).__name__})")
    return result


class ImageContextAdapter:
    """Reversibly wrap a host step; never wrap providers or rewrite old history."""

    def __init__(self, logger: Any) -> None:
        self.logger = logger
        self.active = False
        self._runner_type = None
        self._original = None
        self._wrapper = None
        self._warned = False

    def _warn(self, reason: str) -> None:
        if not self._warned:
            self.logger.warning(
                "[ImageStudio] Tool image context adaptation skipped: %s", reason
            )
            self._warned = True

    def install(self) -> None:
        """Enable only the known AstrBot 4.x async-generator step contract."""
        if self.active:
            return
        try:
            import astrbot
            from astrbot.core.agent.runners.tool_loop_agent_runner import (
                ToolLoopAgentRunner,
            )

            runner_type = ToolLoopAgentRunner
            original = runner_type.step
            version = str(getattr(astrbot, "__version__", ""))
            if (
                not version.startswith("4.")
                or not inspect.isasyncgenfunction(original)
                or tuple(inspect.signature(original).parameters) != ("self",)
            ):
                self._warn("unsupported AstrBot version or step signature")
                return
            previous = getattr(original, WRAPPER_KEY, None)
            if previous is not None:
                previous.restore()
                original = runner_type.step

            @wraps(original)
            async def adapted_step(runner):
                source = original(runner)
                completed = False
                try:
                    async for response in source:
                        yield response
                    completed = True
                finally:
                    try:
                        await source.aclose()
                    finally:
                        self._finish_step(runner, completed=completed)

            setattr(adapted_step, WRAPPER_KEY, self)
            self._runner_type = runner_type
            self._original = original
            self._wrapper = adapted_step
            runner_type.step = adapted_step
            self.active = True
        except Exception as exc:
            self._warn(f"installation failed ({type(exc).__name__})")

    def restore(self) -> None:
        """Disable our layer without replacing another plugin's outer wrapper."""
        self.active = False
        if self._runner_type is not None and self._runner_type.step is self._wrapper:
            self._runner_type.step = self._original

    def bind(self, event: Any, run_context: Any) -> None:
        if self.active and getattr(run_context.context, "event", None) is event:
            event.set_extra(STATE_KEY, _RunState(self, run_context))

    def _finish_step(self, runner: Any, *, completed: bool) -> None:
        try:
            context = runner.run_context
            event = getattr(context.context, "event", None)
            state = event.get_extra(STATE_KEY) if event is not None else None
            if (
                not isinstance(state, _RunState)
                or state.owner is not self
                or state.context is not context
            ):
                return
            pending, state.pending = state.pending, []
            if not self.active or not completed or not pending or runner.was_aborted():
                return
            _rewrite_current_results(context.messages, pending)
        except Exception as exc:
            self._warn(f"unrecognized result layout ({type(exc).__name__})")


def _rewrite_current_results(
    messages: list[Any], pending: list[_PendingResult]
) -> None:
    """Validate the latest tool group completely before changing any message."""
    end = len(messages)
    image_message = None
    if end and messages[end - 1].role == "user":
        image_message = messages[end - 1]
        end -= 1
    start = end
    while start and messages[start - 1].role == "tool":
        start -= 1
    if not start or start == end or messages[start - 1].role != "assistant":
        raise ValueError("missing current tool group")
    calls = {}
    for call in messages[start - 1].tool_calls or []:
        item = call if isinstance(call, dict) else call.model_dump()
        call_id = item["id"]
        if call_id in calls:
            raise ValueError("duplicate tool call id")
        calls[call_id] = item["function"]["name"]

    changes = []
    captions = {}
    remaining = list(pending)
    for message in messages[start:end]:
        name = calls.get(message.tool_call_id)
        if name not in {
            "image_studio_generate",
            "image_studio_task",
            "image_studio_view_asset",
        } or not isinstance(message.content, str):
            continue
        for index, item in enumerate(remaining):
            expected_names = (
                {"image_studio_view_asset"}
                if item.origin == "existing_asset"
                else {"image_studio_generate", "image_studio_task"}
            )
            if name in expected_names and message.content.startswith(
                item.text + "\n\n"
            ):
                break
        else:
            continue
        tail = message.content[len(item.text) :]
        for image_index, asset_id in item.images:
            if not tail.startswith("\n\n"):
                raise ValueError("missing image cache notice")
            notice = _CACHE_NOTICE.match(tail, 2)
            if notice is None:
                raise ValueError("unknown image cache notice")
            path = notice.group(1)
            if Path(path).stem != f"{message.tool_call_id}_{image_index}":
                raise ValueError("image belongs to a different call")
            header = f"[Image from tool '{name}', path='{path}']"
            if header in captions:
                raise ValueError("duplicate image caption")
            size = "预览" if item.detail == "preview" else "原图"
            description = (
                f"Image Studio 生成结果（{size}）：由你调用生图工具生成。本次工具调用未向用户发送图片。"
                if item.origin == "tool_generated"
                else f"Image Studio 已有图片（{size}）：本次只查看，未生成或发送图片。"
            )
            captions[header] = (path, f"{header}\n{description} asset_id={asset_id}")
            tail = tail[notice.end() :]
        # Preserve host repeated-call warnings and follow-up messages verbatim.
        changes.append((message, "content", item.text + tail))
        remaining.pop(index)
    if remaining:
        raise ValueError("plugin results were truncated or not found")

    if image_message is not None:
        parts = image_message.content
        if not isinstance(parts, list):
            raise ValueError("unknown image message format")
        found = set()
        for index, part in enumerate(parts):
            header = getattr(part, "text", None)
            if header not in captions:
                continue
            path, replacement = captions[header]
            image = parts[index + 1] if index + 1 < len(parts) else None
            if (
                header in found
                or getattr(image, "type", None) != "image_url"
                or getattr(image.image_url, "id", None) != path
            ):
                raise ValueError("image caption and image do not match")
            found.add(header)
            changes.append((part, "text", replacement))
        if found != captions.keys():
            raise ValueError("some image captions are missing")
    for target, attribute, value in changes:
        setattr(target, attribute, value)
