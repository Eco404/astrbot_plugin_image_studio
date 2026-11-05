"""Keep generated-image delivery routes scoped to one agent request."""

from __future__ import annotations

from copy import copy
from dataclasses import dataclass
from typing import Any

from astrbot.core.agent.tool import ToolSet

STATE_KEY = "_image_studio_delivery_tools"
_BLOCKED = frozenset({"send_message_to_user", "pc_send_current_media"})
_SENDER = "image_studio_send_output"


class _DeliveryToolSet(ToolSet):
    """Retain isolated schemas even when no supported host runner is bound."""

    def __init__(self, tools):
        super().__init__(list(tools))
        self.views = [self]

    def get_light_tool_set(self):
        view = super().get_light_tool_set()
        self.views.append(view)
        return view

    def get_param_only_tool_set(self):
        view = super().get_param_only_tool_set()
        self.views.append(view)
        return view


@dataclass
class _DeliveryState:
    request: Any
    owned: _DeliveryToolSet
    runner: Any = None
    context: Any = None
    protected: bool = False


def _state(event: Any) -> _DeliveryState | None:
    getter = getattr(event, "get_extra", None)
    state = getter(STATE_KEY) if callable(getter) else None
    return state if isinstance(state, _DeliveryState) else None


def prepare_delivery_tools(event: Any, request: Any) -> None:
    """Copy this request's list without hiding tools or changing their handlers."""
    state = _state(event)
    if state is not None and state.request is request:
        _synchronize(state)
        return
    tools = getattr(getattr(request, "func_tool", None), "tools", None)
    if not isinstance(tools, list) or not any(
        getattr(tool, "name", "") in {"image_studio_generate", "image_studio_task"}
        for tool in tools
    ):
        if state is not None:
            event.set_extra(STATE_KEY, None)
        return
    owned = _DeliveryToolSet(tools)
    request.func_tool = owned
    event.set_extra(STATE_KEY, _DeliveryState(request, owned))


def bind_delivery_runner(runner: Any) -> None:
    """Bind the actual runner before its first model request and on later steps.

    Request hooks may have replaced ToolSet after our preparation. The runner's
    light, parameter-only and handler sets are authoritative at this boundary.
    This state is independent of image previews and image-caption adaptation.
    """
    context = getattr(runner, "run_context", None)
    event = getattr(getattr(context, "context", None), "event", None)
    state = _state(event)
    if state is None or state.request is not getattr(runner, "req", None):
        return
    state.runner, state.context = runner, context
    _synchronize(state)


def _live_runner(state: _DeliveryState) -> Any:
    runner = state.runner
    if (
        runner is not None
        and getattr(runner, "req", None) is state.request
        and getattr(runner, "run_context", None) is state.context
    ):
        return runner
    return None


def _isolate(state: _DeliveryState, owner: Any, attribute: str) -> None:
    current = getattr(owner, attribute, None)
    if not isinstance(current, ToolSet):
        return
    if any(current is view for view in state.owned.views):
        return
    # Preserve third-party ToolSet subclasses and handler objects, while never
    # removing tools from their shared registry or another event's list.
    isolated = copy(current)
    isolated.tools = list(current.tools)
    setattr(owner, attribute, isolated)
    state.owned.views.append(isolated)


def _has_sender(tool_set: Any) -> bool:
    if not isinstance(tool_set, ToolSet):
        return False
    sender = tool_set.get_tool(_SENDER)
    return sender is not None and getattr(sender, "active", True)


def _can_deliver(state: _DeliveryState) -> bool:
    if not _has_sender(getattr(state.request, "func_tool", None)):
        return False
    runner = _live_runner(state)
    if runner is not None and runner.tool_schema_mode == "skills_like":
        return _has_sender(runner._skill_like_raw_tool_set) and _has_sender(
            runner._tool_schema_param_set
        )
    return True


def _synchronize(state: _DeliveryState) -> None:
    _isolate(state, state.request, "func_tool")
    runner = _live_runner(state)
    if runner is not None:
        _isolate(state, runner, "_skill_like_raw_tool_set")
        _isolate(state, runner, "_tool_schema_param_set")
    if state.protected and _can_deliver(state):
        for view in state.owned.views:
            view.tools = [tool for tool in view.tools if tool.name not in _BLOCKED]


def protect_generated_image_delivery(event: Any) -> None:
    """Activate only after this user message obtains registered generated assets."""
    state = _state(event)
    if state is None:
        getter = getattr(event, "get_extra", None)
        request = getter("provider_request") if callable(getter) else None
        if request is None:
            return
        prepare_delivery_tools(event, request)
        state = _state(event)
    if state is None or not _can_deliver(state):
        return
    state.protected = True
    # Re-read actual runner fields: another hook may have rebuilt a set since
    # step entry. Filtering occurs before the host resolves the next tool call.
    _synchronize(state)
