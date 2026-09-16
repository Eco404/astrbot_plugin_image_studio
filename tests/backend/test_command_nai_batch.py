from __future__ import annotations

import asyncio

import aiohttp
import pytest

from astrbot_plugin_image_studio.backend.models import (
    GeneratedImage,
    GenerationRequest,
    ImageProvider,
)
from astrbot_plugin_image_studio.backend.providers.executor import (
    ProviderError,
    ProviderExecutor,
)
from astrbot_plugin_image_studio.backend.providers.executor import _nai_query


class Response:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class Session:
    def __init__(self):
        self.calls = []

    def get(self, endpoint, **kwargs):
        self.calls.append((endpoint, kwargs))
        return Response()


def provider():
    return ImageProvider.from_mapping(
        {
            "id": "nai",
            "kind": "nai_direct",
            "api_key": "test-token",
            "model": "test-model",
        }
    )


def test_nai_command_sends_explicit_empty_negative_without_restoring_upstream_default():
    query = _nai_query(
        provider(),
        GenerationRequest(
            mode="text2img",
            provider_id="nai",
            prompt="landscape",
            negative_prompt="",
            source="command",
        ),
    )
    assert "negative" in query
    assert query["negative"] == ""


@pytest.mark.parametrize("source", ["command", "webui", "llm_tool"])
@pytest.mark.parametrize("count", [1, 3, 6])
def test_nai_executor_always_performs_one_http_request(source, count):
    async def run():
        session = Session()
        executor = ProviderExecutor(session)
        image = GeneratedImage(b"one-image", "image/png")

        async def read(response, config):
            return (image,)

        executor._read_response_images = read
        result = await executor.generate(
            provider(),
            GenerationRequest(
                mode="text2img",
                provider_id="nai",
                prompt="landscape",
                count=count,
                negative_prompt="bad quality",
                parameters={
                    "count": count,
                    "n": count,
                    "concurrency": 3,
                    "batch_mode": "split",
                    "native_batch_size": 4,
                    "max_concurrent_requests": 2,
                },
                source=source,
            ),
        )
        assert result == (image,)
        assert len(session.calls) == 1
        query = session.calls[0][1]["params"]
        assert query["negative"] == "bad quality"
        assert not {
            "count",
            "n",
            "concurrency",
            "batch_mode",
            "native_batch_size",
            "max_concurrent_requests",
        }.intersection(query)

    asyncio.run(run())


@pytest.mark.parametrize("source", ["command", "webui", "llm_tool"])
def test_nai_executor_does_not_retry_upstream_failure(source):
    async def run():
        session = Session()
        executor = ProviderExecutor(session)

        async def read(response, config):
            raise ProviderError("upstream failed")

        executor._read_response_images = read
        with pytest.raises(ProviderError, match="upstream failed"):
            await executor.generate(
                provider(),
                GenerationRequest(
                    mode="text2img",
                    provider_id="nai",
                    prompt="landscape",
                    count=3,
                    source=source,
                ),
            )
        assert len(session.calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    "failure",
    [TimeoutError("private detail"), aiohttp.ClientConnectionError("private detail")],
)
def test_nai_command_network_failures_return_sanitized_provider_errors(failure):
    async def run():
        session = Session()
        executor = ProviderExecutor(session)

        async def read(response, config):
            raise failure

        executor._read_response_images = read
        with pytest.raises(ProviderError, match="NAI 生图失败") as raised:
            await executor.generate(
                provider(),
                GenerationRequest(
                    mode="text2img",
                    provider_id="nai",
                    prompt="landscape",
                    source="command",
                ),
            )
        assert "private detail" not in str(raised.value)
        assert len(session.calls) == 1

    asyncio.run(run())
