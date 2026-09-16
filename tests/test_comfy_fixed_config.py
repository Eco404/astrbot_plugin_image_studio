"""Configuration-only migration to fixed ComfyUI output collection rounds."""

from __future__ import annotations

import copy

import pytest
from astrbot_plugin_image_studio.backend.providers.comfyui.workflows import (
    FIXED_OUTPUT_POLICY,
    inspect_workflow,
    migrate_fixed_outputs,
    normalize_workflow,
    prepare_graph,
)
from astrbot_plugin_image_studio.backend.models import GenerationRequest, ImageProvider


def definition(key="count", *, source="count", value=4):
    return {
        "api_graph": {
            "1": {
                "class_type": "EmptyLatentImage",
                "inputs": {"width": 512, "height": 512, "batch_size": value},
            },
            "2": {
                "class_type": "SaveImage",
                "inputs": {"images": ["1", 0], "filename_prefix": "test"},
            },
        },
        "bindings": {
            key: {
                "source": source,
                "type": "number",
                "node_id": "1",
                "input_name": "batch_size",
            }
        },
        "outputs": ["2"],
    }


def configured(config, parameters=None, *, batch=1, migrate=True, tool=None):
    return ImageProvider.from_mapping(
        {
            "id": "comfy",
            "kind": "comfyui",
            "models": [
                {
                    "id": "workflow",
                    "comfyui": config,
                    "parameters": parameters,
                    "native_batch_size": batch,
                    "native_batch_size_source": "default",
                    "tool": tool or {},
                }
            ],
        },
        migrate_comfyui=migrate,
    ).models[0]


def test_old_count_control_becomes_parameter_and_total_count_starts_at_one():
    value = definition()
    original = copy.deepcopy(value)
    migrated = migrate_fixed_outputs(
        value, {"count": {"type": "integer", "default": 3, "request_key": "count"}}
    )
    assert migrated["comfyui"]["execution_policy"] == FIXED_OUTPUT_POLICY
    assert migrated["comfyui"]["api_graph"] == original["api_graph"]
    assert value == original
    assert migrated["comfyui"]["bindings"]["workflow_count"]["source"] == "parameter"
    assert migrated["parameters"]["workflow_count"]["default"] == 3
    assert migrated["parameters"]["workflow_count"]["request_key"] == "workflow_count"
    assert migrated["parameters"]["count"]["default"] == 1
    assert migrated["parameters"]["count"]["refill_from_history"] is False


def test_schema_alias_count_default_is_not_used_as_plugin_total_default():
    migrated = migrate_fixed_outputs(
        definition("node_batch"),
        {
            "node_batch": {
                "type": "number",
                "default": 5,
                "request_key": "count",
                "label": "Latent batch",
            },
        },
    )
    assert migrated["parameters"]["count"]["default"] == 1
    assert migrated["parameters"]["node_batch"]["default"] == 5
    assert migrated["parameters"]["node_batch"]["request_key"] == "node_batch"


def test_existing_independent_total_and_tool_policy_are_preserved():
    migrated = migrate_fixed_outputs(
        definition("node_batch"),
        {
            "node_batch": {"default": 4, "request_key": "count"},
            "count": {
                "default": 7,
                "max": 12,
                "request_key": "count",
                "description": "我的总量说明",
            },
        },
        {
            "parameters": {
                "node_batch": {"default_override": 5},
                "count": {"exposed": False, "default_override": 8},
            }
        },
    )
    assert migrated["parameters"]["count"]["default"] == 7
    assert migrated["parameters"]["count"]["max"] == 12
    assert migrated["parameters"]["count"]["description"] == "我的总量说明"
    assert migrated["tool"]["parameters"]["count"] == {
        "exposed": False,
        "default_override": 8,
    }
    assert migrated["tool"]["parameters"]["node_batch"]["default_override"] == 5


