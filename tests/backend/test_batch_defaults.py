from __future__ import annotations

import asyncio
import copy
import json
import zipfile
from dataclasses import replace

import pytest

from astrbot_plugin_image_studio.backend.models import ImageProvider
from astrbot_plugin_image_studio.backend.metadata.exchange import export_parameters
from astrbot_plugin_image_studio.backend.providers.executor import ProviderExecutor
from astrbot_plugin_image_studio.backend.gallery.store import GenerationStore
from astrbot_plugin_image_studio.tests.backend.test_model_batches import (
    BatchExecutor,
    provider,
)
from astrbot_plugin_image_studio.tests.backend.test_nai_batches import (
    batch_parameters,
    generate,
    image,
    service,
)


class DiscoveryResponse:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def read(self):
        return json.dumps(self.payload).encode("utf-8")


class DiscoverySession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, **options):
        self.calls.append((url, options))
        return DiscoveryResponse(self.payload)


async def discover(payload, kind="openai_images"):
    session = DiscoverySession(payload)
    models = await ProviderExecutor(session).discover_models(provider(kind))
    assert len(session.calls) == 1
    return models


@pytest.mark.parametrize("kind", ["openai_images", "gemini", "custom_json"])
@pytest.mark.parametrize(
    "capability",
    [
        {"native_batch_size": 6},
        {"max_batch_size": 6},
        {"capabilities": {"max_images_per_request": 6}},
        {"limits": {"max_output_images": 6}},
        {"parameters": {"n": {"maximum": 6}}},
        {"input_schema": {"properties": {"n": {"maximum": 6}}}},
        {"inputSchema": {"properties": {"num_images": {"max": 6}}}},
        {"request_schema": {"properties": {"number_of_images": {"maximum": 6}}}},
    ],
)
def test_discovery_reads_only_explicit_native_image_count_limits(kind, capability):
    async def run():
        key = "models" if kind == "gemini" else "data"
        result = await discover({key: [{"id": "paint", **capability}]}, kind)
        assert result[0]["native_batch_size"] == 6
        assert result[0]["native_batch_size_source"] == "remote"

    asyncio.run(run())


@pytest.mark.parametrize("invalid", [None, False, True, 0, -1, 1.5, "unknown", [], {}])
def test_discovery_invalid_limits_fall_back_without_claiming_remote_capability(invalid):
    async def run():
        result = await discover(
            {"data": [{"id": "paint", "native_batch_size": invalid}]}
        )
        assert result[0]["native_batch_size"] == 1
        assert result[0]["native_batch_size_source"] == "default"

    asyncio.run(run())


def test_discovery_does_not_infer_native_output_capacity_from_other_limits_or_name():
    async def run():
        result = await discover(
            {
                "models": [
                    {
                        "name": "models/image-batch-16",
                        "inputTokenLimit": 8192,
                        "outputTokenLimit": 4096,
                        "max_reference_images": 8,
                        "supportedGenerationMethods": ["generateContent"],
                        "limits": {"tokens": 4000, "max_concurrent_requests": 12},
                    }
                ]
            },
            "gemini",
        )
        assert result[0]["id"] == "image-batch-16"
        assert result[0]["native_batch_size"] == 1
        assert result[0]["native_batch_size_source"] == "default"

    asyncio.run(run())


def test_discovery_accepts_large_positive_integer_limits_and_skips_invalid_candidates():
    async def run():
        result = await discover(
            {
                "data": [
                    {
                        "id": "paint",
                        "native_batch_size": 0,
                        "capabilities": {"max_images_per_request": "32"},
                    }
                ]
            }
        )
        assert result[0]["native_batch_size"] == 32
        assert result[0]["native_batch_size_source"] == "remote"

    asyncio.run(run())


def test_discovered_cache_updates_default_capacity_but_preserves_manual_override():
    async def run():
        original = provider(native_batch_size=None)
        assert original.models[0].native_batch_size_source == "default"
        raw = original.public_dict()
        raw["discovered_models"] = await discover(
            {"data": [{"id": "paint", "native_batch_size": 6}]}
        )
        remote = ImageProvider.from_mapping(raw)
        assert remote.models[0].native_batch_size == 6
        assert remote.models[0].native_batch_size_source == "remote"
        raw = remote.public_dict()
        raw["models"][0]["native_batch_size"] = 3
        raw["models"][0]["native_batch_size_source"] = "manual"
        raw["discovered_models"] = await discover(
            {"data": [{"id": "paint", "native_batch_size": 9}]}
        )
        manual = ImageProvider.from_mapping(raw)
        assert manual.models[0].native_batch_size == 3
        assert manual.models[0].native_batch_size_source == "manual"
        assert manual.discovered_models[0]["native_batch_size"] == 9
        reloaded = ImageProvider.from_mapping(manual.public_dict())
        assert reloaded.models[0].public_dict() == manual.models[0].public_dict()

    asyncio.run(run())


