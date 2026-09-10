from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from astrbot_plugin_image_studio.image_metadata import (
    parse_image_metadata,
    parse_metadata_fields,
)
from astrbot_plugin_image_studio.tests.test_comfy_candidates import parse
from astrbot_plugin_image_studio.tests.test_comfy_display_snapshots import (
    conflicting_observer_workflow,
    dynamic_graph,
    observer_workflow,
    snapshots,
)
from astrbot_plugin_image_studio.tests.test_image_metadata import api_graph


def output_keys(normalized: dict) -> dict[str, str]:
    return {item["node_id"]: item["match_key"] for item in normalized["outputs"]}


def candidate_keys(normalized: dict) -> dict[str, str]:
    return {item["id"]: item["match_key"] for item in normalized["prompt_candidates"]}


def two_save_graph() -> dict:
    graph = api_graph()
    graph["8"] = {"class_type": "CLIPTextEncode", "inputs": {"text": "branch two"}}
    graph["9"] = {
        "class_type": "KSampler",
        "inputs": {**graph["5"]["inputs"], "positive": ["8", 0]},
    }
    graph["10"] = {"class_type": "VAEDecode", "inputs": {"samples": ["9", 0]}}
    graph["11"] = {"class_type": "SaveImage", "inputs": {"images": ["10", 0]}}
    return graph


def test_same_workflow_matches_despite_image_specific_prompt_and_sampler_values() -> (
    None
):
    source = api_graph()
    target = copy.deepcopy(source)
    target["2"]["inputs"]["text"] = "a different subject and composition"
    target["3"]["inputs"]["text"] = "a different negative prompt"
    target["1"]["inputs"]["ckpt_name"] = "another-model.safetensors"
    target["4"]["inputs"].update(width=1024, height=1536)
    target["5"]["inputs"].update(
        seed=1,
        steps=40,
        cfg=7,
        sampler_name="dpmpp_2m",
        scheduler="karras",
        denoise=0.8,
    )
    first, second = parse(source), parse(target)
    assert output_keys(first) == output_keys(second)
    assert candidate_keys(first) == candidate_keys(second)
    assert first["prompt"] != second["prompt"]


def test_match_keys_ignore_dict_order_ui_layout_and_unrelated_nodes() -> None:
    graph = api_graph()
    target = {
        key: {**node, "inputs": dict(reversed(list(node["inputs"].items())))}
        for key, node in reversed(list(graph.items()))
    }
    target["99"] = {
        "class_type": "DebugPrompt",
        "inputs": {"text": "do not match on unrelated debug text"},
    }
    target["2"]["_meta"] = {"title": "renamed by user"}
    workflow = {
        "nodes": [
            {
                "id": 2,
                "type": "CLIPTextEncode",
                "title": "a new title",
                "pos": [555, 333],
                "size": [400, 200],
                "widgets_values": ["stale UI cache"],
            }
        ],
        "groups": [{"title": "a layout-only group", "bounding": [1, 2, 3, 4]}],
    }
    first, second = parse(graph), parse(target, workflow=workflow)
    assert output_keys(first) == output_keys(second)
    assert candidate_keys(first) == candidate_keys(second)


@pytest.mark.parametrize(
    "change",
    ["node_type", "source_node", "output_port", "input_name", "literal_to_link"],
)
def test_reused_node_ids_do_not_match_when_upstream_graph_changes(change: str) -> None:
    source = api_graph()
    target = copy.deepcopy(source)
    if change == "node_type":
        target["4"]["class_type"] = "EmptySD3LatentImage"
    elif change == "source_node":
        target["5"]["inputs"]["positive"] = ["3", 0]
    elif change == "output_port":
        target["5"]["inputs"]["positive"] = ["2", 1]
    elif change == "input_name":
        target["4"]["inputs"]["latent_width"] = target["4"]["inputs"].pop("width")
    else:
        target["8"] = {"class_type": "TextInput_", "inputs": {"text": "positive"}}
        target["2"]["inputs"]["text"] = ["8", 0]
    first, second = parse(source), parse(target)
    assert output_keys(first)["7"] != output_keys(second)["7"]
    shared_candidates = candidate_keys(first).keys() & candidate_keys(second).keys()
    assert shared_candidates
    assert all(
        candidate_keys(first)[key] != candidate_keys(second)[key]
        for key in shared_candidates
    )


