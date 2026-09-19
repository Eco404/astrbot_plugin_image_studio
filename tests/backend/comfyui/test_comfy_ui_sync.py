"""Only explicit plugin writes may alter a submitted UI workflow copy."""

from __future__ import annotations

import copy
import json

import pytest

from astrbot_plugin_image_studio.backend.providers.comfyui.ui_sync import (
    read_widget_values,
    synchronize_workflow,
    widget_slot,
)


def fixture():
    graph = {
        "1": {"class_type": "CLIPTextEncode", "inputs": {"text": "old prompt"}},
        "2": {"class_type": "Seed (rgthree)", "inputs": {"seed": 123}},
        "3": {
            "class_type": "easy showAnything",
            "inputs": {"anything": ["1", 0], "text": "previous run"},
        },
        "4": {"class_type": "LoadImage", "inputs": {"image": "old.png"}},
        "5": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": "old.safetensors"},
        },
    }
    ui = {
        "nodes": [
            {"id": 1, "type": "CLIPTextEncode", "widgets_values": ["old prompt"]},
            {"id": 2, "type": "Seed (rgthree)", "widgets_values": [123, "", "", ""]},
            {
                "id": 3,
                "type": "easy showAnything",
                "widgets_values": [["current runtime snapshot"]],
            },
            {"id": 4, "type": "LoadImage", "widgets_values": ["old.png", "image"]},
            {
                "id": 5,
                "type": "CheckpointLoaderSimple",
                "widgets_values": ["old.safetensors"],
            },
        ],
        "groups": [{"title": "keep", "bounding": [1, 2, 3, 4]}],
        "extra": {"ds": {"scale": 0.75, "offset": [100, 200]}},
        "links": [],
    }
    for node in ui["nodes"]:
        node.update(inputs=[], pos=[100 * node["id"], 120], properties={"keep": True})
    return {"api_graph": graph, "workflow": ui}


def node(result, node_id):
    return next(n for n in result["workflow"]["nodes"] if str(n["id"]) == str(node_id))


def test_final_values_from_fixed_and_runtime_writes_are_synced_without_mutation():
    config = fixture()
    config["workflow_sync_targets"] = [
        {"node_id": "2", "input_name": "seed", "class_type": "Seed (rgthree)"},
        {
            "node_id": "5",
            "input_name": "ckpt_name",
            "class_type": "CheckpointLoaderSimple",
        },
    ]
    graph = copy.deepcopy(config["api_graph"])
    graph["1"]["inputs"]["text"] = "this request's prompt"
    graph["2"]["inputs"]["seed"] = -1
    graph["4"]["inputs"]["image"] = "image_studio/uploaded.png [input]"
    graph["5"]["inputs"]["ckpt_name"] = "replacement.safetensors"
    before_config, before_graph = copy.deepcopy(config), copy.deepcopy(graph)
    result = synchronize_workflow(config, graph, {("1", "text"), ("4", "image")})
    assert result["warnings"] == []
    assert node(result, 1)["widgets_values"] == ["this request's prompt"]
    assert node(result, 2)["widgets_values"] == [-1, "", "", ""]
    assert node(result, 4)["widgets_values"] == [
        "image_studio/uploaded.png [input]",
        "image",
    ]
    assert node(result, 5)["widgets_values"] == ["replacement.safetensors"]
    expected = copy.deepcopy(config["workflow"])
    for changed in result["workflow"]["nodes"]:
        if changed["id"] in {1, 2, 4, 5}:
            expected["nodes"][changed["id"] - 1]["widgets_values"] = changed[
                "widgets_values"
            ]
    assert result["workflow"] == expected
    assert node(result, 3)["widgets_values"] == [["current runtime snapshot"]]
    assert config == before_config and graph == before_graph


def test_untargeted_api_values_never_refresh_saved_ui_values():
    config = fixture()
    graph = copy.deepcopy(config["api_graph"])
    graph["1"]["inputs"]["text"] = "different but not a recorded write"
    assert synchronize_workflow(config, graph) == {
        "workflow": config["workflow"],
        "warnings": [],
    }


