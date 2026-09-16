from __future__ import annotations

import copy
import json

import pytest
from astrbot_plugin_image_studio.backend.metadata.parser import parse_metadata_fields
from astrbot_plugin_image_studio.tests.backend.test_comfy_candidates import parse
from astrbot_plugin_image_studio.tests.backend.test_comfy_display_snapshots import (
    dynamic_graph,
    snapshots,
)
from astrbot_plugin_image_studio.tests.backend.test_image_metadata import api_graph


def dynamic_without_observer() -> dict:
    graph = dynamic_graph()
    del graph["10"]
    return graph


def workflow_observers(graph: dict, *values: tuple[str, int, str]) -> dict:
    nodes = {
        source: {"id": int(source), "type": graph[source]["class_type"]}
        for source, _, _ in values
    }
    links = []
    for index, (source, port, text) in enumerate(values, start=1):
        observer = 100 + index
        nodes[str(observer)] = {
            "id": observer,
            "type": "easy showAnything",
            "inputs": [{"name": "anything", "type": "*", "link": index}],
            "widgets_values": [text],
        }
        links.append([index, int(source), port, observer, 0, "*"])
    return {"nodes": list(nodes.values()), "links": links}


def candidate_ids(normalized: dict) -> set[str]:
    return {item["id"] for item in normalized["prompt_candidates"]}


def append_text_node(graph: dict, *, source_port: int = 0) -> None:
    graph["11"] = {
        "class_type": "Text Concatenate",
        "inputs": {
            "text_a": ["9", source_port],
            "text_b": "final fragment",
            "delimiter": ", ",
        },
    }
    graph["2"]["inputs"]["text"] = ["11", 0]


def test_workflow_snapshot_covers_its_known_producer_literal_and_preserves_raw() -> (
    None
):
    graph = dynamic_without_observer()
    workflow = workflow_observers(graph, ("9", 0, "fixed, random choice"))
    original_graph, original_workflow = copy.deepcopy(graph), copy.deepcopy(workflow)
    raw = {"prompt": json.dumps(graph), "workflow": json.dumps(workflow)}
    metadata = parse_metadata_fields(raw)
    normalized = metadata["normalized"]
    observed = snapshots(normalized)

    assert "9:text_a" not in candidate_ids(normalized)
    assert len(observed) == 1
    assert observed[0]["source_ref"] == "9:0"
    assert observed[0]["snapshot_kind"] == "workflow"
    assert observed[0]["covered_candidates"] == [
        {
            "id": "9:text_a",
            "node_id": "9",
            "node_type": "Text Concatenate",
            "field": "text_a",
        }
    ]
    assert "3:text" in candidate_ids(normalized)
    assert not normalized.get("prompt")
    assert metadata["raw"] == raw
    assert graph == original_graph and workflow == original_workflow


@pytest.mark.parametrize(
    "upstream,downstream,covered",
    [
        ("fixed phrase", "prefix, fixed phrase, suffix", True),
        ("  fixed phrase\r\nsecond line\r  ", "fixed phrase\nsecond line", True),
        ("cat", "category", False),
        ("cat", "bobcat", False),
        ("fixed phrase", "fixed, phrase", False),
        ("fixed  phrase", "fixed phrase", False),
        ("Fixed phrase", "fixed phrase", False),
        ("first, second", "second, first", False),
        ("fixed phrase", "fixed phrase plus", True),
        ("cat", "category, cat", True),
    ],
)
def test_coverage_requires_literal_containment_without_word_fragment_collisions(
    upstream: str, downstream: str, covered: bool
) -> None:
    graph = dynamic_without_observer()
    graph["9"]["inputs"]["text_a"] = upstream
    normalized = parse(graph, workflow=workflow_observers(graph, ("9", 0, downstream)))
    assert ("9:text_a" not in candidate_ids(normalized)) is covered
    assert snapshots(normalized)[0]["text"] == downstream


def test_known_downstream_text_field_can_cover_a_connected_upstream_field() -> None:
    graph = api_graph()
    graph["8"] = {"class_type": "TextInput_", "inputs": {"text": "fixed phrase"}}
    graph["9"] = {
        "class_type": "Text Concatenate",
        "inputs": {
            "text_a": ["8", 0],
            "text_b": "fixed phrase, final fragment",
            "delimiter": ", ",
        },
    }
    graph["2"]["inputs"]["text"] = ["9", 0]
    normalized = parse(graph)
    assert "8:text" not in candidate_ids(normalized)
    survivor = next(
        item for item in normalized["prompt_candidates"] if item["id"] == "9:text_b"
    )
    assert {item["id"] for item in survivor["covered_candidates"]} == {"8:text"}


