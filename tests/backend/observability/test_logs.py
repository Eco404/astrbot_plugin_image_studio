from __future__ import annotations

import asyncio
import io
import json
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import aiohttp
from aiohttp import web

from astrbot_plugin_image_studio.backend.api import logs as logs_api
from astrbot_plugin_image_studio.backend.database.maintenance import directory_space
from astrbot_plugin_image_studio.backend.observability import (
    LoggedSession,
    StructuredLogStore,
    log_context,
    sanitize,
)
from astrbot_plugin_image_studio.backend.observability import store as log_store


def test_redaction_binaries_depth_size_and_unread_streams():
    stream = io.BytesIO(b"image bytes")
    loop = {}
    loop["self"] = loop
    data = sanitize(
        {
            "headers": {"Authorization": "Bearer secret-one", "Cookie": "secret-two"},
            "nested": {"api_key": "secret-three", "accessToken": "secret-four"},
            "encoded": '{"api_key": "secret-eight", "authorization": "Bearer secret-nine"}',
            "error": "bad credential Bearer secret-ten",
            "url": "https://user:secret-five@example.test/api?key=secret-six&size=1K#secret-seven",
            "image": "data:image/png;base64," + "AbCd" * 1024,
            "b64_json": "abcd" * 1024,
            "raw": b"secret-image",
            "stream": stream,
            "loop": loop,
            "huge": ["long text " * 10000] * 1000,
        }
    )
    encoded = json.dumps(data)
    assert all(
        secret not in encoded
        for secret in (
            "secret-one",
            "secret-two",
            "secret-three",
            "secret-four",
            "secret-five",
            "secret-six",
            "secret-seven",
            "secret-eight",
            "secret-nine",
            "secret-ten",
            "secret-image",
        )
    )
    assert "size=1K" in encoded
    assert "AbCdAbCd" not in encoded and "abcdabcd" not in encoded
    assert len(encoded) < 150000
    assert stream.tell() == 0
    assert data["raw"] == {"kind": "binary", "bytes": 12}


def test_filter_pagination_correlation_and_disable(tmp_path):
    async def run():
        logs = StructuredLogStore(tmp_path)
        await logs.record("DEBUG", "request", "not recorded")
        with log_context(operation_id="op-1", provider_id="provider-1"):
            with log_context(operation_id="inner-generation"):
                await logs.record(
                    "INFO", "request", "request", {"prompt": "cat"}, request_id="req-1"
                )
        await logs.record("WARNING", "system", "warning")
        await logs.record("ERROR", "generation", "failed")
        found = await logs.query(page=2, page_size=2)
        assert found["total"] == 3
        assert [item["message"] for item in found["items"]] == ["request"]
        assert found["items"][0]["operation_id"] == "op-1"
        assert found["items"][0]["request_id"] == "req-1"
        assert found["items"][0]["context"]["provider_id"] == "provider-1"
        assert (await logs.query(category="system", level="WARNING"))["total"] == 1
        assert (await logs.query(date_to="2000-01-01"))["total"] == 0
        logs.configure(enabled=False, level="DEBUG", retention_days=0)
        await logs.record("ERROR", "system", "disabled")
        assert (await logs.query())["total"] == 3
        assert (await logs.query())["enabled"] is False
        logs.configure(enabled=True, level="ERROR", retention_days=0)
        await logs.record("WARNING", "system", "excluded")
        await logs.record("ERROR", "system", "included")
        assert (await logs.query())["total"] == 4
        usage = await logs.usage()
        health = directory_space(tmp_path)
        assert health["categories"]["logs"]["file_count"] == usage["file_count"] == 1
        assert health["categories"]["logs"]["file_bytes"] == usage["file_bytes"]

    asyncio.run(run())