@pytest.mark.parametrize("value", [-1, -2, -3, 0, 987654321123456])
def test_rgthree_sentinels_and_concrete_seed_remain_exact(value):
    config = fixture()
    graph = copy.deepcopy(config["api_graph"])
    graph["2"]["inputs"]["seed"] = value
    result = synchronize_workflow(config, graph, [("2", "seed")])
    assert result["warnings"] == []
    assert node(result, 2)["widgets_values"] == [value, "", "", ""]


def test_core_sampler_uint64_seed_preserves_controls_and_integer_precision():
    value = 18446744073709551615
    graph = {"1": {"class_type": "KSampler", "inputs": {"seed": value, "cfg": 4.5}}}
    config = {
        "workflow": {
            "nodes": [
                {
                    "id": 1,
                    "type": "KSampler",
                    "widgets_values": [7, "randomize", 25, 7, "euler", "normal", 1],
                }
            ]
        }
    }
    result = synchronize_workflow(config, graph, [("1", "seed"), ("1", "cfg")])
    assert result["warnings"] == []
    assert node(result, 1)["widgets_values"] == [
        value,
        "randomize",
        25,
        4.5,
        "euler",
        "normal",
        1,
    ]
    assert (
        json.loads(json.dumps(result))["workflow"]["nodes"][0]["widgets_values"][0]
        == value
    )


def test_exact_workflow_json_takes_priority_over_rounded_browser_preview():
    config = fixture()
    exact = 18446744073709551615
    config["workflow"]["nodes"][1]["widgets_values"][0] = exact
    config["workflow_json"] = json.dumps(config["workflow"])
    config["workflow"]["nodes"][1]["widgets_values"][0] = 18446744073709552000
    result = synchronize_workflow(config, config["api_graph"], [("1", "text")])
    assert node(result, 2)["widgets_values"][0] == exact


def test_explicit_named_widget_values_support_unknown_node_without_order_guessing():
    config = {
        "workflow": {
            "nodes": [
                {
                    "id": 1,
                    "type": "ThirdPartyText",
                    "widgets_values": {"text": "old", "other": "keep"},
                }
            ]
        }
    }
    graph = {"1": {"class_type": "ThirdPartyText", "inputs": {"text": "new"}}}
    result = synchronize_workflow(config, graph, [("1", "text")])
    assert result["warnings"] == []
    assert node(result, 1)["widgets_values"] == {"text": "new", "other": "keep"}


@pytest.mark.parametrize(
    ("kind", "field", "widgets"),
    [
        ("MysteryText", "text", ["same input and widget value"]),
        ("Seed (rgthree)", "seed", [123, "unknown control"]),
        ("KSampler", "seed", [7, "randomize", 25]),
        ("LoadImage", "image", ["old.png", "unexpected", "buttons"]),
        ("easy showAnything", "text", [["current display"]]),
        ("CLIPTextEncode", "unexpected", ["prompt"]),
    ],
)
def test_unknown_and_changed_layouts_warn_instead_of_guessing(kind, field, widgets):
    config = {
        "workflow": {"nodes": [{"id": 1, "type": kind, "widgets_values": widgets}]}
    }
    graph = {"1": {"class_type": kind, "inputs": {field: "new"}}}
    result = synchronize_workflow(config, graph, [("1", field)])
    assert result["workflow"] == config["workflow"]
    assert len(result["warnings"]) == 1
    assert "未同步到界面工作流" in result["warnings"][0]


@pytest.mark.parametrize(
    "in_port,in_table", [(True, False), (False, True), (True, True)]
)
def test_linked_converted_widgets_and_virtual_primitives_are_not_silently_overridden(
    in_port, in_table
):
    config = fixture()
    config["workflow"]["nodes"][0]["inputs"] = [
        {
            "name": "text",
            "type": "STRING",
            "widget": {"name": "text"},
            "link": 50 if in_port else None,
        }
    ]
    if in_table:
        config["workflow"]["links"] = [[50, 90, 0, 1, 0, "STRING"]]
    graph = copy.deepcopy(config["api_graph"])
    graph["1"]["inputs"]["text"] = "API literal produced by virtual primitive"
    result = synchronize_workflow(config, graph, [("1", "text")])
    assert node(result, 1) == config["workflow"]["nodes"][0]
    assert len(result["warnings"]) == 1
    assert "连线" in result["warnings"][0]