def test_legacy_tool_count_default_moves_to_node_parameter_only():
    migrated = migrate_fixed_outputs(
        definition(),
        {"count": {"default": 2}},
        {
            "selection_description": "特殊工作流",
            "prompt_profile": "nai_tags",
            "prompt_instructions": "保留标签",
            "parameters": {"count": {"exposed": False, "default_override": 6}},
        },
    )
    assert migrated["tool"]["parameters"]["workflow_count"] == {
        "exposed": False,
        "default_override": 6,
    }
    assert migrated["tool"]["parameters"]["count"] == {"exposed": True}
    assert migrated["tool"]["selection_description"] == "特殊工作流"
    assert migrated["tool"]["prompt_profile"] == "nai_tags"
    assert migrated["tool"]["prompt_instructions"] == "保留标签"


def test_reserved_n_and_name_collisions_get_independent_nonreserved_keys():
    value = definition("n", source="parameter")
    migrated = migrate_fixed_outputs(
        value, {"n": {"default": 4}, "workflow_count": {"default": 99}}
    )
    assert "workflow_count_2" in migrated["comfyui"]["bindings"]
    assert migrated["parameters"]["workflow_count"]["default"] == 99
    assert migrated["parameters"]["workflow_count_2"]["default"] == 4
    assert migrated["parameters"]["count"]["default"] == 1


def test_missing_schema_keeps_original_graph_batch_value():
    migrated = migrate_fixed_outputs(definition(value=6))
    assert migrated["parameters"]["workflow_count"]["default"] == 6
    request = GenerationRequest(
        mode="text2img", provider_id="comfy", prompt="", count=16
    )
    assert prepare_graph(migrated["comfyui"], request)["1"]["inputs"]["batch_size"] == 6


def test_graph_values_take_precedence_only_for_history_imports():
    schema = {"count": {"default": 1, "refill_from_history": False}}
    regular = migrate_fixed_outputs(definition(value=2), schema)
    history = migrate_fixed_outputs(
        definition(value=2), schema, prefer_graph_values=True
    )
    assert regular["parameters"]["workflow_count"]["default"] == 1
    assert history["parameters"]["workflow_count"]["default"] == 2
    assert history["parameters"]["count"]["default"] == 1


def test_differing_legacy_multitarget_values_split_without_overwriting_any_target():
    value = definition(value=2)
    value["api_graph"]["3"] = {
        "class_type": "EmptyLatentImage",
        "inputs": {"batch_size": 5},
    }
    value["bindings"]["count"] = {
        "source": "count",
        "type": "number",
        "targets": [
            {"node_id": "1", "input_name": "batch_size"},
            {"node_id": "3", "input_name": "batch_size"},
        ],
    }
    migrated = migrate_fixed_outputs(
        value, {"count": {"default": 1}}, prefer_graph_values=True
    )
    bindings = migrated["comfyui"]["bindings"]
    assert len(bindings) == 2
    request = GenerationRequest(
        mode="text2img", provider_id="comfy", prompt="", count=16
    )
    graph = prepare_graph(migrated["comfyui"], request)
    assert graph["1"]["inputs"]["batch_size"] == 2
    assert graph["3"]["inputs"]["batch_size"] == 5


def test_migration_is_idempotent_including_saved_schema_and_tool_fields():
    first = migrate_fixed_outputs(
        definition(),
        {"count": {"default": 3}},
        {"parameters": {"count": {"default_override": 2}}},
    )
    first["comfyui"]["parameters_schema"] = copy.deepcopy(first["parameters"])
    second = migrate_fixed_outputs(first["comfyui"], first["parameters"], first["tool"])
    assert second == first


def test_structure_normalization_preserves_legacy_revisions_and_unknown_policy_fails():
    legacy = normalize_workflow(definition())
    assert "execution_policy" not in legacy
    request = GenerationRequest(
        mode="text2img", provider_id="comfy", prompt="", count=7
    )
    assert prepare_graph(legacy, request)["1"]["inputs"]["batch_size"] == 7
    with pytest.raises(ValueError, match="执行策略"):
        normalize_workflow({**definition(), "execution_policy": "future_unknown"})
    with pytest.raises(ValueError, match="插件总量"):
        prepare_graph(
            {**definition(), "execution_policy": FIXED_OUTPUT_POLICY}, request
        )


