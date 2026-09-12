from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections import defaultdict
from dataclasses import replace

import pytest

from astrbot_plugin_image_studio.config import normalize_webui_settings
from astrbot_plugin_image_studio.models import (
    GenerationRequest,
    ImageProvider,
    ReferenceImage,
)
from astrbot_plugin_image_studio.providers import ProviderError, ProviderExecutor
from astrbot_plugin_image_studio.storage import GenerationStore
from astrbot_plugin_image_studio.tests.test_nai_batches import (
    Executor,
    batch_parameters,
    generate,
    image,
    service,
)

KINDS = ("openai_images", "gemini", "custom_json", "nai_direct")
SOURCES = ("webui", "command", "llm_tool")
SCHEDULER_KEYS = {
    "concurrency",
    "batch_mode",
    "native_batch_size",
    "max_concurrent_requests",
}
CONTROLS = {"count", "n", *SCHEDULER_KEYS}


def provider(
    kind="openai_images",
    *,
    native_batch_size=4,
    max_concurrent_requests=8,
    parameters=None,
    limit=8,
    **model,
):
    configured = {
        "id": "paint",
        "supports_img2img": True,
        "max_reference_images": 4,
        "max_concurrent_requests": max_concurrent_requests,
        **model,
    }
    if native_batch_size is not None:
        configured["native_batch_size"] = native_batch_size
    if parameters is not None:
        configured["parameters"] = parameters
    return ImageProvider.from_mapping(
        {
            "id": "studio",
            "name": "Test provider",
            "kind": kind,
            "base_url": "https://invalid.example.test",
            "max_concurrent_generations": limit,
            "models": [configured],
        }
    )


class BatchExecutor(Executor):
    def __init__(self, *, outputs=None, **options):
        super().__init__(**options)
        self.outputs = outputs or {}
        self.model_active = defaultdict(int)
        self.model_peak = defaultdict(int)

    async def generate(self, configured, request):
        index = len(self.calls) + 1
        key = (configured.id, request.model)
        self.model_active[key] += 1
        self.model_peak[key] = max(self.model_peak[key], self.model_active[key])
        try:
            await super().generate(configured, request)
            return self.outputs.get(
                index,
                tuple(image(index * 16 + offset) for offset in range(request.count)),
            )
        finally:
            self.model_active[key] -= 1


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("legacy", [False, True])
def test_unknown_native_capacity_defaults_to_one_and_model_concurrency_to_eight(
    kind, legacy
):
    raw = {"id": "studio", "kind": kind}
    raw.update({"model": "paint"} if legacy else {"models": [{"id": "paint"}]})
    selected = ImageProvider.from_mapping(raw).models[0]
    assert selected.native_batch_size == 1
    assert selected.max_concurrent_requests == 8
    assert selected.native_batch_size_source == (
        "fixed" if kind == "nai_direct" else "default"
    )
    assert selected.parameters["count"]["default"] == 1
    assert SCHEDULER_KEYS.isdisjoint(selected.parameters)
    assert SCHEDULER_KEYS.isdisjoint(selected.llm_exposed_parameter_names)


@pytest.mark.parametrize("kind", KINDS)
def test_count_schema_roundtrips_without_obsolete_scheduler_controls(kind):
    parameters = {
        "count": {"type": "integer", "default": 7, "min": 1, "max": 50},
        **{name: {"type": "integer", "default": 3} for name in SCHEDULER_KEYS},
        "workers": {"type": "integer", "request_key": "concurrency", "default": 2},
    }
    configured = provider(
        kind,
        native_batch_size=30,
        max_concurrent_requests=5,
        parameters=parameters,
        tool={
            "parameters": {
                "count": {"default_override": 9, "exposed": False},
                **{
                    name: {"exposed": True, "default_override": 4}
                    for name in SCHEDULER_KEYS
                },
                "workers": {"exposed": True},
            }
        },
    )
    selected = configured.models[0]
    assert selected.native_batch_size == (1 if kind == "nai_direct" else 30)
    assert selected.max_concurrent_requests == 5
    assert selected.parameters["count"]["default"] == 7
    assert selected.parameters["count"]["max"] == 50
    assert SCHEDULER_KEYS.isdisjoint(selected.parameters)
    assert "workers" not in selected.parameters
    assert SCHEDULER_KEYS.isdisjoint(selected.tool["parameters"])
    assert "workers" not in selected.tool["parameters"]
    normalized, errors = normalize_webui_settings(
        {"providers": [configured.public_dict()]}
    )
    assert errors == []
    restored = ImageProvider.from_mapping(normalized["providers"][0]).models[0]
    assert restored.public_dict() == selected.public_dict()


