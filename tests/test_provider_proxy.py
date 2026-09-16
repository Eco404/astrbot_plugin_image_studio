from __future__ import annotations

import asyncio
import base64
import io
from contextlib import AsyncExitStack, asynccontextmanager

import aiohttp
import pytest
from aiohttp import web
from PIL import Image

from astrbot_plugin_image_studio.backend.config import (
    load_studio_settings,
    normalize_webui_settings,
    runtime_settings,
    save_studio_settings,
)
from astrbot_plugin_image_studio.backend.models import (
    GenerationRequest,
    ImageProvider,
    ReferenceImage,
)
from astrbot_plugin_image_studio.backend.providers.executor import (
    ProviderError,
    ProviderExecutor,
)


def provider(kind="openai_images", **changes):
    return ImageProvider.from_mapping(
        {
            "id": "proxy-test",
            "name": "Proxy test",
            "kind": kind,
            "base_url": "http://upstream.invalid",
            "api_key": "offline-api-token",
            "model": "nai-diffusion-5-full",
            "supports_img2img": True,
            **changes,
        }
    )


def test_proxy_is_optional_and_survives_save_reload(tmp_path):
    async def run():
        assert provider().proxy == provider(proxy="  ").proxy == ""
        proxy = "http://user:p%40ss@127.0.0.1:7890"
        original = provider(proxy=f" {proxy} ")
        assert original.proxy == proxy
        await save_studio_settings(
            tmp_path,
            {
                "providers": [
                    original.public_dict(),
                    provider(id="direct").public_dict(),
                ]
            },
        )
        saved, errors = load_studio_settings(tmp_path)
        assert not errors
        settings, errors = runtime_settings({}, saved)
        assert not errors
        assert [item.proxy for item in settings.providers] == [proxy, ""]
        assert proxy not in repr(original)

    asyncio.run(run())


@pytest.mark.parametrize(
    "proxy",
    [
        "http://127.0.0.1:7890",
        "https://proxy.example:8443/",
        "http://[::1]:7890",
        "http://user:pass@proxy.example",
    ],
)
def test_supported_proxy_addresses(proxy):
    assert provider(proxy=proxy).proxy == proxy


@pytest.mark.parametrize(
    "proxy",
    [
        "127.0.0.1:7890",
        "socks5://user:secret@localhost:1080",
        "http://",
        "http://host:invalid",
        "http://host:0",
        "http://host:65536",
        "http://[::1",
        "http://proxy/path",
        "http://proxy?password=secret",
        "http://proxy#secret",
        "http://proxy\n.example",
        {"password": "secret"},
        False,
    ],
)
def test_invalid_proxy_is_rejected_without_echoing_credentials(proxy):
    with pytest.raises(ValueError, match="网络代理地址无效") as error:
        provider(proxy=proxy)
    assert "secret" not in str(error.value)
    normalized, errors = normalize_webui_settings(
        {"providers": [{"id": "bad", "name": "Bad", "proxy": proxy}]}
    )
    assert not normalized["providers"]
    assert len(errors) == 1 and "网络代理地址无效" in errors[0]
    assert "secret" not in errors[0]


def image_bytes(color):
    output = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(output, "PNG")
    return output.getvalue()


@asynccontextmanager
async def upstream_stub(kind, identity, proxied):
    """Act as a local proxy endpoint or direct origin, without Internet access."""
    traffic = []
    image = image_bytes((identity * 50, 40, 90))

    async def handle(request):
        body = await request.read()
        traffic.append((request.raw_path, dict(request.headers), body))
        if request.path == "/result.png":
            return web.Response(body=image, content_type="image/png")
        if request.path == "/models":
            return web.json_response({"data": [{"id": "test-image"}]})
        if request.path == "/api/api/getUser":
            return web.json_response(
                {"status": "ok", "data": {"enabled": True, "value": 123}}
            )
        if request.path == "/user/subscription":
            return web.json_response(
                {
                    "active": True,
                    "trainingStepsLeft": {
                        "fixedTrainingStepsLeft": 100,
                        "purchasedTrainingSteps": 23,
                    },
                }
            )
        if kind == "novelai_official":
            return web.json_response(
                {"images": [{"image": base64.b64encode(image).decode(), "seed": 42}]},
                status=201,
            )
        origin = (
            f"http://result-{identity}.invalid" if proxied else f"http://{request.host}"
        )
        return web.json_response({"data": [{"url": origin + "/result.png"}]})

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        yield f"http://127.0.0.1:{runner.addresses[0][1]}", traffic, image
    finally:
        await runner.cleanup()


