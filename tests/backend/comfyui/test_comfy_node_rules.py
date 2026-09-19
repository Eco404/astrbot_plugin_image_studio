from __future__ import annotations

import asyncio
from copy import deepcopy
import json

import pytest

from astrbot_plugin_image_studio.backend.metadata.comfyui.user_rules import (
    RULE_FILE,
    describe_nodes,
    evaluate_text_rule,
    get_rules,
    load_rules,
    make_rules,
    matching_rule,
    prepare_rule,
    use_rules,
)


def fixture() -> dict:
    graph = {
        "1": {
            "class_type": "MysteryText",
            "inputs": {"text": ["2", 0], "mode": "keep"},
        },
        "2": {"class_type": "PrimitiveString", "inputs": {"value": "source"}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": ["1", 0]}},
        "4": {"class_type": "SaveImage", "inputs": {"images": ["3", 0]}},
    }
    workflow = {
        "id": "test-workflow",
        "nodes": [
            {
                "id": 1,
                "type": "MysteryText",
                "inputs": [{"name": "text", "type": "STRING", "link": 10}],
                "outputs": [{"name": "text", "type": "STRING", "links": [11]}],
                "widgets_values": ["keep"],
            },
            {
                "id": 2,
                "type": "PrimitiveString",
                "inputs": [],
                "outputs": [{"name": "text", "type": "STRING", "links": [10]}],
            },
            {
                "id": 3,
                "type": "CLIPTextEncode",
                "inputs": [{"name": "text", "type": "STRING", "link": 11}],
                "outputs": [],
            },
        ],
        "links": [[10, 2, 0, 1, 0, "STRING"], [11, 1, 0, 3, 0, "STRING"]],
    }
    return {"prompt": graph, "workflow": workflow}


def draft(**overrides) -> dict:
    return {
        "operation": "passthrough",
        "inputs": ["text"],
        "output_port": 0,
        **overrides,
    }


def test_builtin_catalog_is_declarative_and_defensively_copied():
    rules = get_rules()
    catalog = rules.catalog
    assert catalog["text_sources"]["PrimitiveString"]["output_port"] == 0
    assert catalog["observers"]["easy showAnything"]["input"] == "anything"
    catalog["widgets"].clear()
    assert rules.catalog["widgets"]
    assert "positive" not in catalog["text_sources"]


def test_manual_rule_is_scoped_and_never_guesses_values():
    raw = fixture()
    rule = prepare_rule(raw, "1", draft())
    rules = make_rules([rule])
    assert rule["origin"] == "user"
    assert rule["scope"] == "workflow"
    assert rule["guards"] == {"mode": "keep"}
    assert matching_rule(raw["prompt"], raw["workflow"], "1", rules) == rule
    assert (
        evaluate_text_rule(
            rule, raw["prompt"]["1"]["inputs"], 0, lambda value: "resolved"
        )
        == "resolved"
    )
    assert (
        evaluate_text_rule(
            rule, raw["prompt"]["1"]["inputs"], 1, lambda value: "resolved"
        )
        is None
    )
    assert (
        evaluate_text_rule(rule, raw["prompt"]["1"]["inputs"], 0, lambda value: None)
        is None
    )
    raw["prompt"]["2"]["inputs"]["value"] = "new content"
    assert matching_rule(raw["prompt"], raw["workflow"], "1", rules) == rule
    raw["prompt"]["1"]["inputs"]["mode"] = "rewrite"
    assert matching_rule(raw["prompt"], raw["workflow"], "1", rules) is None


def test_workflow_scope_rejects_other_workflow_and_changed_wiring():
    raw = fixture()
    rules = make_rules([prepare_rule(raw, "1", draft())])
    raw["workflow"]["id"] = "different-workflow"
    assert matching_rule(raw["prompt"], raw["workflow"], "1", rules) is None
    raw = fixture()
    raw["prompt"]["4"]["inputs"]["images"] = ["2", 0]
    assert matching_rule(raw["prompt"], raw["workflow"], "1", rules) is None


def test_type_scope_requires_all_text_ports_connected():
    raw = fixture()
    rule = prepare_rule(raw, "1", draft(scope="type"))
    rules = make_rules([rule])
    raw["workflow"]["id"] = "different-workflow"
    assert matching_rule(raw["prompt"], raw["workflow"], "1", rules) == rule
    raw["workflow"]["nodes"][0]["inputs"].append(
        {"name": "unused", "type": "STRING", "link": None}
    )
    assert matching_rule(raw["prompt"], raw["workflow"], "1", rules) is None
    with pytest.raises(ValueError, match="未连接"):
        prepare_rule(raw, "1", draft(scope="type"))
    local = prepare_rule(raw, "1", draft())
    assert local["scope"] == "workflow"


def test_unconnected_output_blocks_type_scope():
    raw = fixture()
    raw["workflow"]["nodes"][0]["outputs"].append(
        {"name": "other", "type": "STRING", "links": []}
    )
    descriptor = describe_nodes(raw["prompt"], raw["workflow"], {"1"})[0]
    assert not descriptor["type_scope_allowed"]
    with pytest.raises(ValueError, match="未连接"):
        prepare_rule(raw, "1", draft(scope="type"))
    with pytest.raises(ValueError, match="没有连接"):
        prepare_rule(raw, "1", draft(output_port=1))


@pytest.mark.parametrize(
    "change",
    ["absent_workflow", "wrong_type", "different_link", "duplicate_node", "muted"],
)
def test_incomplete_or_inconsistent_ui_never_creates_rule(change):
    raw = fixture()
    if change == "absent_workflow":
        raw.pop("workflow")
    elif change == "wrong_type":
        raw["workflow"]["nodes"][0]["type"] = "Other"
    elif change == "different_link":
        raw["workflow"]["links"][0][1] = 99
    elif change == "duplicate_node":
        raw["workflow"]["nodes"].append(deepcopy(raw["workflow"]["nodes"][0]))
    else:
        raw["workflow"]["nodes"][0]["mode"] = 2
    with pytest.raises(ValueError):
        prepare_rule(raw, "1", draft())


@pytest.mark.parametrize(
    "invalid",
    [
        {"role": "positive"},
        {"operation": "opaque_text_transform"},
        {"inputs": []},
        {"inputs": ["text", "text"]},
        {"output_port": -1},
        {"output_port": True},
        {"inputs": ["mode"]},
        {"strip": "yes"},
        {"scope": "unknown"},
    ],
)
def test_ambiguous_or_unsupported_declarations_rejected(invalid):
    with pytest.raises(ValueError):
        prepare_rule(fixture(), "1", draft(**invalid))


def test_literal_rule_reads_only_selected_saved_text():
    raw = fixture()
    raw["prompt"]["1"]["inputs"]["text"] = "  actual content  "
    raw["workflow"]["nodes"][0]["inputs"][0]["link"] = None
    rule = prepare_rule(raw, "1", draft(operation="literal"))
    assert (
        evaluate_text_rule(rule, raw["prompt"]["1"]["inputs"], 0, lambda value: "wrong")
        == "  actual content  "
    )
    with pytest.raises(ValueError, match="仅文本拼接"):
        prepare_rule(raw, "1", draft(operation="literal", strip=True))


def test_concat_uses_explicit_order_and_separator():
    raw = fixture()
    raw["prompt"]["1"]["inputs"]["second"] = "next"
    raw["workflow"]["nodes"][0]["inputs"].append(
        {"name": "second", "type": "STRING", "link": None}
    )
    rule = prepare_rule(
        raw, "1", draft(operation="concat", inputs=["second", "text"], delimiter=" / ")
    )
    result = evaluate_text_rule(
        rule,
        raw["prompt"]["1"]["inputs"],
        0,
        lambda value: value if isinstance(value, str) else "first",
    )
    assert result == "next / first"
    assert rule["guards"] == {"mode": "keep"}


def test_observer_declares_saved_widget_and_does_not_evaluate_output():
    raw = fixture()
    raw["prompt"]["3"]["inputs"]["text"] = ["2", 0]
    raw["workflow"]["nodes"][0]["outputs"] = []
    raw["workflow"]["nodes"][0]["widgets_values"] = ["observed text"]
    rule = prepare_rule(
        raw, "1", draft(operation="observer", output_port=None, widget_index=0)
    )
    assert rule["widget_index"] == 0
    assert (
        evaluate_text_rule(rule, raw["prompt"]["1"]["inputs"], 0, lambda value: "x")
        is None
    )


def test_wildcard_observer_requires_proven_string_source():
    raw = fixture()
    raw["prompt"]["3"]["inputs"]["text"] = ["2", 0]
    raw["workflow"]["nodes"][0]["outputs"] = []
    raw["workflow"]["nodes"][0]["inputs"][0]["type"] = "*"
    rule = prepare_rule(
        raw, "1", draft(operation="observer", output_port=None, widget_index=0)
    )
    assert rule["operation"] == "observer"
    descriptor = describe_nodes(raw["prompt"], raw["workflow"], {"1"})[0]
    assert descriptor["inputs"][0]["type"] == "STRING"
    assert descriptor["inputs"][0]["declared_type"] == "*"
    raw["workflow"]["nodes"][1]["outputs"][0]["type"] = "IMAGE"
    with pytest.raises(ValueError):
        prepare_rule(
            raw, "1", draft(operation="observer", output_port=None, widget_index=0)
        )


def test_detached_debug_node_cannot_be_bound():
    raw = fixture()
    raw["prompt"]["4"]["inputs"] = {}
    with pytest.raises(ValueError, match="主管线"):
        prepare_rule(raw, "1", draft())


def test_descriptors_include_unconnected_ports_but_not_builtin_nodes():
    raw = fixture()
    items = describe_nodes(raw["prompt"], raw["workflow"], {"1", "2", "3"})
    assert len(items) == 1
    assert items[0]["node_id"] == "1"
    assert items[0]["inputs"][0]["source"] == {"node_id": "2", "output_port": 0}
    assert "rule" not in items[0]
    rule = prepare_rule(raw, "1", draft())
    items = describe_nodes(raw["prompt"], raw["workflow"], {"1"}, make_rules([rule]))
    assert items[0]["rule"] == rule


def test_rules_are_immutable_and_context_is_isolated():
    original = get_rules()
    rule = prepare_rule(fixture(), "1", draft())
    rules = make_rules([rule])
    rule["inputs"].clear()
    copy = rules.user_rules
    copy.clear()
    assert rules.user_rules[0]["inputs"] == ["text"]
    assert original.fingerprint != rules.fingerprint

    async def run():
        async def worker(selected):
            with use_rules(selected):
                await asyncio.sleep(0)
                return get_rules().fingerprint

        return await asyncio.gather(worker(original), worker(rules))

    assert asyncio.run(run()) == [original.fingerprint, rules.fingerprint]
    assert get_rules() is original


def test_load_rules_reads_per_directory_and_reloads_changes(tmp_path):
    assert load_rules(tmp_path).fingerprint == make_rules([]).fingerprint
    rule = prepare_rule(fixture(), "1", draft())
    path = tmp_path / RULE_FILE
    path.write_text(json.dumps({"version": 1, "rules": [rule]}))
    first = load_rules(tmp_path)
    assert first is load_rules(tmp_path)
    assert first.user_rules == [rule]
    path.write_text(json.dumps({"version": 1, "rules": []}))
    assert load_rules(tmp_path).fingerprint != first.fingerprint
    other = tmp_path / "other"
    other.mkdir()
    assert load_rules(other).user_rules == []
    path.write_text("invalid")
    with pytest.raises(ValueError):
        load_rules(tmp_path)
    assert path.read_text() == "invalid"


def test_duplicate_rules_and_polarity_are_rejected():
    rule = prepare_rule(fixture(), "1", draft())
    with pytest.raises(ValueError, match="重复"):
        make_rules([rule, rule])
    with pytest.raises(ValueError, match="字段"):
        make_rules([{**rule, "role": "negative"}])


def test_conflicting_rules_do_not_choose_arbitrarily():
    raw = fixture()
    rule = prepare_rule(raw, "1", draft())
    other = {
        **rule,
        "id": "separate-conflicting-rule",
        "operation": "concat",
        "strip": True,
    }
    assert (
        matching_rule(raw["prompt"], raw["workflow"], "1", make_rules([rule, other]))
        is None
    )


def test_workflow_specific_rule_takes_precedence_over_type_declaration():
    raw = fixture()
    specific = prepare_rule(raw, "1", draft(operation="concat", strip=True))
    generic = prepare_rule(raw, "1", draft(scope="type"))
    rules = make_rules([generic, specific])
    assert matching_rule(raw["prompt"], raw["workflow"], "1", rules) == specific


@pytest.mark.parametrize("operation", ["literal", "passthrough", "observer"])
@pytest.mark.parametrize("hidden_transform", [{"strip": True}, {"delimiter": ", "}])
def test_non_concat_operations_reject_hidden_transform_in_draft_and_saved_rule(
    operation, hidden_transform
):
    raw = fixture()
    selected = draft(operation=operation)
    if operation == "literal":
        raw["prompt"]["1"]["inputs"]["text"] = "  text  "
        raw["workflow"]["nodes"][0]["inputs"][0]["link"] = None
    elif operation == "observer":
        selected.update(output_port=None, widget_index=0)
    canonical = prepare_rule(raw, "1", selected)
    with pytest.raises(ValueError, match="仅文本拼接"):
        prepare_rule(raw, "1", {**selected, **hidden_transform})
    with pytest.raises(ValueError, match="仅文本拼接"):
        make_rules([{**canonical, **hidden_transform}])
