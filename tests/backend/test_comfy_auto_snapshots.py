from __future__ import annotations

import asyncio
import copy
import json

import pytest

from astrbot_plugin_image_studio.backend.gallery.store import GenerationStore
from astrbot_plugin_image_studio.backend.metadata.exchange import (
    export_parameters,
    resolve_parameters,
)
from astrbot_plugin_image_studio.backend.metadata.parser import parse_metadata_fields
from astrbot_plugin_image_studio.tests.backend.test_comfy_candidates import parse
from astrbot_plugin_image_studio.tests.backend.test_comfy_display_snapshots import (
    dynamic_graph,
    snapshots,
)
from astrbot_plugin_image_studio.tests.backend.test_image_metadata import encoded_image
from astrbot_plugin_image_studio.tests.backend.test_parameter_exchange import settings


def workflow(graph: dict, displays: dict[str, str] | None = None) -> dict:
    """Create both serialized graphs; each UI connection has an explicit slot."""
    nodes, links = [], []
    for identifier, record in graph.items():
        node = {
            "id": int(identifier),
            "type": record["class_type"],
            "mode": 0,
            "inputs": [],
        }
        for name, value in record["inputs"].items():
            if (
                isinstance(value, list)
                and len(value) == 2
                and str(value[0]) in graph
                and type(value[1]) is int
            ):
                slot, link_id = len(node["inputs"]), len(links) + 1
                node["inputs"].append({"name": name, "link": link_id, "type": "*"})
                links.append(
                    [link_id, int(value[0]), value[1], int(identifier), slot, "*"]
                )
        if record["class_type"] == "easy showAnything":
            text = (displays or {}).get(identifier, "current complete prompt")
            node["widgets_values"] = [text]
        nodes.append(node)
    return {"nodes": nodes, "links": links}


def ui_node(ui: dict, identifier: str) -> dict:
    return next(node for node in ui["nodes"] if str(node["id"]) == identifier)


def ui_input(ui: dict, identifier: str, name: str) -> dict:
    return next(
        value for value in ui_node(ui, identifier)["inputs"] if value["name"] == name
    )


def ui_link(ui: dict, identifier: str, name: str) -> list:
    link_id = ui_input(ui, identifier, name)["link"]
    return next(value for value in ui["links"] if value[0] == link_id)


@pytest.mark.parametrize(
    "role,field,encoder",
    [("positive", "prompt", "2"), ("negative", "negative_prompt", "3")],
)
def test_auto_snapshot_uses_current_workflow_text_and_records_provenance(
    role: str, field: str, encoder: str
) -> None:
    graph = dynamic_graph(role=role)
    raw = {"prompt": json.dumps(graph), "workflow": json.dumps(workflow(graph))}
    parsed = parse_metadata_fields(raw)
    normalized = parsed["normalized"]
    assert normalized[field] == "current complete prompt"
    assert normalized[f"{field}_status"] == "snapshot"
    assert parsed["raw"] == raw
    candidate = snapshots(normalized)[0]
    assert candidate["auto_applied_to"] == [{"stage_id": "5", "target": field}]
    assert candidate["snapshot_kind"] == "workflow"
    assert candidate["freshness"] == "unverified"
    source = normalized[f"{field}_sources"][0]
    assert source == {
        "kind": "display_snapshot",
        "candidate_id": candidate["id"],
        "source_ref": "9:0",
        "observations": candidate["observations"],
        "conditioning_node_id": encoder,
        "input_name": "text",
        "stage_id": "5",
        "target": field,
        "freshness": "unverified",
    }
    assert normalized["stages"][0][f"{field}_sources"] == [source]
    assert any("快照未与本次执行独立校验" in text for text in parsed["warnings"])