def test_same_text_sibling_nodes_remain_distinct() -> None:
    graph = api_graph()
    for identifier in ("8", "9"):
        graph[identifier] = {
            "class_type": "TextInput_",
            "inputs": {"text": "same fragment"},
        }
    graph["11"] = {
        "class_type": "Text Concatenate",
        "inputs": {"text_a": ["8", 0], "text_b": ["9", 0], "delimiter": ", "},
    }
    graph["2"]["inputs"]["text"] = ["11", 0]
    normalized = parse(graph)
    assert {"8:text", "9:text"} <= candidate_ids(normalized)


def test_same_node_literal_fields_do_not_cover_each_other() -> None:
    graph = api_graph()
    graph["9"] = {
        "class_type": "Text Concatenate",
        "inputs": {
            "text_a": "fixed phrase",
            "text_b": "fixed phrase, final fragment",
            "delimiter": ", ",
        },
    }
    graph["2"]["inputs"]["text"] = ["9", 0]
    assert {"9:text_a", "9:text_b"} <= candidate_ids(parse(graph))


def test_snapshot_does_not_cover_equal_text_on_an_unrelated_conditioning_branch() -> (
    None
):
    graph = dynamic_without_observer()
    graph["11"] = {
        "class_type": "TextInput_",
        "inputs": {"text": "fixed, random choice"},
    }
    graph["12"] = {"class_type": "CLIPTextEncode", "inputs": {"text": ["11", 0]}}
    graph["13"] = {
        "class_type": "ConditioningCombine",
        "inputs": {"conditioning_1": ["2", 0], "conditioning_2": ["12", 0]},
    }
    graph["5"]["inputs"]["positive"] = ["13", 0]
    normalized = parse(
        graph,
        workflow=workflow_observers(graph, ("9", 0, "fixed, random choice")),
    )
    assert "11:text" in candidate_ids(normalized)
    assert len(snapshots(normalized)) == 1


def test_equal_text_on_opposite_role_remains_visible() -> None:
    graph = dynamic_without_observer()
    graph["3"]["inputs"]["text"] = "fixed, random choice"
    normalized = parse(
        graph,
        workflow=workflow_observers(graph, ("9", 0, "fixed, random choice")),
    )
    negative = next(
        item for item in normalized["prompt_candidates"] if item["id"] == "3:text"
    )
    assert negative["role"] == "negative"
    assert snapshots(normalized)[0]["role"] == "positive"


def test_unrelated_same_text_snapshots_remain_separate() -> None:
    graph = dynamic_without_observer()
    graph["11"] = {
        "class_type": "Text Concatenate",
        "inputs": {"text_a": "fixed", "text_b": ["8", 0], "delimiter": ", "},
    }
    graph["12"] = {
        "class_type": "Text Concatenate",
        "inputs": {"text_a": ["9", 0], "text_b": ["11", 0], "delimiter": ", "},
    }
    graph["2"]["inputs"]["text"] = ["12", 0]
    observed = snapshots(
        parse(
            graph,
            workflow=workflow_observers(
                graph,
                ("9", 0, "fixed, random choice"),
                ("11", 0, "fixed, random choice"),
            ),
        )
    )
    assert {item["source_ref"] for item in observed} == {"9:0", "11:0"}
    assert len({item["id"] for item in observed}) == 2


def test_downstream_snapshot_keeps_transitive_covered_candidate_identities() -> None:
    graph = dynamic_without_observer()
    append_text_node(graph)
    upstream = snapshots(
        parse(
            graph,
            workflow=workflow_observers(graph, ("9", 0, "fixed, random choice")),
        )
    )[0]
    normalized = parse(
        graph,
        workflow=workflow_observers(
            graph,
            ("9", 0, "fixed, random choice"),
            ("11", 0, "fixed, random choice, final fragment"),
        ),
    )
    observed = snapshots(normalized)
    assert len(observed) == 1 and observed[0]["source_ref"] == "11:0"
    covered = {item["id"] for item in observed[0]["covered_candidates"]}
    assert {"9:text_a", "11:text_b", upstream["id"]} <= covered
    assert "9:text_a" not in candidate_ids(normalized)
    assert not normalized.get("prompt")


