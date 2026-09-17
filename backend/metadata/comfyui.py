"""Read-only ComfyUI metadata traversal; not workflow execution normalization."""

from __future__ import annotations
import hashlib
import json
import re
from typing import Any
from .common import MAX_DEPTH, MAX_NODES, _json, _is_api_graph, _warning
from .comfyui_graph import _workflow_graph
from .comfyui_candidates import _comfy_display_snapshots, _deduplicate_comfy_candidates


def _comfyui(fields: dict, result: dict, *, output_node_id: str = "") -> None:
    graph = _json(fields.get("prompt"))
    if not _is_api_graph(graph):
        workflow = _json(fields.get("workflow"))
        if workflow is None:
            _warning(result, "ComfyUI 工作流不是可解析的 JSON")
            return
        graph = _workflow_graph(workflow, result)
    if len(graph) > MAX_NODES:
        raise ValueError("ComfyUI 工作流超过 2048 个节点")
    graph = {str(key): node for key, node in graph.items()}

    def reference(value: Any) -> tuple[str, int] | None:
        if (
            isinstance(value, list)
            and len(value) == 2
            and isinstance(value[1], int)
            and not isinstance(value[1], bool)
            and value[1] >= 0
            and str(value[0]) in graph
        ):
            return str(value[0]), value[1]
        return None

    def link(value: Any) -> str | None:
        ref = reference(value)
        return ref[0] if ref else None

    def ref_key(value: Any) -> str | None:
        ref = reference(value)
        return f"{ref[0]}:{ref[1]}" if ref else None

    def inputs(identifier: str) -> dict:
        value = graph[identifier].get("inputs", {})
        return value if isinstance(value, dict) else {}

    save_types = {
        "SaveImage",
        "SaveAnimatedWEBP",
        "SaveAnimatedPNG",
        "SaveImageWebsocket",
    }
    roots = [
        key
        for key, node in graph.items()
        if node.get("class_type") in save_types | {"PreviewImage"}
    ]
    if not roots:
        if output_node_id:
            raise ValueError("所选保存输出节点不存在")
        result["normalized"].update(
            prompt_candidates=[], requires_output_selection=False
        )
        _warning(
            result,
            "未找到已知成图输出节点，完整工作流已保留，无法可靠确定参与生成的参数",
        )
        return

    def visit(
        identifier: str, active: set[str], visited: set[str], order: list[str]
    ) -> None:
        if identifier in active or len(active) > MAX_DEPTH:
            raise ValueError("ComfyUI 工作流存在循环或依赖层数过多")
        if identifier in visited:
            return
        for value in inputs(identifier).values():
            dependency = link(value)
            if dependency is not None:
                visit(dependency, active | {identifier}, visited, order)
        visited.add(identifier)
        order.append(identifier)

    orders: dict[str, list[str]] = {}
    for root in roots:
        orders[root] = []
        visit(root, set(), set(), orders[root])

    def match_digest(value: Any) -> str:
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    # Match the selected branch's wiring, not cached values or node numbers alone.
    # Literal prompts, seeds, models and UI layout may vary between batch images.
    node_shapes = {
        identifier: match_digest(
            [
                node.get("class_type"),
                [
                    [name, ref_key(value)]
                    for name, value in sorted(inputs(identifier).items())
                ],
            ]
        )
        for identifier, node in graph.items()
    }
    branch_keys = {
        root: match_digest(
            [
                "comfy-branch-v1",
                root,
                [[identifier, node_shapes[identifier]] for identifier in sorted(order)],
            ]
        )
        for root, order in orders.items()
    }
    saves = [root for root in roots if graph[root].get("class_type") in save_types]
    if output_node_id and output_node_id not in saves:
        raise ValueError("所选节点不存在或不是保存图片输出")
    selected_root = (
        output_node_id
        if output_node_id
        else saves[0]
        if len(saves) == 1
        else roots[0]
        if not saves and len(roots) == 1
        else None
    )
    selected_roots = [selected_root] if selected_root else [] if saves else roots
    order = list(
        dict.fromkeys(
            identifier for root in selected_roots for identifier in orders[root]
        )
    )
    if len(saves) > 1 and not selected_root:
        _warning(
            result,
            "工作流包含多个保存输出，请先选择当前图片对应的保存分支，再查看参数和提示词候选",
        )
    elif not saves and len(roots) > 1:
        _warning(result, "工作流仅有多个预览输出，无法确定本图对应哪一个预览分支")

    def resolve(value: Any, active: frozenset[str] = frozenset()) -> Any:
        ref = reference(value)
        if ref is None:
            return value if not isinstance(value, (dict, list)) else None
        identifier, port = ref
        if identifier in active or len(active) > MAX_DEPTH:
            return None
        node = graph[identifier]
        kind = node.get("class_type", "")
        data = inputs(identifier)
        active = active | {identifier}
        if port != 0:
            _warning(
                result,
                f"节点 {identifier}（{kind}）输出端口 {port} 的值无法静态解析，已保留引用",
            )
            return None
        if kind in {
            "TextInput_",
            "TextInput",
            "String",
            "PrimitiveString",
        }:
            return resolve(data.get("text", data.get("value")), active)
        if kind in {"Seed (rgthree)", "PrimitiveInt", "INT", "Float", "PrimitiveNode"}:
            return resolve(data.get("seed", data.get("value")), active)
        if kind in {"Text Concatenate", "TextConcatenate"}:
            fragments = [
                resolve(v, active) for k, v in data.items() if k.startswith("text_")
            ]
            if fragments and all(isinstance(v, str) for v in fragments):
                return str(data.get("delimiter", "")).join(fragments)
        _warning(
            result, f"节点 {identifier}（{kind}）的动态值无法静态解析，已保留完整工作流"
        )
        return None

    conditions: dict[str, dict] = {}
    condition_spec = {
        "ConditioningCombine": ("combine", ("conditioning_1", "conditioning_2")),
        "ConditioningConcat": ("concat", ("conditioning_to", "conditioning_from")),
        "ConditioningAverage": ("average", ("conditioning_to", "conditioning_from")),
        "ConditioningSetArea": ("set_area", ("conditioning",)),
        "ConditioningSetAreaPercentage": ("set_area_percentage", ("conditioning",)),
        "ConditioningSetMask": ("set_mask", ("conditioning",)),
        "ConditioningSetTimestepRange": ("set_timestep_range", ("conditioning",)),
        "ConditioningSetAreaStrength": ("set_area_strength", ("conditioning",)),
        "ConditioningZeroOut": ("zero_out", ("conditioning",)),
    }

    def conditioning(value: Any, active: frozenset[str] = frozenset()) -> str | None:
        ref = reference(value)
        if ref is None:
            return None
        identifier, port = ref
        key = f"{identifier}:{port}"
        if key in active or len(active) > MAX_DEPTH:
            raise ValueError("ComfyUI 条件图存在循环或依赖层数过多")
        if key in conditions:
            return key
        kind, data = graph[identifier].get("class_type", ""), inputs(identifier)
        record = {
            "node_id": identifier,
            "output_port": port,
            "type": kind,
            "operation": "unsupported",
            "status": "unsupported",
            "summary_status": "missing",
            "inputs": [],
            "parameters": {},
        }
        conditions[key] = record
        if port != 0:
            record["reason"] = "unsupported_output_port"
            _warning(
                result,
                f"条件节点 {identifier}（{kind}）输出端口 {port} 不受静态解析支持",
            )
            return key
        active = active | {key}
        if kind in {
            "CLIPTextEncode",
            "CLIPTextEncodeSDXL",
            "CLIPTextEncodeSDXLRefiner",
        }:
            text_keys = (
                ("text_g", "text_l") if kind == "CLIPTextEncodeSDXL" else ("text",)
            )
            texts = [resolve(data.get(name)) for name in text_keys]
            record["texts"] = list(
                dict.fromkeys(text for text in texts if isinstance(text, str))
            )
            record["operation"] = "encode_sdxl" if len(text_keys) > 1 else "encode"
            exact = all(isinstance(text, str) for text in texts)
            record["status"] = "supported" if exact else "partial"
            record["summary_status"] = (
                ("summary" if len(text_keys) > 1 else "exact")
                if exact
                else "partial"
                if record["texts"]
                else "missing"
            )
            record["parameters"] = {
                name: resolve(value)
                for name, value in data.items()
                if name not in {*text_keys, "clip"} and reference(value) is None
            }
            for name in text_keys:
                if ref_key(data.get(name)):
                    record["inputs"].append(
                        {"name": name, "ref": ref_key(data[name]), "kind": "text"}
                    )
            if ref_key(data.get("clip")):
                record["clip_ref"] = ref_key(data["clip"])
            return key
        spec = condition_spec.get(kind)
        if spec:
            operation, names = spec
            record["operation"] = operation
            children = []
            for name in names:
                child = conditioning(data.get(name), active)
                if child:
                    record["inputs"].append(
                        {"name": name, "ref": child, "kind": "conditioning"}
                    )
                    children.append(conditions[child])
            record["parameters"] = {
                name: {"ref": ref_key(value)} if reference(value) else value
                for name, value in data.items()
                if name not in names
            }
            complete = len(children) == len(names) and all(
                child["status"] == "supported" for child in children
            )
            if operation == "zero_out" and len(children) == len(names):
                complete = True
                record["zeroed_embedding"] = True
            record["status"] = "supported" if complete else "partial"
            record["summary_status"] = (
                "summary" if complete else "partial" if children else "missing"
            )
            return key
        record["reason"] = "unsupported_node_type"
        record["inputs"] = [
            {"name": name, "ref": ref_key(value), "kind": "unknown"}
            for name, value in data.items()
            if reference(value)
        ]
        _warning(
            result,
            f"条件节点 {identifier}（{kind}）暂不支持静态解析，未推测其输出提示词",
        )
        return key

    def condition_summary(key: str | None) -> tuple[str, str]:
        if not key:
            return "", "missing"
        texts: list[str] = []
        pending, visited = [key], set()
        incomplete = False
        while pending:
            current = pending.pop()
            if current in visited:
                continue
            visited.add(current)
            record = conditions[current]
            incomplete = incomplete or record["status"] != "supported"
            if record.get("zeroed_embedding"):
                continue
            for text in record.get("texts", []):
                if text and text not in texts:
                    texts.append(text)
            pending.extend(
                reversed(
                    [
                        item["ref"]
                        for item in record["inputs"]
                        if item.get("kind") == "conditioning"
                    ]
                )
            )
        status = (
            "partial"
            if incomplete and texts
            else "missing"
            if incomplete
            else conditions[key]["summary_status"]
        )
        return "\n\n".join(texts), status

    def latent(value: Any, active: frozenset[str] = frozenset()) -> dict:
        ref = reference(value)
        if ref is None:
            return {}
        identifier, port = ref
        if identifier in active or len(active) > MAX_DEPTH:
            return {}
        kind = graph[identifier].get("class_type", "")
        data = inputs(identifier)
        active = active | {identifier}
        if port != 0:
            return {"dimensions_status": "unknown"}
        if kind in {"EmptyLatentImage", "EmptySD3LatentImage", "EmptyFlux2LatentImage"}:
            dimensions = {key: resolve(data.get(key)) for key in ("width", "height")}
            known = all(
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and value > 0
                for value in dimensions.values()
            )
            return {
                **(dimensions if known else {}),
                "mode": "text2img",
                "dimensions_status": "known" if known else "unknown",
            }
        if kind == "LoadImage":
            return {"mode": "img2img", "dimensions_status": "unknown"}
        for key in ("latent_image", "samples", "pixels", "image", "images"):
            if key in data and link(data[key]):
                values = latent(data[key], active)
                if kind in {"LatentUpscaleBy", "ImageScaleBy"}:
                    factor = resolve(data.get("scale_by"))
                    for dimension in ("width", "height"):
                        if isinstance(
                            values.get(dimension), (int, float)
                        ) and isinstance(factor, (int, float)):
                            values[dimension] = round(values[dimension] * factor)
                    if (
                        not isinstance(factor, (int, float))
                        or isinstance(factor, bool)
                        or factor <= 0
                    ):
                        values = {
                            name: value
                            for name, value in values.items()
                            if name not in {"width", "height"}
                        }
                        values["dimensions_status"] = "unknown"
                elif kind in {"LatentUpscale", "ImageScale"}:
                    width, height = (
                        resolve(data.get("width")),
                        resolve(data.get("height")),
                    )
                    valid_width = (
                        isinstance(width, (int, float))
                        and not isinstance(width, bool)
                        and width > 0
                    )
                    valid_height = (
                        isinstance(height, (int, float))
                        and not isinstance(height, bool)
                        and height > 0
                    )
                    if valid_width and valid_height:
                        values.update(width=width, height=height)
                        values["dimensions_status"] = "known"
                    elif values.get("dimensions_status") == "known" and (
                        valid_width or valid_height
                    ):
                        if valid_width:
                            values["height"] = round(
                                values["height"] * width / values["width"]
                            )
                            values["width"] = width
                        else:
                            values["width"] = round(
                                values["width"] * height / values["height"]
                            )
                            values["height"] = height
                    else:
                        values.pop("width", None)
                        values.pop("height", None)
                        values["dimensions_status"] = "unknown"
                elif kind == "ImageUpscaleWithModel":
                    values.pop("width", None)
                    values.pop("height", None)
                    values["dimensions_status"] = "unknown"
                    _warning(
                        result,
                        f"节点 {identifier} 的模型放大倍率未记录，后续尺寸不可静态确定",
                    )
                elif kind not in {
                    "KSampler",
                    "KSamplerAdvanced",
                    "VAEDecode",
                    "VAEEncode",
                    "VAEDecodeTiled",
                    "VAEEncodeTiled",
                }:
                    values.pop("width", None)
                    values.pop("height", None)
                    values["dimensions_status"] = "unknown"
                return values
        return {}

    normalized = result["normalized"]

    def prompt_candidates() -> list[dict]:
        if selected_root not in saves:
            return []
        selected_nodes = set(order)
        workflow = _json(fields.get("workflow")) or {}
        workflow_nodes = workflow.get("nodes", [])
        ui_nodes = {}
        duplicate_ids = set()
        if isinstance(workflow_nodes, list) and len(workflow_nodes) <= MAX_NODES:
            for node in workflow_nodes:
                if not isinstance(node, dict) or "id" not in node:
                    continue
                identifier = str(node["id"])
                if identifier in ui_nodes:
                    duplicate_ids.add(identifier)
                ui_nodes[identifier] = node
        for identifier in duplicate_ids:
            ui_nodes.pop(identifier)
        text_fields = {
            "CLIPTextEncode": {"text"},
            "CLIPTextEncodeSDXL": {"text_g", "text_l"},
            "CLIPTextEncodeSDXLRefiner": {"text"},
            "TextInput_": {"text"},
            "TextInput": {"text"},
            "String": {"text", "value"},
            "PrimitiveString": {"value", "text"},
            "Text Concatenate": {"text_a", "text_b", "text_c", "text_d"},
            "TextConcatenate": {"text_a", "text_b", "text_c", "text_d"},
        }
        detail_types = {"FaceDetailer", "FaceDetailerPipe", "DetailerForEach"}
        sampler_types = {"KSampler", "KSamplerAdvanced", *detail_types}
        static_paths = {
            *text_fields,
            *condition_spec,
            "Text Concatenate",
            "TextConcatenate",
        }
        usages: dict[str, dict] = {}
        text_usages: dict[tuple[str, int], dict] = {}

        def collect_usage(value: Any, stage_id: str, role: str) -> None:
            pending = [(value, role, False, False, stage_id, role, 0)]
            visited: set[tuple] = set()
            while pending:
                (
                    value,
                    incoming_role,
                    unknown_path,
                    text_path,
                    consumer,
                    input_name,
                    depth,
                ) = pending.pop()
                ref = reference(value)
                if ref is None or ref[0] not in selected_nodes:
                    continue
                identifier, port = ref
                if depth > MAX_DEPTH:
                    raise ValueError("ComfyUI 文本候选依赖层数过多")
                if text_path:
                    usage = text_usages.setdefault(
                        ref, {"roles": set(), "stage_ids": {}, "consumers": []}
                    )
                    usage["roles"].add(incoming_role)
                    usage["stage_ids"][stage_id] = None
                    receiver = {"node_id": consumer, "input_name": input_name}
                    if receiver not in usage["consumers"]:
                        usage["consumers"].append(receiver)
                identity = identifier, port, incoming_role, unknown_path, text_path
                if identity in visited:
                    continue
                visited.add(identity)
                kind = graph[identifier].get("class_type", "")
                unknown_path = unknown_path or kind not in static_paths or port != 0
                usage = usages.setdefault(
                    identifier,
                    {
                        "roles": set(),
                        "stage_ids": set(),
                        "ports": set(),
                        "unknown_path": False,
                    },
                )
                usage["roles"].add(incoming_role)
                usage["stage_ids"].add(stage_id)
                usage["ports"].add(port)
                usage["unknown_path"] |= unknown_path
                for name, child in inputs(identifier).items():
                    if name in {
                        "clip",
                        "model",
                        "vae",
                        "seed",
                        "noise_seed",
                        "mask",
                        "filename",
                        "filename_prefix",
                    }:
                        continue
                    child_role = (
                        "negative"
                        if name in {"negative", "negative_prompt"}
                        else "positive"
                        if name in {"positive", "positive_prompt"}
                        else incoming_role
                    )
                    if reference(child):
                        child_text_path = text_path or name in text_fields.get(kind, ())
                        pending.append(
                            (
                                child,
                                child_role,
                                unknown_path,
                                child_text_path,
                                identifier,
                                name,
                                depth + 1,
                            )
                        )

        stage_ancestors = {}
        for identifier in order:
            if graph[identifier].get("class_type") not in sampler_types:
                continue
            ancestors: list[str] = []
            visit(identifier, set(), set(), ancestors)
            stage_ancestors[identifier] = set(ancestors)
            for name, role in (("positive", "positive"), ("negative", "negative")):
                collect_usage(inputs(identifier).get(name), identifier, role)
        candidates = []
        for identifier in order:
            kind, data = graph[identifier].get("class_type", ""), inputs(identifier)
            if kind == "Raffle" or re.search(
                r"showanything|debug|preview|saveimage", kind, re.I
            ):
                continue
            known_fields = text_fields.get(kind, set())
            eligible = set(known_fields)
            if kind in detail_types:
                eligible.add("wildcard")
            if not eligible:
                ui = ui_nodes.get(identifier, {})
                declared = ui.get("inputs", []) if ui.get("type") == kind else []
                declared_string_fields = (
                    {
                        entry.get("name")
                        for entry in declared
                        if isinstance(entry, dict)
                        and entry.get("type") == "STRING"
                        and isinstance(entry.get("name"), str)
                    }
                    if isinstance(declared, list)
                    else set()
                )
                obvious_text_node = bool(re.search(r"text|prompt|string", kind, re.I))
                eligible = {
                    name
                    for name in ("prompt", "positive_prompt", "negative_prompt", "text")
                    if obvious_text_node or name in declared_string_fields
                }
            for field in data:
                value = data[field]
                if (
                    field not in eligible
                    or not isinstance(value, str)
                    or not value.strip()
                ):
                    continue
                usage = usages.get(identifier, {})
                roles = set(usage.get("roles", set()))
                if field in {"negative_prompt", "positive_prompt", "wildcard"}:
                    roles = {"negative" if field == "negative_prompt" else "positive"}
                role = "mixed" if len(roles) > 1 else next(iter(roles), "unknown")
                stage_ids = sorted(
                    usage.get("stage_ids")
                    or {
                        stage_id
                        for stage_id, ancestors in stage_ancestors.items()
                        if identifier in ancestors
                    },
                    key=lambda key: order.index(key),
                )
                candidates.append(
                    {
                        "id": f"{identifier}:{field}",
                        "node_id": identifier,
                        "node_type": kind,
                        "field": field,
                        "text": value,
                        "output_node_ids": [selected_root],
                        "stage_ids": stage_ids,
                        "role": role,
                        "status": "template"
                        if field == "wildcard"
                        else "static"
                        if field in known_fields and not usage.get("unknown_path")
                        else "unknown_path",
                        "output_ports": sorted(usage.get("ports", set())),
                    }
                )
        snapshots = _comfy_display_snapshots(
            graph, workflow, text_usages, selected_root, resolve
        )
        downstream: dict[str, list[tuple[int, str, str]]] = {}
        if snapshots:
            for consumer in order:
                for name, value in inputs(consumer).items():
                    ref = reference(value)
                    if ref:
                        downstream.setdefault(ref[0], []).append(
                            (ref[1], consumer, name)
                        )
        snapshot_branch_keys: dict[str, str] = {}

        def snapshot_branch_key(source_ref: str) -> str:
            """Match a displayed output's route to this save, not its ingredients."""
            if source_ref in snapshot_branch_keys:
                return snapshot_branch_keys[source_ref]
            producer, port = source_ref.rsplit(":", 1)
            nodes = {producer}
            edges = []
            pending = [producer]
            while pending:
                identifier = pending.pop()
                for output_port, consumer, name in downstream.get(identifier, ()):
                    if identifier == producer and output_port != int(port):
                        continue
                    edges.append((identifier, output_port, consumer, name))
                    if consumer not in nodes:
                        nodes.add(consumer)
                        pending.append(consumer)
            # All edges are scoped to the selected save's ancestors. Keep IDs,
            # types and ports through that save, excluding other input branches
            # and observers that only display the text.
            key = match_digest(
                [
                    "comfy-display-branch-v1",
                    selected_root,
                    source_ref,
                    [[node, graph[node].get("class_type")] for node in sorted(nodes)],
                    sorted(edges),
                ]
            )
            snapshot_branch_keys[source_ref] = key
            return key

        if snapshots:
            _warning(
                result,
                "发现主链路同一输出端口的关联显示快照；未验证是否为本次执行结果，请核对后手动采用",
            )
            for source_ref in dict.fromkeys(
                item["source_ref"] for item in snapshots if item["conflicting"]
            ):
                _warning(
                    result,
                    f"输出 {source_ref} 的显示快照存在不同文本，请选择一份，不要合并冲突结果",
                )
        for candidate in candidates + snapshots:
            display = candidate["status"] == "display_snapshot"
            identity = (
                [
                    "display",
                    candidate["source_ref"],
                    sorted(
                        (item["node_id"], item["input_name"])
                        for item in candidate["consumers"]
                    ),
                ]
                if display
                else [
                    "field",
                    candidate["node_id"],
                    candidate["node_type"],
                    candidate["field"],
                ]
            )
            candidate["match_key"] = match_digest(
                [
                    "comfy-display-candidate-v2" if display else "comfy-candidate-v1",
                    snapshot_branch_key(candidate["source_ref"])
                    if display
                    else branch_keys[selected_root],
                    identity,
                    candidate["role"],
                    candidate["output_ports"],
                ]
            )
        return _deduplicate_comfy_candidates(
            candidates + snapshots, graph, text_fields, ui_nodes, text_usages
        )

    stages, loras, models = [], [], []
    for identifier in order:
        node = graph[identifier]
        kind, data = node.get("class_type", ""), inputs(identifier)
        if kind in {
            "CheckpointLoaderSimple",
            "CheckpointLoader",
            "UNETLoader",
            "UnetLoaderGGUF",
        }:
            name = resolve(data.get("ckpt_name", data.get("unet_name")))
            if isinstance(name, str) and name not in models:
                models.append(name)
        if kind in {"LoraLoader", "LoraLoaderModelOnly"}:
            name = resolve(data.get("lora_name"))
            strengths = {
                key: resolve(data[key])
                for key in ("strength_model", "strength_clip")
                if key in data
            }
            if name and any(value != 0 for value in strengths.values()):
                loras.append({"node_id": identifier, "name": name, **strengths})
        elif kind == "Power Lora Loader (rgthree)":
            for value in data.values():
                if (
                    isinstance(value, dict)
                    and value.get("on") is True
                    and value.get("lora")
                ):
                    if (
                        value.get("strength", 1) != 0
                        or value.get("strengthTwo", 0) != 0
                    ):
                        loras.append(
                            {
                                "node_id": identifier,
                                "name": value["lora"],
                                "strength_model": value.get("strength", 1),
                                "strength_clip": value.get(
                                    "strengthTwo", value.get("strength", 1)
                                ),
                            }
                        )
        if kind not in {
            "KSampler",
            "KSamplerAdvanced",
            "FaceDetailer",
            "DetailerForEach",
            "FaceDetailerPipe",
        }:
            continue
        stage = {"node_id": identifier, "type": kind}
        for source, destination in {
            "seed": "seed",
            "noise_seed": "seed",
            "steps": "steps",
            "cfg": "guidance_scale",
            "sampler_name": "sampler",
            "scheduler": "scheduler",
            "denoise": "denoising_strength",
            "start_at_step": "start_at_step",
            "end_at_step": "end_at_step",
        }.items():
            if source in data:
                value = resolve(data[source])
                if value is not None:
                    stage[destination] = value
        for name, prompt_key in (
            ("positive", "prompt"),
            ("negative", "negative_prompt"),
        ):
            condition_key = conditioning(data.get(name))
            if condition_key:
                stage[f"{name}_conditioning"] = condition_key
            text, status = condition_summary(condition_key)
            stage[f"{prompt_key}_status"] = status
            if text or status in {"exact", "summary"}:
                stage[prompt_key] = text
        stage.update(latent(data.get("latent_image")))
        stages.append(stage)
    if models:
        normalized["models"] = models
        if len(models) == 1:
            normalized["model"] = models[0]
    if loras:
        normalized["loras"] = loras
    if stages:
        normalized["stages"] = stages
        for key in (
            "seed",
            "sampler",
            "scheduler",
            "mode",
        ):
            values = [stage[key] for stage in stages if key in stage]
            if len(values) == len(stages) and all(
                value == values[0] for value in values
            ):
                normalized[key] = values[0]
        for prompt_key, condition_key in (
            ("prompt", "positive_conditioning"),
            ("negative_prompt", "negative_conditioning"),
        ):
            summaries = list(
                dict.fromkeys(
                    stage[prompt_key] for stage in stages if stage.get(prompt_key)
                )
            )
            leaves: list[str] = []
            visited = set()
            pending = [
                stage[condition_key]
                for stage in reversed(stages)
                if condition_key in stage
            ]
            while pending:
                key = pending.pop()
                if key in visited:
                    continue
                visited.add(key)
                record = conditions[key]
                if record.get("zeroed_embedding"):
                    continue
                for text in record.get("texts", []):
                    if text and text not in leaves:
                        leaves.append(text)
                pending.extend(
                    reversed(
                        [
                            entry["ref"]
                            for entry in record["inputs"]
                            if entry.get("kind") == "conditioning"
                        ]
                    )
                )
            statuses = [
                stage.get(f"{prompt_key}_status", "missing") for stage in stages
            ]
            status = (
                "partial"
                if leaves and any(value in {"partial", "missing"} for value in statuses)
                else "missing"
                if not leaves
                and any(value in {"partial", "missing"} for value in statuses)
                else "exact"
                if len(summaries) <= 1 and all(value == "exact" for value in statuses)
                else "summary"
            )
            normalized[f"{prompt_key}_status"] = status
            if leaves or status in {"exact", "summary"}:
                normalized[prompt_key] = "\n\n".join(leaves)
        if len(stages) == 1:
            normalized.update(
                {
                    key: value
                    for key, value in stages[0].items()
                    if key not in {"node_id", "type"}
                }
            )
        else:
            _warning(
                result,
                "包含多个采样或细节修复阶段，请按阶段查看参数，不能用单组采样参数精确复现",
            )
        if any(
            stage.get("prompt_status") in {"summary", "partial"}
            or stage.get("negative_prompt_status") in {"summary", "partial"}
            for stage in stages
        ):
            _warning(
                result,
                "提示词摘要仅汇集条件链路引用文本；已排除清零条件，但未按权重、区域或时间范围计算实际贡献，不能等同于直接拼接提示词，请保留条件结构",
            )
    normalized["condition_nodes"] = conditions
    normalized["prompt_candidates"] = prompt_candidates()
    normalized["requires_output_selection"] = len(saves) > 1 and not selected_root
    if normalized["requires_output_selection"]:
        normalized["stages"] = []
    normalized["output_nodes"] = roots
    sampler_types = {
        "KSampler",
        "KSamplerAdvanced",
        "FaceDetailer",
        "DetailerForEach",
        "FaceDetailerPipe",
    }
    normalized["outputs"] = [
        {
            "node_id": root,
            "type": graph[root].get("class_type"),
            "kind": "save" if root in saves else "preview",
            "match_key": branch_keys[root],
            "stage_ids": [
                identifier
                for identifier in orders[root]
                if graph[identifier].get("class_type") in sampler_types
            ],
            "image_ref": ref_key(inputs(root).get("images", inputs(root).get("image"))),
        }
        for root in roots
    ]
    if selected_root:
        normalized["selected_output_node"] = selected_root
        normalized["selected_output_ref"] = ref_key(
            inputs(selected_root).get("images", inputs(selected_root).get("image"))
        )
        normalized["output_selection_reason"] = (
            "user_selected_save"
            if output_node_id
            else "single_save"
            if saves
            else "single_preview"
        )
        final_dimensions = latent(
            inputs(selected_root).get("images", inputs(selected_root).get("image"))
        )
        normalized["output_dimensions_status"] = final_dimensions.get(
            "dimensions_status", "unknown"
        )
        if final_dimensions.get("dimensions_status") == "known":
            normalized["width"] = final_dimensions["width"]
            normalized["height"] = final_dimensions["height"]
        else:
            normalized.pop("width", None)
            normalized.pop("height", None)
    else:
        normalized["output_selection_reason"] = (
            "ambiguous_saves" if saves else "ambiguous_previews"
        )
        normalized.pop("width", None)
        normalized.pop("height", None)
