from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from astrbot_plugin_image_studio.image_metadata import (
    parse_image_metadata,
    parse_metadata_fields,
)
from astrbot_plugin_image_studio.tests.test_image_metadata import api_graph


def parsed(graph: dict) -> dict:
    return parse_metadata_fields({"prompt": json.dumps(graph)})


@pytest.mark.parametrize(
    "kind,names,operation",
    [
        ("ConditioningCombine", ("conditioning_1", "conditioning_2"), "combine"),
        ("ConditioningConcat", ("conditioning_to", "conditioning_from"), "concat"),
        ("ConditioningAverage", ("conditioning_to", "conditioning_from"), "average"),
    ],
)
def test_combination_is_structured_and_never_claimed_exact(
    kind, names, operation
) -> None:
    graph = api_graph()
    graph["8"] = {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "second composition", "clip": ["1", 1]},
    }
    graph["9"] = {
        "class_type": kind,
        "inputs": {
            names[0]: ["2", 0],
            names[1]: ["8", 0],
            "conditioning_to_strength": 0.7,
        },
    }
    graph["5"]["inputs"]["positive"] = ["9", 0]
    result = parsed(graph)
    normalized = result["normalized"]
    record = normalized["condition_nodes"]["9:0"]
    assert record["operation"] == operation
    assert record["parameters"]["conditioning_to_strength"] == 0.7
    assert record["inputs"] == [
        {"name": names[0], "ref": "2:0", "kind": "conditioning"},
        {"name": names[1], "ref": "8:0", "kind": "conditioning"},
    ]
    assert "texts" not in record
    assert normalized["prompt"] == "positive\n\nsecond composition"
    assert normalized["prompt_status"] == "summary"
    assert normalized["stages"][0]["positive_conditioning"] == "9:0"
    assert any("不能等同" in warning for warning in result["warnings"])


def test_conditioning_area_mask_weight_and_timestep_keep_linked_mask_port() -> None:
    graph = api_graph()
    graph["8"] = {"class_type": "LoadImage", "inputs": {"image": "mask.png"}}
    graph["9"] = {
        "class_type": "ConditioningSetArea",
        "inputs": {
            "conditioning": ["2", 0],
            "width": 512,
            "height": 256,
            "x": 64,
            "y": 128,
            "strength": 0.5,
        },
    }
    graph["10"] = {
        "class_type": "ConditioningSetMask",
        "inputs": {
            "conditioning": ["9", 0],
            "mask": ["8", 1],
            "strength": 0.8,
            "set_cond_area": "mask bounds",
        },
    }
    graph["11"] = {
        "class_type": "ConditioningSetTimestepRange",
        "inputs": {"conditioning": ["10", 0], "start": 0.2, "end": 0.9},
    }
    graph["5"]["inputs"]["positive"] = ["11", 0]
    normalized = parsed(graph)["normalized"]
    nodes = normalized["condition_nodes"]
    assert nodes["9:0"]["parameters"] == {
        "width": 512,
        "height": 256,
        "x": 64,
        "y": 128,
        "strength": 0.5,
    }
    assert nodes["10:0"]["parameters"]["mask"] == {"ref": "8:1"}
    assert nodes["11:0"]["parameters"] == {"start": 0.2, "end": 0.9}
    assert normalized["prompt"] == "positive"
    assert normalized["prompt_status"] == "summary"


def test_unknown_condition_output_port_is_retained_without_reading_wrong_text() -> None:
    graph = api_graph()
    graph["5"]["inputs"]["positive"] = ["2", 1]
    result = parsed(graph)
    normalized = result["normalized"]
    assert normalized["prompt_status"] == "missing"
    assert "prompt" not in normalized
    node = normalized["condition_nodes"]["2:1"]
    assert node["output_port"] == 1
    assert node["status"] == "unsupported"
    assert node["reason"] == "unsupported_output_port"
    assert "2:0" not in normalized["condition_nodes"]