@pytest.mark.parametrize("kind", KINDS[:-1])
def test_remote_native_capacity_is_used_when_no_manual_value_exists(kind):
    configured = ImageProvider.from_mapping(
        {
            "id": "studio",
            "kind": kind,
            "discovered_models": [{"id": "paint", "native_batch_size": 6}],
            "models": [{"id": "paint"}],
        }
    )
    selected = configured.models[0]
    assert selected.native_batch_size == 6
    assert selected.native_batch_size_source == "remote"
    raw = configured.public_dict()
    raw["models"][0]["native_batch_size"] = 3
    raw["models"][0]["native_batch_size_source"] = "manual"
    manual = ImageProvider.from_mapping(raw).models[0]
    assert manual.native_batch_size == 3
    assert manual.native_batch_size_source == "manual"


@pytest.mark.parametrize("kind", KINDS[:-1])
@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("mode", ["text2img", "img2img"])
def test_automatic_chunks_use_native_capacity_and_reuse_reference_tuple(
    kind, source, mode
):
    async def run():
        executor = BatchExecutor()
        subject = service(
            [provider(kind, parameters=batch_parameters(count=10))], executor=executor
        )
        references = (
            tuple(
                ReferenceImage(
                    str(index), f"ref-{index}.png", image(index).data, "image/png"
                )
                for index in (40, 41)
            )
            if mode == "img2img"
            else ()
        )
        result = await subject.generate(
            mode=mode,
            provider_id="",
            model_ref="studio:paint",
            prompt="A lake in the mountains",
            source=source,
            references=references,
        )
        assert result.request.count == len(result.images) == 10
        assert result.request.native_batch_size == 4
        assert result.request.max_concurrent_requests == 8
        assert [request.count for _, request in executor.calls] == [4, 4, 2]
        assert len(subject.store.history) == 1
        assert not result.warning
        for _, child in executor.calls:
            assert child.references is result.request.references
            assert child.references == references
            assert child.mode == mode
            assert CONTROLS.isdisjoint(child.parameters)

    asyncio.run(run())


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("source", SOURCES)
def test_defaults_allow_one_request_up_to_native_capacity(kind, source):
    async def run():
        executor = BatchExecutor()
        subject = service(
            [provider(kind, native_batch_size=4, parameters=batch_parameters(count=3))],
            executor=executor,
        )
        result = await generate(subject, source=source)
        assert result.request.count == len(result.images) == 3
        assert [request.count for _, request in executor.calls] == (
            [1, 1, 1] if kind == "nai_direct" else [3]
        )
        assert not result.warning

    asyncio.run(run())


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("exposed", [True, False])
def test_llm_defaults_and_hidden_count_are_respected(kind, exposed):
    async def run():
        configured = provider(
            kind,
            parameters=batch_parameters(count=2),
            tool={"parameters": {"count": {"exposed": exposed, "default_override": 5}}},
        )
        subject = service([configured], executor=BatchExecutor())
        result = await generate(
            subject,
            source="llm_tool",
            count=7 if exposed else "invalid hidden count",
            parameters={"count": 7 if exposed else "invalid hidden count"},
        )
        assert result.request.count == len(result.images) == (7 if exposed else 5)
        page = await generate(subject)
        assert page.request.count == len(page.images) == 2

    asyncio.run(run())


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("name,request_key", [("samples", "count"), ("n", "n")])
def test_quantity_aliases_preserve_defaults_and_explicit_values(
    source, name, request_key
):
    async def run():
        configured = provider(
            parameters={
                name: {
                    "type": "integer",
                    "request_key": request_key,
                    "default": 2,
                    "min": 1,
                    "max": 8,
                }
            },
            tool={"parameters": {name: {"exposed": True, "default_override": 3}}},
        )
        subject = service([configured], executor=BatchExecutor())
        result = await generate(subject, source=source)
        assert (
            result.request.count
            == len(result.images)
            == (3 if source == "llm_tool" else 2)
        )
        explicit = await generate(subject, source=source, parameters={name: 5})
        assert explicit.request.count == len(explicit.images) == 5
        for _, child in subject.executor.calls:
            assert CONTROLS.isdisjoint(child.parameters)
            assert name not in child.parameters

    asyncio.run(run())