def test_unconnected_converted_widget_can_be_updated_using_verified_layout():
    config = fixture()
    config["workflow"]["nodes"][0]["inputs"] = [
        {"name": "text", "widget": {"name": "text"}, "link": None}
    ]
    graph = copy.deepcopy(config["api_graph"])
    graph["1"]["inputs"]["text"] = "new"
    result = synchronize_workflow(config, graph, [("1", "text")])
    assert node(result, 1)["widgets_values"] == ["new"]
    assert result["warnings"] == []


@pytest.mark.parametrize(
    "issue", ["missing", "duplicate", "type", "stale_target", "link"]
)
def test_node_identity_and_literal_input_are_required(issue):
    config = fixture()
    graph = copy.deepcopy(config["api_graph"])
    graph["1"]["inputs"]["text"] = "new"
    if issue == "missing":
        config["workflow"]["nodes"].pop(0)
    elif issue == "duplicate":
        config["workflow"]["nodes"].append(
            copy.deepcopy(config["workflow"]["nodes"][0])
        )
    elif issue == "type":
        config["workflow"]["nodes"][0]["type"] = "OtherNode"
    elif issue == "stale_target":
        config["workflow_sync_targets"] = [
            {"node_id": "1", "input_name": "text", "class_type": "OtherNode"}
        ]
    else:
        graph["1"]["inputs"]["text"] = ["2", 0]
    result = synchronize_workflow(config, graph, [("1", "text")])
    assert result["workflow"] == config["workflow"]
    assert len(result["warnings"]) == 1


def test_duplicate_persisted_and_runtime_targets_produce_one_warning():
    config = fixture()
    config["workflow_sync_targets"] = [
        {"node_id": "3", "input_name": "text", "class_type": "easy showAnything"}
    ] * 2
    result = synchronize_workflow(config, config["api_graph"], [("3", "text")] * 2)
    assert len(result["warnings"]) == 1
    assert node(result, 3)["widgets_values"] == [["current runtime snapshot"]]


def test_api_only_workflow_does_not_fabricate_ui_or_warn():
    assert synchronize_workflow({}, {}, [("1", "text")]) == {
        "workflow": None,
        "warnings": [],
    }
    assert synchronize_workflow(
        {"workflow_json": "", "workflow": None}, {}, [("1", "text")]
    ) == {"workflow": None, "warnings": []}


def test_no_targets_still_returns_an_independent_submission_copy():
    config = fixture()
    result = synchronize_workflow(config, config["api_graph"])
    node(result, 1)["widgets_values"][0] = "later node runtime writeback"
    assert config["workflow"]["nodes"][0]["widgets_values"] == ["old prompt"]


@pytest.mark.parametrize("value", [None, {}, "invalid"])
@pytest.mark.parametrize("key", ["nodes", "links"])
def test_invalid_optional_ui_collections_warn_without_stopping_execution(key, value):
    config = fixture()
    config["workflow"][key] = value
    result = synchronize_workflow(config, config["api_graph"], [("1", "text")])
    assert result["workflow"] == config["workflow"]
    assert len(result["warnings"]) == 1
    assert "无效" in result["warnings"][0]


def test_display_cache_is_not_written_even_when_widget_values_are_named():
    config = fixture()
    config["workflow"]["nodes"][2]["widgets_values"] = {"text": "new runtime snapshot"}
    result = synchronize_workflow(config, config["api_graph"], [("3", "text")])
    assert result["workflow"] == config["workflow"]
    assert len(result["warnings"]) == 1
    assert "显示缓存" in result["warnings"][0]


def test_empty_workflow_json_falls_back_to_ui_dictionary():
    config = fixture()
    config["workflow_json"] = "  "
    result = synchronize_workflow(config, config["api_graph"], [("2", "seed")])
    assert result == {"workflow": config["workflow"], "warnings": []}