def test_keys_are_scoped_to_each_save_branch_and_do_not_require_output_selection() -> (
    None
):
    graph = two_save_graph()
    pending = parse(graph)
    assert pending["requires_output_selection"] is True
    assert pending["prompt_candidates"] == []
    assert set(output_keys(pending)) == {"7", "11"}
    assert len(set(output_keys(pending).values())) == 2
    first = parse(graph, output_node_id="7")
    second = parse(graph, output_node_id="11")
    assert output_keys(first) == output_keys(second) == output_keys(pending)
    assert "8:text" not in candidate_keys(first)
    assert "2:text" not in candidate_keys(second)
    # Shared negative text is still associated with a particular selected output.
    assert candidate_keys(first)["3:text"] != candidate_keys(second)["3:text"]


def test_changes_in_another_save_branch_do_not_invalidate_selected_branch() -> None:
    source = two_save_graph()
    target = copy.deepcopy(source)
    target["8"]["class_type"] = "DynamicPromptGenerator"
    target["9"]["inputs"]["positive"] = ["8", 1]
    first, second = parse(source, output_node_id="7"), parse(target, output_node_id="7")
    assert output_keys(first)["7"] == output_keys(second)["7"]
    assert output_keys(first)["11"] != output_keys(second)["11"]
    assert candidate_keys(first) == candidate_keys(second)


def test_separate_save_nodes_of_the_same_image_are_not_the_same_target() -> None:
    graph = api_graph()
    graph["8"] = copy.deepcopy(graph["7"])
    keys = output_keys(parse(graph))
    assert keys["7"] != keys["8"]


def test_static_and_template_candidates_use_own_text_under_stable_matching_keys() -> (
    None
):
    source = api_graph()
    source["8"] = {
        "class_type": "FaceDetailer",
        "inputs": {
            "image": ["6", 0],
            "positive": ["2", 0],
            "negative": ["3", 0],
            "wildcard": "{red|blue} dress",
        },
    }
    source["7"]["inputs"]["images"] = ["8", 0]
    target = copy.deepcopy(source)
    target["8"]["inputs"]["wildcard"] = "{green|yellow} shoes"
    first, second = parse(source), parse(target)
    assert candidate_keys(first) == candidate_keys(second)
    assert candidate_keys(first)["8:wildcard"] != candidate_keys(first)["2:text"]
    first_template = next(
        item for item in first["prompt_candidates"] if item["id"] == "8:wildcard"
    )
    second_template = next(
        item for item in second["prompt_candidates"] if item["id"] == "8:wildcard"
    )
    assert first_template["status"] == second_template["status"] == "template"
    assert first_template["text"] != second_template["text"]


def test_one_node_with_two_prompt_fields_has_distinct_candidate_matching_keys() -> None:
    graph = api_graph()
    graph["2"] = {
        "class_type": "CLIPTextEncodeSDXL",
        "inputs": {
            "text_g": "global prompt",
            "text_l": "local prompt",
            "clip": ["1", 1],
        },
    }
    keys = candidate_keys(parse(graph))
    assert keys["2:text_g"] != keys["2:text_l"]


def test_display_snapshots_match_across_random_text_and_origin_merging() -> None:
    graph = dynamic_graph()
    first = parse(graph, workflow=observer_workflow(["fixed, random choice"]))
    target = copy.deepcopy(graph)
    target["10"]["inputs"]["text"] = "fixed, a different random choice"
    target["8"]["inputs"]["filter_out_tags"] = "changed raffle settings"
    second = parse(target)
    a, b = snapshots(first)[0], snapshots(second)[0]
    assert a["match_key"] == b["match_key"]
    assert a["id"] != b["id"]
    assert a["text"] != b["text"]
    assert len(a["observations"]) == len(b["observations"]) == 1
    assert a["snapshot_kind"] == "workflow" and b["snapshot_kind"] == "api_fallback"
    assert output_keys(first) == output_keys(second)


