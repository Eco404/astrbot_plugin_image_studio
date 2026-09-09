from __future__ import annotations

import json
from pathlib import Path

import pytest

from astrbot_plugin_image_studio.image_metadata import (
    parse_image_metadata,
    parse_metadata_fields,
)
from astrbot_plugin_image_studio.tests.test_image_metadata import api_graph


def parse(
    graph: dict, *, workflow: dict | None = None, output_node_id: str = ""
) -> dict:
    fields = {"prompt": json.dumps(graph)}
    if workflow is not None:
        fields["workflow"] = json.dumps(workflow)
    return parse_metadata_fields(fields, output_node_id=output_node_id)["normalized"]


def test_unknown_path_keeps_connected_static_text_candidates_without_claiming_output() -> (
    None
):
    graph = api_graph()
    graph["8"] = {"class_type": "TextInput_", "inputs": {"text": "known fragment"}}
    graph["9"] = {
        "class_type": "UnknownTransform",
        "inputs": {"source": ["8", 0], "model_name": "not a prompt"},
    }
    graph["2"]["inputs"]["text"] = ["9", 0]
    normalized = parse(graph)
    candidates = {item["id"]: item for item in normalized["prompt_candidates"]}
    assert "prompt" not in normalized
    assert candidates["8:text"]["text"] == "known fragment"
    assert candidates["8:text"]["role"] == "positive"
    assert candidates["8:text"]["status"] == "unknown_path"
    assert candidates["8:text"]["output_node_ids"] == ["7"]
    assert candidates["8:text"]["stage_ids"] == ["5"]
    assert "9:model_name" not in candidates


def test_candidates_exclude_disconnected_and_preview_only_debug_texts() -> None:
    graph = api_graph()
    for index in range(18):
        graph[str(index + 20)] = {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": f"disconnected {index}"},
        }
    graph["40"] = {
        "class_type": "easy showAnything",
        "inputs": {"anything": ["2", 0], "text": "stale cached text"},
    }
    graph["41"] = {
        "class_type": "KSampler",
        "inputs": {**graph["5"]["inputs"], "positive": ["20", 0]},
    }
    graph["42"] = {"class_type": "VAEDecode", "inputs": {"samples": ["41", 0]}}
    graph["43"] = {"class_type": "PreviewImage", "inputs": {"images": ["42", 0]}}
    candidates = parse(graph)["prompt_candidates"]
    assert {item["id"] for item in candidates} == {"2:text", "3:text"}


def test_raffle_filters_are_excluded_but_linked_display_snapshot_is_manual() -> None:
    graph = api_graph()
    graph["8"] = {
        "class_type": "Raffle",
        "inputs": {
            "filter_out_tags": "filter",
            "exclude_tag_categories": "categories",
            "negative_prompt": "filter-specific negative",
            "text": "not executed text",
        },
    }
    graph["9"] = {
        "class_type": "Text Concatenate",
        "inputs": {"text_a": "safe fragment", "text_b": ["8", 0], "delimiter": ", "},
    }
    graph["2"]["inputs"]["text"] = ["9", 0]
    graph["10"] = {
        "class_type": "easy showAnything",
        "inputs": {"anything": ["9", 0], "text": "old output"},
    }
    candidates = parse(graph)["prompt_candidates"]
    assert all(item["node_id"] != "8" for item in candidates)
    snapshots = [item for item in candidates if item["status"] == "display_snapshot"]
    assert len(snapshots) == 1
    assert snapshots[0]["text"] == "old output"
    assert snapshots[0]["source_ref"] == "9:0"
    assert snapshots[0]["freshness"] == "unverified"
    assert not parse(graph).get("prompt")
    assert (
        next(item for item in candidates if item["id"] == "9:text_a")["text"]
        == "safe fragment"
    )


@pytest.mark.parametrize("declared", [True, False])
def test_unknown_node_requires_declared_string_field_or_text_identity(
    declared: bool,
) -> None:
    graph = api_graph()
    graph["8"] = {
        "class_type": "ExternalGenerator",
        "inputs": {"text": "potential text", "filename": "not a candidate"},
    }
    graph["2"]["inputs"]["text"] = ["8", 0]
    workflow = {
        "nodes": [
            {
                "id": 8,
                "type": "ExternalGenerator",
                "inputs": [{"name": "text", "type": "STRING" if declared else "MODEL"}],
            }
        ]
    }
    candidates = {
        item["id"]: item
        for item in parse(graph, workflow=workflow)["prompt_candidates"]
    }
    assert ("8:text" in candidates) is declared
    assert "8:filename" not in candidates
    graph["8"]["class_type"] = "DynamicPromptGenerator"
    candidates = {item["id"]: item for item in parse(graph)["prompt_candidates"]}
    assert candidates["8:text"]["status"] == "unknown_path"


