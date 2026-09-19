"""Validate ComfyUI result writebacks and repair PNG workflow metadata losslessly."""

from __future__ import annotations

import copy
import json
import struct
import zlib
from dataclasses import replace

from ...comfyui.catalog import node_adapter, seed_policy
from ...models import GeneratedImage
from .global_seed import global_seed_workflow_writeback, permitted_global_seed_changes
from .ui_sync import read_widget_values, synchronize_workflow
from .workflows import clear_execution_cache_markers

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_MAX_IMAGE_BYTES = 64 * 1024 * 1024
_MAX_TEXT_BYTES = 4 * 1024 * 1024


def _warning(snapshot, message):
    snapshot["workflow_sync_warnings"] = list(
        dict.fromkeys(snapshot.get("workflow_sync_warnings", []) + [message])
    )


def _inflate(data):
    decoder = zlib.decompressobj()
    result = decoder.decompress(data, _MAX_TEXT_BYTES + 1)
    if len(result) > _MAX_TEXT_BYTES or not decoder.eof or decoder.unused_data:
        raise ValueError("无效或过大的压缩元数据")
    return result


def _text(kind, data):
    keyword, separator, payload = data.partition(b"\x00")
    if not separator or keyword not in {b"prompt", b"workflow"}:
        return None
    if kind == b"zTXt":
        if payload[:1] != b"\x00":
            raise ValueError("不支持的 PNG 文本压缩格式")
        payload = _inflate(payload[1:])
    elif kind == b"iTXt":
        if len(payload) < 2 or payload[0] not in {0, 1} or payload[1] != 0:
            raise ValueError("无效的 PNG 国际化文本")
        compressed, payload = payload[0], payload[2:]
        for _ in range(2):
            _prefix, separator, payload = payload.partition(b"\x00")
            if not separator:
                raise ValueError("无效的 PNG 国际化文本")
        if compressed:
            payload = _inflate(payload)
    if len(payload) > _MAX_TEXT_BYTES:
        raise ValueError("PNG 工作流元数据过大")
    encoding = "utf-8" if kind == b"iTXt" else "latin-1"
    return keyword.decode("ascii"), payload.decode(encoding)


def _read_png(data):
    """Parse bounded chunks without decoding or recompressing image pixels."""
    if not data.startswith(_PNG_SIGNATURE) or len(data) > _MAX_IMAGE_BYTES:
        raise ValueError("无效或过大的 PNG 文件")
    position, chunks, fields, total_text = len(_PNG_SIGNATURE), [], {}, 0
    ended = False
    while position < len(data):
        if position + 12 > len(data):
            raise ValueError("PNG 数据块不完整")
        size = struct.unpack_from(">I", data, position)[0]
        end = position + 12 + size
        if end > len(data):
            raise ValueError("PNG 数据块不完整")
        kind = data[position + 4 : position + 8]
        payload = data[position + 8 : end - 4]
        checksum = struct.unpack_from(">I", data, end - 4)[0]
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != checksum:
            raise ValueError("PNG 数据块校验失败")
        if not chunks and (kind != b"IHDR" or size != 13):
            raise ValueError("PNG 缺少有效文件头")
        key = None
        if kind in {b"tEXt", b"zTXt", b"iTXt"}:
            text = _text(kind, payload)
            if text:
                key, value = text
                if key in fields:
                    raise ValueError("PNG 包含重复的工作流元数据")
                total_text += len(value.encode("utf-8"))
                if total_text > _MAX_TEXT_BYTES:
                    raise ValueError("PNG 工作流元数据过大")
                fields[key] = value
        chunks.append((data[position:end], key))
        position = end
        if kind == b"IEND":
            if size or position != len(data):
                raise ValueError("PNG 结束数据块无效")
            ended = True
            break
    if not ended:
        raise ValueError("PNG 缺少结束数据块")
    return fields, chunks


