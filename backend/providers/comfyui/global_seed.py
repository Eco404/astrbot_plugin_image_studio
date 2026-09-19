"""Conservative support for Easy-Use's server-side global-seed prompt hook.

Verified against ComfyUI-Easy-Use 450b1ce4ce43b2280521c87f5fa388a898fb2ad2:
py/server.py, py/nodes/seed.py and web_version/v1/js/seed.js. The hook, not
this module, chooses random values. Its workflow.seed_widgets prerequisite is
prepared only from verified serialized controls, never from integer positions.
"""

from __future__ import annotations

import copy
from typing import Any

from .ui_sync import check_unlinked, widget_slot

GLOBAL_SEED_CLASS = "easy globalSeed"
GLOBAL_SEED_TOKEN = "$GlobalSeed.value$"
MAX_GLOBAL_SEED = 1125899906842624
_SEED_FIELDS = ("seed_num", "seed", "noise_seed")


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _nodes(workflow: dict) -> dict[str, dict]:
    values = workflow.get("nodes")
    if not isinstance(values, list):
        raise ValueError("缺少完整的界面节点列表")
    result = {}
    for node in values:
        if not isinstance(node, dict) or node.get("id") is None:
            raise ValueError("界面节点信息不完整")
        identity = str(node["id"])
        if identity in result:
            raise ValueError("界面节点 ID 重复")
        result[identity] = node
    return result


def _seed_slot(workflow: dict, ui: dict, api: dict | None) -> tuple[str, int]:
    if api is not None and ui.get("type") != api.get("class_type"):
        raise ValueError("种子节点在执行图和界面图中的类型不一致")
    fields = [
        key
        for key in _SEED_FIELDS
        if api is None or _integer(api.get("inputs", {}).get(key))
    ]
    verified = []
    for name in fields:
        try:
            check_unlinked(workflow, ui, name)
            index = widget_slot(ui, name)
            # Easy-Use writes widgets_values[index], so named dictionaries
            # cannot provide the positional contract required by this hook.
            if isinstance(index, int) and not isinstance(index, bool):
                verified.append((name, index))
        except ValueError:
            pass
    if len(verified) != 1 or api is not None and len(fields) != 1:
        raise ValueError("种子控件索引、连线状态或多个种子字段无法唯一核实")
    return verified[0]


def _policy(graph: dict, workflow: dict | None, *, synthesize: bool) -> dict | None:
    globals_ = [
        (str(key), node)
        for key, node in graph.items()
        if isinstance(node, dict) and node.get("class_type") == GLOBAL_SEED_CLASS
    ]
    if not globals_:
        return None
    if len(globals_) != 1:
        raise ValueError("存在多个全局种子节点，无法确定最终控制节点")
    if not isinstance(workflow, dict):
        raise ValueError("缺少界面工作流，无法提供全局种子所需的 seed_widgets")
    global_id, node = globals_[0]
    inputs = node.get("inputs", {})
    value, mode, action = (
        inputs.get("value"),
        inputs.get("mode"),
        inputs.get("action"),
    )
    if not _integer(value) or not 0 <= value <= MAX_GLOBAL_SEED:
        raise ValueError("全局种子 value 不是支持范围内的固定整数")
    if not isinstance(mode, bool):
        raise ValueError("全局种子 mode 不是明确的布尔值")
    if action not in {"fixed", "randomize"}:
        raise ValueError("递增、递减或逐节点策略暂不支持跨次状态与结果回写核验")
    if action == "randomize" and not mode:
        raise ValueError("生成后随机策略记录的是下一次种子，暂不支持其跨次状态核验")
    nodes = _nodes(workflow)
    ui = nodes.get(global_id)
    if not ui or ui.get("type") != GLOBAL_SEED_CLASS:
        raise ValueError("无法找到对应的全局种子界面节点")
    widgets = ui.get("widgets_values")
    if not isinstance(widgets, list) or len(widgets) != 4:
        raise ValueError("全局种子节点不是已验证的四控件布局")
    for index, name in enumerate(("value", "mode", "action", "last_seed")):
        check_unlinked(workflow, ui, name)
        if widget_slot(ui, name) != index:
            raise ValueError("全局种子控件布局与服务器回写布局不一致")
    if not _integer(widgets[0]) or not isinstance(widgets[1], bool):
        raise ValueError("全局种子节点的界面控件类型不一致")
    if not isinstance(widgets[2], str) or not isinstance(widgets[3], (str, int)):
        raise ValueError("全局种子节点的界面控件类型不一致")
    raw_map = workflow.get("seed_widgets")
    if raw_map is None:
        if not synthesize:
            raise ValueError("提交工作流缺少 seed_widgets")
        raw_map = {}
        for identity, api in graph.items():
            fields = [
                name
                for name in _SEED_FIELDS
                if _integer(api.get("inputs", {}).get(name))
            ]
            if not fields:
                continue
            ui = nodes.get(str(identity))
            if ui is None:
                raise ValueError(f"节点 #{identity} 缺少可核实的种子界面控件")
            _, slot = _seed_slot(workflow, ui, api)
            raw_map[str(identity)] = slot
    if not isinstance(raw_map, dict):
        raise ValueError("seed_widgets 不是节点到控件索引的映射")
    targets = []
    for identity, index in raw_map.items():
        if not isinstance(identity, str) or not _integer(index):
            raise ValueError("seed_widgets 包含无效节点或控件索引")
        ui = nodes.get(identity)
        if ui is None:
            raise ValueError(f"seed_widgets 的节点 #{identity} 不存在")
        api = graph.get(identity)
        field, verified_index = _seed_slot(workflow, ui, api)
        if index != verified_index:
            raise ValueError(f"节点 #{identity} 的 seed_widgets 索引不匹配")
        if api is not None:
            targets.append((identity, field))
    tokens = [
        (str(identity), field)
        for identity, api in graph.items()
        for field, value in api.get("inputs", {}).items()
        if isinstance(value, str) and GLOBAL_SEED_TOKEN in value
    ]
    return {
        "node_id": global_id,
        "value": value,
        "mode": mode,
        "action": action,
        "seed_widgets": copy.deepcopy(raw_map),
        "seed_targets": targets,
        "token_targets": tokens,
    }