@pytest.mark.parametrize(
    "defect",
    [
        "no_workflow",
        "no_ui_encoder",
        "no_ui_sampler",
        "encoder_link_missing",
        "sampler_link_missing",
        "encoder_link_port",
        "sampler_link_port",
        "encoder_type",
        "sampler_type",
        "observer_type",
        "observer_inactive",
        "producer_inactive",
        "encoder_inactive",
        "sampler_inactive",
        "duplicate_encoder",
        "duplicate_text_link",
        "snapshot_empty",
        "snapshot_ambiguous",
    ],
)
def test_incomplete_or_inconsistent_evidence_keeps_snapshot_manual(defect: str) -> None:
    graph = dynamic_graph()
    ui = workflow(graph)
    if defect == "no_workflow":
        ui = None
    elif defect == "no_ui_encoder":
        ui["nodes"].remove(ui_node(ui, "2"))
    elif defect == "no_ui_sampler":
        ui["nodes"].remove(ui_node(ui, "5"))
    elif defect == "encoder_link_missing":
        ui_input(ui, "2", "text")["link"] = None
    elif defect == "sampler_link_missing":
        ui_input(ui, "5", "positive")["link"] = None
    elif defect == "encoder_link_port":
        ui_link(ui, "2", "text")[2] = 1
    elif defect == "sampler_link_port":
        ui_link(ui, "5", "positive")[2] = 1
    elif defect.endswith("_type"):
        identifier = {"encoder_type": "2", "sampler_type": "5", "observer_type": "10"}[
            defect
        ]
        ui_node(ui, identifier)["type"] = "UnrelatedNode"
    elif defect.endswith("_inactive"):
        identifier = {
            "observer_inactive": "10",
            "producer_inactive": "9",
            "encoder_inactive": "2",
            "sampler_inactive": "5",
        }[defect]
        ui_node(ui, identifier)["mode"] = 2
    elif defect == "duplicate_encoder":
        ui["nodes"].append(copy.deepcopy(ui_node(ui, "2")))
    elif defect == "duplicate_text_link":
        ui["links"].append(copy.deepcopy(ui_link(ui, "2", "text")))
    elif defect == "snapshot_empty":
        ui_node(ui, "10")["widgets_values"] = [""]
    elif defect == "snapshot_ambiguous":
        ui_node(ui, "10")["widgets_values"] = ["one", "two"]
    normalized = parse(graph, workflow=ui)
    assert not normalized.get("prompt")
    assert normalized["prompt_status"] == "missing"
    assert not normalized.get("prompt_sources")
    assert all(
        not candidate.get("auto_applied_to") for candidate in snapshots(normalized)
    )


@pytest.mark.parametrize("same_text", [True, False])
def test_multiple_observers_require_one_unambiguous_workflow_text(
    same_text: bool,
) -> None:
    graph = dynamic_graph()
    graph["11"] = copy.deepcopy(graph["10"])
    ui = workflow(
        graph,
        {
            "10": "current complete prompt",
            "11": "current complete prompt" if same_text else "a conflicting prompt",
        },
    )
    normalized = parse(graph, workflow=ui)
    candidates = snapshots(normalized)
    if same_text:
        assert normalized["prompt"] == "current complete prompt"
        assert len(candidates) == 1
        assert len(candidates[0]["observations"]) == 2
        assert len(normalized["prompt_sources"]) == 1
    else:
        assert not normalized.get("prompt")
        assert len(candidates) == 2
        assert all(candidate["conflicting"] for candidate in candidates)
        assert not any(candidate.get("auto_applied_to") for candidate in candidates)


def test_static_text_is_not_overwritten_by_saved_display() -> None:
    graph = dynamic_graph()
    graph["8"] = {"class_type": "TextInput_", "inputs": {"text": "known choice"}}
    normalized = parse(graph, workflow=workflow(graph))
    assert normalized["prompt"] == "fixed, known choice"
    assert normalized["prompt_status"] == "exact"
    assert not normalized.get("prompt_sources")
    assert snapshots(normalized) == []


