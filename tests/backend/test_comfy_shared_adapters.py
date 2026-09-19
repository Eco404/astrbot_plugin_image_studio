"""Shared read/write declarations and explicit random-seed contracts."""

from __future__ import annotations

import copy

import pytest

from astrbot_plugin_image_studio.backend.metadata.comfyui_graph import _workflow_graph
from astrbot_plugin_image_studio.backend.metadata.node_rules import (
    get_rules,
    make_rules,
)
from astrbot_plugin_image_studio.backend.providers.comfyui.workflows import (
    prepare_graph,
    seed_warnings,
)
from astrbot_plugin_image_studio.backend.models import GenerationRequest
from astrbot_plugin_image_studio.tests.backend.test_comfy_seed_warnings import (
    bind,
    definition,
)


@pytest.mark.parametrize("kind", ["SeedNode", "easy seed", "GenerateNoise"])
def test_known_nonnegative_seeds_request_explicit_plugin_binding(kind):
    notices = seed_warnings(definition(-1, kind=kind))
    assert len(notices) == 1
    assert notices[0]["code"] == "seed_requires_binding"
    assert notices[0]["action"] == "bind_seed_source"


@pytest.mark.parametrize(
    "kind,field,maximum",
    [
        ("SeedNode", "seed", 2**63 - 1),
        ("easy seed", "seed", 2**50),
        ("GenerateNoise", "seed", 2**64 - 1),
        ("ImpactInt", "value", 2**63 - 1),
        ("PrimitiveInt", "value", 2**63 - 1),
    ],
)
def test_explicit_seed_binding_uses_catalog_range(kind, field, maximum, monkeypatch):
    config = definition(17, kind=kind, name=field)
    config["bindings"] = {
        "seed_input": {
            "source": "seed",
            "type": "number",
            "max": maximum,
            "targets": [{"node_id": "1", "input_name": field}],
        }
    }
    monkeypatch.setattr(
        "astrbot_plugin_image_studio.backend.providers.comfyui.workflows.secrets.randbelow",
        lambda limit: limit - 1,
    )
    request = GenerationRequest(
        mode="text2img", provider_id="comfy", prompt="", parameters={"seed_input": -1}
    )
    actual = prepare_graph(config, request)
    assert actual["1"]["inputs"][field] == maximum


@pytest.mark.parametrize("kind", ["ImpactInt", "PrimitiveInt"])
def test_generic_integer_never_acquires_seed_semantics_without_binding(kind):
    config = definition(-1, kind=kind, name="value")
    assert seed_warnings(config) == []
    written = set()
    request = GenerationRequest(mode="text2img", provider_id="comfy", prompt="")
    assert (
        prepare_graph(config, request, written_inputs=written)["1"]["inputs"]["value"]
        == -1
    )
    assert not written


def test_easy_seed_range_intersects_multiple_target_bounds(monkeypatch):
    config = bind(definition(17), default=-1)
    config["api_graph"]["3"] = {"class_type": "easy seed", "inputs": {"seed": 1}}
    config["bindings"]["seed_input"]["targets"] = [
        {"node_id": "1", "input_name": "seed"},
        {"node_id": "3", "input_name": "seed"},
    ]
    config["bindings"]["seed_input"].pop("node_id", None)
    config["bindings"]["seed_input"].pop("input_name", None)
    monkeypatch.setattr(
        "astrbot_plugin_image_studio.backend.providers.comfyui.workflows.secrets.randbelow",
        lambda limit: limit - 1,
    )
    request = GenerationRequest(mode="text2img", provider_id="comfy", prompt="")
    graph = prepare_graph(config, request)
    assert graph["1"]["inputs"]["seed"] == graph["3"]["inputs"]["seed"] == 2**50


def test_shared_widget_mapping_is_defensive_and_affects_parser_fingerprint(monkeypatch):
    mapping = get_rules().catalog["widgets"]
    assert mapping["TextInput_"] == ["text"]
    mapping["TextInput_"].clear()
    assert get_rules().catalog["widgets"]["TextInput_"] == ["text"]
    original = make_rules([]).fingerprint
    monkeypatch.setattr(
        "astrbot_plugin_image_studio.backend.metadata.node_rules.catalog_fingerprint",
        lambda: "different-layout-catalog",
    )
    assert make_rules([]).fingerprint != original


def test_metadata_named_fallback_preserves_recorded_positional_seed():
    workflow = {
        "nodes": [
            {
                "id": 1,
                "type": "Seed (rgthree)",
                "widgets_values": [123, "", "", ""],
                "widgets_values_named": {"seed": -1},
            },
            {
                "id": 2,
                "type": "CLIPTextEncode",
                "widgets_values_named": {"text": "named prompt"},
            },
        ],
        "links": [],
    }
    original = copy.deepcopy(workflow)
    graph = _workflow_graph(workflow, {"warnings": []})
    assert graph["1"]["inputs"]["seed"] == 123
    assert graph["2"]["inputs"]["text"] == "named prompt"
    assert workflow == original
