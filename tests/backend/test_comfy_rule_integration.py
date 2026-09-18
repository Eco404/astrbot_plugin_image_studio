from __future__ import annotations

import copy
import json

from astrbot_plugin_image_studio.backend.metadata.node_rules import (
    make_rules,
    prepare_rule,
    use_rules,
)
from astrbot_plugin_image_studio.backend.metadata.parser import parse_metadata_fields
from astrbot_plugin_image_studio.tests.backend.test_comfy_auto_snapshots import workflow
from astrbot_plugin_image_studio.tests.backend.test_comfy_display_snapshots import (
    dynamic_graph,
)
from astrbot_plugin_image_studio.tests.backend.test_image_metadata import api_graph


def fields(graph, descriptions):
    ui = workflow(graph)
    for node in ui["nodes"]:
        info = descriptions.get(str(node["id"]))
        if info is None:
            continue
        node["outputs"] = (
            [{"name": "text", "type": "STRING"}] if info.get("output", True) else []
        )
        for item in node["inputs"]:
            item["type"] = "STRING"
        present = {item["name"] for item in node["inputs"]}
        for name, value in graph[str(node["id"])]["inputs"].items():
            if name not in present:
                node["inputs"].append(
                    {
                        "name": name,
                        "type": "STRING" if isinstance(value, str) else "BOOLEAN",
                        "link": None,
                    }
                )
        if "widgets" in info:
            node["widgets_values"] = info["widgets"]
    return {"prompt": json.dumps(graph), "workflow": json.dumps(ui)}


def rule_for(raw, node_id, operation, inputs, **kw):
    return prepare_rule(
        raw,
        node_id,
        {
            "scope": "workflow",
            "operation": operation,
            "inputs": inputs,
            "output_port": 0,
            "delimiter": "",
            "strip": False,
            **kw,
        },
    )


def test_literal_user_rule_is_scoped_and_provenance_is_not_polarity():
    graph = api_graph()
    graph["20"] = {
        "class_type": "UserTextSource",
        "inputs": {"negative_prompt": "a sunny landscape"},
    }
    graph["2"]["inputs"]["text"] = ["20", 0]
    raw = fields(graph, {"20": {}})
    before = parse_metadata_fields(raw)
    assert not before["normalized"].get("prompt")
    assert before["normalized"]["node_rule_candidates"][0]["node_id"] == "20"
    rule = rule_for(raw, "20", "literal", ["negative_prompt"])
    with use_rules(make_rules([rule])):
        parsed = parse_metadata_fields(raw)
    normalized = parsed["normalized"]
    assert normalized["prompt"] == "a sunny landscape"
    assert normalized["prompt_status"] == "declared"
    assert normalized["prompt_sources"][0]["kind"] == "user_rule"
    assert normalized["prompt_sources"][0]["target"] == "prompt"
    assert normalized["negative_prompt"] == before["normalized"]["negative_prompt"]
    assert parsed["raw"] == raw
    assert parsed["rules_fingerprint"] != before["rules_fingerprint"]
    assert not parse_metadata_fields(raw)["normalized"].get("prompt")


def test_explicit_concat_uses_selected_order_and_control_changes_invalidate():
    graph = api_graph()
    graph["20"] = {
        "class_type": "UserJoin",
        "inputs": {"a": "first", "b": "second", "enabled": True},
    }
    graph["2"]["inputs"]["text"] = ["20", 0]
    raw = fields(graph, {"20": {}})
    rule = rule_for(raw, "20", "concat", ["b", "a"], delimiter=" | ")
    with use_rules(make_rules([rule])):
        result = parse_metadata_fields(raw)["normalized"]
        assert result["prompt"] == "second | first"
        assert result["prompt_status"] == "declared"
        changed = copy.deepcopy(raw)
        graph["20"]["inputs"]["enabled"] = False
        changed["prompt"] = json.dumps(graph)
        assert not parse_metadata_fields(changed)["normalized"].get("prompt")


def test_explicit_passthrough_can_carry_trusted_snapshot_but_unknown_cannot():
    graph = dynamic_graph()
    graph["20"] = {"class_type": "UserPassThrough", "inputs": {"text": ["9", 0]}}
    graph["2"]["inputs"]["text"] = ["20", 0]
    raw = fields(graph, {"20": {}})
    assert not parse_metadata_fields(raw)["normalized"].get("prompt")
    rule = rule_for(raw, "20", "passthrough", ["text"])
    with use_rules(make_rules([rule])):
        result = parse_metadata_fields(raw)["normalized"]
    assert result["prompt"] == "current complete prompt"
    assert result["prompt_status"] == "snapshot"
    assert {source["kind"] for source in result["prompt_sources"]} == {
        "user_rule",
        "display_snapshot",
    }


def test_manual_observer_reads_only_selected_workflow_widget():
    graph = dynamic_graph()
    graph["10"] = {"class_type": "UserDisplay", "inputs": {"content": ["9", 0]}}
    raw = fields(graph, {"10": {"output": False, "widgets": ["label", "saved result"]}})
    assert not parse_metadata_fields(raw)["normalized"].get("prompt")
    rule = rule_for(
        raw, "10", "observer", ["content"], output_port=None, widget_index=1
    )
    with use_rules(make_rules([rule])):
        result = parse_metadata_fields(raw)["normalized"]
    assert result["prompt"] == "saved result"
    assert result["prompt_status"] == "snapshot"
    assert result["prompt_sources"][0]["observations"][0]["origin"] == "user"