@pytest.mark.parametrize("kind", ["text", "conditioning"])
def test_unknown_intermediate_operation_does_not_adopt_upstream_snapshot(
    kind: str,
) -> None:
    graph = dynamic_graph()
    if kind == "text":
        graph["11"] = {
            "class_type": "UnknownTextTransform",
            "inputs": {"text": ["9", 0]},
        }
        graph["2"]["inputs"]["text"] = ["11", 0]
    else:
        graph["11"] = {
            "class_type": "UnknownConditioningTransform",
            "inputs": {"conditioning": ["2", 0]},
        }
        graph["5"]["inputs"]["positive"] = ["11", 0]
    normalized = parse(graph, workflow=workflow(graph))
    assert not normalized.get("prompt")
    assert normalized["prompt_status"] == "missing"
    assert not normalized.get("prompt_sources")
    assert snapshots(normalized)
    assert not any(
        candidate.get("auto_applied_to") for candidate in snapshots(normalized)
    )


@pytest.mark.parametrize("shared_encoder", [True, False])
@pytest.mark.parametrize("break_negative", [True, False])
def test_positive_and_negative_evidence_is_verified_independently(
    shared_encoder: bool, break_negative: bool
) -> None:
    graph = dynamic_graph()
    if shared_encoder:
        graph["5"]["inputs"]["negative"] = ["2", 0]
    else:
        graph["3"]["inputs"]["text"] = ["9", 0]
    ui = workflow(graph)
    if break_negative:
        ui_link(ui, "5", "negative")[2] = 1
    normalized = parse(graph, workflow=ui)
    assert normalized["prompt"] == "current complete prompt"
    candidate = snapshots(normalized)[0]
    assert candidate["role"] == "mixed"
    if break_negative:
        assert not normalized.get("negative_prompt")
        assert not normalized.get("negative_prompt_sources")
        assert candidate["auto_applied_to"] == [{"stage_id": "5", "target": "prompt"}]
    else:
        assert normalized["negative_prompt"] == "current complete prompt"
        assert normalized["negative_prompt_sources"][0]["target"] == "negative_prompt"
        assert {item["target"] for item in candidate["auto_applied_to"]} == {
            "prompt",
            "negative_prompt",
        }


@pytest.mark.parametrize("break_dynamic_edge", [True, False])
def test_known_conditioning_combination_is_summary_with_path_scoped_sources(
    break_dynamic_edge: bool,
) -> None:
    graph = dynamic_graph()
    graph["11"] = {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "a second known prompt"},
    }
    graph["12"] = {
        "class_type": "ConditioningCombine",
        "inputs": {"conditioning_1": ["2", 0], "conditioning_2": ["11", 0]},
    }
    graph["5"]["inputs"]["positive"] = ["12", 0]
    ui = workflow(graph)
    if break_dynamic_edge:
        ui_input(ui, "12", "conditioning_1")["link"] = None
    normalized = parse(graph, workflow=ui)
    if break_dynamic_edge:
        assert normalized["prompt"] == "a second known prompt"
        assert normalized["prompt_status"] == "partial"
        assert not normalized.get("prompt_sources")
    else:
        assert (
            normalized["prompt"] == "current complete prompt\n\na second known prompt"
        )
        assert normalized["prompt_status"] == "summary"
        assert normalized["prompt_sources"][0]["conditioning_node_id"] == "2"


def test_zeroed_conditioning_never_reintroduces_displayed_text() -> None:
    graph = dynamic_graph()
    graph["11"] = {
        "class_type": "ConditioningZeroOut",
        "inputs": {"conditioning": ["2", 0]},
    }
    graph["5"]["inputs"]["positive"] = ["11", 0]
    normalized = parse(graph, workflow=workflow(graph))
    assert normalized["prompt"] == ""
    assert normalized["prompt_status"] == "summary"
    assert not normalized.get("prompt_sources")
    assert not any(
        candidate.get("auto_applied_to") for candidate in snapshots(normalized)
    )


