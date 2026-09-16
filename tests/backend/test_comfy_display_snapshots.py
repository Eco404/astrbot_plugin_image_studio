from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from astrbot_plugin_image_studio.backend.metadata.parser import (
    parse_image_metadata,
    parse_metadata_fields,
)
from astrbot_plugin_image_studio.backend.metadata.exchange import (
    export_parameters,
    resolve_parameters,
)
from astrbot_plugin_image_studio.backend.gallery.store import GenerationStore
from astrbot_plugin_image_studio.tests.backend.test_comfy_candidates import parse
from astrbot_plugin_image_studio.tests.backend.test_image_metadata import (
    api_graph,
    encoded_image,
)
from astrbot_plugin_image_studio.tests.backend.test_parameter_exchange import settings


def dynamic_graph(*, role: str = "positive") -> dict:
    graph = api_graph()
    graph["8"] = {
        "class_type": "Raffle",
        "inputs": {"filter_out_tags": "filter", "text": "not executed raffle text"},
    }
    graph["9"] = {
        "class_type": "Text Concatenate",
        "inputs": {"text_a": "fixed", "text_b": ["8", 0], "delimiter": ", "},
    }
    graph["2" if role == "positive" else "3"]["inputs"]["text"] = ["9", 0]
    graph["10"] = {
        "class_type": "easy showAnything",
        "inputs": {"anything": ["9", 0], "text": "fixed, random choice"},
    }
    return graph


def snapshots(normalized: dict) -> list[dict]:
    return [
        item
        for item in normalized["prompt_candidates"]
        if item["status"] == "display_snapshot"
    ]


def observer_workflow(text: object, *, producer: int = 9, port: int = 0) -> dict:
    return {
        "nodes": [
            {"id": producer, "type": "Text Concatenate"},
            {
                "id": 10,
                "type": "easy showAnything",
                "inputs": [{"name": "anything", "type": "*", "link": 1}],
                "widgets_values": text,
            },
        ],
        "links": [[1, producer, port, 10, 0, "*"]],
    }


def conflicting_observer_workflow() -> dict:
    workflow = observer_workflow(["fixed, first observed choice"])
    workflow["nodes"].append(
        {
            "id": 11,
            "type": "easy showAnything",
            "inputs": [{"name": "anything", "type": "*", "link": 2}],
            "widgets_values": [["fixed, second observed choice"]],
        }
    )
    workflow["links"].append([2, 9, 0, 11, 0, "*"])
    return workflow


@pytest.mark.parametrize("role,consumer", [("positive", "2"), ("negative", "3")])
def test_shared_output_observation_is_manual_and_carries_exact_provenance(
    role: str, consumer: str
) -> None:
    normalized = parse(dynamic_graph(role=role))
    values = snapshots(normalized)
    assert len(values) == 1
    candidate = values[0]
    assert candidate["text"] == "fixed, random choice"
    assert candidate["source_ref"] == "9:0"
    assert candidate["freshness"] == "unverified"
    assert candidate["snapshot_kind"] == "api_fallback"
    assert candidate["role"] == role
    assert candidate["consumers"] == [{"node_id": consumer, "input_name": "text"}]
    assert candidate["observations"] == [
        {
            "node_id": "10",
            "node_type": "easy showAnything",
            "source": "prompt",
            "field": "inputs.text",
        }
    ]
    assert candidate["output_node_ids"] == ["7"]
    assert candidate["stage_ids"] == ["5"]
    assert not candidate.get("conflicting", False)
    assert not normalized.get("prompt" if role == "positive" else "negative_prompt")
    assert all(item["node_id"] != "8" for item in normalized["prompt_candidates"])


def test_identical_api_and_workflow_snapshots_prefer_workflow_source() -> None:
    graph = dynamic_graph()
    normalized = parse(graph, workflow=observer_workflow(["fixed, random choice"]))
    values = snapshots(normalized)
    assert len(values) == 1
    assert {item["source"] for item in values[0]["observations"]} == {
        "workflow",
    }
    assert values[0]["snapshot_kind"] == "workflow"
    assert not values[0].get("conflicting", False)
    assert not normalized.get("prompt")


