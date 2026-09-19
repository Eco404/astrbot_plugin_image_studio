"""Condition summaries with explicit, per-consumer display-snapshot evidence."""

from __future__ import annotations


def summarize_conditioning(
    conditions: dict,
    encoded_fields: dict,
    key: str | None,
    *,
    encoded_sources: dict,
    stage_id: str,
    role: str,
    target: str,
    verified_inputs: set[tuple[str, str]],
    verified_save_paths: set[str],
    resolve_observed_text,
) -> tuple[list[str], str, list[dict]]:
    """Only supplement encoder inputs reached through verified known operations.

    Evidence belongs to a particular stage/path, so an encoder also used behind
    an unknown or mismatched path cannot lend that path its trusted result.
    """
    if not key:
        return [], "missing", []
    cache: dict[tuple[str, bool], tuple[list[str], str, list[dict]]] = {}

    def evaluate(current: str, trusted: bool) -> tuple[list[str], str, list[dict]]:
        identity = current, trusted
        if identity in cache:
            return cache[identity]
        record = conditions[current]
        node_id = record["node_id"]
        if record.get("zeroed_embedding"):
            return [], "summary", []
        texts: list[str] = []
        sources: list[dict] = []
        if record["operation"] in {"encode", "encode_sdxl"}:
            fields = encoded_fields[current]
            refs = {
                item["name"]: item["ref"]
                for item in record["inputs"]
                if item["kind"] == "text"
            }
            complete = True
            for name, original in fields.items():
                value = original
                if (
                    not isinstance(value, str)
                    and trusted
                    and (node_id, name) in verified_inputs
                    and name in refs
                ):
                    value, observed_sources = resolve_observed_text(refs[name])
                    for source in observed_sources:
                        sources.append(
                            {
                                **source,
                                "conditioning_node_id": node_id,
                                "input_name": name,
                                "stage_id": stage_id,
                                "target": target,
                            }
                        )
                if isinstance(value, str):
                    for source in encoded_sources.get(current, {}).get(name, []):
                        sources.append(
                            {
                                **source,
                                "conditioning_node_id": node_id,
                                "input_name": name,
                                "stage_id": stage_id,
                                "target": target,
                            }
                        )
                    if value and value not in texts:
                        texts.append(value)
                else:
                    complete = False
            status = (
                "partial"
                if texts and not complete
                else "missing"
                if not complete
                else "summary"
                if record["operation"] == "encode_sdxl"
                else "snapshot"
                if any(source["kind"] == "display_snapshot" for source in sources)
                else "declared"
                if sources
                else "exact"
            )
        elif record["operation"] == "unsupported":
            status = "missing"
        else:
            children = [
                item for item in record["inputs"] if item["kind"] == "conditioning"
            ]
            # Missing required operands must not become complete just because
            # the operands that are present have resolvable text.
            expected = (
                2 if record["operation"] in {"combine", "concat", "average"} else 1
            )
            complete = len(children) == expected
            for child in children:
                child_texts, child_status, child_sources = evaluate(
                    child["ref"],
                    trusted and (node_id, child["name"]) in verified_inputs,
                )
                complete = complete and child_status not in {"missing", "partial"}
                for text in child_texts:
                    if text not in texts:
                        texts.append(text)
                for source in child_sources:
                    if source not in sources:
                        sources.append(source)
            status = "summary" if complete else "partial" if texts else "missing"
        cache[identity] = texts, status, sources
        return cache[identity]

    return evaluate(
        key, stage_id in verified_save_paths and (stage_id, role) in verified_inputs
    )