@pytest.mark.parametrize("source", SOURCES)
def test_total_count_is_bounded_by_schema_not_native_capacity(source):
    async def run():
        parameters = batch_parameters(count=7)
        parameters["count"]["max"] = 30
        executor = BatchExecutor()
        subject = service(
            [provider(native_batch_size=9, parameters=parameters)], executor=executor
        )
        result = await generate(subject, source=source, count=27)
        assert len(result.images) == result.request.count == 27
        assert [request.count for _, request in executor.calls] == [9, 9, 9]

    asyncio.run(run())


@pytest.mark.parametrize("source", SOURCES)
def test_raw_scheduler_overrides_are_ignored_at_every_entrypoint(source):
    async def run():
        executor = BatchExecutor()
        subject = service(
            [provider(native_batch_size=2, max_concurrent_requests=2)],
            executor=executor,
        )
        result = await generate(
            subject,
            source=source,
            count=5,
            parameters={name: "ignored invalid value" for name in SCHEDULER_KEYS},
        )
        assert [request.count for _, request in executor.calls] == [2, 2, 1]
        assert result.request.native_batch_size == 2
        assert result.request.max_concurrent_requests == 2
        assert SCHEDULER_KEYS.isdisjoint(result.request.parameters)
        assert all(CONTROLS.isdisjoint(child.parameters) for _, child in executor.calls)

    asyncio.run(run())


def test_partial_request_results_preserve_order_and_record_request_and_image_counts(
    tmp_path,
):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        executor = BatchExecutor(
            blocked=True,
            failures={2},
            outputs={1: (image(10), image(11), image(12), image(13), image(14))},
        )
        subject = service(
            [provider(parameters=batch_parameters(count=10))],
            executor=executor,
            store=store,
        )
        pending = asyncio.create_task(generate(subject))
        await executor.wait_started(3)
        for index in (2, 0, 1):
            executor.gates[index].set()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        result = await asyncio.wait_for(pending, 2)
        assert executor.completed == [3, 1]
        assert result.images == tuple(
            image(index) for index in (10, 11, 12, 13, 14, 48, 49)
        )
        assert "upstream failure 2" in result.warning
        assert "NAI" not in result.warning
        assert len(executor.calls) == 3
        assert (await store.list_generations({}))["total"] == 1
        detail = await store.generation_detail(result.generation_id)
        assert detail["parameters"]["count"] == 10
        assert detail["parameters"]["native_batch_size"] == 4
        assert detail["parameters"]["max_concurrent_requests"] == 8
        assert [value["sha256"] for value in detail["images"]] == [
            hashlib.sha256(value.data).hexdigest() for value in result.images
        ]
        batch = detail["supplemental"]["batch"]
        assert batch["request_count"] == 3
        assert batch["failed_requests"] == 1
        assert batch["returned_images"] == 7
        assert batch["request_sizes"] == [4, 4, 2]
        assert batch["failures"] == [{"index": 2, "error": "upstream failure 2"}]

    asyncio.run(run())


@pytest.mark.parametrize("returned_count", [2, 5])
def test_native_response_quantity_mismatch_preserves_all_images_without_retry(
    returned_count,
):
    async def run():
        outputs = tuple(image(100 + index) for index in range(returned_count))
        executor = BatchExecutor(outputs={1: outputs})
        subject = service(
            [provider(parameters=batch_parameters(count=4))], executor=executor
        )
        result = await generate(subject)
        assert result.images == outputs
        assert len(executor.calls) == 1
        assert result.warning
        assert str(returned_count) in result.warning
        assert len(subject.store.history) == 1

    asyncio.run(run())