def test_workflow_writeback_replaces_previous_api_display_without_changing_raw() -> (
    None
):
    graph = dynamic_graph()
    fields = {
        "prompt": json.dumps(graph),
        "workflow": json.dumps(observer_workflow(["fixed, current choice"])),
    }
    result = parse_metadata_fields(fields)
    values = snapshots(result["normalized"])
    assert [item["text"] for item in values] == ["fixed, current choice"]
    assert values[0]["snapshot_kind"] == "workflow"
    assert not values[0]["conflicting"]
    assert not result["normalized"].get("prompt")
    assert result["raw"] == fields
    assert "fixed, random choice" in result["raw"]["prompt"]


@pytest.mark.parametrize("value", [None, [], "", " ", ["one", "two"]])
def test_missing_or_invalid_workflow_text_keeps_labelled_api_fallback(value) -> None:
    values = snapshots(parse(dynamic_graph(), workflow=observer_workflow(value)))
    assert [item["text"] for item in values] == ["fixed, random choice"]
    assert values[0]["snapshot_kind"] == "api_fallback"
    assert values[0]["observations"][0]["source"] == "prompt"


def test_distinct_observer_conflicts_are_not_automatically_adopted() -> None:
    graph = dynamic_graph()
    values = snapshots(parse(graph, workflow=conflicting_observer_workflow()))
    assert {item["text"] for item in values} == {
        "fixed, first observed choice",
        "fixed, second observed choice",
    }
    assert len({item["id"] for item in values}) == 2
    assert all(item["source_ref"] == "9:0" and item["conflicting"] for item in values)
    assert all(item["freshness"] == "unverified" for item in values)
    assert all(item["snapshot_kind"] == "workflow" for item in values)
    assert all(len(item["observations"]) == 1 for item in values)


def test_multiple_observers_of_same_output_are_deduplicated() -> None:
    graph = dynamic_graph()
    graph["11"] = {
        "class_type": "easy showAnything",
        "inputs": {"anything": ["9", 0], "text": "fixed, random choice"},
    }
    values = snapshots(parse(graph))
    assert len(values) == 1
    assert {item["node_id"] for item in values[0]["observations"]} == {"10", "11"}


@pytest.mark.parametrize(
    "value",
    ["snapshot", ["snapshot"], [["snapshot"]]],
)
def test_api_accepts_unambiguous_single_string_snapshot_shapes(value: object) -> None:
    graph = dynamic_graph()
    graph["10"]["inputs"]["text"] = value
    assert [item["text"] for item in snapshots(parse(graph))] == ["snapshot"]


@pytest.mark.parametrize(
    "value", [[], ["first", "second"], {"text": "snapshot"}, [1], "", " "]
)
def test_api_does_not_guess_at_ambiguous_or_nontext_snapshots(value: object) -> None:
    graph = dynamic_graph()
    graph["10"]["inputs"]["text"] = value
    assert snapshots(parse(graph)) == []


@pytest.mark.parametrize("value", [["snapshot"], [["snapshot"]]])
def test_workflow_accepts_single_text_widget_without_api_snapshot(
    value: object,
) -> None:
    graph = dynamic_graph()
    del graph["10"]["inputs"]["text"]
    values = snapshots(parse(graph, workflow=observer_workflow(value)))
    assert [item["text"] for item in values] == ["snapshot"]
    assert values[0]["observations"][0]["source"] == "workflow"


@pytest.mark.parametrize("value", [["first", "second"], [{"text": "snapshot"}], []])
def test_workflow_does_not_join_multiple_widgets_or_serialize_objects(
    value: object,
) -> None:
    graph = dynamic_graph()
    del graph["10"]["inputs"]["text"]
    assert snapshots(parse(graph, workflow=observer_workflow(value))) == []


@pytest.mark.parametrize("source", [["9", 1], ["1", 0], ["2", 0], ["4", 0]])
def test_unrelated_ports_and_nontext_outputs_are_not_prompt_observations(
    source: list,
) -> None:
    graph = dynamic_graph()
    graph["10"]["inputs"]["anything"] = source
    assert snapshots(parse(graph)) == []


