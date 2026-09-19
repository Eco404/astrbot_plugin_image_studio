"""Read-only notices for locked seeds on the selected image output pipeline."""

from __future__ import annotations

import copy
import io
import json

import pytest
from astrbot_plugin_image_studio.backend.providers.comfyui.imports import import_result
from astrbot_plugin_image_studio.backend.providers.comfyui.workflows import (
    prepare_graph,
    seed_warnings,
)
from astrbot_plugin_image_studio.backend.models import GenerationRequest, ImageProvider
from astrbot_plugin_image_studio.backend.generation.service import _parameters_for_model
from PIL import Image, PngImagePlugin


def definition(value=123, *, kind="KSampler", name="seed"):
    return {
        "api_graph": {
            "1": {
                "class_type": kind,
                "inputs": {name: value},
                "is_changed": [123, "old snapshot"],
            },
            "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}},
        },
        "bindings": {},
        "outputs": ["2"],
        "workflow": {
            "nodes": [{"id": 999, "type": "Seed (rgthree)", "widgets_values": [9999]}],
            "links": [],
        },
    }


def bind(value, *, source="seed", default=None):
    value = copy.deepcopy(value)
    value["bindings"]["seed_input"] = {
        "source": source,
        "type": "number",
        "node_id": "1",
        "input_name": "seed",
    }
    if default is not None:
        value["bindings"]["seed_input"]["default"] = default
    return value


def test_fixed_ksampler_seed_has_safe_randomization_instructions():
    warnings = seed_warnings(definition(0))
    assert len(warnings) == 1
    assert warnings[0]["node_id"] == "1"
    assert warnings[0]["input_name"] == "seed"
    assert warnings[0]["class_type"] == "KSampler"
    assert warnings[0]["value"] == 0
    assert warnings[0]["code"] == "fixed_seed"
    assert warnings[0]["action"] == "bind_seed_source"
    assert "随机种子已锁定" in warnings[0]["message"]
    assert "“种子”来源" in warnings[0]["message"]
    assert "原始 seed 字段不接受 -1" in warnings[0]["message"]


@pytest.mark.parametrize(
    "kind,field",
    [
        ("KSamplerAdvanced", "noise_seed"),
        ("RandomNoise", "noise_seed"),
        ("CustomSampler", "random_seed"),
    ],
)
def test_only_explicit_seed_field_names_are_identified(kind, field):
    warnings = seed_warnings(definition(42, kind=kind, name=field))
    assert len(warnings) == 1 and warnings[0]["input_name"] == field
    assert seed_warnings(definition(42, kind=kind, name="seedling_count")) == []


def test_selected_output_scope_excludes_debug_and_ui_canvas_nodes():
    value = definition(1)
    value["api_graph"].update(
        {
            "90": {"class_type": "KSampler", "inputs": {"seed": 90}},
            "91": {"class_type": "SaveImage", "inputs": {"images": ["90", 0]}},
        }
    )
    assert [item["node_id"] for item in seed_warnings(value)] == ["1"]
    value["outputs"] = ["91"]
    assert [item["node_id"] for item in seed_warnings(value)] == ["90"]


def test_unselected_workflow_scans_known_save_pipeline_not_debug_preview():
    value = definition()
    value["outputs"] = []
    value["api_graph"]["90"] = {"class_type": "KSampler", "inputs": {"seed": 90}}
    value["api_graph"]["91"] = {
        "class_type": "PreviewImage",
        "inputs": {"images": ["90", 0]},
    }
    assert [item["node_id"] for item in seed_warnings(value)] == ["1"]
    del value["api_graph"]["2"]
    del value["api_graph"]["91"]
    assert seed_warnings(value) == []


def test_linked_sampler_seed_warns_about_upstream_seed_node_only():
    value = definition(["seed", 0])
    value["api_graph"]["seed"] = {
        "class_type": "Seed (rgthree)",
        "inputs": {"seed": 9223372036854775807},
    }
    warnings = seed_warnings(value)
    assert len(warnings) == 1 and warnings[0]["node_id"] == "seed"
    assert warnings[0]["action"] == "set_fixed_random"
    assert "固定值改为 -1" in warnings[0]["message"]
    assert warnings[0]["value"] == "9223372036854775807"


@pytest.mark.parametrize("sentinel", [-1, -2, -3, "-1", "-2", "-3"])
def test_rgthree_random_increment_decrement_are_not_reported_as_locked(sentinel):
    original = definition(sentinel, kind="Seed (rgthree)")
    before = copy.deepcopy(original)
    assert seed_warnings(original) == []
    assert original == before


@pytest.mark.parametrize("value", [True, False, None, "unresolved_seed", 1.5, -7])
def test_unknown_or_nonintegral_seed_values_are_not_guessed(value):
    assert seed_warnings(definition(value, kind="ThirdPartySampler")) == []