def test_empty_result_is_a_failed_request_and_all_failures_are_not_saved():
    async def run():
        executor = BatchExecutor(outputs={1: (), 3: ()}, failures={2})
        subject = service(
            [provider(parameters=batch_parameters(count=10))], executor=executor
        )
        with pytest.raises(ProviderError) as raised:
            await generate(subject)
        assert raised.value.images == ()
        assert [index for index, _ in raised.value.failures] == [1, 2, 3]
        assert len(executor.calls) == 3
        assert subject.store.history == []

    asyncio.run(run())


def test_model_limit_is_shared_across_simultaneous_tasks():
    async def run():
        configured = provider(
            native_batch_size=1,
            max_concurrent_requests=2,
            parameters=batch_parameters(count=4),
            limit=8,
        )
        executor = BatchExecutor(blocked=True)
        subject = service([configured], executor=executor)
        first = asyncio.create_task(generate(subject))
        await executor.wait_started(2)
        second = asyncio.create_task(generate(subject))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(executor.calls) == 2
        executor.release_all()
        results = await asyncio.wait_for(asyncio.gather(first, second), 2)
        assert [len(result.images) for result in results] == [4, 4]
        assert executor.model_peak[("studio", "paint")] == 2
        assert executor.model_active[("studio", "paint")] == 0

    asyncio.run(run())


def test_provider_limit_is_shared_between_different_model_limits():
    async def run():
        configured = provider(
            native_batch_size=1,
            max_concurrent_requests=1,
            parameters=batch_parameters(count=3),
            limit=2,
        )
        second_model = replace(
            configured.models[0], id="second", max_concurrent_requests=2
        )
        configured = replace(configured, models=(*configured.models, second_model))
        executor = BatchExecutor(blocked=True)
        subject = service([configured], executor=executor)
        first = asyncio.create_task(generate(subject, model_ref="studio:paint"))
        await executor.wait_started(1)
        second = asyncio.create_task(generate(subject, model_ref="studio:second"))
        await executor.wait_started(1)
        await asyncio.sleep(0)
        assert len(executor.calls) == 2
        assert executor.model_active[("studio", "paint")] == 1
        assert executor.model_active[("studio", "second")] == 1
        executor.release_all()
        results = await asyncio.wait_for(asyncio.gather(first, second), 2)
        assert [len(result.images) for result in results] == [3, 3]
        assert executor.peak["studio"] == 2
        assert executor.model_peak[("studio", "paint")] == 1

    asyncio.run(run())


def test_model_limit_update_preserves_running_slots_and_applies_to_queued_tasks():
    async def run():
        configured = provider(native_batch_size=1, max_concurrent_requests=2, limit=8)
        executor = BatchExecutor(blocked=True)
        subject = service([configured], executor=executor)
        first = asyncio.create_task(generate(subject, count=3))
        await executor.wait_started(2)
        updated = replace(
            configured,
            models=(replace(configured.models[0], max_concurrent_requests=1),),
        )
        subject.update_settings(
            replace(subject.settings, providers=(updated,), revision=1)
        )
        second = asyncio.create_task(generate(subject, count=2))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(executor.calls) == 2
        executor.gates[0].set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(executor.calls) == 2
        executor.release_all()
        results = await asyncio.wait_for(asyncio.gather(first, second), 2)
        assert [len(result.images) for result in results] == [3, 2]
        assert executor.model_peak[("studio", "paint")] == 2
        assert executor.model_active[("studio", "paint")] == 0
        assert results[0].request.max_concurrent_requests == 2
        assert results[1].request.max_concurrent_requests == 1

    asyncio.run(run())


