from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace

import aiohttp
import httpx
import pytest

from astrbot_plugin_image_studio.backend.models import ImageProvider
from astrbot_plugin_image_studio.backend.providers.executor import (
    ProviderError,
    ProviderExecutor,
)
from astrbot_plugin_image_studio.tests.support.webui_harness import create_app

PREFIX = "/astrbot_plugin_image_studio/studio/provider-quota"
SECRET = "server-only-quota-token"


def provider(**changes):
    return ImageProvider.from_mapping(
        {
            "id": "nai",
            "name": "NAI",
            "kind": "nai_direct",
            "base_url": "https://quota.example.test/base/",
            "api_key": SECRET,
            "custom_headers": '{"X-Account": "saved", "content-type": "text/plain"}',
            **changes,
        }
    )


class Response:
    def __init__(self, payload=None, status=200, error=None):
        self.payload = payload
        self.status = status
        self.error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def json(self):
        if self.error:
            raise self.error
        return self.payload


class Session:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response


@pytest.mark.parametrize("value", [0, 128, " 128 "])
@pytest.mark.parametrize("enabled", [True, False])
def test_fetch_quota_uses_nai_account_protocol(value, enabled):
    async def run():
        session = Session(
            Response(
                {
                    "status": "ok",
                    "data": {"value": value, "enabled": enabled, "balance": 42},
                }
            )
        )
        started = time.time()
        result = await ProviderExecutor(session).fetch_quota(provider())
        assert result["remaining"] == int(value)
        assert result["enabled"] is enabled
        assert started <= result["checked_at"] <= time.time()
        assert set(result) == {"remaining", "enabled", "checked_at"}
        [(url, options)] = session.calls
        assert url == "https://quota.example.test/base/api/api/getUser"
        assert options["json"] == {"toUserId": SECRET}
        assert options["headers"] == {
            "X-Account": "saved",
            "Content-Type": "application/json",
        }
        assert options["timeout"].total == 15
        assert options["allow_redirects"] is False
        assert SECRET not in json.dumps(result)

    asyncio.run(run())


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"status": "ok", "data": None},
        {"status": "ok", "data": []},
        {"status": "ok", "data": {"enabled": True}},
        {"status": "ok", "data": {"value": None, "enabled": True}},
        {"status": "ok", "data": {"value": True, "enabled": True}},
        {"status": "ok", "data": {"value": -1, "enabled": True}},
        {"status": "ok", "data": {"value": 1.5, "enabled": True}},
        {"status": "ok", "data": {"value": "", "enabled": True}},
        {"status": "ok", "data": {"value": "unlimited", "enabled": True}},
        {"status": "ok", "data": {"value": 1}},
        {"status": "ok", "data": {"value": 1, "enabled": "false"}},
        {"status": "ok", "data": {"value": 1, "enabled": 1}},
    ],
)
def test_fetch_quota_rejects_missing_or_invalid_account_fields(payload):
    with pytest.raises(ProviderError, match="额度查询失败"):
        asyncio.run(
            ProviderExecutor(Session(Response(payload))).fetch_quota(provider())
        )


@pytest.mark.parametrize("status", [301, 401, 404, 429, 500])
def test_fetch_quota_does_not_echo_upstream_http_error_body(status):
    session = Session(Response({"message": SECRET}, status=status))
    with pytest.raises(ProviderError) as failure:
        asyncio.run(ProviderExecutor(session).fetch_quota(provider()))
    assert str(failure.value) == f"额度查询失败：上游返回 HTTP {status}"


@pytest.mark.parametrize(
    "response,error,expected",
    [
        (
            Response({"status": "error", "message": SECRET + " user not found"}),
            None,
            "上游未确认账户",
        ),
        (Response(error=json.JSONDecodeError(SECRET, SECRET, 0)), None, "响应格式无效"),
        (None, TimeoutError(SECRET), "查询超时"),
        (None, aiohttp.ClientConnectionError(SECRET), "无法连接服务商"),
        (None, aiohttp.InvalidURL(SECRET), "响应格式无效"),
    ],
)
def test_fetch_quota_errors_are_sanitized(response, error, expected):
    with pytest.raises(ProviderError, match=expected) as failure:
        asyncio.run(ProviderExecutor(Session(response, error)).fetch_quota(provider()))
    assert SECRET not in str(failure.value)
    assert "https://" not in str(failure.value)