def test_concurrent_append_clear_and_bounded_corruption(tmp_path):
    async def run():
        logs = StructuredLogStore(tmp_path)
        await asyncio.gather(
            *(logs.record("INFO", "operation", f"record-{i}") for i in range(100))
        )
        assert (await logs.query())["total"] == 100
        file = next(logs.directory.glob("*.log"))
        with file.open("ab") as stream:
            stream.write(b"not-json\n" + b"x" * (1024 * 1024) + b"\n")
        await logs.record("INFO", "system", "after corruption")
        query = await logs.query(page_size=200)
        assert query["total"] == 101
        assert query["items"][0]["message"] == "after corruption"
        assert len({entry["id"] for entry in query["items"]}) == 101
        await asyncio.gather(logs.clear(), logs.record("INFO", "system", "concurrent"))
        assert (await logs.query())["total"] in (0, 1)
        await logs.clear()
        assert (await logs.query())["total"] == 0
        assert not list(logs.directory.glob("*.log"))

    asyncio.run(run())


def test_retention_and_clear_only_touch_owned_regular_logs(tmp_path):
    async def run():
        logs = StructuredLogStore(tmp_path, retention_days=0)
        logs.directory.mkdir()
        today = datetime.now(timezone.utc).date()
        recent = logs.directory / ((today - timedelta(days=6)).isoformat() + ".log")
        old = logs.directory / ((today - timedelta(days=7)).isoformat() + ".log")
        recent.write_text("{}\n")
        old.write_text("{}\n")
        unrelated = logs.directory / "user_notes.log"
        unrelated.write_text("keep")
        target = tmp_path / "unrelated.txt"
        target.write_text("must stay")
        link = logs.directory / "2000-01-01.log"
        link.symlink_to(target)
        assert (await logs.cleanup())["files_removed"] == 0
        logs.configure(enabled=True, level="INFO", retention_days=10**100)
        assert (await logs.cleanup())["files_removed"] == 0
        logs.configure(enabled=False, level="INFO", retention_days=7)
        assert (await logs.cleanup())["files_removed"] == 1
        assert recent.exists() and not old.exists()
        assert (await logs.clear())["files_removed"] == 1
        assert unrelated.read_text() == "keep" and target.read_text() == "must stay"
        assert link.is_symlink()

    asyncio.run(run())


def test_query_snapshot_does_not_block_new_generation_logs(tmp_path, monkeypatch):
    async def run():
        logs = StructuredLogStore(tmp_path)
        await logs.record("INFO", "system", "before query")
        started, release = threading.Event(), threading.Event()
        original = log_store._reverse_lines

        def paused(*args):
            started.set()
            assert release.wait(3)
            yield from original(*args)

        monkeypatch.setattr(log_store, "_reverse_lines", paused)
        query = asyncio.create_task(logs.query())
        try:
            assert await asyncio.to_thread(started.wait, 3)
            await asyncio.wait_for(logs.record("INFO", "generation", "new request"), 1)
        finally:
            release.set()
        found = await query
        assert [item["message"] for item in found["items"]] == ["before query"]
        assert (await logs.query())["total"] == 2

    asyncio.run(run())


def test_write_failure_does_not_fail_operation(tmp_path, monkeypatch):
    async def run():
        logs = StructuredLogStore(tmp_path)

        def fail(*args):
            raise OSError("disk full with confidential details")

        monkeypatch.setattr(logs, "_append", fail)
        await logs.record("ERROR", "system", "important event")

    asyncio.run(run())


def test_api_validates_filters_and_requires_literal_confirmation(tmp_path, monkeypatch):
    async def run():
        logs = StructuredLogStore(tmp_path)
        api = logs_api.LogsAPI(get_logs=lambda: logs)
        await logs.record("INFO", "system", "keep unless confirmed")
        for body in ({}, {"confirm": "true"}, {"confirm": 1}, []):

            async def read(default=None):
                return body

            monkeypatch.setattr(logs_api, "web_request", SimpleNamespace(json=read))
            assert (await api._api_logs_clear()).status_code == 400
        monkeypatch.setattr(
            logs_api, "web_request", SimpleNamespace(query={"level": "bogus"})
        )
        assert (await api._api_logs_query()).status_code == 400
        assert (await logs.query())["total"] == 1

        async def confirm(default=None):
            return {"confirm": True}

        monkeypatch.setattr(
            logs_api, "web_request", SimpleNamespace(json=confirm, query={})
        )
        assert (await api._api_logs_clear()).status_code == 200
        response = await api._api_logs_query()
        assert json.loads(response.body)["total"] == 0

    asyncio.run(run())


