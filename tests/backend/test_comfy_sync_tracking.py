"""Persist fixed-edit provenance and capture only inputs actually written at runtime."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from astrbot_plugin_image_studio.backend.providers.comfyui.workflows import (
    migrate_fixed_outputs,
    normalize_workflow,
    prepare_graph,
)


def definition():
    return {
        "api_graph": {
            "1": {"class_type": "CLIPTextEncode", "inputs": {"text": "original"}},
            "2": {"class_type": "Seed (rgthree)", "inputs": {"seed": 123}},
            "3": {
                "class_type": "SaveImage",
                "inputs": {"images": ["1", 0], "filename_prefix": "original"},
            },
            "4": {"class_type": "LoadImage", "inputs": {"image": "old.png"}},
        },
        "workflow": {
            "nodes": [
                {"id": 1, "type": "CLIPTextEncode", "widgets_values": ["original"]},
                {
                    "id": 2,
                    "type": "Seed (rgthree)",
                    "widgets_values": [123, "", "", ""],
                },
            ],
            "links": [],
        },
        "outputs": ["3"],
        "bindings": {},
    }


def request(**values):
    return SimpleNamespace(
        prompt="new text", negative_prompt="", parameters={}, **values
    )


def binding(node_id, input_name, **values):
    return {
        "targets": [{"node_id": node_id, "input_name": input_name}],
        "source": "parameter",
        "type": "text",
        **values,
    }


def test_fixed_edits_survive_normalization_migration_and_json_roundtrips():
    original = definition()
    config = {
        **original,
        "input_overrides": [{"node_id": "2", "input_name": "seed", "value": -1}],
    }
    result = normalize_workflow(config)
    targets = [{"node_id": "2", "input_name": "seed", "class_type": "Seed (rgthree)"}]
    assert result["workflow_sync_targets"] == targets
    assert "input_overrides" not in result
    result = migrate_fixed_outputs(json.loads(json.dumps(result)))["comfyui"]
    assert normalize_workflow(result)["workflow_sync_targets"] == targets
    assert result["api_graph"]["2"]["inputs"]["seed"] == -1
    assert result["workflow"] == original["workflow"]
    assert original["api_graph"]["2"]["inputs"]["seed"] == 123


def test_targets_merge_without_replaying_older_values_or_rounding_uint64():
    config = definition()
    config["input_overrides"] = [
        {"node_id": "2", "input_name": "seed", "value": "18446744073709551615"}
    ]
    saved = normalize_workflow(config)
    saved["input_overrides"] = [
        {"node_id": "2", "input_name": "seed", "value": "18446744073709551614"},
        {"node_id": "1", "input_name": "text", "value": "edited"},
    ]
    result = normalize_workflow(saved)
    assert len(result["workflow_sync_targets"]) == 2
    assert result["api_graph"]["2"]["inputs"]["seed"] == 18446744073709551614
    returned = copy.deepcopy(result)
    returned["api_graph"]["2"]["inputs"]["seed"] = 456
    returned["api_graph_json"] = json.dumps(returned["api_graph"])
    assert normalize_workflow(returned)["api_graph"]["2"]["inputs"]["seed"] == 456


@pytest.mark.parametrize(
    "targets",
    [
        "bad",
        [None],
        [{"node_id": "missing", "input_name": "seed"}],
        [{"node_id": "3", "input_name": "images"}],
        [{"node_id": "2", "input_name": "seed", "class_type": "OtherSeed"}],
    ],
)
def test_invalid_or_rewired_sync_targets_fail_explicitly(targets):
    with pytest.raises(ValueError, match="界面工作流同步目标"):
        normalize_workflow({**definition(), "workflow_sync_targets": targets})


def test_import_does_not_infer_edits_from_mismatching_canvas_values():
    config = definition()
    config["workflow"]["nodes"][0]["widgets_values"] = ["newer display"]
    assert "workflow_sync_targets" not in normalize_workflow(config)


def test_runtime_records_bound_prompt_and_final_reference_names_only():
    config = definition()
    config["bindings"] = {
        "positive": binding("1", "text", source="prompt"),
        "ref": binding("4", "image", source="reference", type="image"),
        "optional": binding("3", "filename_prefix"),
    }
    written = set()
    graph = prepare_graph(
        config, request(), ["uploaded/new.png"], written_inputs=written
    )
    assert written == {("1", "text"), ("4", "image")}
    assert graph["1"]["inputs"]["text"] == "new text"
    assert graph["4"]["inputs"]["image"] == "uploaded/new.png"
    assert graph["3"]["inputs"]["filename_prefix"] == "original"


def test_empty_append_is_not_written_but_explicit_default_is():
    config = definition()
    config["bindings"] = {
        "append": binding("1", "text", mode="append", default=""),
        "default": binding("3", "filename_prefix", default="original"),
    }
    written = set()
    prepare_graph(config, request(), written_inputs=written)
    assert written == {("3", "filename_prefix")}


def test_random_seed_records_final_value_without_mutating_template(monkeypatch):
    config = definition()
    config["bindings"] = {
        "rng": binding("2", "seed", type="number", source="seed", default=-1)
    }
    monkeypatch.setattr(
        "astrbot_plugin_image_studio.backend.providers.comfyui.workflows.secrets.randbelow",
        lambda upper: 789,
    )
    written = set()
    graph = prepare_graph(config, request(), written_inputs=written)
    assert graph["2"]["inputs"]["seed"] == 789
    assert written == {("2", "seed")}
    assert config["api_graph"]["2"]["inputs"]["seed"] == 123


def test_invalid_request_does_not_publish_partial_write_targets():
    config = definition()
    config["bindings"] = {"positive": binding("1", "text", source="prompt")}
    written = set()
    with pytest.raises(ValueError, match="没有绑定这些参考图"):
        prepare_graph(config, request(), ["unused.png"], written_inputs=written)
    assert not written


@pytest.mark.parametrize("seed", [-1, -2, -3])
def test_legacy_rgthree_random_mode_declares_only_its_writeback_target(seed):
    config = definition()
    config["api_graph"]["2"]["inputs"]["seed"] = seed
    config["api_graph"]["1"]["inputs"]["text"] = "untracked old edit"
    written = set()
    prepare_graph(config, request(), written_inputs=written)
    assert written == {("2", "seed")}