def test_existing_random_seed_binding_uses_schema_then_binding_then_graph_defaults():
    value = bind(definition(123), default=17)
    schema = {
        "friendly_seed": {
            "request_key": "seed_input",
            "default": "-1",
            "label": "随机种子",
        }
    }
    assert seed_warnings(value, schema) == []
    value["bindings"]["seed_input"]["default"] = -1
    assert seed_warnings(value) == []
    schema["friendly_seed"]["default"] = 777
    warning = seed_warnings(value, schema)[0]
    assert warning["value"] == 777
    assert warning["parameter_name"] == "friendly_seed"
    assert warning["action"] == "set_parameter_random"


def test_current_fixed_editor_override_and_updated_default_are_reflected():
    original = definition(123, kind="Seed (rgthree)")
    original["api_graph_json"] = json.dumps(original["api_graph"])
    original["input_overrides"] = [{"node_id": "1", "input_name": "seed", "value": -1}]
    assert seed_warnings(original) == []
    bound = bind(definition(123), source="parameter", default=123)
    assert seed_warnings(bound, {"seed_input": {"default": 777}})[0]["value"] == 777
    bound["bindings"]["seed_input"]["source"] = "seed"
    assert seed_warnings(bound, {"seed_input": {"default": -1}}) == []


def test_native_literal_negative_one_requires_binding_instead_of_claiming_random():
    warning = seed_warnings(definition(-1))[0]
    assert warning["code"] == "seed_requires_binding"
    assert warning["action"] == "bind_seed_source"
    assert "不能直接填 -1" in warning["message"]
    assert "已锁定" not in warning["message"]


def test_custom_named_input_with_explicit_seed_binding_is_recognized():
    value = definition(42, kind="PrimitiveInt", name="value")
    value["bindings"]["rng"] = {
        "source": "seed",
        "type": "number",
        "node_id": "1",
        "input_name": "value",
        "default": 42,
    }
    assert seed_warnings(value)[0]["input_name"] == "value"
    assert seed_warnings(value, {"rng": {"default": -1}}) == []


def test_warning_analysis_preserves_metadata_and_execution_cache_cleanup():
    value = definition(18446744073709551615)
    before = copy.deepcopy(value)
    rendered = import_result(value)
    assert rendered["seed_warnings"][0]["value"] == "18446744073709551615"
    assert rendered["comfyui"]["api_graph"]["1"]["is_changed"] == [123, "old snapshot"]
    assert rendered["comfyui"]["workflow"] == value["workflow"]
    assert (
        json.loads(rendered["comfyui"]["api_graph_json"])["1"]["inputs"]["seed"]
        == 18446744073709551615
    )
    assert value == before
    request = GenerationRequest(mode="text2img", provider_id="comfy", prompt="")
    assert "is_changed" not in prepare_graph(value, request)["1"]
    assert value == before


def test_import_result_accepts_current_schema_in_keyword_or_editor_envelope():
    value = bind(definition(123))
    schema = {"friendly_seed": {"request_key": "seed_input", "default": -1}}
    assert import_result(value, parameters=schema)["seed_warnings"] == []
    assert (
        import_result({"comfyui": value, "parameters": schema})["seed_warnings"] == []
    )
    value["parameters_schema"] = schema
    assert import_result(value)["seed_warnings"] == []


def test_png_import_exposes_notices_without_touching_original_prompt_metadata():
    value = definition(18446744073709551615, kind="Seed (rgthree)")
    info = PngImagePlugin.PngInfo()
    info.add_text("prompt", json.dumps(value["api_graph"]))
    info.add_text("workflow", json.dumps(value["workflow"]))
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(buffer, "PNG", pnginfo=info)
    parsed = import_result(buffer.getvalue())
    assert parsed["seed_warnings"][0]["value"] == "18446744073709551615"
    assert parsed["api_graph"] == value["api_graph"]
    assert import_result(parsed)["seed_warnings"][0]["action"] == "set_fixed_random"


def test_seed_binding_schema_allows_random_sentinel_without_changing_node_bounds(
    monkeypatch,
):
    value = bind(definition(17), default=17)
    value["bindings"]["seed_input"].update(min=0, max=31)
    model = ImageProvider.from_mapping(
        {
            "id": "comfy",
            "kind": "comfyui",
            "models": [
                {
                    "id": "workflow",
                    "comfyui": value,
                    "parameters": {
                        "friendly_seed": {
                            "type": "integer",
                            "request_key": "seed_input",
                            "default": 17,
                            "min": 0,
                            "max": 31,
                        }
                    },
                }
            ],
        }
    ).models[0]
    assert model.parameters["friendly_seed"]["min"] == -1
    assert model.parameters["friendly_seed"]["default"] == 17
    assert model.comfyui["bindings"]["seed_input"]["min"] == 0
    monkeypatch.setattr(
        "astrbot_plugin_image_studio.backend.providers.comfyui.workflows.secrets.randbelow",
        lambda width: width - 1,
    )
    parameters = _parameters_for_model({"friendly_seed": -1}, model)
    request = GenerationRequest(
        mode="text2img", provider_id="comfy", prompt="", parameters=parameters
    )
    assert prepare_graph(model.comfyui, request)["1"]["inputs"]["seed"] == 31


