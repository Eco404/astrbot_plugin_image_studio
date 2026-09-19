"""Declarative text-node rules; declarations never execute workflow code.

The built-in catalog describes known behavior. User rules are explicit claims
bound to a port signature and configuration guards, not learned transformations.
Polarity remains a property of each workflow's conditioning path.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
from pathlib import Path
from typing import Any

from ..common import MAX_METADATA_BYTES, MAX_NODES, _is_api_graph, _json
from ...comfyui.catalog import analysis_catalog, analysis_fingerprint

RULE_FILE = "comfyui_parser_rules.json"
MAX_RULES = 256
OPERATIONS = {"literal", "passthrough", "concat", "observer"}
_DRAFT_KEYS = {
    "operation",
    "scope",
    "inputs",
    "output_port",
    "delimiter",
    "strip",
    "widget_index",
}
_RULE_KEYS = _DRAFT_KEYS | {
    "id",
    "node_type",
    "workflow_key",
    "node_id",
    "signature",
    "guards",
    "origin",
}


def _encode(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_encode(value).encode()).hexdigest()


@dataclass(frozen=True)
class RuleSet:
    """Immutable serialized data, with defensive copies at consumer boundaries."""

    _user_json: str
    fingerprint: str

    @property
    def catalog(self) -> dict:
        return analysis_catalog()

    @property
    def user_rules(self) -> list[dict]:
        return json.loads(self._user_json)


def _validate_rule(rule: Any) -> None:
    if not isinstance(rule, dict) or set(rule) - _RULE_KEYS:
        raise ValueError("文本节点规则包含不支持的字段")
    if rule.get("operation") not in OPERATIONS or rule.get("scope") not in {
        "workflow",
        "type",
    }:
        raise ValueError("文本节点规则的操作或作用范围无效")
    for key in ("id", "node_type", "signature"):
        if not isinstance(rule.get(key), str) or not 0 < len(rule[key]) <= 256:
            raise ValueError(f"文本节点规则 {key} 无效")
    if rule.get("origin") != "user":
        raise ValueError("自定义文本节点规则必须保留用户声明来源")
    if rule["scope"] == "workflow" and not all(
        isinstance(rule.get(key), str) and rule[key]
        for key in ("workflow_key", "node_id")
    ):
        raise ValueError("工作流规则缺少工作流或节点标识")
    names = rule.get("inputs")
    if (
        not isinstance(names, list)
        or not 1 <= len(names) <= 32
        or any(not isinstance(name, str) or not 0 < len(name) <= 256 for name in names)
        or len(set(names)) != len(names)
    ):
        raise ValueError("请选择明确且不重复的文本输入")
    if rule["operation"] != "concat" and len(names) != 1:
        raise ValueError("该关系只能绑定一个文本输入")
    port = rule.get("output_port")
    if rule["operation"] == "observer":
        if (
            port is not None
            or type(rule.get("widget_index")) is not int
            or not 0 <= rule["widget_index"] < 128
        ):
            raise ValueError("显示回写规则需要有效的控件位置，不绑定输出")
    elif (
        type(port) is not int
        or not 0 <= port < 128
        or rule.get("widget_index") is not None
    ):
        raise ValueError("文本输出端口无效")
    if (
        not isinstance(rule.get("delimiter"), str)
        or len(rule["delimiter"]) > 256
        or type(rule.get("strip")) is not bool
    ):
        raise ValueError("文本拼接或去空白设置无效")
    if rule["operation"] != "concat" and (rule["delimiter"] != "" or rule["strip"]):
        raise ValueError("仅文本拼接关系可以设置分隔符或去除首尾空白")
    if not isinstance(rule.get("guards"), dict) or len(rule["guards"]) > 256:
        raise ValueError("文本节点规则缺少有效的控制项约束")
    if len(_encode(rule).encode()) > 65536:
        raise ValueError("文本节点规则过大")


def make_rules(user_rules: list[dict]) -> RuleSet:
    if not isinstance(user_rules, list) or len(user_rules) > MAX_RULES:
        raise ValueError(f"文本节点规则必须为数组且不超过 {MAX_RULES} 条")
    ids = set()
    for rule in user_rules:
        _validate_rule(rule)
        if rule["id"] in ids:
            raise ValueError("文本节点规则标识重复")
        ids.add(rule["id"])
    encoded = _encode(user_rules)
    return RuleSet(
        encoded,
        _digest([analysis_fingerprint(), user_rules]),
    )


_DEFAULT_RULES = make_rules([])
_ACTIVE_RULES: ContextVar[RuleSet] = ContextVar(
    "comfy_text_rules", default=_DEFAULT_RULES
)


def get_rules() -> RuleSet:
    return _ACTIVE_RULES.get()


@contextmanager
def use_rules(rules: RuleSet) -> Iterator[RuleSet]:
    if not isinstance(rules, RuleSet):
        raise ValueError("文本节点规则集无效")
    token = _ACTIVE_RULES.set(rules)
    try:
        yield rules
    finally:
        _ACTIVE_RULES.reset(token)


def load_rules(data_dir: str | Path | None = None) -> RuleSet:
    if data_dir is None:
        return _DEFAULT_RULES
    path = Path(data_dir) / RULE_FILE
    if not path.exists():
        return _DEFAULT_RULES
    stat = path.stat()
    if stat.st_size > MAX_METADATA_BYTES:
        raise ValueError("自定义文本节点规则文件过大")
    return _load_rules(
        str(path.resolve()), stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size
    )


@lru_cache(maxsize=32)
def _load_rules(path: str, mtime_ns: int, ctime_ns: int, size: int) -> RuleSet:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("version") != 1
            or set(payload) != {"version", "rules"}
        ):
            raise ValueError("自定义文本节点规则文件版本或格式无效")
        return make_rules(payload["rules"])
    except (OSError, UnicodeError, RecursionError, TypeError) as exc:
        raise ValueError("无法读取自定义文本节点规则文件") from exc


def _ref(value: Any, graph: dict) -> tuple[str, int] | None:
    if (
        isinstance(value, list)
        and len(value) == 2
        and str(value[0]) in graph
        and type(value[1]) is int
        and value[1] >= 0
    ):
        return str(value[0]), value[1]
    return None


def _workflow_key(graph: dict, workflow: dict) -> str:
    # Saved content/seed changes do not create a new workflow. Wiring changes do.
    return _digest(
        [
            "text-binding-workflow-v1",
            str(workflow.get("id", "")),
            [
                [
                    key,
                    node.get("class_type"),
                    [
                        [name, _ref(value, graph)]
                        for name, value in sorted(node.get("inputs", {}).items())
                    ],
                ]
                for key, node in sorted(graph.items())
                if isinstance(node, dict) and isinstance(node.get("inputs", {}), dict)
            ],
        ]
    )


def _ui_nodes(workflow: dict) -> dict:
    entries = workflow.get("nodes", [])
    if not isinstance(entries, list) or len(entries) > MAX_NODES:
        return {}
    result, duplicates = {}, set()
    for item in entries:
        if not isinstance(item, dict) or "id" not in item:
            continue
        key = str(item["id"])
        if key in result:
            duplicates.add(key)
        result[key] = item
    return {key: value for key, value in result.items() if key not in duplicates}


def _descriptor_context(graph: dict, workflow: dict) -> dict:
    links, duplicates = {}, set()
    for edge in (
        workflow.get("links", []) if isinstance(workflow.get("links"), list) else []
    ):
        if isinstance(edge, list) and len(edge) >= 6:
            if str(edge[0]) in links:
                duplicates.add(str(edge[0]))
            links[str(edge[0])] = edge
    connected = {
        ref
        for node in graph.values()
        if isinstance(node, dict) and isinstance(node.get("inputs", {}), dict)
        for value in node.get("inputs", {}).values()
        if (ref := _ref(value, graph))
    }
    return {
        "ui_nodes": _ui_nodes(workflow),
        "links": links,
        "duplicate_links": duplicates,
        "connected": connected,
        "workflow_key": _workflow_key(graph, workflow),
    }


def _descriptor(
    graph: dict, workflow: dict, node_id: str, context: dict | None = None
) -> dict | None:
    context = context or _descriptor_context(graph, workflow)
    node = graph.get(node_id, {})
    ui_nodes = context["ui_nodes"]
    ui = ui_nodes.get(node_id, {})
    kind = node.get("class_type")
    data = node.get("inputs", {})
    if not isinstance(kind, str) or not isinstance(data, dict):
        return None
    ui_inputs, ui_outputs = ui.get("inputs"), ui.get("outputs")
    complete = (
        ui.get("type") == kind
        and isinstance(ui_inputs, list)
        and isinstance(ui_outputs, list)
        and ui.get("mode", 0) == 0
    )
    entries = ui_inputs if isinstance(ui_inputs, list) else []
    entries = [
        entry
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
    ]
    if isinstance(ui_inputs, list) and len(entries) != len(ui_inputs):
        complete = False
    if len({entry["name"] for entry in entries}) != len(entries):
        complete = False
    links, duplicated = context["links"], context["duplicate_links"]
    inputs = []
    by_name = {entry["name"]: entry for entry in entries}
    for name in dict.fromkeys([*by_name, *data]):
        entry = by_name.get(name, {})
        value = data.get(name)
        ref = _ref(value, graph)
        item = {
            "name": name,
            "type": entry.get("type", "unknown"),
            "connected": ref is not None,
            "has_value": name in data and ref is None,
        }
        if name in data and ref is None and not isinstance(value, (list, dict)):
            item["value"] = value
        if ref:
            item["source"] = {"node_id": ref[0], "output_port": ref[1]}
            edge = links.get(str(entry.get("link")))
            index = entries.index(entry) if entry in entries else -1
            if (
                not edge
                or str(entry.get("link")) in duplicated
                or (str(edge[1]), edge[2], str(edge[3]), edge[4])
                != (ref[0], ref[1], node_id, index)
            ):
                complete = False
            source_ui = ui_nodes.get(ref[0], {})
            source_outputs = source_ui.get("outputs", [])
            if (
                item["type"] == "*"
                and source_ui.get("type") == graph[ref[0]].get("class_type")
                and source_ui.get("mode", 0) == 0
                and isinstance(source_outputs, list)
                and ref[1] < len(source_outputs)
                and isinstance(source_outputs[ref[1]], dict)
                and source_outputs[ref[1]].get("type") == "STRING"
            ):
                item["declared_type"] = "*"
                item["type"] = "STRING"
        elif entry.get("link") is not None:
            complete = False
        inputs.append(item)
    outputs = []
    for index, entry in enumerate(ui_outputs if isinstance(ui_outputs, list) else []):
        if not isinstance(entry, dict) or not isinstance(entry.get("type"), str):
            complete = False
            continue
        outputs.append(
            {
                "index": index,
                "name": str(entry.get("name", index)),
                "type": entry["type"],
                "connected": (node_id, index) in context["connected"],
            }
        )
    text_inputs = [item for item in inputs if item["type"] == "STRING"]
    text_outputs = [item for item in outputs if item["type"] == "STRING"]
    if not text_inputs and not text_outputs:
        return None
    signature = _digest(
        [
            kind,
            [
                [
                    item["name"],
                    item["type"],
                    item.get("declared_type"),
                    item["connected"],
                    item["has_value"],
                ]
                for item in inputs
            ],
            [[item["index"], item["type"]] for item in outputs],
        ]
    )
    unconnected = any(not item["connected"] for item in [*text_inputs, *text_outputs])
    widget_values = ui.get("widgets_values", [])
    widgets = [
        {"index": index, "value": value}
        for index, value in enumerate(
            widget_values if isinstance(widget_values, list) else []
        )
        if isinstance(value, str) and index < 128
    ]
    return {
        "node_id": node_id,
        "node_type": kind,
        "inputs": inputs,
        "outputs": outputs,
        "widgets": widgets,
        "signature": signature,
        "workflow_key": context["workflow_key"],
        "complete": complete,
        "bindable": complete,
        "type_scope_allowed": complete and not unconnected,
        "reason": ""
        if complete
        else "缺少完整端口信息，或 API 图与界面工作流的连线不一致",
        "type_scope_reason": "存在未连接的文本端口，仅允许绑定当前工作流"
        if unconnected
        else "",
    }


def _builtin_types(rules: RuleSet) -> set[str]:
    catalog = rules.catalog
    return set().union(
        *(
            catalog.get(key, {})
            for key in (
                "text_sources",
                "concatenators",
                "observers",
                "text_encoders",
                "condition_nodes",
            )
        )
    )


def describe_nodes(
    graph: dict,
    workflow: dict,
    active_ids: set[str] | list[str],
    rules: RuleSet | None = None,
) -> list[dict]:
    rules = rules or get_rules()
    builtin_types = _builtin_types(rules)
    result = []
    context = _descriptor_context(graph, workflow)
    for node_id in sorted(set(map(str, active_ids))):
        if graph.get(node_id, {}).get("class_type") in builtin_types:
            continue
        descriptor = _descriptor(graph, workflow, node_id, context)
        if descriptor:
            rule = _match_descriptor(graph, node_id, descriptor, rules)
            if rule:
                descriptor["rule"] = rule
            result.append(descriptor)
    return result


def _guard_values(data: dict, selected: list[str]) -> dict:
    return {name: value for name, value in data.items() if name not in selected}


def matching_rule(
    graph: dict, workflow: dict, node_id: str, rules: RuleSet | None = None
) -> dict | None:
    node_id = str(node_id)
    rules = rules or get_rules()
    if not any(
        rule["node_type"] == graph.get(node_id, {}).get("class_type")
        for rule in rules.user_rules
    ):
        return None
    descriptor = _descriptor(graph, workflow, node_id)
    if not descriptor or not descriptor["complete"]:
        return None
    return _match_descriptor(graph, node_id, descriptor, rules)


def _match_descriptor(
    graph: dict, node_id: str, descriptor: dict, rules: RuleSet
) -> dict | None:
    if not descriptor["complete"]:
        return None
    matches = []
    for rule in rules.user_rules:
        if rule["node_type"] != descriptor["node_type"]:
            continue
        if rule["signature"] != descriptor["signature"]:
            continue
        if rule["scope"] == "workflow" and (
            rule["node_id"] != node_id
            or rule["workflow_key"] != descriptor["workflow_key"]
        ):
            continue
        if rule["scope"] == "type" and not descriptor["type_scope_allowed"]:
            continue
        if (
            _guard_values(graph[node_id].get("inputs", {}), rule["inputs"])
            != rule["guards"]
        ):
            continue
        matches.append(rule)
    local = [rule for rule in matches if rule["scope"] == "workflow"]
    matches = local or matches
    return matches[0] if len(matches) == 1 else None


def prepare_rule(raw_metadata: dict, node_id: str, draft: dict) -> dict:
    if not isinstance(draft, dict) or set(draft) - _DRAFT_KEYS:
        raise ValueError("文本绑定包含不支持的字段；正反向由工作流判断")
    if not isinstance(raw_metadata, dict):
        raise ValueError("工作流元数据无效")
    raw = raw_metadata.get("raw", raw_metadata)
    if not isinstance(raw, dict):
        raise ValueError("工作流元数据无效")
    graph, workflow = _json(raw.get("prompt")), _json(raw.get("workflow"))
    if (
        not _is_api_graph(graph)
        or not isinstance(workflow, dict)
        or len(graph) > MAX_NODES
    ):
        raise ValueError("手动绑定需要同时保留 API 图和完整界面工作流")
    graph = {str(key): value for key, value in graph.items()}
    node_id = str(node_id)
    descriptor = _descriptor(graph, workflow, node_id)
    if not descriptor or not descriptor["complete"]:
        raise ValueError("该节点缺少可核对的完整文本端口信息")
    if descriptor["node_type"] in _builtin_types(get_rules()):
        raise ValueError("该节点已有内置解析规则")
    catalog = get_rules().catalog
    active = set()
    pending = [
        key
        for key, node in graph.items()
        if node.get("class_type") in {*catalog["save_types"], "PreviewImage"}
    ]
    while pending:
        key = pending.pop()
        if key in active:
            continue
        active.add(key)
        data = graph[key].get("inputs", {})
        if isinstance(data, dict):
            pending.extend(
                ref[0] for value in data.values() if (ref := _ref(value, graph))
            )
    observed = draft.get("operation") == "observer" and any(
        (ref := _ref(value, graph)) and ref[0] in active
        for value in graph[node_id].get("inputs", {}).values()
    )
    if node_id not in active and not observed:
        raise ValueError("该节点不在成图主管线中，也未观察主管线的文本输出")
    scope = draft.get("scope", "workflow")
    if scope == "type" and not descriptor["type_scope_allowed"]:
        raise ValueError("存在未连接的文本端口，不能保存为同类节点通用规则")
    names = draft.get("inputs", [])
    rule = {
        "id": _digest(
            [
                scope,
                descriptor["workflow_key"] if scope == "workflow" else "",
                node_id if scope == "workflow" else descriptor["node_type"],
                descriptor["signature"],
            ]
        )[:32],
        "node_type": descriptor["node_type"],
        "scope": scope,
        "workflow_key": descriptor["workflow_key"] if scope == "workflow" else "",
        "node_id": node_id if scope == "workflow" else "",
        "signature": descriptor["signature"],
        "operation": draft.get("operation"),
        "inputs": names,
        "output_port": draft.get("output_port"),
        "delimiter": draft.get("delimiter", ""),
        "strip": draft.get("strip", False),
        "guards": {},
        "origin": "user",
    }
    if draft.get("widget_index") is not None:
        rule["widget_index"] = draft["widget_index"]
    _validate_rule(rule)
    available = {item["name"]: item for item in descriptor["inputs"]}
    for name in names:
        item = available.get(name)
        if not item or (
            item["type"] != "STRING"
            and not (
                rule["operation"] == "literal" and isinstance(item.get("value"), str)
            )
        ):
            raise ValueError("只能绑定已声明的文本输入或直接文本字段")
        if rule["operation"] == "literal" and (
            item["connected"] or not isinstance(item.get("value"), str)
        ):
            raise ValueError("字段直接输出需要未连接且保存了文字的输入")
        if (
            rule["operation"] != "literal"
            and not item["connected"]
            and not isinstance(item.get("value"), str)
        ):
            raise ValueError("所选文本输入未连接且没有可读取的值")
    if rule["operation"] == "observer":
        observed_ref = _ref(graph[node_id]["inputs"].get(names[0]), graph)
        if not observed_ref or observed_ref[0] not in active:
            raise ValueError("显示节点所选输入必须观察成图主管线的文本输出")
        if not any(
            item["index"] == rule["widget_index"] for item in descriptor["widgets"]
        ):
            raise ValueError("所选控件没有保存文本回写")
    else:
        outputs = {item["index"]: item for item in descriptor["outputs"]}
        if outputs.get(rule["output_port"], {}).get("type") != "STRING":
            raise ValueError("请选择已声明的 STRING 输出端口")
        if not any(
            _ref(value, graph) == (node_id, rule["output_port"])
            for key in active
            if isinstance(graph[key].get("inputs", {}), dict)
            for value in graph[key].get("inputs", {}).values()
        ):
            raise ValueError("所选文本输出没有连接到成图主管线")
    rule["guards"] = _guard_values(graph[node_id]["inputs"], names)
    _validate_rule(rule)
    return json.loads(_encode(rule))


def evaluate_text_rule(
    rule: dict, inputs: dict, port: int, resolve: Callable[[Any], Any]
) -> str | None:
    """Evaluate only finite, explicitly declared text operations; never infer."""
    if rule.get("operation") not in {
        "literal",
        "passthrough",
        "concat",
    } or port != rule.get("output_port"):
        return None
    fragments = []
    for name in rule.get("inputs", []):
        if name not in inputs:
            return None
        value = (
            inputs[name] if rule["operation"] == "literal" else resolve(inputs[name])
        )
        if not isinstance(value, str):
            return None
        fragments.append(value)
    if not fragments:
        return None
    result = (
        rule["delimiter"].join(fragments)
        if rule["operation"] == "concat"
        else fragments[0]
    )
    return (
        result.strip()
        if rule["operation"] == "concat" and rule.get("strip")
        else result
    )