def _replace_workflow(chunks, workflow):
    encoded = json.dumps(workflow, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(encoded) > _MAX_TEXT_BYTES:
        raise ValueError("同步后的 PNG 工作流元数据过大")
    payload = b"workflow\x00\x00\x00\x00\x00" + encoded
    body = b"iTXt" + payload
    replacement = (
        struct.pack(">I", len(payload))
        + body
        + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    )
    result = _PNG_SIGNATURE + b"".join(
        replacement if key == "workflow" else raw for raw, key in chunks
    )
    if len(result) > _MAX_IMAGE_BYTES:
        raise ValueError("同步后的 PNG 文件超过大小限制")
    return result


def _nodes(workflow):
    if not isinstance(workflow, dict) or not isinstance(workflow.get("nodes"), list):
        raise TypeError("界面工作流节点无效")
    result = {}
    for node in workflow["nodes"]:
        if not isinstance(node, dict) or node.get("id") is None:
            raise ValueError("界面工作流节点无效")
        node_id = str(node["id"])
        if node_id in result:
            raise ValueError("界面工作流节点 ID 重复")
        result[node_id] = node
    return result


def _display_text(value):
    return isinstance(value, str) or (
        isinstance(value, list) and all(isinstance(item, str) for item in value)
    )


def _repair_display_names(workflow):
    for node in workflow["nodes"]:
        if not node_adapter(node.get("type")).get("display"):
            continue
        positional = node.get("widgets_values")
        named = node.get("widgets_values_named")
        if (
            not isinstance(positional, list)
            or not isinstance(named, dict)
            or "text" not in named
        ):
            continue
        # Known observer serialization stores either its text string or the
        # one text widget's list of observed strings. Never use API.inputs.text.
        if len(positional) != 1 or not _display_text(positional[0]):
            continue
        value, previous = positional[0], named["text"]
        if isinstance(previous, str):
            if isinstance(value, str):
                named["text"] = value
            elif len(value) == 1:
                named["text"] = value[0]
        elif isinstance(previous, list) and all(
            isinstance(item, str) for item in previous
        ):
            named["text"] = [value] if isinstance(value, str) else copy.deepcopy(value)


def _verified_ui(returned_ui, submitted, actual_graph, targets, expected_ui):
    """Allow only known seed writebacks and display text, never arbitrary UI edits."""
    expected = synchronize_workflow(
        {"workflow": expected_ui}, actual_graph, targets=targets
    )
    original_nodes, returned_nodes = _nodes(submitted["workflow"]), _nodes(returned_ui)
    if original_nodes.keys() != returned_nodes.keys():
        raise ValueError("返回图片的界面节点与提交工作流不一致")
    expected_nodes = _nodes(expected["workflow"])
    for node_id, field in targets:
        source, returned = original_nodes.get(node_id), returned_nodes.get(node_id)
        if not source or not returned or source.get("type") != returned.get("type"):
            raise ValueError("返回图片的种子节点不一致")
        allowed = [
            *read_widget_values(source, field),
            *read_widget_values(expected_nodes[node_id], field),
        ]
        if any(value not in allowed for value in read_widget_values(returned, field)):
            raise ValueError("返回图片的界面种子不是本次请求或实际执行的值")
    normalized = synchronize_workflow(
        {"workflow": returned_ui}, actual_graph, targets=targets
    )
    if normalized["warnings"]:
        raise ValueError("无法可靠定位返回图片中的种子控件")
    comparison = copy.deepcopy(normalized["workflow"])
    for node in comparison["nodes"]:
        previous = expected_nodes[str(node["id"])]
        if node.get("type") == previous.get("type") and node_adapter(
            node.get("type")
        ).get("display"):
            widgets = node.get("widgets_values")
            if isinstance(widgets, list) and all(
                _display_text(value) for value in widgets
            ):
                if "widgets_values" in previous:
                    node["widgets_values"] = copy.deepcopy(previous["widgets_values"])
                else:
                    node.pop("widgets_values", None)
            named = node.get("widgets_values_named")
            previous_named = previous.get("widgets_values_named")
            if (
                isinstance(named, dict)
                and isinstance(previous_named, dict)
                and "text" in named
                and "text" in previous_named
                and _display_text(named["text"])
            ):
                named["text"] = copy.deepcopy(previous_named["text"])
    if comparison != expected["workflow"]:
        raise ValueError("返回图片的界面工作流含无法核实的改动")
    _repair_display_names(normalized["workflow"])
    return normalized["workflow"]


def _recover(snapshot, fields):
    original_graph = snapshot["api_graph"]
    random_targets = {}
    for node_id, node in original_graph.items():
        policy = seed_policy(node.get("class_type"))
        field = policy.get("field")
        if policy.get("writeback") == "rgthree" and node.get("inputs", {}).get(
            field
        ) in policy.get("negative_sentinels", []):
            random_targets[(str(node_id), field)] = policy
    recovered, corrected_ui = set(), None
    try:
        if not fields:
            return None
        returned = json.loads(fields.get("prompt", ""))
        if not isinstance(returned, dict) or returned.keys() != original_graph.keys():
            raise ValueError("返回图片的执行节点与本次提交不一致")
        if any(not isinstance(node, dict) for node in returned.values()):
            raise ValueError("返回图片的执行节点无效")
        clear_execution_cache_markers(returned)
        expected = copy.deepcopy(original_graph)
        for (node_id, field), policy in random_targets.items():
            value = returned[node_id].get("inputs", {}).get(field)
            if (
                isinstance(value, int)
                and not isinstance(value, bool)
                and policy["min"] <= value <= policy["max"]
            ):
                expected[node_id]["inputs"][field] = value
                recovered.add((node_id, field))
        global_targets = permitted_global_seed_changes(
            expected, returned, snapshot.get("workflow")
        )
        if global_targets is None:
            recovered.clear()
            raise ValueError("返回图片的执行参数含无法核实的改动")
        expected_ui = global_seed_workflow_writeback(
            expected, returned, snapshot.get("workflow")
        )
        ui_graph = copy.deepcopy(returned)
        ui_targets = recovered | global_targets
        if expected_ui is not None:
            expected_nodes = _nodes(expected_ui)
            for node_id, api_node in returned.items():
                if api_node.get("class_type") == "easy globalSeed":
                    ui_graph[node_id]["inputs"]["last_seed"] = expected_nodes[node_id][
                        "widgets_values"
                    ][-1]
                    ui_targets.add((node_id, "last_seed"))
        if ui_targets:
            synchronized = synchronize_workflow(
                {**snapshot, "workflow": expected_ui, "workflow_json": None},
                ui_graph,
                targets=ui_targets,
            )
            snapshot.update(
                api_graph=returned,
                api_graph_json=json.dumps(returned, ensure_ascii=False),
                workflow=synchronized["workflow"],
            )
            for warning in synchronized["warnings"]:
                _warning(snapshot, warning)
        if "workflow" in fields and snapshot.get("workflow") is not None:
            # Validation needs the pre-execution sentinel values as well as the
            # concrete returned seed. Keep the caller's submitted workflow here.
            corrected_ui = json.loads(fields["workflow"])
        return corrected_ui, ui_graph, ui_targets, expected_ui
    except (ValueError, TypeError, AttributeError, RecursionError) as exc:
        _warning(snapshot, f"返回图片的工作流元数据未同步：{exc}；原文件保持不变。")
        return None
    finally:
        if random_targets.keys() - recovered:
            _warning(
                snapshot,
                "图片未提供可核实的实际随机种子回写；执行记录保留提交值，"
                "再次运行工作流可能重新随机。",
            )


def prepare_output(image: GeneratedImage, submitted: dict) -> GeneratedImage:
    """Repair validated PNG workflow metadata without changing compressed pixels."""
    snapshot = copy.deepcopy(submitted)
    snapshot.setdefault("workflow_sync_warnings", [])
    content = image.data
    if content.startswith(_PNG_SIGNATURE):
        try:
            fields, chunks = _read_png(content)
            result = _recover(snapshot, fields)
            if result:
                returned_ui, actual_graph, targets, expected_ui = result
                if returned_ui is not None:
                    corrected_ui = _verified_ui(
                        returned_ui, submitted, actual_graph, targets, expected_ui
                    )
                    snapshot["workflow"] = corrected_ui
                    if corrected_ui != returned_ui:
                        content = _replace_workflow(chunks, corrected_ui)
        except (
            ValueError,
            TypeError,
            AttributeError,
            OSError,
            RecursionError,
            zlib.error,
        ) as exc:
            _warning(snapshot, f"返回图片的工作流元数据未同步：{exc}；原文件保持不变。")
    elif snapshot.get("workflow") is not None:
        _warning(
            snapshot,
            "当前仅支持无损同步 PNG 中的工作流元数据；此图片格式的原文件保持不变，"
            "拖回 ComfyUI 后请核对种子等参数。",
        )
    snapshot["workflow_json"] = (
        json.dumps(snapshot["workflow"], ensure_ascii=False)
        if snapshot.get("workflow") is not None
        else None
    )
    return replace(
        image,
        data=content,
        effective_parameters={**image.effective_parameters, "_comfyui": snapshot},
    )


def image_workflow_snapshot(image: GeneratedImage, submitted: dict) -> dict:
    """Compatibility helper for callers that only need the checked snapshot."""
    return prepare_output(image, submitted).effective_parameters["_comfyui"]