@pytest.mark.parametrize("source", ["webui", "command", "llm_tool"])
def test_reproduction_uses_current_page_batch_defaults_without_changing_history_or_exports(
    tmp_path, source
):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        parameters = {
            **batch_parameters(count=10),
            "seed": {"type": "integer", "default": 42},
            "quality": {"type": "text", "default": "high"},
        }
        subject = service(
            [
                provider(
                    native_batch_size=4,
                    max_concurrent_requests=6,
                    parameters=parameters,
                )
            ],
            executor=BatchExecutor(),
            store=store,
        )
        result = await generate(subject, source=source)
        before = await store.generation_detail(
            result.generation_id, include_assets=False
        )
        assert before["parameters"]["count"] == 10
        assert before["parameters"]["native_batch_size"] == 4
        assert before["parameters"]["max_concurrent_requests"] == 6

        updated = provider(
            native_batch_size=3,
            max_concurrent_requests=1,
            parameters={
                "count": {
                    **batch_parameters(count=2)["count"],
                    "refill_from_history": False,
                },
                "seed": {"type": "integer", "default": 100},
                "quality": {"type": "text", "default": "low"},
            },
            tool={"parameters": {"count": {"exposed": True, "default_override": 7}}},
        )
        settings_snapshot = copy.deepcopy(updated.public_dict())
        subject.update_settings(
            replace(subject.settings, providers=(updated,), revision=1)
        )
        draft = await subject.reproduction_plan(result.generation_id)
        assert draft["count"] == 2
        assert draft["native_batch_size"] == 3
        assert draft["max_concurrent_requests"] == 1
        assert draft["for_reproduction"] is True
        assert draft["parameters"]["seed"] == 42
        assert draft["parameters"]["quality"] == "high"
        assert draft["parameters"]["count"] == 2
        assert subject.settings.providers[0].public_dict() == settings_snapshot
        after = await store.generation_detail(
            result.generation_id, include_assets=False
        )
        assert after == before

        copied = json.loads(export_parameters(after)["content"])
        assert copied["data"]["count"] == 10
        assert copied["data"]["native_batch_size"] == 4
        assert copied["data"]["max_concurrent_requests"] == 6
        archive_path = await store.export_generations([result.generation_id])
        with zipfile.ZipFile(archive_path) as archive:
            assert archive.testzip() is None
            names = [name for name in archive.namelist() if name.endswith(".json")]
            assert len(names) == 10
            for name in names:
                exported = json.loads(archive.read(name))
                assert exported["parameters"] == before["parameters"]
                assert exported["supplemental"] == before["supplemental"]

    asyncio.run(run())


def test_import_reproduction_uses_current_batch_defaults_and_retains_import_metadata(
    tmp_path,
):
    async def run():
        store = GenerationStore(tmp_path)
        await store.initialize()
        imported = await store.import_image(
            image(120).data,
            "import.png",
            {
                "model": "paint",
                "mode": "text2img",
                "generation_engine": "openai_images",
                "prompt": "mountain lake",
                "parameters": {"count": 12},
            },
        )
        before = await store.generation_detail(
            imported["generation_id"], include_assets=False
        )
        configured = provider(
            native_batch_size=3,
            max_concurrent_requests=2,
            parameters={
                "count": {
                    **batch_parameters(count=2)["count"],
                    "refill_from_history": False,
                }
            },
            tool={"parameters": {"count": {"default_override": 7, "exposed": True}}},
        )
        subject = service([configured], executor=BatchExecutor(), store=store)
        snapshot = copy.deepcopy(configured.public_dict())
        draft = await subject.reproduction_plan(imported["generation_id"])
        assert draft["model_ref"] == "studio:paint"
        assert draft["count"] == 2
        assert draft["native_batch_size"] == 3
        assert draft["max_concurrent_requests"] == 2
        assert draft["for_reproduction"] is True
        assert configured.public_dict() == snapshot
        assert (
            await store.generation_detail(
                imported["generation_id"], include_assets=False
            )
            == before
        )

    asyncio.run(run())
