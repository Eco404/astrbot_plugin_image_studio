"""Gallery request summaries omit empty scaffolding without losing node intent."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from astrbot_plugin_image_studio.models import ImageProvider
from astrbot_plugin_image_studio.parameter_exchange import (
    export_parameters,
    request_snapshot,
)
from astrbot_plugin_image_studio.storage import compact_comfy_request
from astrbot_plugin_image_studio.tests.test_comfy_runtime import (
    runtime_fixture,
    workflow,
)


def test_compaction_is_shallow_preserves_false_zero_and_only_removes_known_internal_key():
    nested = {"blank": "", "is_changed": None}
    original = {
        "prompt": "",
        "negative_prompt": None,
        "size": "  ",
        "count": 1,
        "parameters": {
            "empty": "",
            "blank": " \n",
            "none": None,
            "list": [],
            "object": {},
            "seed": 0,
            "enabled": False,
            "nested": nested,
            "_comfy_job_id": "private-routing-id",
            "_comfy_custom": "keep",
        },
    }
    result = compact_comfy_request(original)
    assert result == {
        "count": 1,
        "parameters": {
            "seed": 0,
            "enabled": False,
            "nested": nested,
            "_comfy_custom": "keep",
        },
    }
    assert original["parameters"]["empty"] == ""
    assert result["parameters"]["nested"] == {"blank": "", "is_changed": None}


@pytest.mark.parametrize("kind", ["comfyui", "openai_images"])
def test_old_comfy_exports_are_compact_without_altering_other_provider_empty_intent(
    kind,
):
    snapshot = request_snapshot(
        {
            "provider_kind": kind,
            "mode": "text2img",
            "provider_id": "p",
            "model": "m",
            "original_prompt": "",
            "parameters": {
                "negative_prompt": "",
                "size": "",
                "count": 1,
                "parameters": {"_comfy_job_id": "internal", "seed": 0},
            },
        }
    )
    if kind == "comfyui":
        assert all(key not in snapshot for key in ("prompt", "negative_prompt", "size"))
        assert snapshot["parameters"] == {"seed": 0}
    else:
        assert snapshot["negative_prompt"] == ""
        assert snapshot["prompt"] == ""


@pytest.mark.parametrize("record_prefix", [True, False])
def test_saved_comfy_requests_omit_empties_but_reproduction_retains_allowed_empty_nodes(
    tmp_path, monkeypatch, record_prefix
):
    async def run():
        definition = workflow()
        definition["api_graph"]["10"] = {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": "avoid"},
        }
        definition["api_graph"]["11"] = {
            "class_type": "CustomNode",
            "inputs": {"enabled": True},
        }
        definition["bindings"] = {
            "prefix": {
                "source": "parameter",
                "type": "text",
                "node_id": "1",
                "input_name": "text",
            },
            "sampling_seed": {
                "source": "parameter",
                "type": "number",
                "node_id": "3",
                "input_name": "seed",
            },
            "enabled": {
                "source": "parameter",
                "type": "boolean",
                "node_id": "11",
                "input_name": "enabled",
            },
            "negative": {
                "source": "negative_prompt",
                "type": "text",
                "node_id": "10",
                "input_name": "text",
            },
        }
        runtime, service, provider, client = await runtime_fixture(
            tmp_path, monkeypatch, config=definition
        )
        raw = provider.public_dict()
        raw["models"][0]["negative_prompt_default"] = "avoid"
        raw["models"][0]["parameters"]["prefix"]["record_in_history"] = record_prefix
        provider = ImageProvider.from_mapping(raw)
        service.update_settings(replace(service.settings, providers=(provider,)))
        try:
            result = await service.generate(
                mode="text2img",
                provider_id="comfy",
                model="workflow",
                prompt="",
                negative_prompt="",
                parameters={
                    "prefix": "",
                    "sampling_seed": 0,
                    "enabled": False,
                    "empty_extra": [],
                    "nested": {"keep_empty": ""},
                },
            )
            detail = await service.store.generation_detail(result.generation_id)
            original = detail["parameters"]
            effective = detail["images"][0]["supplemental"]["effective_request"]
            for stored in (original, effective):
                assert "negative_prompt" not in stored and "size" not in stored
                assert (
                    not {"_comfy_job_id", "prefix", "empty_extra"}
                    & stored.get("parameters", {}).keys()
                )
                assert stored["parameters"]["sampling_seed"] == 0
                assert stored["parameters"]["enabled"] is False
                assert stored["parameters"]["nested"] == {"keep_empty": ""}
            copied = json.loads(export_parameters(detail)["content"])["data"]
            assert all(
                key not in copied for key in ("prompt", "negative_prompt", "size")
            )
            actual = detail["images"][0]["supplemental"]["comfyui"]["api_graph"]
            assert actual["1"]["inputs"]["text"] == actual["10"]["inputs"]["text"] == ""
            plan = await service.reproduction_plan(result.generation_id)
            assert plan["parameters"]["prefix"] == (
                "" if record_prefix else "fixed prompt"
            )
            assert plan["negative_prompt"] == ""
            assert len([call for call in client.calls if call[0] == "submit"]) == 1
        finally:
            await runtime.close()

    asyncio.run(run())