@pytest.mark.parametrize(
    "changes", [{"kind": "openai_images"}, {"enabled": False}, {"api_key": "  "}]
)
def test_executor_rejects_unsupported_or_incomplete_provider(changes):
    session = Session()
    with pytest.raises(ProviderError):
        asyncio.run(ProviderExecutor(session).fetch_quota(provider(**changes)))
    assert session.calls == []


def test_quota_api_uses_saved_provider_and_never_generates(tmp_path):
    async def run():
        app = await create_app(tmp_path, seed=False)
        plugin = app.state.plugin
        session = Session(
            Response({"status": "ok", "data": {"value": 62, "enabled": False}})
        )
        plugin._settings = replace(plugin._settings, providers=(provider(),))
        plugin._service.executor = ProviderExecutor(session)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            result = await client.get(
                PREFIX,
                params={
                    "provider_id": "nai",
                    "api_key": "untrusted-browser-token",
                    "base_url": "https://untrusted.example/",
                    "refresh": "1",
                },
            )
            assert result.status_code == 200
            assert set(result.json()) == {
                "provider_id",
                "remaining",
                "enabled",
                "checked_at",
            }
            assert result.json()["remaining"] == 62
            assert result.json()["enabled"] is False
            assert result.json()["provider_id"] == "nai"
            assert result.headers["cache-control"] == "no-store"
            assert SECRET not in result.text
            assert (
                session.calls[0][0] == "https://quota.example.test/base/api/api/getUser"
            )
            assert session.calls[0][1]["json"] == {"toUserId": SECRET}
            again = await client.get(PREFIX, params={"provider_id": "nai"})
            assert again.status_code == 200 and len(session.calls) == 2
            records = await client.get("/astrbot_plugin_image_studio/gallery/list")
            assert records.json()["total"] == 0
            assert all(
                url.endswith("/api/api/getUser") for url, _options in session.calls
            )

    asyncio.run(run())


@pytest.mark.parametrize(
    "provider_id,changes,status,expected",
    [
        ("", {}, 400, "请选择"),
        ("missing", {}, 404, "不存在"),
        ("nai", {"enabled": False}, 404, "未启用"),
        ("nai", {"kind": "gemini"}, 400, "不支持"),
        ("nai", {"api_key": ""}, 400, "未配置密钥"),
    ],
)
def test_quota_api_validates_saved_provider_before_network(
    tmp_path, provider_id, changes, status, expected
):
    async def run():
        app = await create_app(tmp_path, seed=False)
        plugin = app.state.plugin
        session = Session()
        plugin._settings = replace(plugin._settings, providers=(provider(**changes),))
        plugin._service.executor = ProviderExecutor(session)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            result = await client.get(PREFIX, params={"provider_id": provider_id})
            assert result.status_code == status and expected in result.json()["message"]
            assert session.calls == []

    asyncio.run(run())


def test_quota_api_reports_sanitized_failure_or_initialization(tmp_path):
    async def run():
        app = await create_app(tmp_path, seed=False)
        plugin = app.state.plugin
        plugin._settings = replace(plugin._settings, providers=(provider(),))
        plugin._service.executor = ProviderExecutor(Session(error=TimeoutError(SECRET)))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            result = await client.get(PREFIX, params={"provider_id": "nai"})
            assert result.status_code == 502 and "超时" in result.json()["message"]
            assert SECRET not in result.text
            plugin._service = None
            result = await client.get(PREFIX, params={"provider_id": "nai"})
            assert result.status_code == 503 and "初始化" in result.json()["message"]

    asyncio.run(run())