def test_unknown_conditioning_and_dynamic_text_produce_partial_known_summary() -> None:
    graph = api_graph()
    graph["8"] = {
        "class_type": "CustomCondition",
        "inputs": {"text": "must not be guessed", "conditioning": ["3", 0]},
    }
    graph["9"] = {
        "class_type": "ConditioningCombine",
        "inputs": {"conditioning_1": ["2", 0], "conditioning_2": ["8", 0]},
    }
    graph["5"]["inputs"]["positive"] = ["9", 0]
    result = parsed(graph)
    assert result["normalized"]["prompt"] == "positive"
    assert result["normalized"]["prompt_status"] == "partial"
    assert result["normalized"]["condition_nodes"]["8:0"]["status"] == "unsupported"
    assert all("must not" not in warning for warning in result["warnings"])


def test_zeroed_conditioning_keeps_source_structure_but_not_source_summary() -> None:
    graph = api_graph()
    graph["8"] = {
        "class_type": "ConditioningZeroOut",
        "inputs": {"conditioning": ["2", 0]},
    }
    graph["5"]["inputs"]["positive"] = ["8", 0]
    normalized = parsed(graph)["normalized"]
    assert normalized["condition_nodes"]["2:0"]["texts"] == ["positive"]
    assert normalized["condition_nodes"]["8:0"]["zeroed_embedding"] is True
    assert normalized["prompt"] == ""
    assert normalized["prompt_status"] == "summary"
    assert normalized["stages"][0]["prompt"] == ""
    graph["9"] = {"class_type": "CLIPTextEncode", "inputs": {"text": "surviving text"}}
    graph["10"] = {
        "class_type": "ConditioningCombine",
        "inputs": {"conditioning_1": ["8", 0], "conditioning_2": ["9", 0]},
    }
    graph["5"]["inputs"]["positive"] = ["10", 0]
    normalized = parsed(graph)["normalized"]
    assert normalized["prompt"] == "surviving text"
    assert normalized["stages"][0]["prompt"] == "surviving text"


@pytest.mark.parametrize("strength", [0, 1])
def test_average_extreme_weights_are_marked_as_reference_summary(strength: int) -> None:
    graph = api_graph()
    graph["8"] = {
        "class_type": "ConditioningAverage",
        "inputs": {
            "conditioning_to": ["2", 0],
            "conditioning_from": ["3", 0],
            "conditioning_to_strength": strength,
        },
    }
    graph["5"]["inputs"]["positive"] = ["8", 0]
    result = parsed(graph)
    assert result["normalized"]["prompt_status"] == "summary"
    assert (
        result["normalized"]["condition_nodes"]["8:0"]["parameters"][
            "conditioning_to_strength"
        ]
        == strength
    )
    assert any("未按权重" in warning for warning in result["warnings"])


def test_unique_save_excludes_unrelated_preview_and_disconnected_nodes() -> None:
    graph = api_graph()
    graph["8"] = {"class_type": "CLIPTextEncode", "inputs": {"text": "preview-only"}}
    graph["9"] = {
        "class_type": "KSampler",
        "inputs": {**graph["5"]["inputs"], "positive": ["8", 0], "seed": 99},
    }
    graph["10"] = {"class_type": "VAEDecode", "inputs": {"samples": ["9", 0]}}
    graph["11"] = {"class_type": "PreviewImage", "inputs": {"images": ["10", 0]}}
    graph["12"] = {"class_type": "CLIPTextEncode", "inputs": {"text": "unconnected"}}
    result = parsed(graph)
    normalized = result["normalized"]
    assert normalized["selected_output_node"] == "7"
    assert normalized["selected_output_ref"] == "6:0"
    assert normalized["output_selection_reason"] == "single_save"
    assert normalized["prompt"] == "positive"
    assert [stage["node_id"] for stage in normalized["stages"]] == ["5"]
    assert normalized["outputs"][1]["stage_ids"] == ["9"]
    assert normalized["outputs"][1]["kind"] == "preview"
    assert "8:0" not in normalized["condition_nodes"]
    assert "12:0" not in normalized["condition_nodes"]
    assert not any("多个成图" in warning for warning in result["warnings"])