@pytest.mark.parametrize(
    "minimum,maximum", [(5, 17), (0, 4294967295), ("9", "4294967295")]
)
def test_random_seed_uses_declared_nonnegative_integer_range(
    monkeypatch, minimum, maximum
):
    value = bind(definition(17), default=-1)
    value["bindings"]["seed_input"].update(min=minimum, max=maximum)
    widths = []

    def last(width):
        widths.append(width)
        return width - 1

    monkeypatch.setattr(
        "astrbot_plugin_image_studio.backend.providers.comfyui.workflows.secrets.randbelow",
        last,
    )
    request = GenerationRequest(
        mode="text2img", provider_id="comfy", prompt="", parameters={"seed_input": -1}
    )
    assert prepare_graph(value, request)["1"]["inputs"]["seed"] == int(maximum)
    assert widths == [int(maximum) - max(0, int(minimum)) + 1]
    # The binding fallback follows the same bounds when no separate field was supplied.
    request = GenerationRequest(mode="text2img", provider_id="comfy", prompt="")
    assert prepare_graph(value, request)["1"]["inputs"]["seed"] == int(maximum)


def test_rgthree_seed_binding_without_declared_bounds_respects_known_node_limit(
    monkeypatch,
):
    value = bind(definition(17, kind="Seed (rgthree)"), default=-1)
    monkeypatch.setattr(
        "astrbot_plugin_image_studio.backend.providers.comfyui.workflows.secrets.randbelow",
        lambda width: width - 1,
    )
    request = GenerationRequest(
        mode="text2img", provider_id="comfy", prompt="", parameters={"seed_input": -1}
    )
    assert prepare_graph(value, request)["1"]["inputs"]["seed"] == 2**50


def test_random_seed_intersects_all_known_target_limits(monkeypatch):
    value = bind(definition(17), default=-1)
    value["api_graph"]["3"] = {"class_type": "Seed (rgthree)", "inputs": {"seed": 17}}
    value["bindings"]["seed_input"] = {
        "source": "seed",
        "type": "number",
        "min": 100,
        "max": "18446744073709551615",
        "targets": [
            {"node_id": "1", "input_name": "seed"},
            {"node_id": "3", "input_name": "seed"},
        ],
    }
    monkeypatch.setattr(
        "astrbot_plugin_image_studio.backend.providers.comfyui.workflows.secrets.randbelow",
        lambda width: width - 1,
    )
    request = GenerationRequest(
        mode="text2img", provider_id="comfy", prompt="", parameters={"seed_input": -1}
    )
    graph = prepare_graph(value, request)
    assert graph["1"]["inputs"]["seed"] == graph["3"]["inputs"]["seed"] == 2**50


def test_default_plugin_random_range_stays_63_bits_and_invalid_range_is_actionable(
    monkeypatch,
):
    value = bind(definition(17), default=-1)
    monkeypatch.setattr(
        "astrbot_plugin_image_studio.backend.providers.comfyui.workflows.secrets.randbelow",
        lambda width: width - 1,
    )
    request = GenerationRequest(
        mode="text2img", provider_id="comfy", prompt="", parameters={"seed_input": -1}
    )
    assert prepare_graph(value, request)["1"]["inputs"]["seed"] == 2**63 - 1
    value["bindings"]["seed_input"].update(min=100, max=31)
    with pytest.raises(ValueError, match="非负随机种子范围"):
        prepare_graph(value, request)


def test_plain_parameters_and_fixed_negative_values_are_not_made_random():
    value = bind(definition(-2, kind="Seed (rgthree)"), source="parameter", default=-2)
    value["bindings"]["seed_input"].update(min=-3, max=2**50)
    model = ImageProvider.from_mapping(
        {
            "id": "comfy",
            "kind": "comfyui",
            "models": [
                {
                    "id": "workflow",
                    "comfyui": value,
                    "parameters": {
                        "seed_input": {"type": "integer", "default": -2, "min": -3}
                    },
                }
            ],
        }
    ).models[0]
    assert model.parameters["seed_input"]["min"] == -3
    request = GenerationRequest(
        mode="text2img", provider_id="comfy", prompt="", parameters={"seed_input": -2}
    )
    assert prepare_graph(model.comfyui, request)["1"]["inputs"]["seed"] == -2
    assert prepare_graph(definition(-1), request)["1"]["inputs"]["seed"] == -1
    plain = bind(definition(17), source="parameter", default=17)
    plain["bindings"]["seed_input"].update(min=0, max=31)
    plain_model = ImageProvider.from_mapping(
        {
            "id": "comfy",
            "kind": "comfyui",
            "models": [
                {
                    "id": "workflow",
                    "comfyui": plain,
                    "parameters": {
                        "seed_input": {"type": "integer", "default": 17, "min": 0}
                    },
                }
            ],
        }
    ).models[0]
    assert plain_model.parameters["seed_input"]["min"] == 0