OPERATIONS = (
    [
        (kind, "text2img", "json_data_url")
        for kind in (
            "openai_images",
            "gemini",
            "custom_json",
            "nai_direct",
            "novelai_official",
        )
    ]
    + [
        (kind, "img2img", "json_data_url")
        for kind in ("openai_images", "gemini", "custom_json", "novelai_official")
    ]
    + [("openai_images", "img2img", "multipart")]
    + [
        (kind, "models", "json_data_url")
        for kind in ("openai_images", "gemini", "custom_json")
    ]
    + [(kind, "quota", "json_data_url") for kind in ("nai_direct", "novelai_official")]
)


@pytest.mark.parametrize("kind,operation,request_format", OPERATIONS)
def test_all_http_paths_use_provider_proxy_without_shared_session_leakage(
    kind, operation, request_format, monkeypatch
):
    # A blank provider must stay direct even when the environment has a proxy.
    monkeypatch.setenv("HTTP_PROXY", "http://environment-proxy.invalid:7890")

    async def run():
        async with AsyncExitStack() as stack:
            session = await stack.enter_async_context(aiohttp.ClientSession())
            executor = ProviderExecutor(session)
            cases = []
            for identity in (1, 2, 3):
                proxied = identity != 3
                url, traffic, image = await stack.enter_async_context(
                    upstream_stub(kind, identity, proxied)
                )
                proxy = (
                    url.replace("http://", "http://user:password@") if proxied else ""
                )
                configured = provider(
                    kind,
                    id=f"p{identity}",
                    proxy=proxy,
                    base_url=f"http://upstream-{identity}.invalid" if proxied else url,
                    models_path="/models",
                    edit_request_format=request_format,
                )
                cases.append((configured, traffic, image))

            async def execute(configured):
                if operation == "models":
                    return await executor.discover_models(configured)
                if operation == "quota":
                    return await executor.fetch_quota(configured)
                request = GenerationRequest(
                    mode=operation,
                    provider_id=configured.id,
                    model=configured.model,
                    prompt="a tree",
                    size="64x64",
                    references=(
                        ReferenceImage(
                            "ref", "ref.png", image_bytes("blue"), "image/png"
                        ),
                    )
                    if operation == "img2img"
                    else (),
                )
                return await executor.generate(configured, request)

            results = await asyncio.gather(*(execute(item[0]) for item in cases))
            for (configured, traffic, image), result in zip(cases, results):
                if operation == "quota":
                    assert result["remaining"] == 123
                elif operation == "models":
                    assert result[0]["id"] == "test-image"
                else:
                    assert result[0].data == image
                download = (
                    operation in {"text2img", "img2img"} and kind != "novelai_official"
                )
                assert len(traffic) == (2 if download else 1)
                for path, headers, body in traffic:
                    assert path.startswith("http://") is bool(configured.proxy)
                    assert ("Proxy-Authorization" in headers) is bool(configured.proxy)
                    assert b"password" not in body and b"proxy" not in body
                    if configured.proxy:
                        assert (
                            headers["Proxy-Authorization"]
                            == "Basic dXNlcjpwYXNzd29yZA=="
                        )

    asyncio.run(run())


def test_unreachable_proxy_never_falls_back_to_direct():
    async def run():
        async with upstream_stub("nai_direct", 1, False) as (url, traffic, _):
            async with aiohttp.ClientSession() as session:
                # Use a port just released by the OS, rather than a live service.
                server = await asyncio.start_server(lambda *_: None, "127.0.0.1", 0)
                port = server.sockets[0].getsockname()[1]
                server.close()
                await server.wait_closed()
                configured = provider(
                    "nai_direct", base_url=url, proxy=f"http://127.0.0.1:{port}"
                )
                with pytest.raises(ProviderError, match="无法连接服务商"):
                    await ProviderExecutor(session).fetch_quota(configured)
                assert traffic == []

    asyncio.run(run())


def test_settings_and_draft_endpoints_reject_bad_proxy_without_saving(tmp_path):
    import httpx

    from astrbot_plugin_image_studio.tests.webui_harness import create_app

    async def run():
        app = await create_app(tmp_path)
        prefix = "/astrbot_plugin_image_studio"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            original = (await client.get(prefix + "/settings/get")).json()
            draft = original["webui"]
            draft["providers"][0]["proxy"] = "socks5://user:secret@localhost:1080"
            responses = [
                await client.post(
                    prefix + "/settings/save",
                    json={
                        "base": original["base"],
                        "studio": draft,
                        "settings_revision": draft["revision"],
                    },
                )
            ]
            for endpoint in ("provider/models", "model/test"):
                responses.append(
                    await client.post(
                        prefix + "/" + endpoint,
                        json={
                            "provider": draft["providers"][0],
                            "model_id": "studio-image",
                        },
                    )
                )
            for response in responses:
                assert response.status_code == 400
                assert "网络代理地址无效" in response.text
                assert "secret" not in response.text
            saved = (await client.get(prefix + "/settings/get")).json()
            assert saved["webui"]["providers"][0]["proxy"] == ""

    asyncio.run(run())