def test_named_sibling_and_positional_seed_are_both_updated_without_reordering():
    config = fixture()
    seed_node = config["workflow"]["nodes"][1]
    seed_node["widgets_values_named"] = {
        "button": "keep",
        "seed": 123,
        "other": {"nested": ["keep"]},
    }
    graph = copy.deepcopy(config["api_graph"])
    graph["2"]["inputs"]["seed"] = -1
    result = synchronize_workflow(config, graph, [("2", "seed")])
    assert result["warnings"] == []
    assert read_widget_values(node(result, 2), "seed") == [-1, -1]
    assert list(node(result, 2)["widgets_values_named"]) == ["button", "seed", "other"]
    assert seed_node["widgets_values_named"]["seed"] == 123


def test_named_and_dictionary_widgets_are_independent_representations():
    config = fixture()
    config["workflow"]["nodes"][0]["widgets_values"] = {"text": "old"}
    config["workflow"]["nodes"][0]["widgets_values_named"] = {"text": "older"}
    graph = copy.deepcopy(config["api_graph"])
    graph["1"]["inputs"]["text"] = "new"
    result = synchronize_workflow(config, graph, [("1", "text")])
    assert result["warnings"] == []
    assert read_widget_values(node(result, 1), "text") == ["new", "new"]


def test_unknown_named_widget_can_update_but_cannot_prove_positional_mapping():
    config = {
        "workflow": {
            "nodes": [
                {
                    "id": 1,
                    "type": "UnknownTextNode",
                    "widgets_values": ["old", "also old"],
                    "widgets_values_named": {"other": "also old", "text": "old"},
                }
            ]
        }
    }
    graph = {"1": {"class_type": "UnknownTextNode", "inputs": {"text": "new"}}}
    result = synchronize_workflow(config, graph, [("1", "text")])
    assert node(result, 1)["widgets_values"] == ["old", "also old"]
    assert node(result, 1)["widgets_values_named"] == {
        "other": "also old",
        "text": "new",
    }
    assert len(result["warnings"]) == 1
    assert "位置" in result["warnings"][0]
    with pytest.raises(ValueError, match="布局"):
        read_widget_values(node(result, 1), "text")


def test_unknown_named_only_node_needs_no_positional_guess():
    config = {
        "workflow": {
            "nodes": [
                {
                    "id": 1,
                    "type": "UnknownTextNode",
                    "widgets_values_named": {"text": "old"},
                }
            ]
        }
    }
    graph = {"1": {"class_type": "UnknownTextNode", "inputs": {"text": "new"}}}
    result = synchronize_workflow(config, graph, [("1", "text")])
    assert result["warnings"] == []
    assert read_widget_values(node(result, 1), "text") == ["new"]


def test_known_layout_can_add_missing_named_field_without_guessing():
    config = fixture()
    config["workflow"]["nodes"][0]["widgets_values_named"] = {"unrelated": "keep"}
    graph = copy.deepcopy(config["api_graph"])
    graph["1"]["inputs"]["text"] = "new"
    result = synchronize_workflow(config, graph, [("1", "text")])
    assert result["warnings"] == []
    assert node(result, 1)["widgets_values_named"] == {
        "unrelated": "keep",
        "text": "new",
    }


@pytest.mark.parametrize("named", [None, [], "invalid"])
def test_invalid_named_sibling_warns_even_when_positional_update_succeeds(named):
    config = fixture()
    config["workflow"]["nodes"][0]["widgets_values_named"] = named
    graph = copy.deepcopy(config["api_graph"])
    graph["1"]["inputs"]["text"] = "new"
    result = synchronize_workflow(config, graph, [("1", "text")])
    assert node(result, 1)["widgets_values"] == ["new"]
    assert node(result, 1)["widgets_values_named"] == named
    assert len(result["warnings"]) == 1


def test_named_runtime_display_cache_is_still_protected():
    config = fixture()
    config["workflow"]["nodes"][2]["widgets_values_named"] = {"text": "current"}
    result = synchronize_workflow(config, config["api_graph"], [("3", "text")])
    assert result["workflow"] == config["workflow"]
    assert "显示缓存" in result["warnings"][0]