def test_multiple_outputs_require_explicit_branch_before_adopting_snapshot() -> None:
    graph = dynamic_graph()
    graph["11"] = {"class_type": "SaveImage", "inputs": {"images": ["6", 0]}}
    ui = workflow(graph)
    undecided = parse(graph, workflow=ui)
    assert undecided["requires_output_selection"]
    assert not undecided.get("prompt")
    assert not undecided.get("prompt_sources")
    chosen = parse(graph, workflow=ui, output_node_id="11")
    assert chosen["prompt"] == "current complete prompt"
    assert snapshots(chosen)[0]["output_node_ids"] == ["11"]


@pytest.mark.parametrize(
    "defect",
    [
        "missing_save",
        "inactive_save",
        "inactive_decoder",
        "save_link_node",
        "save_link_port",
        "decoder_link_node",
        "decoder_link_missing",
    ],
)
def test_saved_branch_must_match_api_and_workflow_graphs(defect: str) -> None:
    graph = dynamic_graph()
    ui = workflow(graph)
    if defect == "missing_save":
        ui["nodes"].remove(ui_node(ui, "7"))
    elif defect == "inactive_save":
        ui_node(ui, "7")["mode"] = 2
    elif defect == "inactive_decoder":
        ui_node(ui, "6")["mode"] = 2
    elif defect == "save_link_node":
        ui_link(ui, "7", "images")[1] = 5
    elif defect == "save_link_port":
        ui_link(ui, "7", "images")[2] = 1
    elif defect == "decoder_link_node":
        ui_link(ui, "6", "samples")[1] = 4
    elif defect == "decoder_link_missing":
        ui_input(ui, "6", "samples")["link"] = None
    normalized = parse(graph, workflow=ui)
    assert not normalized.get("prompt")
    assert not normalized.get("prompt_sources")
    assert snapshots(normalized)
    assert not any(
        candidate.get("auto_applied_to") for candidate in snapshots(normalized)
    )


def test_workflow_only_metadata_does_not_claim_execution_graph_evidence() -> None:
    graph = dynamic_graph()
    ui = workflow(graph)
    result = parse_metadata_fields({"workflow": json.dumps(ui)})
    normalized = result["normalized"]
    assert normalized["selected_output_node"] == "7"
    assert snapshots(normalized)
    assert not normalized.get("prompt")
    assert not normalized.get("prompt_sources")
    assert not any(
        candidate.get("auto_applied_to") for candidate in snapshots(normalized)
    )


def test_ui_only_observer_keeps_saved_text_available_without_auto_adoption() -> None:
    graph = dynamic_graph()
    ui = workflow(graph)
    del graph["10"]
    normalized = parse(graph, workflow=ui)
    assert [candidate["text"] for candidate in snapshots(normalized)] == [
        "current complete prompt"
    ]
    assert not normalized.get("prompt")
    assert not normalized.get("prompt_sources")


def test_duplicate_named_consumer_inputs_are_not_unambiguous_evidence() -> None:
    graph = dynamic_graph()
    ui = workflow(graph)
    ui_node(ui, "2")["inputs"].append({"name": "text", "link": None})
    normalized = parse(graph, workflow=ui)
    assert not normalized.get("prompt")
    assert not normalized.get("prompt_sources")


def test_unknown_image_postprocessor_does_not_change_prompt_role() -> None:
    graph = dynamic_graph()
    graph["11"] = {
        "class_type": "CustomImagePostprocess",
        "inputs": {"images": ["6", 0]},
    }
    graph["7"]["inputs"]["images"] = ["11", 0]
    normalized = parse(graph, workflow=workflow(graph))
    assert normalized["prompt"] == "current complete prompt"
    assert normalized["prompt_status"] == "snapshot"


