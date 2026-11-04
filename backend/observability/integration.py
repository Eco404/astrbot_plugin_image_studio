"""Operation logging at the Page API boundary, without consuming request bodies."""

import time
import uuid
import inspect
import json
from functools import wraps

from .store import log_context


def observe_tool(handler):
    """Record tool boundaries, including validation failures before generation."""
    signature = inspect.signature(handler)

    @wraps(handler)
    async def observed(self, *args, **kwargs):
        logs = getattr(self, "_logs", None)
        if logs is None or not logs.enabled:
            return await handler(self, *args, **kwargs)
        with log_context(operation_id=uuid.uuid4().hex):
            started = time.monotonic()
            if logs.accepts("DEBUG"):
                bound = signature.bind(self, *args, **kwargs)
                arguments = {
                    key: value
                    for key, value in bound.arguments.items()
                    if key not in {"self", "event"}
                }
                await logs.record(
                    "DEBUG",
                    "operation",
                    "调用插件工具",
                    data={
                        "tool": handler.__name__,
                        "arguments": arguments,
                    },
                )
            try:
                result = await handler(self, *args, **kwargs)
            except Exception as exc:
                await logs.record(
                    "ERROR",
                    "operation",
                    "插件工具异常",
                    data={
                        "tool": handler.__name__,
                        "error": str(exc),
                    },
                )
                raise
            details = {
                "tool": handler.__name__,
                "duration_ms": round((time.monotonic() - started) * 1000),
            }
            failed = bool(getattr(result, "isError", False))
            for content in getattr(result, "content", ()):
                if getattr(content, "type", "") != "text":
                    continue
                try:
                    payload = json.loads(content.text)
                except (TypeError, ValueError):
                    continue
                if isinstance(payload, dict):
                    details.update(
                        {
                            key: payload[key]
                            for key in (
                                "status",
                                "request_status",
                                "generation_id",
                                "task_id",
                                "error",
                            )
                            if key in payload
                        }
                    )
                break
            await logs.record(
                "WARNING" if failed else "INFO",
                "operation",
                "插件工具返回错误" if failed else "插件工具执行完成",
                data=details,
            )
            return result

    return observed


def observe_api(plugin, handler, route, methods):
    # Read polling and log management should not fill the log they display.
    if "POST" not in methods or route.startswith("logs"):
        return handler

    @wraps(handler)
    async def observed(*args, **kwargs):
        logs = getattr(plugin, "_logs", None)
        if logs is None or (not logs.enabled and route != "settings/save"):
            return await handler(*args, **kwargs)
        started = time.monotonic()
        with log_context(operation_id=uuid.uuid4().hex):
            try:
                response = await handler(*args, **kwargs)
            except Exception as exc:
                await logs.record(
                    "ERROR",
                    "operation",
                    "页面操作失败",
                    data={
                        "route": route,
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                    },
                )
                raise
            status = getattr(response, "status_code", 200)
            details = {
                "route": route,
                "status": status,
                "duration_ms": round((time.monotonic() - started) * 1000),
            }
            # JSONResponse has an already-materialized body. Never consume a
            # streaming export or copy a potentially large image preview.
            body = getattr(response, "body", None)
            if isinstance(body, bytes) and len(body) <= 65536:
                try:
                    payload = json.loads(body)
                except (ValueError, UnicodeError):
                    payload = None
                if isinstance(payload, dict):
                    for key in ("generation_id", "task_id", "settings_revision"):
                        if key in payload:
                            details[key] = payload[key]
                    if status >= 400:
                        details["error"] = payload.get("message", payload.get("error"))
            await logs.record(
                "ERROR" if status >= 500 else "WARNING" if status >= 400 else "INFO",
                "operation",
                "页面操作完成",
                data=details,
            )
            return response

    return observed