def prepare_global_seed(config: dict, graph: dict, workflow: dict | None) -> dict:
    """Prepare the required map without changing input values or running RNG.

    Existing metadata is retained only after validating every supplied entry.
    Missing maps are synthesized atomically; one unverified candidate prevents
    activating only a subset of the graph's global-seed behavior.
    """
    result = {"workflow": copy.deepcopy(workflow), "warnings": [], "policy": None}
    try:
        policy = _policy(graph, workflow, synthesize=True)
        if policy is not None:
            result["workflow"]["seed_widgets"] = policy["seed_widgets"]
            result["policy"] = policy
    except (ValueError, TypeError, AttributeError) as exc:
        result["warnings"] = [
            f"Easy-Use 全局种子未通过核验：{exc}；保留原始设置，"
            "请在 ComfyUI 核对全局种子行为与复现参数。"
        ]
    return result


def permitted_global_seed_changes(
    submitted: dict, returned: dict, workflow: dict | None
) -> set[tuple[str, str]] | None:
    """Verify the entire returned graph against one supported Easy-Use hook.

    None means unverified; an empty set means no graph changes. This is only a
    result validator: it neither invokes RNG nor reimplements execution state.
    Other known runtime changes must be validated separately by the caller.
    """
    try:
        policy = _policy(submitted, workflow, synthesize=False)
        if policy is None:
            return set() if returned == submitted else None
        global_id = policy["node_id"]
        actual = returned[global_id]["inputs"]["value"]
        if not _integer(actual) or not 0 <= actual <= MAX_GLOBAL_SEED:
            return None
        if policy["action"] == "fixed" and actual != policy["value"]:
            return None
        expected = copy.deepcopy(submitted)
        changes = set()

        def put(identity: str, field: str, value: Any) -> None:
            if expected[identity]["inputs"][field] != value:
                changes.add((identity, field))
            expected[identity]["inputs"][field] = value

        put(global_id, "value", actual)
        for identity, field in policy["seed_targets"]:
            if not _integer(returned[identity]["inputs"][field]):
                return None
            put(identity, field, actual)
        for identity, field in policy["token_targets"]:
            value = expected[identity]["inputs"][field]
            put(identity, field, value.replace(GLOBAL_SEED_TOKEN, str(actual)))
        return changes if returned == expected else None
    except (ValueError, TypeError, AttributeError, KeyError):
        return None


def global_seed_workflow_writeback(
    submitted: dict, returned: dict, workflow: dict | None
) -> dict | None:
    """Return the exact UI seed writes made by a verified server hook.

    The server stores the *previous UI value* in last_seed, not the input's
    last_seed string. Token replacements have no generic frontend writeback.
    """
    if permitted_global_seed_changes(submitted, returned, workflow) is None:
        return None
    try:
        policy = _policy(submitted, workflow, synthesize=False)
        result = copy.deepcopy(workflow)
        if policy is None:
            return result
        nodes = _nodes(result)
        controls = nodes[policy["node_id"]]["widgets_values"]
        controls[-1] = controls[0]
        controls[0] = returned[policy["node_id"]]["inputs"]["value"]
        for identity, field in policy["seed_targets"]:
            slot = policy["seed_widgets"][identity]
            nodes[identity]["widgets_values"][slot] = returned[identity]["inputs"][
                field
            ]
        return result
    except (ValueError, TypeError, AttributeError, KeyError):
        return None