def test_conflicting_snapshots_share_structure_key_but_keep_distinct_provenance() -> (
    None
):
    normalized = parse(dynamic_graph(), workflow=conflicting_observer_workflow())
    candidates = snapshots(normalized)
    assert len(candidates) == 2
    assert len({item["match_key"] for item in candidates}) == 1
    assert len({item["id"] for item in candidates}) == 2
    assert all(item["conflicting"] for item in candidates)
    assert {
        tuple(
            (entry["node_id"], entry["source"], entry["field"])
            for entry in item["observations"]
        )
        for item in candidates
    } == {
        (("10", "workflow", "widgets_values"),),
        (("11", "workflow", "widgets_values"),),
    }


def test_display_observers_do_not_enter_output_structure_key() -> None:
    source = dynamic_graph()
    target = copy.deepcopy(source)
    target["11"] = {
        "class_type": "easy showAnything",
        "inputs": {"anything": ["9", 0], "text": "fixed, random choice"},
    }
    first, second = parse(source), parse(target)
    assert output_keys(first) == output_keys(second)
    assert snapshots(first)[0]["match_key"] == snapshots(second)[0]["match_key"]
    assert len(snapshots(second)[0]["observations"]) == 2


def test_display_producer_port_and_consumer_role_are_part_of_matching_identity() -> (
    None
):
    source = dynamic_graph()
    target = copy.deepcopy(source)
    target["2"]["inputs"]["text"] = ["9", 1]
    target["10"]["inputs"]["anything"] = ["9", 1]
    first, second = snapshots(parse(source)), snapshots(parse(target))
    assert len(first) == len(second) == 1
    assert first[0]["source_ref"] == "9:0"
    assert second[0]["source_ref"] == "9:1"
    assert first[0]["match_key"] != second[0]["match_key"]
    negative = snapshots(parse(dynamic_graph(role="negative")))
    assert len(negative) == 1
    assert first[0]["match_key"] != negative[0]["match_key"]


def test_matching_metadata_is_opaque_and_does_not_modify_raw_workflows() -> None:
    graph = dynamic_graph()
    graph["8"]["inputs"]["filter_out_tags"] = "private raffle setting /home/owner/key"
    graph["10"]["inputs"]["text"] = "private generated observation"
    raw = {
        "prompt": json.dumps(graph),
        "workflow": json.dumps(observer_workflow(["private UI snapshot"])),
    }
    before = copy.deepcopy(raw)
    parsed = parse_metadata_fields(raw)
    assert raw == before == parsed["raw"]
    keys = [
        *output_keys(parsed["normalized"]).values(),
        *candidate_keys(parsed["normalized"]).values(),
    ]
    assert keys and all(isinstance(key, str) and 16 <= len(key) <= 128 for key in keys)
    assert all(
        word not in key
        for key in keys
        for word in ("private", "owner", "filter_out_tags", "random", "CLIPTextEncode")
    )
    assert "match_key" not in parsed["raw"]["prompt"]
    assert "match_key" not in parsed["raw"]["workflow"]


@pytest.mark.parametrize("output", ["188", "189"])
def test_available_raffle_sample_matches_preferred_snapshot_structure(
    output: str,
) -> None:
    sample = Path(__file__).parents[1] / "data" / "image" / "Anima_00002_.png"
    if not sample.exists():
        pytest.skip("本地参考样本不在发布仓库中")
    metadata = parse_image_metadata(sample.read_bytes())
    pending = metadata["normalized"]
    assert pending["requires_output_selection"]
    assert pending["prompt_candidates"] == []
    saves = [item for item in pending["outputs"] if item["kind"] == "save"]
    assert {item["node_id"] for item in saves} == {"188", "189"}
    assert len({item["match_key"] for item in saves}) == 2
    selected = parse_metadata_fields(metadata["raw"], output_node_id=output)
    observed = snapshots(selected["normalized"])
    assert len(observed) == 1
    assert len({item["match_key"] for item in observed}) == 1
    assert len({item["id"] for item in observed}) == 1
    assert {item["source_ref"] for item in observed} == {"137:0"}
    assert [len(item["text"]) for item in observed] == [610]
    assert selected["raw"] == metadata["raw"]
