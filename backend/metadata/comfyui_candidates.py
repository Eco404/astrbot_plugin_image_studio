"""Read-only prompt candidates, display snapshots and proven-ancestry deduplication."""

from __future__ import annotations
import hashlib
import re
from collections.abc import Callable
from typing import Any
from .common import MAX_DEPTH, MAX_NODES


def _display_text(value: Any) -> str | None:
    """Read a single saved string, never stringify tensors or join output batches."""

    for _ in range(4):
        if isinstance(value, str):
            return value if value.strip() else None
        if not isinstance(value, list) or len(value) != 1:
            return None
        value = value[0]
    return None


def _comfy_display_snapshots(
    graph: dict,
    workflow: dict,
    text_usages: dict,
    selected_root: str,
    resolve: Callable[[Any], Any],
    *,
    verified_inputs: set[tuple[str, str]] | None = None,
) -> list[dict]:
    """Attach unverified display caches only to unresolved main-chain text ports."""

    # Each adapter declares the observed input and the saved-text field. Generic
    # names such as 'debug' or 'preview' are not evidence of a text observer.
    adapters = {"easy showAnything": ("anything", "text")}
    nodes = workflow.get("nodes", [])
    ui_nodes = {}
    duplicate_ids = set()
    if isinstance(nodes, list) and len(nodes) <= MAX_NODES:
        for node in nodes:
            if not isinstance(node, dict) or "id" not in node:
                continue
            identifier = str(node["id"])
            if identifier in ui_nodes:
                duplicate_ids.add(identifier)
            ui_nodes[identifier] = node
    for identifier in duplicate_ids:
        ui_nodes.pop(identifier, None)
    links = {}
    duplicate_links = set()
    for item in (
        workflow.get("links", []) if isinstance(workflow.get("links"), list) else []
    ):
        if not isinstance(item, list) or len(item) < 6:
            continue
        identifier = str(item[0])
        if identifier in links:
            duplicate_links.add(identifier)
        links[identifier] = item
    for identifier in duplicate_links:
        links.pop(identifier, None)

    def api_ref(value: Any) -> tuple[str, int] | None:
        if (
            isinstance(value, list)
            and len(value) == 2
            and str(value[0]) in graph
            and type(value[1]) is int
            and value[1] >= 0
        ):
            return str(value[0]), value[1]
        return None

    def ui_ref(node: dict, name: str | None = None) -> tuple[str, int] | None:
        entries = node.get("inputs", [])
        if not isinstance(entries, list):
            return None
        found = []
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict) or (
                name is not None and entry.get("name") != name
            ):
                continue
            edge = links.get(str(entry.get("link")))
            if (
                edge
                and str(edge[3]) == str(node.get("id"))
                and edge[4] == index
                and str(edge[1]) in ui_nodes
                and type(edge[2]) is int
                and edge[2] >= 0
            ):
                found.append((str(edge[1]), edge[2]))
        return found[0] if len(found) == 1 else None

    setters: dict[str, list[str]] = {}
    for identifier, node in ui_nodes.items():
        widgets = node.get("widgets_values")
        if (
            node.get("type") == "SetNode"
            and isinstance(widgets, list)
            and widgets
            and isinstance(widgets[0], str)
        ):
            setters.setdefault(widgets[0], []).append(identifier)

    def canonical_ui(
        ref: tuple[str, int] | None,
        visited: frozenset = frozenset(),
        *,
        require_active: bool = False,
    ) -> tuple[str, int] | None:
        if ref is None or ref in visited or len(visited) >= MAX_DEPTH:
            return None
        identifier, port = ref
        node = ui_nodes.get(identifier, {})
        kind = node.get("type")
        if require_active and node.get("mode", 0) != 0:
            return None
        if identifier in graph and graph[identifier].get("class_type") != kind:
            return None
        if port == 0 and kind == "GetNode":
            if identifier in graph:
                # Only resolve frontend aliases erased from the executed graph.
                return None
            widgets = node.get("widgets_values")
            matches = (
                setters.get(widgets[0], [])
                if isinstance(widgets, list) and widgets and isinstance(widgets[0], str)
                else []
            )
            return (
                canonical_ui(
                    (matches[0], 0), visited | {ref}, require_active=require_active
                )
                if len(matches) == 1
                else None
            )
        if port == 0 and kind in {"SetNode", "Reroute"}:
            source = canonical_ui(
                ui_ref(node), visited | {ref}, require_active=require_active
            )
            if identifier in graph:
                data = graph[identifier].get("inputs", {})
                sources = (
                    [api_ref(value) for value in data.values()]
                    if isinstance(data, dict)
                    else []
                )
                sources = [value for value in sources if value is not None]
                if len(sources) != 1 or sources[0] != source:
                    return None
            return source
        if identifier in graph and graph[identifier].get("class_type") == kind:
            return ref
        return None

    if verified_inputs is not None:
        for identifier, node in ui_nodes.items():
            api_node = graph.get(identifier, {})
            if (
                node.get("type") != api_node.get("class_type")
                or node.get("mode", 0) != 0
            ):
                continue
            data = api_node.get("inputs", {})
            entries = node.get("inputs", [])
            if not isinstance(data, dict) or not isinstance(entries, list):
                continue
            for name, value in data.items():
                if (
                    sum(
                        isinstance(entry, dict) and entry.get("name") == name
                        for entry in entries
                    )
                    != 1
                ):
                    continue
                ref = api_ref(value)
                if ref and canonical_ui(ui_ref(node, name), require_active=True) == ref:
                    verified_inputs.add((identifier, name))

    observations: dict[tuple[str, int], dict[str, list[dict]]] = {}
    unresolved = {}

    def collect(ref: tuple[str, int] | None, value: Any, origin: dict) -> bool:
        if ref not in text_usages:
            return False
        # Unknown text transforms can consume non-text dependencies too. An
        # observer of such a tensor is not a cache of the resulting string.
        kind = graph[ref[0]].get("class_type", "")
        if kind.startswith("Conditioning") or kind in {
            "CLIPTextEncode",
            "CLIPTextEncodeSDXL",
            "CLIPTextEncodeSDXLRefiner",
            "KSampler",
            "KSamplerAdvanced",
            "FaceDetailer",
            "FaceDetailerPipe",
            "DetailerForEach",
            "CheckpointLoaderSimple",
            "CLIPLoader",
            "DualCLIPLoader",
            "UNETLoader",
            "VAELoader",
            "VAEDecode",
            "VAEEncode",
            "LoraLoader",
            "LoraLoaderModelOnly",
            "EmptyLatentImage",
            "EmptySD3LatentImage",
        }:
            return False
        ui = ui_nodes.get(ref[0], {})
        outputs = ui.get("outputs")
        if (
            ui.get("type") == kind
            and isinstance(outputs, list)
            and ref[1] < len(outputs)
        ):
            output = outputs[ref[1]]
            output_type = output.get("type") if isinstance(output, dict) else None
            if isinstance(output_type, str) and output_type not in {"", "*", "STRING"}:
                return False
        text = _display_text(value)
        if text is None:
            return False
        if ref not in unresolved:
            unresolved[ref] = not isinstance(resolve(list(ref)), str)
        if not unresolved[ref]:
            return False
        origins = observations.setdefault(ref, {}).setdefault(text, [])
        if origin not in origins:
            origins.append(origin)
        return True

    # Easy-Use writes the executed value to workflow.widgets_values. Its API
    # inputs.text is the display widget serialized before execution, often from
    # the previous run. Prefer a valid, identically wired workflow observation
    # for this observer; preserve the API value only as a labelled fallback.
    preferred_observers = set()
    for identifier, node in ui_nodes.items():
        kind = node.get("type")
        if kind not in adapters or (
            identifier in graph and graph[identifier].get("class_type") != kind
        ):
            continue
        input_name, _ = adapters[kind]
        ref = canonical_ui(ui_ref(node, input_name))
        if identifier in graph:
            data = graph[identifier].get("inputs", {})
            if not isinstance(data, dict) or api_ref(data.get(input_name)) != ref:
                continue
        if collect(
            ref,
            node.get("widgets_values"),
            {
                "node_id": identifier,
                "node_type": kind,
                "source": "workflow",
                "field": "widgets_values",
            },
        ):
            preferred_observers.add((identifier, ref))

    for identifier, node in graph.items():
        kind = node.get("class_type")
        if kind not in adapters:
            continue
        input_name, field = adapters[kind]
        data = node.get("inputs", {})
        if not isinstance(data, dict):
            continue
        ref = api_ref(data.get(input_name))
        if (identifier, ref) in preferred_observers:
            continue
        collect(
            ref,
            data.get(field),
            {
                "node_id": identifier,
                "node_type": kind,
                "source": "prompt",
                "field": f"inputs.{field}",
            },
        )

    candidates = []
    for ref, texts in observations.items():
        usage = text_usages[ref]
        source_ref = f"{ref[0]}:{ref[1]}"
        roles = usage["roles"]
        for text, origins in texts.items():
            candidates.append(
                {
                    "id": f"display:{source_ref}:{hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]}",
                    "node_id": origins[0]["node_id"],
                    "node_type": origins[0]["node_type"],
                    "field": "display_text",
                    "text": text,
                    "output_node_ids": [selected_root],
                    "stage_ids": list(usage["stage_ids"]),
                    "role": "mixed" if len(roles) > 1 else next(iter(roles), "unknown"),
                    "status": "display_snapshot",
                    "snapshot_kind": "workflow"
                    if any(item["source"] == "workflow" for item in origins)
                    else "api_fallback",
                    "freshness": "unverified",
                    "source_ref": source_ref,
                    "output_ports": [ref[1]],
                    "observations": origins,
                    "consumers": usage["consumers"],
                    "conflicting": len(texts) > 1,
                }
            )
    return candidates