@pytest.mark.parametrize("first_stage_empty", [False, True])
def test_different_sampling_stages_keep_separate_text_and_provenance(
    first_stage_empty: bool,
) -> None:
    graph = dynamic_graph()
    graph["11"] = {
        "class_type": "Raffle",
        "inputs": {"text": "unresolved second stage"},
    }
    graph["12"] = {"class_type": "CLIPTextEncode", "inputs": {"text": ["11", 0]}}
    graph["13"] = {
        "class_type": "easy showAnything",
        "inputs": {"anything": ["11", 0], "text": "stale second stage"},
    }
    graph["14"] = {
        "class_type": "KSampler",
        "inputs": {
            **graph["5"]["inputs"],
            "positive": ["12", 0],
            "latent_image": ["5", 0],
        },
    }
    graph["6"]["inputs"]["samples"] = ["14", 0]
    if first_stage_empty:
        graph["2"]["inputs"]["text"] = ""
    ui = workflow(graph, {"10": "first stage prompt", "13": "second stage prompt"})
    normalized = parse(graph, workflow=ui)
    assert normalized["prompt"] == (
        "second stage prompt"
        if first_stage_empty
        else "first stage prompt\n\nsecond stage prompt"
    )
    assert normalized["prompt_status"] == "summary"
    stages = {stage["node_id"]: stage for stage in normalized["stages"]}
    assert stages["5"]["prompt"] == ("" if first_stage_empty else "first stage prompt")
    assert stages["14"]["prompt"] == "second stage prompt"
    assert [source["stage_id"] for source in normalized["prompt_sources"]] == (
        ["14"] if first_stage_empty else ["5", "14"]
    )
    assert [source["source_ref"] for source in normalized["prompt_sources"]] == (
        ["11:0"] if first_stage_empty else ["9:0", "11:0"]
    )


@pytest.mark.parametrize("override", [None, "user chosen prompt", ""])
def test_parser_upgrade_preserves_raw_and_explicit_import_overrides(
    tmp_path, monkeypatch, override: str | None
) -> None:
    import astrbot_plugin_image_studio.backend.metadata.parser as metadata_module

    graph = dynamic_graph()
    raw = {"prompt": json.dumps(graph), "workflow": json.dumps(workflow(graph))}
    data = encoded_image(fields=raw)
    current_version = metadata_module.PARSER_VERSION
    current_parser = metadata_module.parse_image_metadata

    def old_parser(data: bytes) -> dict:
        result = current_parser(data)
        result["parser_version"] = current_version - 1
        normalized = result["normalized"]
        normalized.pop("prompt", None)
        normalized.pop("prompt_sources", None)
        normalized["prompt_status"] = "missing"
        for stage in normalized["stages"]:
            stage.pop("prompt", None)
            stage.pop("prompt_sources", None)
            stage["prompt_status"] = "missing"
        for candidate in snapshots(normalized):
            candidate.pop("auto_applied_to", None)
        return result

    async def run() -> None:
        monkeypatch.setattr(metadata_module, "PARSER_VERSION", current_version - 1)
        monkeypatch.setattr(metadata_module, "parse_image_metadata", old_parser)
        store = GenerationStore(tmp_path)
        await store.initialize()
        imported = await store.import_image(
            data, "snapshot.png", {} if override is None else {"prompt": override}
        )
        before = await store.generation_detail(
            imported["generation_id"], include_assets=False
        )
        monkeypatch.setattr(metadata_module, "PARSER_VERSION", current_version)
        monkeypatch.setattr(metadata_module, "parse_image_metadata", current_parser)
        await GenerationStore(tmp_path).initialize()
        after = await store.generation_detail(
            imported["generation_id"], include_assets=False
        )
        image = after["images"][0]
        assert image["metadata"]["raw"] == raw
        assert image["metadata"]["normalized"]["prompt"] == "current complete prompt"
        assert image["metadata"]["normalized"]["prompt_sources"]
        assert (
            image["supplemental"]["overrides"]
            == before["images"][0]["supplemental"]["overrides"]
        )
        assert after["original_prompt"] == (
            "current complete prompt" if override is None else override
        )
        assert after["created_at"] == before["created_at"]
        assert (
            export_parameters(after, format_name="workflow")["content"]
            == raw["workflow"]
        )
        exported = export_parameters(after)
        restored = resolve_parameters(
            exported["content"], settings(), "nai:nai-diffusion-4-5-full"
        )
        assert restored["draft"]["prompt"] == after["original_prompt"]

    asyncio.run(run())
