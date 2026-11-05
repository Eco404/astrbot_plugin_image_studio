"""Observe real aiohttp calls without consuming response or upload streams."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from .store import StructuredLogStore, sanitize


def _body_details(kwargs: dict[str, Any]) -> dict[str, Any]:
    if "json" in kwargs:
        return {"format": "json", "body": kwargs["json"]}
    if "data" not in kwargs:
        return {"format": "none"}
    data = kwargs["data"]
    # FormData has not yet been serialized. Inspect descriptions only; never
    # call data(), read(), or seek(), which can alter an upload or its boundary.
    fields = getattr(data, "_fields", None)
    if isinstance(fields, list):
        return {
            "format": "multipart" if getattr(data, "is_multipart", False) else "form",
            "fields": [
                {
                    "name": options.get("name"),
                    "filename": options.get("filename"),
                    "headers": headers,
                    "value": sanitize({str(options.get("name", "")): value}).get(
                        str(options.get("name", ""))
                    ),
                }
                for options, headers, value in fields
            ],
        }
    return {"format": "data", "body": data}


def _response_details(response: Any) -> dict[str, Any]:
    raw_response = getattr(response, "_response", response)
    headers = getattr(raw_response, "headers", {})
    result = {
        "content_type": headers.get("Content-Type", ""),
        "content_length": headers.get("Content-Length"),
    }
    # Only inspect bytes aiohttp already cached as a consequence of the caller's
    # read/json/text. ComfyUI's streamed responses remain untouched.
    body = getattr(raw_response, "_body", None)
    if isinstance(body, bytes):
        result["bytes_read"] = len(body)
        if len(body) <= 128 * 1024 and (
            "json" in str(result["content_type"]).lower()
            or "text" in str(result["content_type"]).lower()
        ):
            try:
                result["body"] = json.loads(body)
            except (ValueError, UnicodeError, RecursionError):
                result["body"] = body[:4096].decode("utf-8", errors="replace")
        else:
            result["body"] = {"kind": "binary or large response", "bytes": len(body)}
    return result


class _Response:
    def __init__(self, response: Any, owner: "_Request") -> None:
        self._wrapped, self._owner = response, owner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        try:
            return await self._wrapped.__aexit__(exc_type, exc, traceback)
        finally:
            await self._owner.finish(exc)

    async def read(self, *args, **kwargs):
        return await self._read("read", *args, **kwargs)

    async def text(self, *args, **kwargs):
        return await self._read("text", *args, **kwargs)

    async def json(self, *args, **kwargs):
        return await self._read("json", *args, **kwargs)

    async def _read(self, method, *args, **kwargs):
        try:
            result = await getattr(self._wrapped, method)(*args, **kwargs)
        except BaseException as exc:
            await self._owner.finish(exc)
            raise
        if not self._owner.entered:
            await self._owner.finish()
        return result


class _Request:
    def __init__(
        self,
        owner: "LoggedSession",
        method: str,
        url: Any,
        kwargs: dict[str, Any],
        *,
        websocket: bool = False,
    ) -> None:
        self.owner, self.method, self.url, self.kwargs = owner, method, url, kwargs
        self.websocket = websocket
        self.request_id = uuid.uuid4().hex
        self.started = 0.0
        self.entered = self.finished = False
        self.context = self.response = None

    def __await__(self):
        return self.open().__await__()

    async def __aenter__(self):
        self.entered = True
        return await self.open()

    async def __aexit__(self, exc_type, exc, traceback):
        try:
            if self.context is not None:
                return await self.context.__aexit__(exc_type, exc, traceback)
        finally:
            await self.finish(exc)

    def identity(self):
        return {
            "provider_id": self.owner.provider_id,
            "method": self.method,
            "url": str(self.url),
            "transport": "websocket" if self.websocket else "http",
        }

    async def open(self):
        self.started = time.monotonic()
        if self.owner.logs.accepts("DEBUG"):
            headers = dict(getattr(self.owner.session, "headers", {}))
            headers.update(self.kwargs.get("headers") or {})
            await self.record(
                "DEBUG",
                "发送服务商请求",
                {
                    **self.identity(),
                    "headers": headers,
                    "params": (
                        dict(self.kwargs["params"])
                        if isinstance(self.kwargs.get("params"), (list, tuple))
                        else self.kwargs.get("params")
                    ),
                    "proxy": self.kwargs.get("proxy"),
                    **_body_details(self.kwargs),
                },
            )
        try:
            if self.websocket:
                self.context = self.owner.session.ws_connect(self.url, **self.kwargs)
            else:
                self.context = self.owner.session.request(
                    self.method, self.url, **self.kwargs
                )
            self.response = (
                await self.context.__aenter__() if self.entered else await self.context
            )
            if self.websocket:
                # Observe only the handshake, never change message iteration.
                await self.finish()
                return self.response
            return _Response(self.response, self)
        except BaseException as exc:
            await self.finish(exc)
            raise

    async def record(self, level: str, message: str, data: Any):
        await self.owner.logs.record(
            level,
            "request",
            message,
            data,
            request_id=self.request_id,
            operation_id=self.owner.operation_id or None,
        )

    async def finish(self, exc: BaseException | None = None):
        if self.finished:
            return
        self.finished = True
        raw_response = getattr(self.response, "_response", self.response)
        status = getattr(raw_response, "status", None)
        error = exc is not None or (isinstance(status, int) and status >= 400)
        level = "ERROR" if error else "INFO"
        data = {
            **self.identity(),
            "status": status,
            "duration_ms": round((time.monotonic() - self.started) * 1000, 2),
        }
        if exc is not None:
            # Exceptions may embed complete URLs or even credentials. Retain a
            # sanitized description; request bodies remain DEBUG-only.
            data["error"] = {"type": type(exc).__name__, "message": str(exc)}
        if self.response is not None:
            details = _response_details(self.response)
            data.update({key: val for key, val in details.items() if key != "body"})
        await self.record(level, "服务商请求失败" if error else "服务商请求完成", data)
        if self.response is not None and self.owner.logs.accepts("DEBUG"):
            await self.record(
                "DEBUG",
                "服务商响应内容",
                {
                    **self.identity(),
                    "status": status,
                    **_response_details(self.response),
                },
            )


class LoggedSession:
    """A duck-typed ClientSession proxy; request options pass through unchanged.

    INFO stores request status/timing; DEBUG additionally stores redacted JSON,
    multipart metadata, and already-read response summaries. It never reads a
    response solely for logging, nor consumes a reference-image stream.
    """

    def __init__(
        self,
        session: Any,
        logs: StructuredLogStore,
        *,
        provider_id: str = "",
        operation_id: str = "",
    ) -> None:
        self.session, self.logs = session, logs
        self.provider_id, self.operation_id = provider_id, operation_id

    def __getattr__(self, name: str) -> Any:
        return getattr(self.session, name)

    def request(self, method: str, url: Any, **kwargs):
        if not self.logs.enabled:
            return self.session.request(method, url, **kwargs)
        return _Request(self, method.upper(), url, kwargs)

    def get(self, url: Any, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url: Any, **kwargs):
        return self.request("POST", url, **kwargs)

    def put(self, url: Any, **kwargs):
        return self.request("PUT", url, **kwargs)

    def patch(self, url: Any, **kwargs):
        return self.request("PATCH", url, **kwargs)

    def delete(self, url: Any, **kwargs):
        return self.request("DELETE", url, **kwargs)

    def head(self, url: Any, **kwargs):
        kwargs.setdefault("allow_redirects", False)
        return self.request("HEAD", url, **kwargs)

    def ws_connect(self, url: Any, **kwargs):
        if not self.logs.enabled:
            return self.session.ws_connect(url, **kwargs)
        return _Request(self, "GET", url, kwargs, websocket=True)