def _deduplicate_comfy_candidates(
    candidates: list[dict],
    graph: dict,
    text_fields: dict,
    ui_nodes: dict,
    text_usages: dict,
) -> list[dict]:
    """Hide covered text only along proven text ports, keeping raw data intact."""
    # An encoder's output is conditioning, not its literal prompt. Unknown
    # multi-output nodes likewise cannot tell us which inputs an output carries.
    text_nodes = {
        kind: fields
        for kind, fields in text_fields.items()
        if not kind.startswith("CLIPTextEncode")
    }

    def text_inputs(identifier: str) -> set[str]:
        kind = graph[identifier].get("class_type", "")
        if kind in text_nodes:
            return text_nodes[kind]
        if kind.startswith("CLIPTextEncode"):
            return set()
        ui = ui_nodes.get(identifier, {})
        outputs = ui.get("outputs")
        if (
            ui.get("type") != kind
            or not isinstance(outputs, list)
            or len(outputs) != 1
            or not isinstance(outputs[0], dict)
            or outputs[0].get("type") != "STRING"
        ):
            return set()
        declared = ui.get("inputs", [])
        if not isinstance(declared, list):
            return set()
        names = {"text", "prompt", "positive_prompt", "negative_prompt"}
        nontext = set()
        for entry in declared:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                continue
            if entry.get("type") == "STRING":
                names.add(entry["name"])
            elif entry.get("type") not in (None, "", "*"):
                nontext.add(entry["name"])
        return names - nontext

    dependencies: dict[tuple[str, int], set[tuple[str, int]]] = {}
    for ref, usage in text_usages.items():
        for consumer in usage["consumers"]:
            identifier = consumer["node_id"]
            if consumer["input_name"] in text_inputs(identifier):
                dependencies.setdefault((identifier, 0), set()).add(ref)

    def candidate_refs(candidate: dict) -> set[tuple[str, int]]:
        if candidate["status"] == "display_snapshot":
            identifier, port = candidate["source_ref"].rsplit(":", 1)
            return {(identifier, int(port))}
        if candidate["field"] not in text_nodes.get(
            candidate["node_type"], set()
        ) or candidate["output_ports"] != [0]:
            return set()
        return {(candidate["node_id"], 0)}

    refs = {candidate["id"]: candidate_refs(candidate) for candidate in candidates}
    ancestry = {}

    def ancestors(identifier: str) -> set[tuple[str, int]]:
        if identifier not in ancestry:
            visited: set[tuple[str, int]] = set()
            pending = list(refs[identifier])
            while pending:
                ref = pending.pop()
                for parent in dependencies.get(ref, ()):
                    if parent not in visited:
                        visited.add(parent)
                        pending.append(parent)
            ancestry[identifier] = visited
        return ancestry[identifier]

    def normalized_text(value: str) -> str:
        return value.replace("\r\n", "\n").replace("\r", "\n").strip()

    texts = {
        candidate["id"]: normalized_text(candidate["text"]) for candidate in candidates
    }
    patterns = {}

    def word_char(char: str) -> bool:
        return char.isascii() and (char.isalnum() or char == "_")

    def contains(upper: str, lower: str) -> bool:
        if len(texts[upper]) > len(texts[lower]) or not texts[upper]:
            return False
        if upper not in patterns:
            text = texts[upper]
            patterns[upper] = re.compile(
                (r"(?<![A-Za-z0-9_])" if word_char(text[0]) else "")
                + re.escape(text)
                + (r"(?![A-Za-z0-9_])" if word_char(text[-1]) else "")
            )
        return patterns[upper].search(texts[lower]) is not None

    covered_by = {}
    for upper in candidates:
        upper_id = upper["id"]
        if not refs[upper_id] or not upper["stage_ids"] or upper.get("conflicting"):
            continue
        for lower in reversed(candidates):
            lower_id = lower["id"]
            if (
                upper_id == lower_id
                or not refs[lower_id]
                or upper["role"] == "unknown"
                or upper["role"] != lower["role"]
                or upper["output_node_ids"] != lower["output_node_ids"]
                or not set(upper["stage_ids"]).issubset(lower["stage_ids"])
                or lower.get("conflicting")
                or lower.get("snapshot_kind") == "api_fallback"
            ):
                continue
            same_producer = (
                upper["status"] != "display_snapshot"
                and lower["status"] == "display_snapshot"
                and refs[upper_id] == refs[lower_id]
            )
            if not same_producer and not refs[upper_id].issubset(ancestors(lower_id)):
                continue
            if contains(upper_id, lower_id):
                covered_by[upper_id] = lower_id
                break

    by_id = {candidate["id"]: candidate for candidate in candidates}
    for identifier, target in covered_by.items():
        while target in covered_by:
            target = covered_by[target]
        upper = by_id[identifier]
        by_id[target].setdefault("covered_candidates", []).append(
            {key: upper[key] for key in ("id", "node_id", "node_type", "field")}
        )
    return [candidate for candidate in candidates if candidate["id"] not in covered_by]
