from __future__ import annotations

import asyncio

import aiohttp
import pytest

from astrbot_plugin_image_studio.models import (
    GeneratedImage,
    GenerationRequest,
    ImageProvider,
)
from astrbot_plugin_image_studio.providers import ProviderError, ProviderExecutor
from astrbot_plugin_image_studio.providers import _nai_query


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


@pytest.mark.parametrize("count", [1, 3, 6])
def test_nai_command_makes_one_request_per_requested_image(count):
    async def run():
        session = Session()
        executor = ProviderExecutor(session)
        images = [
            GeneratedImage(f"image-{index}".encode(), "image/png")
            for index in range(count)
        ]
        reads = []

        async def read(response, config):
            assert len(session.calls) == len(reads) + 1
            reads.append(response)
            return (images[len(reads) - 1],)

        executor._read_response_images = read
        result = await executor.generate(
            provider(),
            GenerationRequest(
                mode="text2img",
                provider_id="nai",
                prompt="landscape",
                count=count,
                negative_prompt="bad quality",
                source="command",
            ),
        )
        assert result == tuple(images)
        assert len(session.calls) == count
        assert all(
            call[1]["params"]["negative"] == "bad quality" for call in session.calls
        )

    asyncio.run(run())


def test_nai_command_stops_after_upstream_failure_without_retrying():
    async def run():
        session = Session()
        executor = ProviderExecutor(session)

        async def read(response, config):
            if len(session.calls) == 2:
                raise ProviderError("upstream failed")
            return (GeneratedImage(b"first", "image/png"),)

        executor._read_response_images = read
        with pytest.raises(ProviderError, match="已完成 1 张.*尚未保存或投递"):
            await executor.generate(
                provider(),
                GenerationRequest(
                    mode="text2img",
                    provider_id="nai",
                    prompt="landscape",
                    count=3,
                    source="command",
                ),
            )
        assert len(session.calls) == 2

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


@pytest.mark.parametrize("source", ["webui", "llm_tool"])
def test_nai_other_sources_keep_existing_single_request_behavior(source):
    async def run():
        session = Session()
        executor = ProviderExecutor(session)

        async def read(response, config):
            return (GeneratedImage(b"one", "image/png"),)

        executor._read_response_images = read
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