def test_http_preserves_json_upload_stream_proxy_and_redacts(tmp_path):
    async def run():
        received = []

        async def handler(request):
            if request.content_type.startswith("multipart"):
                fields = await request.post()
                received.append(fields["image"].file.read())
                return web.json_response({"ok": True})
            received.append(await request.json())
            return web.json_response(
                {"data": [{"b64_json": "abcd" * 1024}], "token": "hidden-response"}
            )

        app = web.Application()
        app.router.add_post("/generate", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        logs = StructuredLogStore(tmp_path, level="DEBUG")
        url = f"http://127.0.0.1:{runner.addresses[0][1]}"
        try:
            async with aiohttp.ClientSession(
                headers={
                    "Authorization": "Bearer top-secret",
                    "X-Auth": "opaque-secret",
                }
            ) as raw:
                session = LoggedSession(raw, logs, provider_id="test")
                with log_context(operation_id="test-generation"):
                    async with session.post(
                        url + "/generate",
                        json={
                            "prompt": "cat",
                            "image": "data:image/png;base64," + "abcd" * 1024,
                        },
                        params=[("api_key", "query-secret")],
                        proxy=None,
                    ) as response:
                        assert response.status == 200
                        assert (await response.json())["token"] == "hidden-response"
                    form = aiohttp.FormData()
                    form.add_field(
                        "image",
                        io.BytesIO(b"original image bytes"),
                        filename="source.png",
                        content_type="image/png",
                    )
                    async with session.post(url + "/generate", data=form) as response:
                        assert await response.json() == {"ok": True}
                    # Also cover awaiting the context manager directly.
                    response = await session.post(
                        url + "/generate", json={"prompt": "awaited"}
                    )
                    assert (await response.json())["token"] == "hidden-response"
                    response.release()
                assert received[0]["prompt"] == "cat"
                assert received[1] == b"original image bytes"
                assert received[2]["prompt"] == "awaited"
                entries = (await logs.query(page_size=100))["items"]
                assert len(entries) == 9
                assert all(
                    entry["operation_id"] == "test-generation" for entry in entries
                )
                encoded = json.dumps(entries)
                for secret in (
                    "top-secret",
                    "opaque-secret",
                    "query-secret",
                    "hidden-response",
                    "abcdabcd",
                ):
                    assert secret not in encoded
                assert '"format": "multipart"' in encoded
                assert '"kind": "stream"' in encoded
                assert len({entry["request_id"] for entry in entries}) == 3
                assert all(entry["data"]["provider_id"] == "test" for entry in entries)
        finally:
            await runner.cleanup()

    asyncio.run(run())


def test_http_info_omits_body_and_keeps_failures(tmp_path):
    async def run():
        async def handler(request):
            return web.json_response({"error": "upstream error"}, status=429)

        app = web.Application()
        app.router.add_get("/error", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        logs = StructuredLogStore(tmp_path)
        url = f"http://127.0.0.1:{runner.addresses[0][1]}/error"
        try:
            async with aiohttp.ClientSession() as raw:
                session = LoggedSession(raw, logs)
                async with session.get(url) as response:
                    assert response.status == 429
                    await response.read()
                entries = (await logs.query())["items"]
                assert len(entries) == 1
                assert entries[0]["level"] == "ERROR"
                assert entries[0]["data"]["status"] == 429
                assert "body" not in entries[0]["data"]
                assert "upstream error" not in json.dumps(entries)
                logs.configure(enabled=False, level="DEBUG", retention_days=7)
                async with session.get(url) as response:
                    await response.read()
                assert (await logs.query())["total"] == 1
        finally:
            await runner.cleanup()

    asyncio.run(run())
