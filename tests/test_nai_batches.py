from __future__ import annotations

import asyncio
import io
from collections import defaultdict
from dataclasses import replace

import pytest
from PIL import Image

from astrbot_plugin_image_studio.config import (
    HistorySettings,
    RuntimeSettings,
)
from astrbot_plugin_image_studio.models import (
    GeneratedImage,
    ImageProvider,
)
from astrbot_plugin_image_studio.providers import ProviderError
from astrbot_plugin_image_studio.service import ImageGenerationService


def image(index):
    output = io.BytesIO()
    Image.new("RGB", (8, 8), (index % 256, 40, 80)).save(output, format="PNG")
    return GeneratedImage(output.getvalue(), "image/png")


def provider(
    provider_id="nai",
    *,
    limit=4,
    parameters=None,
    tool=None,
    kind="nai_direct",
    max_concurrent_requests=8,
):
    model = {"id": "paint", "max_concurrent_requests": max_concurrent_requests}
    if parameters is not None:
        model["parameters"] = parameters
    if tool is not None:
        model["tool"] = tool
    return ImageProvider.from_mapping(
        {
            "id": provider_id,
            "kind": kind,
            "max_concurrent_generations": limit,
            "models": [model],
        }
    )


def batch_parameters(*, count=1):
    return {
        "count": {"type": "integer", "default": count, "min": 1, "max": 16},
    }


class MemoryStore:
    def __init__(self):
        self.history = []
        self.discarded = []

    async def record_success(self, **kwargs):
        self.history.append(kwargs)
        return "saved-batch"

    async def discard_staged_references(self, references):
        self.discarded.append(references)


class Executor:
    def __init__(self, *, blocked=False, failures=()):
        self.calls = []
        self.failures = set(failures)
        self.active = defaultdict(int)
        self.peak = defaultdict(int)
        self.batch_active = defaultdict(int)
        self.batch_peak = defaultdict(int)
        self.starts = asyncio.Queue()
        self.blocked = blocked
        self.gates = []
        self.completed = []

    async def generate(self, configured, request):
        index = len(self.calls) + 1
        self.calls.append((configured, request))
        self.active[configured.id] += 1
        self.peak[configured.id] = max(
            self.peak[configured.id], self.active[configured.id]
        )
        batch = (configured.id, request.prompt)
        self.batch_active[batch] += 1
        self.batch_peak[batch] = max(self.batch_peak[batch], self.batch_active[batch])
        gate = asyncio.Event()
        self.gates.append(gate)
        self.starts.put_nowait(index)
        try:
            if self.blocked:
                await gate.wait()
            await asyncio.sleep(0)
            if index in self.failures:
                raise ProviderError(f"upstream failure {index}")
            self.completed.append(index)
            return (image(index),)
        finally:
            self.active[configured.id] -= 1
            self.batch_active[batch] -= 1

    async def wait_started(self, count):
        return [await asyncio.wait_for(self.starts.get(), 2) for _ in range(count)]

    def release_all(self):
        self.blocked = False
        for gate in self.gates:
            gate.set()


def service(configured=None, *, executor=None, store=None):
    providers = tuple(configured or [provider()])
    default = f"{providers[0].id}:paint"
    settings = RuntimeSettings(
        enable_llm_tool=True,
        providers=providers,
        history=HistorySettings(True, 100, 0, True),
        revision=0,
        default_page_text2img_model_ref=default,
        default_tool_text2img_model_ref=default,
    )
    return ImageGenerationService(
        settings=settings,
        executor=executor or Executor(),
        store=store or MemoryStore(),
    )


async def generate(subject, *, source="webui", **options):
    return await subject.generate(
        mode="text2img", provider_id="", prompt="landscape", source=source, **options
    )


@pytest.mark.parametrize("source", ["webui", "command", "llm_tool"])
def test_nai_default_is_one_image_with_fixed_native_capacity(source):
    async def run():
        subject = service()
        result = await generate(subject, source=source)
        assert result.request.count == len(result.images) == 1
        assert result.request.native_batch_size == 1
        assert result.request.max_concurrent_requests == 8
        assert "concurrency" not in result.request.parameters
        assert len(subject.executor.calls) == len(subject.store.history) == 1
        assert not result.warning

    asyncio.run(run())