def test_multiple_save_outputs_preserve_branches_without_selecting_one() -> None:
    graph = api_graph()
    graph["8"] = {"class_type": "CLIPTextEncode", "inputs": {"text": "second output"}}
    graph["9"] = {
        "class_type": "KSampler",
        "inputs": {**graph["5"]["inputs"], "positive": ["8", 0], "seed": 99},
    }
    graph["10"] = {"class_type": "VAEDecode", "inputs": {"samples": ["9", 0]}}
    graph["11"] = {"class_type": "SaveImage", "inputs": {"images": ["10", 0]}}
    result = parsed(graph)
    normalized = result["normalized"]
    assert normalized["output_selection_reason"] == "ambiguous_saves"
    assert "selected_output_node" not in normalized
    assert "width" not in normalized
    assert [item["stage_ids"] for item in normalized["outputs"]] == [["5"], ["9"]]
    assert normalized["prompt"] == "positive\n\nsecond output"
    assert normalized["prompt_status"] == "summary"
    assert any("多个保存输出" in warning for warning in result["warnings"])


def test_shared_conditioning_dag_stays_flat_and_summary_deduplicates() -> None:
    graph = api_graph()
    previous = "2"
    for index in range(20, 50):
        graph[str(index)] = {
            "class_type": "ConditioningCombine",
            "inputs": {
                "conditioning_1": [previous, 0],
                "conditioning_2": [previous, 0],
            },
        }
        previous = str(index)
    graph["5"]["inputs"]["positive"] = [previous, 0]
    normalized = parsed(graph)["normalized"]
    assert normalized["prompt"] == "positive"
    assert len(normalized["condition_nodes"]) == 32
    assert len(json.dumps(normalized["condition_nodes"])) < 20_000


@pytest.mark.parametrize("unknown_upscale", [False, True])
def test_image_scaling_preserves_mode_and_only_known_dimensions(
    unknown_upscale: bool,
) -> None:
    graph = api_graph()
    start = "6"
    if unknown_upscale:
        graph["8"] = {
            "class_type": "ImageUpscaleWithModel",
            "inputs": {"image": ["6", 0], "upscale_model": ["12", 0]},
        }
        graph["12"] = {
            "class_type": "UpscaleModelLoader",
            "inputs": {"model_name": "4x_filename_is_not_a_guarantee.pth"},
        }
        start = "8"
    graph["9"] = {
        "class_type": "ImageScaleBy",
        "inputs": {"image": [start, 0], "scale_by": 0.5},
    }
    graph["10"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["9", 0]}}
    graph["11"] = {
        "class_type": "KSampler",
        "inputs": {**graph["5"]["inputs"], "latent_image": ["10", 0]},
    }
    graph["13"] = {"class_type": "VAEDecode", "inputs": {"samples": ["11", 0]}}
    graph["7"]["inputs"]["images"] = ["13", 0]
    normalized = parsed(graph)["normalized"]
    assert normalized["mode"] == "text2img"
    final = normalized["stages"][-1]
    if unknown_upscale:
        assert "width" not in final and "height" not in final
        assert "width" not in normalized
        assert normalized["output_dimensions_status"] == "unknown"
    else:
        assert (final["width"], final["height"]) == (416, 608)
        assert (normalized["width"], normalized["height"]) == (416, 608)


