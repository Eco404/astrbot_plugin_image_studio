"""Pure metadata decoding, safety bounds and container recognition."""

from __future__ import annotations
import json
import math
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

PARSER_VERSION = 9
MAX_IMAGE_BYTES = 64 * 1024 * 1024
MAX_METADATA_BYTES = 4 * 1024 * 1024
MAX_PIXELS = 64_000_000
MAX_NODES = 2048
MAX_DEPTH = 100
MAX_SAFE_INTEGER = 9007199254740991


def _safe(value: Any, depth: int = 0) -> Any:
    if depth > MAX_DEPTH:
        raise ValueError("元数据嵌套层数过多")
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value) if abs(value) > MAX_SAFE_INTEGER else value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("元数据包含非有限数值")
        return value
    if isinstance(value, dict):
        return {str(k): _safe(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(v, depth + 1) for v in value]
    return str(value)


def _json(value: Any) -> dict | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    try:
        result = json.loads(value)
        return result if isinstance(result, dict) else None
    except (ValueError, RecursionError):
        return None


def _warning(result: dict, message: str) -> None:
    if message not in result["warnings"]:
        result["warnings"].append(message)


def _decode_comment(value: bytes | str) -> str:
    if isinstance(value, str):
        return value.rstrip("\x00")
    if value.startswith(b"ASCII\x00\x00\x00"):
        return value[8:].rstrip(b"\x00").decode("utf-8", errors="replace")
    if value.startswith(b"UNICODE\x00"):
        body = value[8:]
        if body.startswith((b"\xff\xfe", b"\xfe\xff")):
            encoding = "utf-16"
        else:
            even_zeros = body[::2].count(0)
            odd_zeros = body[1::2].count(0)
            encoding = "utf-16-le" if odd_zeros > even_zeros else "utf-16-be"
        return body.decode(encoding, errors="replace").rstrip("\x00")
    if value.startswith(b"JIS\x00\x00\x00\x00\x00"):
        return value[8:].rstrip(b"\x00").decode("shift_jis", errors="replace")
    return value.rstrip(b"\x00").decode("utf-8-sig", errors="replace")


def _creation_timestamp(fields: dict, result: dict) -> None:
    candidates = (
        ("DateTimeOriginal", "OffsetTimeOriginal", "SubSecTimeOriginal"),
        ("DateTimeDigitized", "OffsetTimeDigitized", "SubSecTimeDigitized"),
        ("Creation Time", "", ""),
        ("CreationTime", "", ""),
        ("CreateDate", "", ""),
        ("DateCreated", "", ""),
        ("date:create", "", ""),
        ("Generated At", "", ""),
        ("Generation Date", "", ""),
    )
    for key, offset_key, subseconds_key in candidates:
        value = fields.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        text = value.strip()
        if re.match(r"^\d{4}:\d{2}:\d{2}\s", text):
            text = text[:10].replace(":", "-") + text[10:]
        try:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                parsed = parsedate_to_datetime(text)
            offset = str(fields.get(offset_key) or "").strip()
            if parsed.tzinfo is None and offset:
                if not re.fullmatch(r"[+-]\d{2}:?\d{2}", offset):
                    raise ValueError("invalid UTC offset")
                parsed = parsed.replace(
                    tzinfo=datetime.fromisoformat("2000-01-01T00:00:00" + offset).tzinfo
                )
            subseconds = str(fields.get(subseconds_key) or "").strip()
            if subseconds and not parsed.microsecond and subseconds.isdigit():
                parsed = parsed.replace(microsecond=int(subseconds[:6].ljust(6, "0")))
            assumed_timezone = parsed.tzinfo is None
            if assumed_timezone:
                parsed = parsed.replace(tzinfo=timezone.utc)
            timestamp = parsed.timestamp()
            if not 0 <= timestamp < 253402300800:
                raise ValueError("invalid creation date")
        except (ValueError, TypeError, OverflowError):
            _warning(result, f"图片时间字段 {key} 无效，已忽略")
            continue
        result["normalized"]["generated_at"] = timestamp
        result["normalized"]["generated_at_source"] = key
        if assumed_timezone:
            result["normalized"]["generated_at_timezone_assumed"] = True
            _warning(result, f"图片时间字段 {key} 未记录时区，暂按 UTC 解释，请核对")
        return


def _object_member_texts(text: str) -> dict[str, str]:
    """Keep nested workflow JSON slices exact instead of reserializing its numbers."""
    decoder = json.JSONDecoder()
    result: dict[str, str] = {}
    index = len(text) - len(text.lstrip()) + 1
    try:
        while index < len(text):
            while index < len(text) and text[index].isspace():
                index += 1
            if index == len(text) or text[index] == "}":
                break
            key, index = decoder.raw_decode(text, index)
            while index < len(text) and text[index].isspace():
                index += 1
            if not isinstance(key, str) or text[index] != ":":
                return {}
            index += 1
            while index < len(text) and text[index].isspace():
                index += 1
            start = index
            _, index = decoder.raw_decode(text, index)
            result[key] = text[start:index]
            while index < len(text) and text[index].isspace():
                index += 1
            if index == len(text) or text[index] != ",":
                break
            index += 1
    except (ValueError, IndexError, RecursionError):
        return {}
    return result


def _is_api_graph(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and bool(value)
        and all(
            isinstance(node, dict) and isinstance(node.get("class_type"), str)
            for node in value.values()
        )
    )


def _unpack_container_fields(raw: dict) -> dict:
    """Recognize metadata moved into EXIF text by image format converters."""
    fields = dict(raw)

    def unpack(text: str, depth: int = 0) -> None:
        if depth > 3:
            return
        text = text.strip()
        tagged = re.match(
            r"^(workflow|prompt|parameters|comment)\s*:\s*(.+)$",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        hint = tagged[1].lower() if tagged else ""
        candidate = tagged[2] if tagged else text
        obj = _json(candidate)
        if obj is None:
            if hint == "parameters":
                fields.setdefault("parameters", candidate)
            return
        if _is_api_graph(obj):
            fields.setdefault("prompt", candidate)
            return
        if isinstance(obj.get("nodes"), list) and "links" in obj:
            fields.setdefault("workflow", candidate)
            return
        if any(key in obj for key in ("uc", "v4_prompt", "request_type")) and any(
            key in obj for key in ("steps", "scale", "sampler", "v4_prompt")
        ):
            fields.setdefault("Comment", candidate)
            fields.setdefault("Software", "NovelAI")
            return
        members = _object_member_texts(candidate)
        canonical = {
            key.lower(): key
            for key in (
                "prompt",
                "workflow",
                "parameters",
                "Comment",
                "Software",
                "Description",
                "Source",
            )
        }
        for key, value in obj.items():
            target = canonical.get(key.lower())
            if target and isinstance(value, (str, dict)):
                fields.setdefault(
                    target,
                    value
                    if isinstance(value, str)
                    else members.get(key, json.dumps(value, ensure_ascii=False)),
                )
            elif key.lower() in {"metadata", "png_text", "exif", "generation_data"}:
                if isinstance(value, str):
                    unpack(value, depth + 1)
                elif isinstance(value, dict) and key in members:
                    unpack(members[key], depth + 1)

    for key in (
        "UserComment",
        "ImageDescription",
        "Make",
        "Model",
        "Artist",
        "Copyright",
        "Description",
    ):
        if isinstance(raw.get(key), str):
            unpack(raw[key])
    return fields