def test_negative_and_shared_role_and_wildcard_are_explicit() -> None:
    graph = api_graph()
    graph["5"]["inputs"]["negative"] = ["2", 0]
    graph["8"] = {
        "class_type": "FaceDetailer",
        "inputs": {
            "image": ["6", 0],
            "positive": ["2", 0],
            "negative": ["2", 0],
            "wildcard": "{template|variant}",
        },
    }
    graph["7"]["inputs"]["images"] = ["8", 0]
    candidates = {item["id"]: item for item in parse(graph)["prompt_candidates"]}
    assert candidates["2:text"]["role"] == "mixed"
    assert candidates["8:wildcard"]["role"] == "positive"
    assert candidates["8:wildcard"]["status"] == "template"
    assert candidates["8:wildcard"]["stage_ids"] == ["8"]


def test_unknown_output_port_preserves_candidate_without_claiming_exact_result() -> (
    None
):
    graph = api_graph()
    graph["5"]["inputs"]["positive"] = ["2", 1]
    normalized = parse(graph)
    candidate = next(
        item for item in normalized["prompt_candidates"] if item["id"] == "2:text"
    )
    assert candidate["output_ports"] == [1]
    assert candidate["status"] == "unknown_path"
    assert normalized["prompt_status"] == "missing"


def test_multiple_saves_require_choice_and_never_merge_branches() -> None:
    graph = api_graph()
    graph["8"] = {"class_type": "CLIPTextEncode", "inputs": {"text": "branch two"}}
    graph["9"] = {
        "class_type": "KSampler",
        "inputs": {**graph["5"]["inputs"], "positive": ["8", 0]},
    }
    graph["10"] = {"class_type": "VAEDecode", "inputs": {"samples": ["9", 0]}}
    graph["11"] = {"class_type": "SaveImage", "inputs": {"images": ["10", 0]}}
    normalized = parse(graph)
    assert normalized["requires_output_selection"]
    assert normalized["prompt_candidates"] == []
    assert normalized["stages"] == []
    assert "prompt" not in normalized and "negative_prompt" not in normalized
    chosen = parse(graph, output_node_id="11")
    assert chosen["selected_output_node"] == "11"
    assert chosen["output_selection_reason"] == "user_selected_save"
    assert chosen["requires_output_selection"] is False
    assert chosen["prompt"] == "branch two"
    assert {item["id"] for item in chosen["prompt_candidates"]} == {"8:text", "3:text"}
    assert all(
        item["output_node_ids"] == ["11"] for item in chosen["prompt_candidates"]
    )
    assert chosen["output_nodes"] == ["7", "11"]


def test_preview_only_has_no_candidates_and_cannot_be_chosen_as_save() -> None:
    graph = api_graph()
    graph["7"]["class_type"] = "PreviewImage"
    assert parse(graph)["prompt_candidates"] == []
    with pytest.raises(ValueError, match="保存"):
        parse(graph, output_node_id="7")
    with pytest.raises(ValueError, match="保存"):
        parse(graph, output_node_id="absent")
    with pytest.raises(ValueError, match="字符串"):
        parse(graph, output_node_id=[])
    with pytest.raises(ValueError, match="ComfyUI"):
        parse_metadata_fields({}, output_node_id="7")


@pytest.mark.parametrize(
    "name", ["7F7066F37BF383F6E36E4F7F335CCCF6.png", "ComfyUI_00005_.png"]
)
def test_available_dynamic_samples_offer_only_connected_candidates(name: str) -> None:
    sample = Path(__file__).parents[1] / "data" / "image" / name
    if not sample.exists():
        pytest.skip("本地参考样本不在发布仓库中")
    metadata = parse_image_metadata(sample.read_bytes())
    candidates = metadata["normalized"]["prompt_candidates"]
    static_candidates = [
        item for item in candidates if item["status"] != "display_snapshot"
    ]
    assert [(item["id"], len(item["text"])) for item in static_candidates] == [
        ("170:text", 105),
        ("171:text", 173),
        ("188:wildcard", 13),
    ]
    assert all(item["output_node_ids"] == ["175"] for item in candidates)
    assert all(item["node_id"] not in {"69", "101", "102"} for item in candidates)
    assert len(candidates[0]["stage_ids"]) == 4
    snapshots = [item for item in candidates if item["status"] == "display_snapshot"]
    assert len(snapshots) == 2
    assert all(
        item["node_id"] == "90" and item["source_ref"] == "73:0" for item in snapshots
    )
    assert all(
        item["conflicting"] and item["freshness"] == "unverified" for item in snapshots
    )
    assert {item["observations"][0]["source"] for item in snapshots} == {
        "prompt",
        "workflow",
    }
