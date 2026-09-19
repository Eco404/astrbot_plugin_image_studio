"""A global-seed hook is authorized by verified metadata, never seed-like values."""

from __future__ import annotations

import copy

import pytest

from astrbot_plugin_image_studio.backend.providers.comfyui.global_seed import (
    GLOBAL_SEED_TOKEN,
    MAX_GLOBAL_SEED,
    global_seed_workflow_writeback,
    permitted_global_seed_changes,
    prepare_global_seed,
)


def fixture(action="randomize", mode=True):
    graph = {
        "1": {
            "class_type": "easy globalSeed",
            "inputs": {"value": 12, "mode": mode, "action": action, "last_seed": ""},
        },
        "2": {"class_type": "KSampler", "inputs": {"seed": 33, "steps": 20}},
        "3": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": "seed=" + GLOBAL_SEED_TOKEN},
        },
    }
    workflow = {
        "nodes": [
            {
                "id": 1,
                "type": "easy globalSeed",
                "widgets_values": [12, mode, action, "previous"],
            },
            {
                "id": 2,
                "type": "KSampler",
                "widgets_values": [33, "fixed", 20, 7, "euler", "normal", 1],
            },
            {
                "id": 3,
                "type": "CLIPTextEncode",
                "widgets_values": ["seed=" + GLOBAL_SEED_TOKEN],
            },
        ],
        "links": [],
        "extra": {"unchanged": True},
    }
    return graph, workflow


def returned(graph, value):
    result = copy.deepcopy(graph)
    result["1"]["inputs"]["value"] = value
    result["2"]["inputs"]["seed"] = value
    result["3"]["inputs"]["text"] = "seed=" + str(value)
    return result


def test_missing_map_is_built_without_randomizing_or_mutating_inputs():
    graph, workflow = fixture()
    original = copy.deepcopy((graph, workflow))
    result = prepare_global_seed({}, graph, workflow)
    assert result["warnings"] == []
    assert result["workflow"]["seed_widgets"] == {"2": 0}
    assert result["policy"]["seed_targets"] == [("2", "seed")]
    assert result["policy"]["token_targets"] == [("3", "text")]
    assert (graph, workflow) == original
    assert result["workflow"]["nodes"] == workflow["nodes"]


def test_existing_verified_map_is_preserved_including_explicit_omissions():
    graph, workflow = fixture()
    workflow["seed_widgets"] = {}
    result = prepare_global_seed({}, graph, workflow)
    assert result["warnings"] == []
    assert result["policy"]["seed_targets"] == []
    assert result["workflow"] == workflow


@pytest.mark.parametrize("existing", [False, True])
def test_unverified_seed_node_prevents_partial_map_activation(existing):
    graph, workflow = fixture()
    graph["4"] = {"class_type": "UnknownSeed", "inputs": {"seed": 33}}
    workflow["nodes"].append({"id": 4, "type": "UnknownSeed", "widgets_values": [33]})
    if existing:
        workflow["seed_widgets"] = {"2": 0, "4": 0}
    result = prepare_global_seed({}, graph, workflow)
    assert result["policy"] is None
    assert result["warnings"]
    assert result["workflow"] == workflow


def test_unknown_integer_fields_never_become_seed_targets():
    graph, workflow = fixture()
    graph["4"] = {"class_type": "UnknownNode", "inputs": {"value": 33, "steps": 30}}
    result = prepare_global_seed({}, graph, workflow)
    assert result["warnings"] == []
    assert result["workflow"]["seed_widgets"] == {"2": 0}


@pytest.mark.parametrize("mapping", [{"2": 1}, {"2": True}, {"999": 0}, [], {2: 0}])
def test_incorrect_seed_map_is_not_trusted(mapping):
    graph, workflow = fixture()
    workflow["seed_widgets"] = mapping
    result = prepare_global_seed({}, graph, workflow)
    assert result["policy"] is None
    assert result["warnings"]
    assert result["workflow"] == workflow


def test_connected_or_virtual_seed_inputs_are_not_rewritten_by_inferred_map():
    graph, workflow = fixture()
    workflow["nodes"][1]["inputs"] = [
        {"name": "seed", "widget": {"name": "seed"}, "link": 42}
    ]
    result = prepare_global_seed({}, graph, workflow)
    assert result["policy"] is None
    assert "seed_widgets" not in result["workflow"]
    assert result["warnings"]