def test_partial_stage_coverage_keeps_upstream_snapshot_and_literal() -> None:
    graph = dynamic_without_observer()
    append_text_node(graph)
    graph["2"]["inputs"]["text"] = ["9", 0]
    graph["12"] = {"class_type": "CLIPTextEncode", "inputs": {"text": ["11", 0]}}
    graph["13"] = {
        "class_type": "FaceDetailer",
        "inputs": {"image": ["6", 0], "positive": ["12", 0], "negative": ["3", 0]},
    }
    graph["7"]["inputs"]["images"] = ["13", 0]
    normalized = parse(
        graph,
        workflow=workflow_observers(
            graph, ("11", 0, "fixed, random choice, final fragment")
        ),
    )
    literal = next(
        item for item in normalized["prompt_candidates"] if item["id"] == "9:text_a"
    )
    assert set(literal["stage_ids"]) == {"5", "13"}
    assert snapshots(normalized)[0]["stage_ids"] == ["13"]
    observed = snapshots(
        parse(
            graph,
            workflow=workflow_observers(
                graph,
                ("9", 0, "fixed, random choice"),
                ("11", 0, "fixed, random choice, final fragment"),
            ),
        )
    )
    assert {item["source_ref"] for item in observed} == {"9:0", "11:0"}


def test_mixed_role_upstream_is_not_covered_by_positive_only_downstream() -> None:
    graph = dynamic_without_observer()
    append_text_node(graph)
    graph["3"]["inputs"]["text"] = ["9", 0]
    normalized = parse(
        graph,
        workflow=workflow_observers(
            graph, ("11", 0, "fixed, random choice, final fragment")
        ),
    )
    upstream = next(
        item for item in normalized["prompt_candidates"] if item["id"] == "9:text_a"
    )
    assert upstream["role"] == "mixed"
    assert snapshots(normalized)[0]["role"] == "positive"


def test_matching_mixed_roles_allow_upstream_coverage() -> None:
    graph = dynamic_without_observer()
    append_text_node(graph)
    graph["3"]["inputs"]["text"] = ["11", 0]
    normalized = parse(
        graph,
        workflow=workflow_observers(
            graph, ("11", 0, "fixed, random choice, final fragment")
        ),
    )
    assert "9:text_a" not in candidate_ids(normalized)
    assert snapshots(normalized)[0]["role"] == "mixed"


def test_matching_negative_roles_allow_upstream_coverage_without_autofill() -> None:
    graph = dynamic_graph(role="negative")
    del graph["10"]
    normalized = parse(
        graph,
        workflow=workflow_observers(graph, ("9", 0, "fixed, random choice")),
    )
    assert "9:text_a" not in candidate_ids(normalized)
    assert snapshots(normalized)[0]["role"] == "negative"
    assert not normalized.get("negative_prompt")


def test_alternate_source_port_does_not_cover_observed_output_or_literal() -> None:
    graph = dynamic_without_observer()
    append_text_node(graph, source_port=1)
    graph["12"] = {"class_type": "CLIPTextEncode", "inputs": {"text": ["9", 0]}}
    graph["13"] = {
        "class_type": "ConditioningCombine",
        "inputs": {"conditioning_1": ["2", 0], "conditioning_2": ["12", 0]},
    }
    graph["5"]["inputs"]["positive"] = ["13", 0]
    normalized = parse(
        graph,
        workflow=workflow_observers(
            graph,
            ("9", 0, "fixed, random choice"),
            ("11", 0, "fixed, random choice, final fragment"),
        ),
    )
    observed = snapshots(normalized)
    assert {item["source_ref"] for item in observed} == {"9:0", "11:0"}
    literal = next(
        item for item in normalized["prompt_candidates"] if item["id"] == "9:text_a"
    )
    assert literal["output_ports"] == [0, 1]


def test_api_fallback_snapshot_does_not_hide_upstream_literal() -> None:
    normalized = parse(dynamic_graph())
    assert "9:text_a" in candidate_ids(normalized)
    assert snapshots(normalized)[0]["snapshot_kind"] == "api_fallback"


def test_api_fallback_downstream_cannot_hide_upstream_workflow_snapshot() -> None:
    graph = dynamic_without_observer()
    append_text_node(graph)
    graph["12"] = {
        "class_type": "easy showAnything",
        "inputs": {
            "anything": ["11", 0],
            "text": "fixed, random choice, final fragment",
        },
    }
    normalized = parse(
        graph,
        workflow=workflow_observers(graph, ("9", 0, "fixed, random choice")),
    )
    assert "11:text_b" in candidate_ids(normalized)
    assert {
        item["source_ref"]: item["snapshot_kind"] for item in snapshots(normalized)
    } == {
        "9:0": "workflow",
        "11:0": "api_fallback",
    }