def test_unknown_debug_node_is_not_assumed_to_display_input_verbatim() -> None:
    graph = dynamic_graph()
    graph["10"]["class_type"] = "CustomDebugShowAnything"
    assert snapshots(parse(graph)) == []


def test_nontext_dependency_of_dynamic_text_node_is_not_a_prompt_snapshot() -> None:
    graph = dynamic_graph()
    graph["11"] = {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "conditioning used by extension"},
    }
    graph["8"]["inputs"]["conditioning"] = ["11", 0]
    graph["12"] = {
        "class_type": "easy showAnything",
        "inputs": {"anything": ["11", 0], "text": "tensor representation"},
    }
    assert {item["source_ref"] for item in snapshots(parse(graph))} == {"9:0"}


def test_api_observer_link_takes_precedence_over_conflicting_ui_link() -> None:
    graph = dynamic_graph()
    graph["10"]["inputs"]["anything"] = ["9", 1]
    assert snapshots(parse(graph, workflow=observer_workflow(["snapshot"]))) == []


def test_ui_alias_cannot_override_an_existing_api_alias_input() -> None:
    graph = dynamic_graph()
    del graph["10"]
    graph["22"] = {"class_type": "Reroute", "inputs": {"input": ["8", 1]}}
    workflow = observer_workflow(["stale snapshot"])
    workflow["nodes"].append(
        {
            "id": 22,
            "type": "Reroute",
            "inputs": [{"name": "", "type": "*", "link": 2}],
        }
    )
    workflow["links"] = [[1, 22, 0, 10, 0, "*"], [2, 9, 0, 22, 0, "*"]]
    assert snapshots(parse(graph, workflow=workflow)) == []


@pytest.mark.parametrize("defect", ["duplicate_node", "duplicate_link", "wrong_target"])
def test_ui_snapshot_requires_unambiguous_node_and_link_identity(defect: str) -> None:
    graph = dynamic_graph()
    del graph["10"]
    workflow = observer_workflow(["snapshot"])
    if defect == "duplicate_node":
        workflow["nodes"].append(dict(workflow["nodes"][0]))
    elif defect == "duplicate_link":
        workflow["links"].append(list(workflow["links"][0]))
    else:
        workflow["links"][0][3] = 100
    assert snapshots(parse(graph, workflow=workflow)) == []


def test_cyclic_ui_alias_cannot_be_associated_with_a_main_output() -> None:
    graph = dynamic_graph()
    del graph["10"]
    workflow = observer_workflow(["snapshot"])
    workflow["nodes"].append(
        {
            "id": 22,
            "type": "Reroute",
            "inputs": [{"name": "", "type": "*", "link": 2}],
        }
    )
    workflow["links"] = [[1, 22, 0, 10, 0, "*"], [2, 22, 0, 22, 0, "*"]]
    assert snapshots(parse(graph, workflow=workflow)) == []


def test_known_static_text_does_not_need_or_get_overwritten_by_observer_snapshot() -> (
    None
):
    graph = dynamic_graph()
    graph["9"]["inputs"]["text_b"] = "static second fragment"
    normalized = parse(graph)
    assert normalized["prompt"] == "fixed, static second fragment"
    assert snapshots(normalized) == []


def test_shared_positive_and_negative_input_preserves_both_consumers() -> None:
    graph = dynamic_graph()
    graph["3"]["inputs"]["text"] = ["9", 0]
    values = snapshots(parse(graph))
    assert len(values) == 1
    assert values[0]["role"] == "mixed"
    assert {item["node_id"] for item in values[0]["consumers"]} == {"2", "3"}


def test_multiple_saves_limit_observers_to_selected_branch() -> None:
    graph = dynamic_graph()
    graph["11"] = {"class_type": "CLIPTextEncode", "inputs": {"text": "branch two"}}
    graph["12"] = {
        "class_type": "KSampler",
        "inputs": {**graph["5"]["inputs"], "positive": ["11", 0]},
    }
    graph["13"] = {"class_type": "VAEDecode", "inputs": {"samples": ["12", 0]}}
    graph["14"] = {"class_type": "SaveImage", "inputs": {"images": ["13", 0]}}
    assert snapshots(parse(graph)) == []
    assert snapshots(parse(graph, output_node_id="14")) == []
    assert len(snapshots(parse(graph, output_node_id="7"))) == 1