@pytest.mark.parametrize(
    "action,mode",
    [
        ("increment", True),
        ("decrement", False),
        ("randomize for each node", True),
        ("randomize", False),
        ("unknown", True),
    ],
)
def test_unsupported_stateful_policies_report_precise_boundary(action, mode):
    graph, workflow = fixture(action, mode)
    result = prepare_global_seed({}, graph, workflow)
    assert result["policy"] is None
    assert result["warnings"]
    assert result["workflow"] == workflow


def test_absent_ui_and_multiple_global_nodes_warn_even_without_written_inputs():
    graph, workflow = fixture()
    assert prepare_global_seed({}, graph, None)["warnings"]
    graph["4"] = copy.deepcopy(graph["1"])
    result = prepare_global_seed({}, graph, workflow)
    assert "多个全局种子" in result["warnings"][0]


@pytest.mark.parametrize("actual", [0, 1, MAX_GLOBAL_SEED])
def test_only_exact_hook_graph_changes_are_accepted(actual):
    graph, workflow = fixture()
    workflow = prepare_global_seed({}, graph, workflow)["workflow"]
    result = returned(graph, actual)
    assert permitted_global_seed_changes(graph, result, workflow) == {
        ("1", "value"),
        ("2", "seed"),
        ("3", "text"),
    }
    ui = global_seed_workflow_writeback(graph, result, workflow)
    assert ui["nodes"][0]["widgets_values"] == [actual, True, "randomize", 12]
    assert ui["nodes"][1]["widgets_values"][0] == actual
    assert ui["nodes"][2] == workflow["nodes"][2]
    assert ui["extra"] == workflow["extra"]


@pytest.mark.parametrize("mode", [False, True])
def test_fixed_global_seed_must_match_submitted_value(mode):
    graph, workflow = fixture("fixed", mode)
    workflow = prepare_global_seed({}, graph, workflow)["workflow"]
    assert permitted_global_seed_changes(graph, returned(graph, 12), workflow)
    assert permitted_global_seed_changes(graph, returned(graph, 13), workflow) is None


@pytest.mark.parametrize("actual", [-1, MAX_GLOBAL_SEED + 1, True, 1.5, "1"])
def test_random_returned_global_seed_must_be_exact_integer_in_range(actual):
    graph, workflow = fixture()
    workflow = prepare_global_seed({}, graph, workflow)["workflow"]
    assert (
        permitted_global_seed_changes(graph, returned(graph, actual), workflow) is None
    )


@pytest.mark.parametrize("change", ["seed", "prompt", "steps", "class", "extra_node"])
def test_unrelated_or_inconsistent_graph_mutations_are_rejected(change):
    graph, workflow = fixture()
    workflow = prepare_global_seed({}, graph, workflow)["workflow"]
    result = returned(graph, 123)
    if change == "seed":
        result["2"]["inputs"]["seed"] = 124
    elif change == "prompt":
        result["3"]["inputs"]["text"] = "unverified changed text"
    elif change == "steps":
        result["2"]["inputs"]["steps"] = 21
    elif change == "class":
        result["2"]["class_type"] = "OtherSampler"
    else:
        result["4"] = copy.deepcopy(graph["3"])
    assert permitted_global_seed_changes(graph, result, workflow) is None
    assert global_seed_workflow_writeback(graph, result, workflow) is None


def test_missing_map_does_not_make_returned_random_values_trustworthy():
    graph, workflow = fixture()
    assert permitted_global_seed_changes(graph, returned(graph, 123), workflow) is None


@pytest.mark.parametrize("bad", [True, 1.0, "1"])
def test_mapped_returned_seed_cannot_use_python_equal_noninteger_types(bad):
    graph, workflow = fixture()
    workflow = prepare_global_seed({}, graph, workflow)["workflow"]
    result = returned(graph, 1)
    result["2"]["inputs"]["seed"] = bad
    assert permitted_global_seed_changes(graph, result, workflow) is None


def test_unrelated_workflows_keep_normal_noop_behavior():
    graph = {"1": {"class_type": "CLIPTextEncode", "inputs": {"text": "unchanged"}}}
    assert prepare_global_seed({}, graph, None) == {
        "workflow": None,
        "warnings": [],
        "policy": None,
    }
    assert permitted_global_seed_changes(graph, graph, None) == set()
    changed = copy.deepcopy(graph)
    changed["1"]["inputs"]["text"] = "different"
    assert permitted_global_seed_changes(graph, changed, None) is None