def test_batch_size_suggestions_are_plain_parameters():
    value = definition()
    value["bindings"] = {}
    assert (
        inspect_workflow(value)["suggested_bindings"]["node_1_batch_size"]["source"]
        == "parameter"
    )


def test_default_model_loading_migrates_but_frozen_old_task_loading_does_not():
    old = configured(definition(), {"count": {"default": 4}}, migrate=False)
    new = configured(definition(), {"count": {"default": 4}})
    assert old.comfyui["bindings"]["count"]["source"] == "count"
    assert "execution_policy" not in old.comfyui
    assert old.parameters["count"]["default"] == 4
    assert new.comfyui["execution_policy"] == FIXED_OUTPUT_POLICY
    assert new.parameters["count"]["default"] == 1
    assert new.parameters["workflow_count"]["default"] == 4


def test_user_filled_round_size_is_kept_without_count_binding():
    value = definition()
    value["bindings"] = {}
    assert configured(value, batch=4).native_batch_size == 4
    assert configured(value, batch=4, migrate=False).native_batch_size == 1
    fixed = migrate_fixed_outputs(value)["comfyui"]
    assert configured(fixed, batch=4, migrate=False).native_batch_size == 4


def test_comfy_total_description_changes_without_touching_other_provider_defaults():
    model = configured(definition())
    assert "超量截断" in model.parameters["count"]["description"]
    assert "不自动补齐" in model.parameters["count"]["description"]
    ordinary = ImageProvider.from_mapping(
        {"id": "image", "kind": "openai_images", "models": [{"id": "test"}]}
    ).models[0]
    assert (
        "按模型原生批次上限自动拆分请求" in ordinary.parameters["count"]["description"]
    )
    assert "超量截断" not in ordinary.parameters["count"]["description"]


def test_shared_legacy_count_schema_does_not_overwrite_previous_migrated_field():
    value = definition("foo")
    value["api_graph"]["3"] = {
        "class_type": "EmptyLatentImage",
        "inputs": {"batch_size": 4},
    }
    value["bindings"]["bar"] = {
        "source": "count",
        "type": "number",
        "node_id": "3",
        "input_name": "batch_size",
    }
    migrated = migrate_fixed_outputs(
        value,
        {"foo": {"default": 6, "request_key": "count"}},
        {"parameters": {"foo": {"default_override": 7}}},
    )
    assert migrated["parameters"]["foo"]["request_key"] == "foo"
    assert migrated["parameters"]["bar"]["request_key"] == "bar"
    assert (
        migrated["parameters"]["foo"]["default"]
        == migrated["parameters"]["bar"]["default"]
        == 6
    )
    assert migrated["tool"]["parameters"]["foo"]["default_override"] == 7
    assert migrated["tool"]["parameters"]["bar"]["default_override"] == 7
    request = GenerationRequest(
        mode="text2img",
        provider_id="comfy",
        prompt="",
        count=16,
        parameters={"foo": 2, "bar": 5},
    )
    graph = prepare_graph(migrated["comfyui"], request)
    assert graph["1"]["inputs"]["batch_size"] == 2
    assert graph["3"]["inputs"]["batch_size"] == 5


def test_long_public_key_collision_stays_within_parameter_name_limit():
    key = "x" * 64
    value = definition("foo")
    value["api_graph"]["3"] = {
        "class_type": "EmptyLatentImage",
        "inputs": {"batch_size": 4},
    }
    value["bindings"][key] = {
        "source": "parameter",
        "type": "number",
        "node_id": "3",
        "input_name": "batch_size",
    }
    migrated = migrate_fixed_outputs(value, {key: {"default": 6, "request_key": "foo"}})
    assert len(migrated["parameters"]) == 3
    assert all(len(name) <= 64 for name in migrated["parameters"])
    assert {item["request_key"] for item in migrated["parameters"].values()} == {
        "foo",
        key,
        "count",
    }
    assert (
        migrate_fixed_outputs(
            migrated["comfyui"], migrated["parameters"], migrated["tool"]
        )
        == migrated
    )