@pytest.mark.parametrize("source", ["webui", "command", "llm_tool"])
def test_nai_model_quantity_and_shared_concurrency_defaults_apply(source):
    async def run():
        subject = service(
            [provider(parameters=batch_parameters(count=6), max_concurrent_requests=2)]
        )
        result = await generate(subject, source=source)
        assert result.request.count == len(result.images) == 6
        assert result.request.max_concurrent_requests == 2
        assert len(subject.executor.calls) == 6
        assert all(item.count == 1 for _, item in subject.executor.calls)
        assert subject.executor.peak["nai"] == 2
        assert len(subject.store.history) == 1
        assert not result.warning

    asyncio.run(run())


@pytest.mark.parametrize("exposed", [True, False])
@pytest.mark.parametrize("default_key", ["default", "default_override"])
def test_nai_tool_quantity_defaults_keep_legacy_policy_values(exposed, default_key):
    async def run():
        subject = service(
            [
                provider(
                    parameters=batch_parameters(count=1),
                    tool={
                        "parameters": {"count": {default_key: 3, "exposed": exposed}}
                    },
                )
            ]
        )
        result = await generate(
            subject,
            source="llm_tool",
            count=5,
            parameters={"count": 5, "concurrency": "ignored old setting"},
        )
        assert result.request.count == len(result.images) == (5 if exposed else 3)
        page = await generate(subject)
        assert page.request.count == len(page.images) == 1

    asyncio.run(run())


@pytest.mark.parametrize("null_default", ["model", "default_override", "default"])
def test_nai_null_default_never_reintroduces_hidden_count(null_default):
    async def run():
        parameters = batch_parameters(count=2)
        count_policy = {"exposed": False}
        if null_default == "model":
            parameters["count"]["default"] = None
        else:
            count_policy[null_default] = None
        subject = service(
            [
                provider(
                    parameters=parameters, tool={"parameters": {"count": count_policy}}
                )
            ]
        )
        try:
            result = await generate(
                subject, source="llm_tool", count=8, parameters={"count": 6, "n": 7}
            )
        except ValueError:
            assert subject.executor.calls == []
            assert subject.store.history == []
        else:
            assert result.request.count == len(result.images) == 2

    asyncio.run(run())


def test_nai_fixed_seed_is_preserved_for_every_subrequest():
    async def run():
        subject = service()
        result = await generate(
            subject, count=3, parameters={"seed": 42, "concurrency": 2}
        )
        assert len(result.images) == 3
        assert [item.parameters["seed"] for _, item in subject.executor.calls] == [
            42
        ] * 3
        assert all(
            "concurrency" not in item.parameters for _, item in subject.executor.calls
        )

    asyncio.run(run())


def test_nai_default_descriptions_follow_existing_schema_limits():
    configured = provider(
        parameters={"count": {"type": "integer", "default": 1, "min": 1, "max": 4}}
    )
    descriptor = configured.models[0].parameters["count"]
    assert descriptor["max"] == 4
    assert descriptor["description"].endswith("取值范围：[1, 4]。")


def test_nai_custom_count_description_is_preserved():
    configured = provider(
        parameters={"count": {"default": 2, "description": "Custom description"}}
    )
    assert (
        configured.models[0].parameters["count"]["description"] == "Custom description"
    )


def test_nai_settings_save_keeps_existing_provider_concurrency_slots():
    async def run():
        configured = provider(limit=2)
        executor = Executor(blocked=True)
        subject = service([configured], executor=executor)
        running = asyncio.create_task(generate(subject, count=3))
        await executor.wait_started(2)
        updated = replace(configured, max_concurrent_generations=1)
        subject.update_settings(
            replace(subject.settings, providers=(updated,), revision=1)
        )
        waiting = asyncio.create_task(generate(subject, count=2))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(executor.calls) == 2
        executor.gates[0].set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(executor.calls) == 2
        executor.release_all()
        await asyncio.wait_for(asyncio.gather(running, waiting), 2)
        assert len(executor.calls) == 5
        assert executor.peak["nai"] == 2

    asyncio.run(run())


@pytest.mark.parametrize("returned_count", [0, 2])
def test_nai_abnormal_image_count_keeps_paid_results_and_reports_mismatch(
    returned_count,
):
    async def run():
        class InvalidResultExecutor(Executor):
            async def generate(self, configured, request):
                result = await super().generate(configured, request)
                return result * returned_count if result == (image(2),) else result

        subject = service(executor=InvalidResultExecutor())
        result = await generate(subject, count=3)
        assert result.images == (
            (image(1), image(3))
            if returned_count == 0
            else (image(1), image(2), image(2), image(3))
        )
        assert result.warning
        assert len(subject.executor.calls) == 3
        assert len(subject.store.history) == 1

    asyncio.run(run())