@pytest.mark.parametrize(
    "kind,field,widgets,index",
    [
        ("TextInput_", "text", ["old"], 0),
        ("Text Multiline", "text", ["old"], 0),
        ("Text Multiline", "text", ["old", True], 0),
        ("Text Concatenate", "delimiter", [", ", "true"], 0),
        ("UltralyticsDetectorProvider", "model_name", ["bbox/old.pt"], 0),
        ("SAMLoader", "model_name", ["old.pth", "AUTO"], 0),
        ("UpscaleModelLoader", "model_name", ["old.pth"], 0),
        ("SeedNode", "seed", [9, "fixed"], 0),
        ("PrimitiveInt", "value", [9, "fixed"], 0),
        ("easy seed", "seed", [9, "randomize"], 0),
        ("easy globalSeed", "value", [9, True, "randomize", ""], 0),
        ("StringConstantMultiline", "string", ["old", True], 0),
        ("JoinStringMulti", "delimiter", [3, ", ", False, None], 1),
        ("ComfySwitchNode", "switch", [True], 0),
        (
            "Raffle",
            "filter_out_tags",
            [9, "fixed", True, True, False, False, "", "old", "", ""],
            7,
        ),
        (
            "GenerateNoise",
            "seed",
            [512, 512, 1, 9, "randomize", 1.0, False, False, "4", "BCHW"],
            3,
        ),
    ],
)
def test_verified_additional_layouts_update_only_requested_field(
    kind, field, widgets, index
):
    config = {
        "workflow": {
            "nodes": [
                {
                    "id": 1,
                    "type": kind,
                    "widgets_values": widgets,
                    "widgets_values_named": {field: widgets[index]},
                }
            ]
        }
    }
    graph = {
        "1": {
            "class_type": kind,
            "inputs": {
                field: False
                if isinstance(widgets[index], bool)
                else 321
                if isinstance(widgets[index], int)
                else "new"
            },
        }
    }
    result = synchronize_workflow(config, graph, [("1", field)])
    assert result["warnings"] == []
    expected = list(widgets)
    expected[index] = graph["1"]["inputs"][field]
    assert node(result, 1)["widgets_values"] == expected
    assert read_widget_values(node(result, 1), field) == [expected[index]] * 2


def test_facedetailer_seed_mapping_accounts_for_frontend_seed_control():
    widgets = [
        512,
        True,
        1024,
        19,
        "randomize",
        20,
        7.0,
        "euler",
        "normal",
        0.5,
        5,
        True,
        True,
        0.5,
        10,
        3.0,
        "center-1",
        0,
        0.93,
        0,
        0.7,
        "False",
        10,
        "",
        1,
        False,
        20,
        False,
        False,
    ]
    config = {
        "workflow": {
            "nodes": [
                {
                    "id": 1,
                    "type": "FaceDetailer",
                    "widgets_values": widgets,
                    "widgets_values_named": {"seed": 19, "steps": 20},
                }
            ]
        }
    }
    graph = {"1": {"class_type": "FaceDetailer", "inputs": {"seed": 987654321123456}}}
    result = synchronize_workflow(config, graph, [("1", "seed")])
    assert result["warnings"] == []
    assert widget_slot(node(result, 1), "seed") == 3
    assert read_widget_values(node(result, 1), "seed") == [987654321123456] * 2
    assert node(result, 1)["widgets_values"][4:] == widgets[4:]


def test_frontend_control_field_cannot_be_written_via_named_escape_hatch():
    config = {
        "workflow": {
            "nodes": [
                {
                    "id": 1,
                    "type": "KSampler",
                    "widgets_values_named": {"control_after_generate": "randomize"},
                }
            ]
        }
    }
    graph = {
        "1": {"class_type": "KSampler", "inputs": {"control_after_generate": "fixed"}}
    }
    result = synchronize_workflow(config, graph, [("1", "control_after_generate")])
    assert result["workflow"] == config["workflow"]
    assert "前端控件" in result["warnings"][0]
