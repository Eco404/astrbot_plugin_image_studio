"""Explicit ComfyUI API graph imports and parameter bindings.

This module intentionally does not convert UI graphs or infer bindings while
executing. Unknown nodes and unbound inputs remain intact for ComfyUI to run.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import secrets
from typing import Any

MAX_GRAPH_BYTES = 16 * 1024 * 1024
MAX_NODES = 10000
FIXED_OUTPUT_POLICY = "fixed_outputs_v1"
FIXED_OUTPUT_COUNT_DESCRIPTION = (
    "本次期望获取的总图片数；按每轮出图张数安排固定轮次，超量截断，"
    "不足时不自动补齐，不修改工作流节点参数。"
)
FIXED_OUTPUT_COUNT_SCHEMA = {
    "type": "integer",
    "label": "本次总张数",
    "description": FIXED_OUTPUT_COUNT_DESCRIPTION,
    "default": 1,
    "min": 1,
    "max": 16,
    "step": 1,
    "request_key": "count",
    "refill_from_history": False,
}
BINDING_TYPES = {"text", "number", "boolean", "select", "image", "mask"}
BINDING_SOURCES = {
    "prompt",
    "negative_prompt",
    "width",
    "height",
    "seed",
    "count",
    "reference",
    "parameter",
}


def _object(value: Any) -> Any:
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_GRAPH_BYTES:
            raise ValueError("ComfyUI 工作流超过 16 MiB")
        try:
            return json.loads(value)
        except (ValueError, RecursionError) as exc:
            raise ValueError("ComfyUI 工作流不是有效 JSON") from exc
    return value


def is_api_graph(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and bool(value)
        and all(
            isinstance(node, dict)
            and isinstance(node.get("class_type"), str)
            and bool(node["class_type"].strip())
            and isinstance(node.get("inputs"), dict)
            for node in value.values()
        )
    )


def graph_fingerprint(graph: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            graph,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def is_link(value: Any, graph: dict[str, Any] | None = None) -> bool:
    # API connections are [node_id, output_slot], not arbitrary array inputs.
    return (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], (str, int))
        and not isinstance(value[0], bool)
        and isinstance(value[1], int)
        and not isinstance(value[1], bool)
        and value[1] >= 0
        and (graph is None or str(value[0]) in graph)
    )


def normalize_workflow(value: Any) -> dict[str, Any]:
    """Accept API JSON, PNG metadata, or our saved workflow configuration."""
    value = _object(value)
    if not isinstance(value, dict):
        raise ValueError("请提供 ComfyUI API 工作流 JSON 或包含 prompt 的图片元数据")
    # Gallery records store metadata under image_metadata/raw; tolerate the
    # metadata envelope without depending on the gallery parser's internals.
    envelope = value
    for _ in range(4):
        if (
            is_api_graph(value)
            or "api_graph_json" in value
            or "api_graph" in value
            or "prompt" in value
        ):
            break
        child = next(
            (
                value[key]
                for key in ("comfyui", "image_metadata", "metadata", "raw")
                if isinstance(value.get(key), (dict, str))
            ),
            None,
        )
        if child is None:
            break
        value = _object(child)
        if not isinstance(value, dict):
            break
    if not isinstance(value, dict):
        raise ValueError("图片未包含可执行的 ComfyUI API 工作流")
    graph = (
        value
        if is_api_graph(value)
        else _object(
            value.get("api_graph_json", value.get("api_graph", value.get("prompt")))
        )
    )
    if not is_api_graph(graph):
        if "workflow" in value or isinstance(value.get("nodes"), list):
            raise ValueError(
                "仅包含 ComfyUI 界面工作流，请在 ComfyUI 导出 API 格式；不能直接执行界面图"
            )
        raise ValueError("图片或 JSON 中没有有效的 ComfyUI API 图（prompt）")
    if len(graph) > MAX_NODES:
        raise ValueError("ComfyUI 工作流节点数量超过上限")
    try:
        raw = json.dumps(graph, ensure_ascii=False, allow_nan=False)
        if len(raw.encode("utf-8")) > MAX_GRAPH_BYTES:
            raise ValueError("ComfyUI 工作流超过 16 MiB")
        graph = json.loads(raw)
    except (TypeError, OverflowError, RecursionError) as exc:
        raise ValueError("ComfyUI API 图必须是有效 JSON 数据") from exc
    graph = {str(key): node for key, node in graph.items()}
    owner = value if not is_api_graph(value) else {}
    overrides = owner.get("input_overrides", [])
    if not isinstance(overrides, list):
        raise ValueError("工作流输入修改必须是数组")
    for override in overrides:
        if not isinstance(override, dict) or "value" not in override:
            raise ValueError("工作流输入修改需要节点、字段和具体值")
        node_id, name = (
            str(override.get("node_id", "")),
            str(override.get("input_name", "")),
        )
        if node_id not in graph or name not in graph[node_id]["inputs"]:
            raise ValueError(f"输入修改目标不存在：节点 #{node_id} 的 {name}")
        original, replacement = graph[node_id]["inputs"][name], override["value"]
        if is_link(original, graph) or is_link(replacement, graph):
            raise ValueError(f"不能通过输入修改替换节点 #{node_id} 的连线 {name}")
        if (
            isinstance(original, int)
            and not isinstance(original, bool)
            and isinstance(replacement, str)
            and replacement.lstrip("+-").isdigit()
        ):
            replacement = int(replacement)
        graph[node_id]["inputs"][name] = replacement
    # Keep the exact JSON text alongside the browser preview. JavaScript's
    # Number cannot round-trip uint64 sampler seeds and node integer inputs.
    raw = json.dumps(graph, ensure_ascii=False, allow_nan=False)
    if len(raw.encode("utf-8")) > MAX_GRAPH_BYTES:
        raise ValueError("ComfyUI 工作流超过 16 MiB")
    bindings_raw = owner.get("bindings", envelope.get("bindings", {}))
    if not isinstance(bindings_raw, dict):
        raise ValueError("工作流输入绑定必须是对象")
    bindings: dict[str, Any] = {}
    occupied: set[tuple[str, str]] = set()
    for key, item in bindings_raw.items():
        if (
            not isinstance(key, str)
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", key)
            or not isinstance(item, dict)
        ):
            raise ValueError(
                "工作流参数键必须以英文字母开头，仅含字母、数字、下划线或连字符，长度不超过 64"
            )
        targets = item.get("targets")
        if targets is None and "node_id" in item and "input_name" in item:
            targets = [{"node_id": item["node_id"], "input_name": item["input_name"]}]
        if not isinstance(targets, list) or not targets:
            raise ValueError(f"参数 {key} 没有绑定输入节点")
        normalized_targets = []
        for target in targets:
            if not isinstance(target, dict):
                raise ValueError(f"参数 {key} 的绑定目标无效")
            node_id, name = (
                str(target.get("node_id", "")),
                str(target.get("input_name", "")),
            )
            if node_id not in graph or name not in graph[node_id]["inputs"]:
                raise ValueError(
                    f"参数 {key} 绑定失效：节点 #{node_id} 的输入 {name} 不存在"
                )
            if is_link(graph[node_id]["inputs"][name], graph):
                raise ValueError(
                    f"参数 {key} 绑定的是节点 #{node_id} 的连线 {name}，请选择实际值输入节点"
                )
            expected = str(target.get("class_type", ""))
            actual = graph[node_id]["class_type"]
            if expected and expected != actual:
                raise ValueError(
                    f"参数 {key} 绑定失效：节点 #{node_id} 类型已从 {expected} 变为 {actual}"
                )
            if (node_id, name) in occupied:
                raise ValueError(f"节点 #{node_id} 的输入 {name} 被重复绑定")
            occupied.add((node_id, name))
            normalized_targets.append(
                {"node_id": node_id, "input_name": name, "class_type": actual}
            )
        kind, source = (
            str(item.get("type", "text")),
            str(item.get("source", "parameter")),
        )
        if kind not in BINDING_TYPES or source not in BINDING_SOURCES:
            raise ValueError(f"参数 {key} 的绑定类型或来源不受支持")
        mode = str(item.get("mode", "replace"))
        if mode not in {"replace", "append"} or mode == "append" and kind != "text":
            raise ValueError(f"参数 {key} 仅文本绑定支持追加")
        binding = {
            **copy.deepcopy(item),
            "targets": normalized_targets,
            "type": kind,
            "source": source,
            "mode": mode,
        }
        binding.pop("node_id", None)
        binding.pop("input_name", None)
        if source == "reference" or kind in {"image", "mask"}:
            index = item.get("reference_index", 0)
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or not 0 <= index < 8
            ):
                raise ValueError(f"参数 {key} 的参考图序号无效")
            binding["reference_index"] = index
            binding["source"] = "reference"
            if kind == "mask" and any(
                target["class_type"] != "LoadImage" or target["input_name"] != "image"
                for target in normalized_targets
            ):
                raise ValueError(
                    f"参数 {key} 的蒙版绑定目前仅支持标准 LoadImage 节点的 image 输入（白色重绘，黑色保留）"
                )
        bindings[key] = binding
    outputs = owner.get("outputs", envelope.get("outputs", []))
    if not isinstance(outputs, list):
        raise ValueError("工作流输出节点必须是数组")
    outputs = list(dict.fromkeys(str(item) for item in outputs))
    if any(item not in graph for item in outputs):
        raise ValueError("选定的输出节点已不存在，请重新选择")
    ui = _object(
        owner.get(
            "workflow_json",
            owner.get(
                "workflow", envelope.get("workflow_json", envelope.get("workflow"))
            ),
        )
    )
    if ui is not None and not isinstance(ui, dict):
        raise ValueError("ComfyUI 界面工作流必须是 JSON 对象")
    policy = owner.get("execution_policy", envelope.get("execution_policy"))
    if policy not in (None, "", FIXED_OUTPUT_POLICY):
        raise ValueError(f"不支持的 ComfyUI 执行策略：{policy}")
    return {
        "api_graph": graph,
        "api_graph_json": raw,
        "workflow": copy.deepcopy(ui),
        "workflow_json": json.dumps(ui, ensure_ascii=False, allow_nan=False)
        if ui is not None
        else None,
        "bindings": bindings,
        "outputs": outputs,
        "fingerprint": graph_fingerprint(graph),
        **({"execution_policy": policy} if policy else {}),
        **(
            {"parameters_schema": copy.deepcopy(owner["parameters_schema"])}
            if isinstance(owner.get("parameters_schema"), dict)
            else {}
        ),
    }


def migrate_fixed_outputs(
    config: Any,
    parameters: Any = None,
    tool: Any = None,
    *,
    prefer_graph_values: bool = False,
) -> dict[str, Any]:
    """Migrate editable model configuration, never immutable execution revisions.

    Plugin count is an independent output target. Former count-to-node bindings
    become ordinary parameters, keeping their configured defaults and policy.
    """
    config = normalize_workflow(config)
    if parameters is None:
        parameters = config.get("parameters_schema", {})
    if isinstance(parameters, str):
        parameters = _object(parameters) if parameters.strip() else {}
    raw_schema = copy.deepcopy(parameters) if isinstance(parameters, dict) else {}
    raw_schema = {
        key: value for key, value in raw_schema.items() if isinstance(value, dict)
    }
    raw_tool = copy.deepcopy(tool) if isinstance(tool, dict) else {}
    original_tool_parameters = raw_tool.get("parameters")
    original_tool_parameters = (
        copy.deepcopy(original_tool_parameters)
        if isinstance(original_tool_parameters, dict)
        else {}
    )
    tool_parameters = copy.deepcopy(original_tool_parameters)
    aliases = {"count", "n"}
    bindings = config["bindings"]
    occupied = set(bindings) | set(raw_schema) | aliases
    migrated_bindings, migrated_schema = {}, copy.deepcopy(raw_schema)
    consumed = set()

    def unused_key(base: str) -> str:
        if len(base) > 64:
            base = base[:51] + "_" + hashlib.sha256(base.encode()).hexdigest()[:12]
        key, index = base, 2
        while key in occupied:
            suffix = f"_{index}"
            key = base[: 64 - len(suffix)] + suffix
            index += 1
        occupied.add(key)
        return key

    plans = []
    for old_key, original_binding in bindings.items():
        targets = original_binding["targets"]
        values = [
            config["api_graph"][target["node_id"]]["inputs"][target["input_name"]]
            for target in targets
        ]
        if (
            prefer_graph_values
            and original_binding["source"] == "count"
            and any(value != values[0] for value in values[1:])
        ):
            # Old execution snapshots occasionally contain manually edited,
            # differing targets. Preserve each one rather than choosing a value.
            for index, target in enumerate(targets):
                base = "workflow_count" if old_key in aliases else old_key[:48]
                new_key = unused_key(f"{base}_node_{index + 1}")
                plans.append(
                    (old_key, {**original_binding, "targets": [target]}, new_key)
                )
        else:
            plans.append((old_key, original_binding, None))

    for old_key, original_binding, forced_key in plans:
        binding = copy.deepcopy(original_binding)
        source = binding["source"]
        parameter_input = source not in {"prompt", "negative_prompt", "reference"}
        exact_matches = [old_key] if parameter_input and old_key in raw_schema else []
        wire_matches = []
        if parameter_input:
            wire_matches = [
                name
                for name, descriptor in raw_schema.items()
                if str(descriptor.get("request_key") or name) == old_key
            ]
        matches = (
            (exact_matches or wire_matches)
            if source == "count" or old_key in aliases
            else (wire_matches or exact_matches)
        )
        if not matches and source == "count":
            # Old source=count consumed the shared count control even when the
            # binding itself used an unrelated node-based name.
            matches = [
                name
                for name, descriptor in raw_schema.items()
                if name in aliases or descriptor.get("request_key") in aliases
            ]
        schema_key = matches[0] if matches else None
        descriptor = copy.deepcopy(raw_schema.get(schema_key, {}))
        new_key = forced_key or (
            unused_key("workflow_count") if old_key in aliases else old_key
        )
        if source == "count":
            binding["source"] = "parameter"
        if binding.get("request_key") in aliases or new_key != old_key:
            binding["request_key"] = new_key
        migrated_bindings[new_key] = binding
        if not parameter_input:
            continue
        public_key = forced_key or (
            schema_key if schema_key and schema_key not in aliases else new_key
        )
        reused_schema = schema_key in consumed
        if reused_schema:
            public_key = new_key
        # A renamed public field must not overwrite an unrelated schema entry.
        if (
            public_key != schema_key or reused_schema
        ) and public_key in migrated_schema:
            public_key = unused_key(new_key + "_input")
        target = binding["targets"][0]
        graph_default = config["api_graph"][target["node_id"]]["inputs"][
            target["input_name"]
        ]
        descriptor.setdefault("type", binding["type"])
        descriptor.setdefault("label", binding.get("label", public_key))
        descriptor.setdefault("default", binding.get("default", graph_default))
        if source == "count" and prefer_graph_values:
            descriptor["default"] = copy.deepcopy(graph_default)
        descriptor["request_key"] = new_key
        for name in ("min", "max", "description", "step"):
            if name in binding:
                descriptor.setdefault(name, binding[name])
        if "options" in binding:
            descriptor.setdefault("choices", copy.deepcopy(binding["options"]))
        if schema_key:
            consumed.add(schema_key)
            if not reused_schema:
                migrated_schema.pop(schema_key, None)
            if schema_key in original_tool_parameters:
                if not reused_schema:
                    tool_parameters.pop(schema_key, None)
                tool_parameters[public_key] = copy.deepcopy(
                    original_tool_parameters[schema_key]
                )
        migrated_schema[public_key] = descriptor
        # Source=count previously ignored binding.default. Pin the ordinary
        # fallback to the actual configured control (or the graph literal).
        if source == "count":
            binding["default"] = copy.deepcopy(descriptor["default"])

    independent = [
        name
        for name, descriptor in raw_schema.items()
        if name not in consumed
        and (name in aliases or descriptor.get("request_key") in aliases)
    ]
    total_key = (
        "count" if "count" in independent else independent[0] if independent else None
    )
    total = copy.deepcopy(raw_schema[total_key]) if total_key else {}
    old_generic = (
        "本次希望生成的总图片数；按模型原生批次上限自动拆分请求，实际返回取决于上游。"
    )
    description = str(total.get("description") or "")
    if not description or description.startswith(old_generic):
        total["description"] = FIXED_OUTPUT_COUNT_DESCRIPTION
    for name in independent:
        migrated_schema.pop(name, None)
        if name != "count":
            tool_parameters.pop(name, None)
    if total_key and total_key in original_tool_parameters:
        tool_parameters["count"] = copy.deepcopy(original_tool_parameters[total_key])
    total = {
        **FIXED_OUTPUT_COUNT_SCHEMA,
        **total,
        "request_key": "count",
        "refill_from_history": False,
    }
    migrated_schema["count"] = total
    if any(name != "negative_prompt" for name in original_tool_parameters):
        # Existing per-field policies restrict tool exposure. The newly added
        # independent output count is available unless it already had a policy.
        tool_parameters.setdefault("count", {"exposed": True})
    if "parameters" in raw_tool or tool_parameters:
        raw_tool["parameters"] = tool_parameters
    config["bindings"] = migrated_bindings
    config["execution_policy"] = FIXED_OUTPUT_POLICY
    if "parameters_schema" in config:
        config["parameters_schema"] = copy.deepcopy(migrated_schema)
    return {"comfyui": config, "parameters": migrated_schema, "tool": raw_tool}


def inspect_workflow(config: Any) -> dict[str, Any]:
    """Suggest editable literal inputs, never apply these suggestions implicitly."""
    config = normalize_workflow(config)
    graph = config["api_graph"]
    outputs = [
        node_id
        for node_id, node in graph.items()
        if node["class_type"] in {"SaveImage", "PreviewImage", "SaveAnimatedWEBP"}
    ]
    # Suggestions cover chosen outputs' ancestors; structural validation later
    # still covers the full submitted graph, including disconnected nodes.
    active: set[str] = set()
    pending = list(config["outputs"] or outputs or graph)
    while pending:
        node_id = pending.pop()
        if node_id in active or node_id not in graph:
            continue
        active.add(node_id)
        pending.extend(
            str(value[0])
            for value in graph[node_id]["inputs"].values()
            if is_link(value, graph)
        )
    suggestions = {}
    nodes = []
    for node_id, node in graph.items():
        meta = node.get("_meta") if isinstance(node.get("_meta"), dict) else {}
        title = str(meta.get("title") or node["class_type"])
        inputs = []
        for name, value in node["inputs"].items():
            linked = is_link(value, graph)
            inputs.append(
                {
                    "name": name,
                    "value": copy.deepcopy(value),
                    "linked": linked,
                    "value_text": json.dumps(value, ensure_ascii=False),
                }
            )
            if (
                linked
                or node_id not in active
                or not isinstance(value, (str, int, float, bool))
            ):
                continue
            kind = (
                "boolean"
                if isinstance(value, bool)
                else "number"
                if isinstance(value, (int, float))
                else "text"
            )
            source = name if name in {"width", "height", "seed"} else "parameter"
            if node["class_type"] == "LoadImage" and name == "image":
                kind, source = "image", "reference"
            # Even familiar CLIP text nodes may serve several conditioning
            # branches. Let the user choose positive/negative semantics.
            raw_key = f"node_{node_id}_{name}"
            key = re.sub(r"[^A-Za-z0-9_-]", "_", raw_key)
            if key != raw_key or len(key) > 64:
                key = key[:51] + "_" + hashlib.sha256(raw_key.encode()).hexdigest()[:12]
            suggestions[key] = {
                "label": f"{title} · {name}",
                "type": kind,
                "source": source,
                "default": value,
                "targets": [
                    {
                        "node_id": node_id,
                        "input_name": name,
                        "class_type": node["class_type"],
                    }
                ],
            }
            if isinstance(value, int) and not isinstance(value, bool):
                suggestions[key]["integer"] = True
                if abs(value) > 9007199254740991:
                    suggestions[key]["default"] = str(value)
                    suggestions[key]["integer_format"] = "decimal_string"
        nodes.append(
            {
                "id": node_id,
                "class_type": node["class_type"],
                "title": title,
                "inputs": inputs,
                "in_selected_pipeline": node_id in active,
            }
        )
    return {
        "fingerprint": config["fingerprint"],
        "nodes": nodes,
        "suggested_bindings": suggestions,
        "outputs": outputs,
        "selected_outputs": config["outputs"],
        "node_count": len(graph),
    }


def _parameter_value(
    key: str, binding: dict, request: Any, references: list[str]
) -> tuple[bool, Any]:
    source = binding["source"]
    parameters = request.parameters
    if source == "reference":
        index = binding["reference_index"]
        return (True, references[index]) if index < len(references) else (False, None)
    if source == "parameter":
        return (True, parameters[key]) if key in parameters else (False, None)
    if source in {"prompt", "negative_prompt"}:
        return True, getattr(request, source, "")
    if source == "count":
        return True, request.count
    if source in {"width", "height"}:
        if key in parameters:
            return True, parameters[key]
        if source in parameters:
            return True, parameters[source]
        size = str(request.size or "").lower().split("x")
        if len(size) == 2 and all(part.strip().isdigit() for part in size):
            return True, int(size[0 if source == "width" else 1])
        return False, None
    if source == "seed":
        if "seed" not in parameters and key not in parameters:
            return False, None
        value = parameters.get(key, parameters.get("seed"))
        if value in (-1, "-1"):
            value = secrets.randbits(63)
        return True, value
    return False, None


def _typed_value(key: str, binding: dict, value: Any) -> Any:
    kind = binding["type"]
    if kind == "number":
        if isinstance(value, bool):
            raise ValueError(f"参数 {key} 应为数字")
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"参数 {key} 应为数字") from exc
        if not math.isfinite(number):
            raise ValueError(f"参数 {key} 必须是有限数字")
        if binding.get("integer") and not number.is_integer():
            raise ValueError(f"参数 {key} 应为整数")
        for bound, comparison in (("min", number.__lt__), ("max", number.__gt__)):
            if binding.get(bound) is not None and comparison(float(binding[bound])):
                raise ValueError(f"参数 {key} 超出范围：{bound}={binding[bound]}")
        # Preserve large integer seeds, which cannot round-trip through float.
        value = (
            int(value)
            if isinstance(value, int)
            or isinstance(value, str)
            and value.lstrip("+-").isdigit()
            else int(number)
            if number.is_integer()
            else number
        )
    elif kind == "boolean":
        if isinstance(value, str) and value.lower() in {"true", "false"}:
            value = value.lower() == "true"
        if not isinstance(value, bool):
            raise ValueError(f"参数 {key} 应为布尔值")
    elif kind in {"text", "image", "mask"}:
        if not isinstance(value, str):
            raise ValueError(f"参数 {key} 应为文本")
    elif kind == "select":
        options = binding.get("options", [])
        options = [
            item.get("value") if isinstance(item, dict) else item for item in options
        ]
        if options and value not in options:
            raise ValueError(f"参数 {key} 不在可选值中")
    if binding.get("required") and (
        value is None or isinstance(value, str) and not value.strip()
    ):
        raise ValueError(f"请填写参数 {key}")
    return value


def clear_execution_cache_markers(graph: dict) -> None:
    """Clear prior-run fingerprints on an execution copy, not archived metadata."""
    for node in graph.values():
        # ComfyUI trusts this field instead of calling the node's IS_CHANGED.
        # Replaying it can freeze random nodes even when their seed stays -1.
        node.pop("is_changed", None)


def prepare_graph(
    config: Any, request: Any, uploadedrefs: list[str] | tuple[str, ...] = ()
) -> dict:
    config = normalize_workflow(config)
    if config.get("execution_policy") == FIXED_OUTPUT_POLICY and any(
        binding["source"] == "count"
        or key in {"count", "n"}
        or binding.get("request_key") in {"count", "n"}
        for key, binding in config["bindings"].items()
    ):
        raise ValueError(
            "固定出图策略不允许把插件总量 count/n 绑定到工作流节点，请迁移为普通参数"
        )
    graph = config["api_graph"]
    clear_execution_cache_markers(graph)
    references = list(uploadedrefs)
    used_reference_indices = set()
    for key, binding in config["bindings"].items():
        present, value = _parameter_value(key, binding, request, references)
        if not present:
            if binding.get("required"):
                raise ValueError(
                    f"缺少工作流输入 {key}"
                    + (
                        f"（第 {binding['reference_index'] + 1} 张参考图）"
                        if binding["source"] == "reference"
                        else ""
                    )
                )
            if "default" not in binding:
                continue
            value = binding["default"]
        if binding["source"] == "reference" and present:
            used_reference_indices.add(binding["reference_index"])
        value = _typed_value(key, binding, value)
        for target in binding["targets"]:
            inputs = graph[target["node_id"]]["inputs"]
            name = target["input_name"]
            if binding["mode"] == "append":
                if value:
                    inputs[name] = (
                        str(inputs[name]) + str(binding.get("separator", "\n")) + value
                    )
            else:
                inputs[name] = value
    unused = set(range(len(references))) - used_reference_indices
    if unused:
        raise ValueError(
            "工作流没有绑定这些参考图：" + "、".join(str(i + 1) for i in sorted(unused))
        )
    return graph