def test_conflicting_workflow_snapshots_cannot_hide_upstream_literal() -> None:
    graph = dynamic_without_observer()
    normalized = parse(
        graph,
        workflow=workflow_observers(
            graph,
            ("9", 0, "fixed, first choice"),
            ("9", 0, "fixed, second choice"),
        ),
    )
    assert "9:text_a" in candidate_ids(normalized)
    assert len(snapshots(normalized)) == 2
    assert all(item["conflicting"] for item in snapshots(normalized))


def test_conflicting_upstream_snapshots_remain_available_under_full_downstream() -> (
    None
):
    graph = dynamic_without_observer()
    append_text_node(graph)
    normalized = parse(
        graph,
        workflow=workflow_observers(
            graph,
            ("9", 0, "fixed, first choice"),
            ("9", 0, "fixed, second choice"),
            ("11", 0, "fixed, first choice; fixed, second choice, final fragment"),
        ),
    )
    upstream = [item for item in snapshots(normalized) if item["source_ref"] == "9:0"]
    assert len(upstream) == 2
    assert all(item["conflicting"] for item in upstream)


def test_nontext_dependency_is_not_a_confirmed_text_dataflow() -> None:
    graph = dynamic_without_observer()
    graph["11"] = {"class_type": "TextInput_", "inputs": {"text": "fixed"}}
    graph["9"]["inputs"]["metadata"] = ["11", 0]
    normalized = parse(
        graph,
        workflow=workflow_observers(graph, ("9", 0, "fixed, random choice")),
    )
    assert "11:text" in candidate_ids(normalized)


def test_unknown_transform_does_not_prove_its_input_reaches_snapshot_output() -> None:
    graph = dynamic_without_observer()
    graph["11"] = {"class_type": "TextInput_", "inputs": {"text": "fixed"}}
    graph["12"] = {"class_type": "CustomTextTransform", "inputs": {"text": ["11", 0]}}
    graph["9"]["inputs"]["text_a"] = ["12", 0]
    normalized = parse(
        graph,
        workflow=workflow_observers(graph, ("9", 0, "fixed, random choice")),
    )
    assert "11:text" in candidate_ids(normalized)


@pytest.mark.parametrize("defect", ["nontext_input", "duplicate_ui_node"])
def test_generic_intermediate_requires_unambiguous_text_input_declaration(
    defect: str,
) -> None:
    graph = dynamic_without_observer()
    graph["11"] = {"class_type": "TextInput_", "inputs": {"text": "fixed"}}
    graph["12"] = {"class_type": "CustomTextTransform", "inputs": {"text": ["11", 0]}}
    graph["9"]["inputs"]["text_a"] = ["12", 0]
    workflow = workflow_observers(graph, ("9", 0, "fixed, random choice"))
    intermediate = {
        "id": 12,
        "type": "CustomTextTransform",
        "inputs": [
            {"name": "text", "type": "MODEL" if defect == "nontext_input" else "STRING"}
        ],
        "outputs": [{"name": "result", "type": "STRING"}],
    }
    workflow["nodes"].append(intermediate)
    if defect == "duplicate_ui_node":
        workflow["nodes"].append(copy.deepcopy(intermediate))
    normalized = parse(graph, workflow=workflow)
    assert "11:text" in candidate_ids(normalized)


def test_unknown_producer_literal_does_not_cover_its_upstream_literal() -> None:
    graph = api_graph()
    graph["8"] = {"class_type": "TextInput_", "inputs": {"text": "fixed phrase"}}
    graph["9"] = {
        "class_type": "CustomTextTransform",
        "inputs": {"text": ["8", 0], "prompt": "fixed phrase, final fragment"},
    }
    graph["2"]["inputs"]["text"] = ["9", 0]
    assert {"8:text", "9:prompt"} <= candidate_ids(parse(graph))


def test_generic_multi_output_snapshot_cannot_hide_its_producer_literal() -> None:
    graph = dynamic_without_observer()
    graph["9"] = {
        "class_type": "CustomTextGenerator",
        "inputs": {"text": "fixed", "source": ["8", 0]},
    }
    workflow = workflow_observers(graph, ("9", 0, "fixed, random choice"))
    workflow["nodes"][0]["outputs"] = [
        {"name": "first", "type": "STRING"},
        {"name": "second", "type": "STRING"},
    ]
    normalized = parse(graph, workflow=workflow)
    assert "9:text" in candidate_ids(normalized)
    assert snapshots(normalized)[0]["source_ref"] == "9:0"