def test_ui_only_observer_can_follow_unique_get_set_and_reroute() -> None:
    graph = dynamic_graph()
    del graph["10"]
    workflow = observer_workflow(["snapshot"])
    workflow["nodes"].extend(
        [
            {
                "id": 20,
                "type": "SetNode",
                "widgets_values": ["shared text"],
                "inputs": [{"name": "STRING", "type": "STRING", "link": 2}],
            },
            {"id": 21, "type": "GetNode", "widgets_values": ["shared text"]},
            {
                "id": 22,
                "type": "Reroute",
                "inputs": [{"name": "", "type": "*", "link": 3}],
            },
        ]
    )
    workflow["links"] = [
        [1, 22, 0, 10, 0, "*"],
        [2, 9, 0, 20, 0, "STRING"],
        [3, 21, 0, 22, 0, "STRING"],
    ]
    values = snapshots(parse(graph, workflow=workflow))
    assert len(values) == 1 and values[0]["source_ref"] == "9:0"
    workflow["nodes"].append(
        {
            "id": 23,
            "type": "SetNode",
            "widgets_values": ["shared text"],
            "inputs": [{"name": "STRING", "type": "STRING", "link": 4}],
        }
    )
    workflow["links"].append([4, 8, 0, 23, 0, "STRING"])
    assert snapshots(parse(graph, workflow=workflow)) == []


def test_import_export_and_resolve_keep_snapshot_manual_and_preserve_raw(
    tmp_path,
) -> None:
    graph = dynamic_graph()
    raw = {
        "prompt": json.dumps(graph),
        "workflow": json.dumps(observer_workflow(["fixed, older choice"])),
    }
    image = encoded_image(fields=raw)

    async def run() -> None:
        store = GenerationStore(tmp_path)
        await store.initialize()
        item = await store.import_image(
            image, "snapshot.png", {"prompt": "fixed, chosen by user"}
        )
        detail = await store.generation_detail(
            item["generation_id"], include_assets=False
        )
        assert detail["original_prompt"] == "fixed, chosen by user"
        assert detail["images"][0]["metadata"]["raw"] == raw
        assert not detail["images"][0]["metadata"]["normalized"].get("prompt")
        assert len(snapshots(detail["images"][0]["metadata"]["normalized"])) == 1
        exported = export_parameters(detail)
        restored = resolve_parameters(
            exported["content"], settings(), "nai:nai-diffusion-4-5-full"
        )
        assert restored["draft"]["prompt"] == "fixed, chosen by user"
        assert len(snapshots(restored["unmapped"])) == 1
        assert (
            export_parameters(detail, format_name="workflow")["content"]
            == raw["workflow"]
        )
        fresh = resolve_parameters(
            raw["prompt"], settings(), "nai:nai-diffusion-4-5-full"
        )
        assert fresh["draft"]["prompt"] == ""

    asyncio.run(run())


@pytest.mark.parametrize("output", ["188", "189"])
def test_available_raffle_sample_prefers_current_workflow_snapshot(output: str) -> None:
    sample = Path(__file__).parents[2] / "data" / "image" / "Anima_00002_.png"
    if not sample.exists():
        pytest.skip("本地参考样本不在发布仓库中")
    metadata = parse_image_metadata(sample.read_bytes())
    selected = parse_metadata_fields(metadata["raw"], output_node_id=output)
    values = snapshots(selected["normalized"])
    assert [len(item["text"]) for item in values] == [610]
    assert all(item["source_ref"] == "137:0" for item in values)
    assert all(
        not item["conflicting"] and item["snapshot_kind"] == "workflow"
        for item in values
    )
    assert {
        observation["node_id"]
        for candidate in values
        for observation in candidate["observations"]
    } == {"143"}
    assert {entry["id"] for entry in values[0].get("covered_candidates", [])} == {
        "119:text",
        "120:text",
    }
    assert selected["raw"] == metadata["raw"]
    assert not selected["normalized"].get("prompt")