def test_cancellation_releases_both_limits_and_stops_queued_requests():
    async def run():
        configured = provider(
            "gemini",
            native_batch_size=1,
            max_concurrent_requests=2,
            parameters=batch_parameters(count=6),
            limit=3,
        )
        executor = BatchExecutor(blocked=True)
        subject = service([configured], executor=executor)
        pending = asyncio.create_task(generate(subject))
        await executor.wait_started(2)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 2)
        assert executor.active["studio"] == 0
        assert executor.model_active[("studio", "paint")] == 0
        executor.release_all()
        await asyncio.sleep(0)
        assert len(executor.calls) == 2
        assert subject.store.history == []
        result = await asyncio.wait_for(generate(subject, count=2), 2)
        assert len(result.images) == 2
        assert len(executor.calls) == 4

    asyncio.run(run())


class ImageResponse:
    status = 200
    headers = {"Content-Type": "image/png"}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def read(self):
        return image(90).data


class ImageSession:
    def __init__(self, response=None):
        self.calls = []
        self.response = response or ImageResponse()

    def post(self, url, **options):
        self.calls.append((url, options))
        return self.response


@pytest.mark.parametrize("kind", KINDS[:-1])
def test_provider_json_never_receives_local_scheduler_parameters(kind):
    async def run():
        session = ImageSession()
        request = GenerationRequest(
            mode="text2img",
            provider_id="studio",
            model="paint",
            prompt="mountain lake",
            count=2,
            parameters={"count": 99, "n": 98, **dict.fromkeys(SCHEDULER_KEYS, 97)},
        )
        await ProviderExecutor(session).generate(provider(kind), request)
        payload = session.calls[0][1]["json"]
        assert {"count", *SCHEDULER_KEYS}.isdisjoint(payload)
        if kind == "gemini":
            assert "n" not in payload
            assert payload["generationConfig"]["candidateCount"] == 2
        else:
            assert payload["n"] == 2

    asyncio.run(run())


def test_native_upstream_response_above_eight_images_is_not_silently_truncated():
    async def run():
        images = tuple(image(index) for index in range(10))

        class JSONImageResponse(ImageResponse):
            headers = {"Content-Type": "application/json"}

            async def read(self):
                return json.dumps(
                    {
                        "data": [
                            {"b64_json": base64.b64encode(item.data).decode("ascii")}
                            for item in images
                        ]
                    }
                ).encode("utf-8")

        session = ImageSession(JSONImageResponse())
        result = await ProviderExecutor(session).generate(
            provider(native_batch_size=10),
            GenerationRequest(
                mode="text2img",
                provider_id="studio",
                model="paint",
                prompt="mountain lake",
                count=10,
            ),
        )
        assert result == images
        assert len(session.calls) == 1

    asyncio.run(run())


def test_openai_multipart_uses_only_normalized_request_count():
    async def run():
        session = ImageSession()
        request = GenerationRequest(
            mode="img2img",
            provider_id="studio",
            model="paint",
            prompt="mountain lake",
            count=3,
            parameters={"count": 99, "n": 98, **dict.fromkeys(SCHEDULER_KEYS, 97)},
            references=(ReferenceImage("ref", "ref.png", image(1).data, "image/png"),),
        )
        await ProviderExecutor(session).generate(provider(), request)
        fields = session.calls[0][1]["data"]._fields
        assert [
            value for disposition, _, value in fields if disposition["name"] == "n"
        ] == ["3"]
        assert {"count", *SCHEDULER_KEYS}.isdisjoint(
            {disposition["name"] for disposition, _, _ in fields}
        )

    asyncio.run(run())


def test_custom_template_count_and_n_use_the_actual_chunk_size():
    async def run():
        configured = replace(
            provider("custom_json", parameters=batch_parameters(count=10)),
            request_template=json.dumps({"amount": "{{count}}", "samples": "{{n}}"}),
        )
        session = ImageSession()
        subject = service([configured], executor=ProviderExecutor(session))
        result = await generate(subject)
        assert len(result.images) == len(session.calls) == 3
        assert [call[1]["json"] for call in session.calls] == [
            {"amount": size, "samples": size} for size in (4, 4, 2)
        ]
        assert result.warning

    asyncio.run(run())