def test_workflow_widgets_and_output_ports_map_to_the_correct_inputs() -> None:
    workflow = {
        "nodes": [
            {
                "id": 1,
                "type": "CLIPTextEncode",
                "inputs": [],
                "widgets_values": ["positive"],
            },
            {
                "id": 2,
                "type": "CLIPTextEncode",
                "inputs": [],
                "widgets_values": ["additional"],
            },
            {
                "id": 3,
                "type": "ConditioningAverage",
                "inputs": [
                    {"name": "conditioning_to", "link": 1},
                    {"name": "conditioning_from", "link": 2},
                ],
                "widgets_values": [0.25],
            },
            {
                "id": 4,
                "type": "KSamplerAdvanced",
                "inputs": [{"name": "positive", "link": 3}],
                "widgets_values": [
                    "enable",
                    18446744073709551615,
                    "fixed",
                    30,
                    6,
                    "euler",
                    "normal",
                    2,
                    25,
                    "disable",
                ],
            },
            {
                "id": 5,
                "type": "VAEDecode",
                "inputs": [{"name": "samples", "link": 4}],
                "widgets_values": [],
            },
            {
                "id": 6,
                "type": "SaveImage",
                "inputs": [{"name": "images", "link": 5}],
                "widgets_values": ["example"],
            },
        ],
        "links": [
            [1, 1, 0, 3, 0, "CONDITIONING"],
            [2, 2, 0, 3, 1, "CONDITIONING"],
            [3, 3, 0, 4, 1, "CONDITIONING"],
            [4, 4, 0, 5, 0, "LATENT"],
            [5, 5, 0, 6, 0, "IMAGE"],
        ],
    }
    normalized = parse_metadata_fields({"workflow": json.dumps(workflow)})["normalized"]
    assert (
        normalized["condition_nodes"]["3:0"]["parameters"]["conditioning_to_strength"]
        == 0.25
    )
    assert normalized["stages"][0]["seed"] == "18446744073709551615"
    assert normalized["stages"][0]["start_at_step"] == 2
    assert normalized["stages"][0]["end_at_step"] == 25
    altered = copy.deepcopy(workflow)
    altered["links"][1][2] = 1
    normalized = parse_metadata_fields({"workflow": json.dumps(altered)})["normalized"]
    assert normalized["condition_nodes"]["2:1"]["status"] == "unsupported"
    assert normalized["prompt"] == "positive"
    assert normalized["prompt_status"] == "partial"


def test_missing_latent_size_is_unknown_instead_of_crashing_or_fabricating() -> None:
    graph = api_graph()
    graph["4"]["inputs"] = {"width": 832}
    normalized = parsed(graph)["normalized"]
    assert normalized["output_dimensions_status"] == "unknown"
    assert "width" not in normalized
    assert normalized["mode"] == "text2img"


def test_explicit_image_scale_recovers_dimensions_after_unknown_upscale() -> None:
    graph = api_graph()
    graph["8"] = {"class_type": "ImageUpscaleWithModel", "inputs": {"image": ["6", 0]}}
    graph["9"] = {
        "class_type": "ImageScale",
        "inputs": {"image": ["8", 0], "width": 1024, "height": 1536, "crop": "center"},
    }
    graph["7"]["inputs"]["images"] = ["9", 0]
    normalized = parsed(graph)["normalized"]
    assert normalized["output_dimensions_status"] == "known"
    assert (normalized["width"], normalized["height"]) == (1024, 1536)


def test_available_multistage_sample_keeps_only_connected_conditioning() -> None:
    sample = Path(__file__).parents[1] / "data" / "image" / "149183219_p5.webp"
    if not sample.is_file():
        pytest.skip("本地参考样本不在发布仓库中")
    result = parse_image_metadata(sample.read_bytes())
    normalized = result["normalized"]
    assert normalized["selected_output_node"] == "130"
    assert normalized["output_selection_reason"] == "single_save"
    assert [stage["node_id"] for stage in normalized["stages"]] == [
        "8",
        "19",
        "127",
        "139",
    ]
    assert set(normalized["condition_nodes"]) == {"6:0", "5:0", "69:0", "67:0"}
    graph = json.loads(result["raw"]["prompt"])
    expected_length = (
        len(graph["6"]["inputs"]["text"]) + 2 + len(graph["67"]["inputs"]["text"])
    )
    assert len(normalized["prompt"]) == expected_length
    assert len(normalized["negative_prompt"]) == len(graph["5"]["inputs"]["text"])
    assert normalized["prompt_status"] == "summary"
    assert "width" not in normalized["stages"][-1]
    assert normalized["output_dimensions_status"] == "unknown"
