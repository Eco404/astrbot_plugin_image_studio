"""Request outcome evidence survives adapter failures and split batches."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from astrbot_plugin_image_studio.backend.providers.errors import (
    ProviderBatchError,
    ProviderError,
    ProviderPartialResponseError,
)
from astrbot_plugin_image_studio.backend.providers.executor import ProviderExecutor
from astrbot_plugin_image_studio.backend.providers.http import ImageResponseReader
from astrbot_plugin_image_studio.tests.backend.providers.test_nai_batches import (
    generate,
    image,
    provider,
    service,
)
from astrbot_plugin_image_studio.tests.backend.providers.test_novelai_provider import (
    Response as NovelAIResponse,
    Session,
    provider as novelai_provider,
    request,
)


class Response:
    def __init__(self, status=200, raw=b"{}"):
        self.status = status
        self.raw = raw
        self.headers = {"Content-Type": "application/json"}

    async def read(self):
        return self.raw

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


@pytest.mark.parametrize(
    "status,expected",
    [
        (400, "rejected"),
        (401, "rejected"),
        (403, "rejected"),
        (404, "rejected"),
        (408, "unknown"),
        (422, "rejected"),
        (429, "rejected"),
        (500, "unknown"),
        (502, "unknown"),
        (503, "unknown"),
    ],
)
def test_generation_http_status_is_evidence_not_error_text(status, expected):
    response = Response(status, raw=b"a timeout-like error message")
    with pytest.raises(ProviderError) as error:
        asyncio.run(ImageResponseReader(Session()).read_images(response, provider()))
    assert error.value.request_status == expected
    assert f"HTTP {status}" in str(error.value)


@pytest.mark.parametrize("raw", [b"invalid JSON", b"{}", b'{"data":[]}'])
def test_unusable_success_response_does_not_claim_request_was_rejected(raw):
    with pytest.raises(ProviderError) as error:
        asyncio.run(
            ImageResponseReader(Session()).read_images(Response(raw=raw), provider())
        )
    assert error.value.request_status == "unknown"


def test_result_download_rejection_is_not_generation_rejection():
    session = Session(response=Response(404))
    with pytest.raises(ProviderError, match="结果图下载失败") as error:
        asyncio.run(
            ImageResponseReader(session).download_image(
                "https://result.invalid/image", provider()
            )
        )
    assert error.value.request_status == "unknown"
    assert len(session.calls) == 1


@pytest.mark.parametrize("kind", ["custom_json", "nai_direct", "novelai_official"])
def test_adapter_validation_is_known_unsubmitted(kind):
    session = Session()
    configured = provider(kind=kind)
    submitted = request(model="paint")
    if kind == "custom_json":
        configured = replace(configured, request_template="{not valid json")
    elif kind == "nai_direct":
        submitted = replace(submitted, mode="img2img")
    else:
        configured = novelai_provider()
        submitted = request(parameters={"seed": True})
    with pytest.raises(ProviderError) as error:
        asyncio.run(ProviderExecutor(session).generate(configured, submitted))
    assert error.value.request_status == "not_submitted"
    assert session.calls == []


@pytest.mark.parametrize(
    "status,expected",
    [(400, "rejected"), (429, "rejected"), (408, "unknown"), (503, "unknown")],
)
def test_novelai_generation_http_outcome_matches_generic_reader(status, expected):
    session = Session(response=NovelAIResponse(status=status))
    with pytest.raises(ProviderError) as error:
        asyncio.run(ProviderExecutor(session).generate(novelai_provider(), request()))
    assert error.value.request_status == expected
    assert len(session.calls) == 1


def test_novelai_response_decode_failure_is_not_preflight_validation():
    session = Session(response=NovelAIResponse(raw=b"broken response"))
    with pytest.raises(ProviderError) as error:
        asyncio.run(ProviderExecutor(session).generate(novelai_provider(), request()))
    assert error.value.request_status == "unknown"
    assert len(session.calls) == 1


class FailingExecutor:
    def __init__(self, failures):
        self.failures = iter(failures)
        self.calls = 0

    async def generate(self, configured, submitted):
        self.calls += 1
        failure = next(self.failures)
        if failure is not None:
            raise failure
        return (image(self.calls),)


@pytest.mark.parametrize(
    "states,expected",
    [
        (["not_submitted"], "not_submitted"),
        (["not_submitted", "not_submitted"], "not_submitted"),
        (["rejected"], "rejected"),
        (["not_submitted", "rejected"], "rejected"),
        (["rejected", "unknown"], "unknown"),
        (["not_submitted", "unknown"], "unknown"),
    ],
)
def test_batch_combines_all_failed_request_outcomes(states, expected):
    executor = FailingExecutor(
        [ProviderError("same message", request_status=state) for state in states]
    )
    subject = service(executor=executor)
    with pytest.raises(ProviderBatchError) as error:
        asyncio.run(generate(subject, count=len(states)))
    assert error.value.request_status == expected
    assert error.value.failure_states == tuple(enumerate(states, 1))
    assert not error.value.images
    assert executor.calls == len(states)
    assert subject.store.history == []
    message = str(error.value)
    assert f"计划 {len(states)} 次请求" in message
    assert "发送" not in message
    preflight_count = states.count("not_submitted")
    if preflight_count:
        assert f"其中 {preflight_count} 次在提交前失败" in message
    if expected == "not_submitted":
        assert "本批没有提交生图请求" in message
        assert "修正输入后再提交" in message
        assert "消耗额度" not in message
        assert "请勿自动重试" not in message
    else:
        assert "请勿自动重试整个批次" in message


@pytest.mark.parametrize(
    "failure", [ValueError("bad result"), TimeoutError(), ProviderError("unspecified")]
)
def test_execution_failure_without_evidence_is_unknown(failure):
    executor = FailingExecutor([failure])
    with pytest.raises(ProviderBatchError) as error:
        asyncio.run(generate(service(executor=executor)))
    assert error.value.request_status == "unknown"
    assert executor.calls == 1


def test_partial_batch_keeps_images_when_other_response_raises_value_error():
    subject = service(
        executor=FailingExecutor([None, ValueError("bad response field")])
    )
    result = asyncio.run(generate(subject, count=2))
    assert len(result.images) == 1
    assert result.partial
    assert "bad response field" in result.warning
    assert len(subject.store.history) == 1


def test_run_provider_value_error_cannot_escape_as_preflight_validation():
    subject = service()

    async def fail(*_args):
        raise ValueError("decode error")

    subject.run_provider_request = fail
    with pytest.raises(ProviderError, match="decode error") as error:
        asyncio.run(generate(subject))
    assert error.value.request_status == "unknown"


def test_actual_preflight_validation_still_raises_before_execution():
    executor = FailingExecutor([])
    subject = service(executor=executor)
    with pytest.raises(ValueError):
        asyncio.run(generate(subject, count=-1))
    assert executor.calls == 0


def test_legacy_and_partial_failures_default_to_unknown():
    assert ProviderError("legacy").request_status == "unknown"
    assert (
        ProviderPartialResponseError((image(1),), ((2, "broken"),)).request_status
        == "unknown"
    )
    assert ProviderBatchError(1, (), ((1, "broken"),)).request_status == "unknown"
