from __future__ import annotations

import io
import json
import math
import re
import warnings
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from PIL import Image

PARSER_VERSION = 4
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


def parse_image_metadata(data: bytes) -> dict:
    """Read embedded text without executing workflows or fetching resources."""
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("图片为空或超过 64 MiB 限制")
    fields: dict[str, Any] = {}
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                width, height = image.size
                if width * height > MAX_PIXELS:
                    raise ValueError("图片超过 6400 万像素限制")
                if image.format == "PNG":
                    # Pillow reads trailing PNG text chunks lazily.
                    fields.update(image.text)
                for key, value in image.info.items():
                    if isinstance(value, str):
                        fields.setdefault(key, value)
                    elif key in {"xmp", "XML:com.adobe.xmp"} and isinstance(
                        value, bytes
                    ):
                        fields[key] = value.decode("utf-8", errors="replace")
                exif = image.getexif()
                tags = dict(exif)
                if 34665 in exif:
                    tags.update(exif.get_ifd(34665))
                for tag, key in (
                    (270, "ImageDescription"),
                    (271, "Make"),
                    (272, "Model"),
                    (305, "Software"),
                    (306, "DateTime"),
                    (315, "Artist"),
                    (33432, "Copyright"),
                    (36867, "DateTimeOriginal"),
                    (36868, "DateTimeDigitized"),
                    (36880, "OffsetTime"),
                    (36881, "OffsetTimeOriginal"),
                    (36882, "OffsetTimeDigitized"),
                    (37520, "SubSecTime"),
                    (37521, "SubSecTimeOriginal"),
                    (37522, "SubSecTimeDigitized"),
                    (37510, "UserComment"),
                ):
                    value = tags.get(tag)
                    if isinstance(value, (str, bytes)):
                        fields.setdefault(key, _decode_comment(value))
    except (
        OSError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise ValueError("无法读取图片或图片尺寸超出限制") from exc
    return parse_metadata_fields(fields, width=width, height=height)


def parse_metadata_fields(fields: dict, *, width: int = 0, height: int = 0) -> dict:
    if not isinstance(fields, dict):
        raise ValueError("元数据必须是对象")
    raw = {
        str(key): _decode_comment(value) if isinstance(value, bytes) else _safe(value)
        for key, value in fields.items()
    }
    if len(json.dumps(raw, ensure_ascii=False).encode("utf-8")) > MAX_METADATA_BYTES:
        raise ValueError("图片元数据超过 4 MiB 限制")
    fields = raw = _unpack_container_fields(raw)
    result = {
        "format": "unknown",
        "parser_version": PARSER_VERSION,
        "raw": raw,
        "normalized": {"mode": "unknown"},
        "warnings": [],
    }
    if width > 0 and height > 0:
        result["normalized"]["file_dimensions"] = {"width": width, "height": height}
    _creation_timestamp(fields, result)
    comment = _json(fields.get("Comment"))
    software = str(fields.get("Software", "")).lower()
    if "novelai" in software or (
        comment is not None
        and any(k in comment for k in ("v4_prompt", "uc", "request_type"))
        and any(k in comment for k in ("steps", "scale", "sampler"))
    ):
        result["format"] = "novelai"
        if comment is None:
            _warning(result, "已识别 NovelAI 来源，但 Comment 不是可解析的 JSON")
            if isinstance(fields.get("Description"), str):
                result["normalized"]["prompt"] = fields["Description"]
        else:
            _novelai(comment, result)
        if "model" not in result["normalized"] and fields.get("Source"):
            result["normalized"]["model"] = str(fields["Source"])
    elif "workflow" in fields or _is_api_graph(_json(fields.get("prompt"))):
        result["format"] = "comfyui"
        _comfyui(fields, result)
    else:
        for key in ("parameters", "UserComment", "ImageDescription", "Description"):
            value = fields.get(key)
            if isinstance(value, str) and _parse_infotext(value, result):
                result["format"] = "a1111"
                break
    return _safe(result)


def _novelai(parameters: dict, result: dict) -> None:
    target = result["normalized"]
    for source, destination in {
        "prompt": "prompt",
        "uc": "negative_prompt",
        "negative_prompt": "negative_prompt",
        "model_name": "model",
        "model": "model",
        "width": "width",
        "height": "height",
        "seed": "seed",
        "steps": "steps",
        "scale": "guidance_scale",
        "cfg_rescale": "cfg_rescale",
        "sampler": "sampler",
        "noise_schedule": "scheduler",
        "n_samples": "count",
        "strength": "strength",
        "noise": "noise",
    }.items():
        if source in parameters and parameters[source] is not None:
            target[destination] = parameters[source]
    for source, destination in (
        ("v4_prompt", "prompt"),
        ("v4_negative_prompt", "negative_prompt"),
    ):
        value = parameters.get(source)
        if isinstance(value, dict):
            caption = value.get("caption", {})
            if isinstance(caption, dict):
                if destination not in target and isinstance(
                    caption.get("base_caption"), str
                ):
                    target[destination] = caption["base_caption"]
                if caption.get("char_captions"):
                    target.setdefault("characters", {})[destination] = caption[
                        "char_captions"
                    ]
                    _warning(
                        result,
                        "包含多角色描述，简要提示词不能表达全部角色信息，请保留原始 NovelAI 参数",
                    )
    action = parameters.get("action")
    request_type = parameters.get("request_type")
    if action == "img2img" or request_type == "ImageGenerateRequest":
        target["mode"] = "img2img"
    elif action == "generate" or request_type == "PromptGenerateRequest":
        target["mode"] = "text2img"


def _split_settings(text: str) -> list[str]:
    parts: list[str] = []
    start, depth, quote, escaped = 0, 0, "", False
    for index, char in enumerate(text):
        if escaped:
            escaped = False
        elif char == "\\" and quote:
            escaped = True
        elif quote:
            if char == quote:
                quote = ""
        elif char == '"':
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            parts.append(text[start:index].strip())
            start = index + 1
    parts.append(text[start:].strip())
    return parts


def _scalar(value: str) -> Any:
    value = value.strip()
    if re.fullmatch(r"[-+]?\d+", value):
        return int(value)
    if re.fullmatch(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?", value):
        number = float(value)
        return number if math.isfinite(number) else value
    if value.startswith('"') and value.endswith('"'):
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


def _parse_infotext(text: str, result: dict) -> bool:
    matches = list(re.finditer(r"(?:^|\n)Steps:\s*\d+\s*(?:,|$)", text))
    if not matches:
        return False
    start = matches[-1].start()
    body = text[:start].strip()
    settings_text = text[start:].strip()
    parameters: dict[str, Any] = {}
    previous_key = ""
    for token in _split_settings(settings_text):
        match = re.match(r"^([\w][\w /().-]*):\s*(.*)$", token, flags=re.DOTALL)
        if match:
            previous_key = match[1]
            parameters[previous_key] = _scalar(match[2])
        elif previous_key:
            parameters[previous_key] = f"{parameters[previous_key]}, {token}"
    if not any(key in parameters for key in ("Sampler", "Seed", "Size", "CFG scale")):
        return False
    parts = re.split(r"(?:^|\n)Negative prompt:\s*", body, maxsplit=1)
    normalized = result["normalized"]
    normalized["prompt"] = parts[0].strip()
    if len(parts) == 2:
        normalized["negative_prompt"] = parts[1].strip()
    _a1111_parameters(parameters, normalized)
    normalized["parameters"] = parameters
    _warning(
        result,
        "Stable Diffusion 参数未必包含生成模式；降噪强度不能单独区分图生图和高清修复",
    )
    return True


def _a1111_parameters(parameters: dict, normalized: dict) -> None:
    for source, destination in {
        "Steps": "steps",
        "Sampler": "sampler",
        "Schedule type": "scheduler",
        "CFG scale": "guidance_scale",
        "Seed": "seed",
        "Model": "model",
        "Model hash": "model_hash",
        "Denoising strength": "denoising_strength",
        "prompt": "prompt",
        "negative_prompt": "negative_prompt",
    }.items():
        if source in parameters:
            normalized[destination] = parameters[source]
    size = re.fullmatch(r"(\d+)\s*[xX×]\s*(\d+)", str(parameters.get("Size", "")))
    if size:
        normalized.update(width=int(size[1]), height=int(size[2]))
    loras = []
    for match in re.finditer(
        r"<lora:([^<>]+):([-+]?\d*\.?\d+)>", str(normalized.get("prompt", ""))
    ):
        loras.append({"name": match[1], "strength": float(match[2])})
    if loras:
        normalized["loras"] = loras


def _is_api_graph(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and bool(value)
        and all(
            isinstance(node, dict) and isinstance(node.get("class_type"), str)
            for node in value.values()
        )
    )


def _workflow_graph(workflow: dict, result: dict) -> dict:
    nodes = workflow.get("nodes")
    if not isinstance(nodes, list) or len(nodes) > MAX_NODES:
        raise ValueError("ComfyUI 工作流节点无效或超过 2048 个")
    workflow_links = workflow.get("links", [])
    if not isinstance(workflow_links, list):
        raise ValueError("ComfyUI 工作流 links 必须是数组")
    links = {}
    for link in workflow_links:
        if isinstance(link, list) and len(link) >= 6:
            links[str(link[0])] = [str(link[1]), link[2]]
    widget_names = {
        "KSampler": (
            "seed",
            "control_after_generate",
            "steps",
            "cfg",
            "sampler_name",
            "scheduler",
            "denoise",
        ),
        "KSamplerAdvanced": (
            "add_noise",
            "noise_seed",
            "control_after_generate",
            "steps",
            "cfg",
            "sampler_name",
            "scheduler",
            "start_at_step",
            "end_at_step",
            "return_with_leftover_noise",
        ),
        "CLIPTextEncode": ("text",),
        "CLIPTextEncodeSDXL": (
            "width",
            "height",
            "crop_w",
            "crop_h",
            "target_width",
            "target_height",
            "text_g",
            "text_l",
        ),
        "CLIPTextEncodeSDXLRefiner": ("ascore", "width", "height", "text"),
        "ConditioningAverage": ("conditioning_to_strength",),
        "ConditioningSetArea": ("width", "height", "x", "y", "strength"),
        "ConditioningSetAreaPercentage": ("width", "height", "x", "y", "strength"),
        "ConditioningSetMask": ("strength", "set_cond_area"),
        "ConditioningSetTimestepRange": ("start", "end"),
        "ConditioningSetAreaStrength": ("strength",),
        "CheckpointLoaderSimple": ("ckpt_name",),
        "UNETLoader": ("unet_name", "weight_dtype"),
        "LoraLoader": ("lora_name", "strength_model", "strength_clip"),
        "LoraLoaderModelOnly": ("lora_name", "strength_model"),
        "EmptyLatentImage": ("width", "height", "batch_size"),
        "EmptySD3LatentImage": ("width", "height", "batch_size"),
        "LatentUpscaleBy": ("upscale_method", "scale_by"),
        "LatentUpscale": ("upscale_method", "width", "height", "crop"),
        "ImageScaleBy": ("upscale_method", "scale_by"),
        "ImageScale": ("upscale_method", "width", "height", "crop"),
        "SaveImage": ("filename_prefix",),
        "Seed (rgthree)": ("seed", "control_after_generate"),
        "PrimitiveString": ("value",),
        "PrimitiveInt": ("value",),
        "VAELoader": ("vae_name",),
    }
    graph = {}
    for node in nodes:
        if not isinstance(node, dict) or "id" not in node:
            continue
        kind = node.get("type", "")
        if not isinstance(kind, str):
            raise ValueError("ComfyUI 节点 type 必须是字符串")
        input_entries = node.get("inputs", [])
        if not isinstance(input_entries, list):
            raise ValueError("ComfyUI 节点 inputs 必须是数组")
        inputs = {}
        widgets = node.get("widgets_values", [])
        if isinstance(widgets, list):
            inputs.update(zip(widget_names.get(kind, ()), widgets))
        for entry in input_entries:
            if isinstance(entry, dict) and str(entry.get("link")) in links:
                name = entry.get("name", "")
                if not isinstance(name, str):
                    raise ValueError("ComfyUI 节点输入 name 必须是字符串")
                inputs[name] = links[str(entry["link"])]
        graph[str(node["id"])] = {"class_type": kind, "inputs": inputs}
    _warning(
        result,
        "仅有 ComfyUI 界面工作流，按已知标准节点读取参数；自定义节点控件与执行状态可能无法还原",
    )
    return graph


def _comfyui(fields: dict, result: dict) -> None:
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
    saves = [root for root in roots if graph[root].get("class_type") in save_types]
    selected_root = (
        saves[0]
        if len(saves) == 1
        else roots[0]
        if not saves and len(roots) == 1
        else None
    )
    selected_roots = [selected_root] if selected_root else saves or roots
    order = list(
        dict.fromkeys(
            identifier for root in selected_roots for identifier in orders[root]
        )
    )
    if len(saves) > 1:
        _warning(
            result,
            "工作流包含多个保存输出，图片未指明对应输出；按输出分支保留参数，摘要不能确定本图的唯一来源",
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
            "single_save" if saves else "single_preview"
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


def parse_parameter_text(content: str) -> dict:
    """Recognize copied parameter formats; unsupported text is not a prompt."""
    if not isinstance(content, str) or not content.strip():
        raise ValueError("请粘贴完整参数")
    if len(content.encode("utf-8")) > MAX_METADATA_BYTES:
        raise ValueError("参数内容超过 4 MiB 限制")
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*\n?|\n?```$", "", stripped).strip()
    obj = _json(stripped)
    if obj is None:
        result = parse_metadata_fields({"parameters": stripped})
        if result["format"] != "unknown":
            return result
        raise ValueError(
            "无法识别参数格式，请粘贴 Image Studio、NovelAI、ComfyUI 或 Stable Diffusion 完整参数"
        )
    if obj.get("format") == "image_studio":
        if obj.get("version") != 1 or not isinstance(obj.get("data"), dict):
            raise ValueError("Image Studio 参数版本或数据结构不受支持")
        request = _safe(obj["data"])
        result = parse_metadata_fields({"image_studio": content})
        result["request"] = request
        result["envelope"] = _safe(obj)
        parameters = request.get("parameters", {})
        if not isinstance(parameters, dict):
            parameters = {}
        result["normalized"].update(
            {
                key: request[key]
                for key in (
                    "prompt",
                    "negative_prompt",
                    "model",
                    "mode",
                    "width",
                    "height",
                    "count",
                )
                if key in request
            }
        )
        result["normalized"].update(
            {
                key: parameters[key]
                for key in ("seed", "steps", "sampler")
                if key in parameters
            }
        )
        return result
    if obj.get("format") == "a1111" and isinstance(obj.get("parameters"), dict):
        if obj.get("version") != 1:
            raise ValueError("Stable Diffusion 参数版本不受支持")
        raw = obj.get("raw")
        result = parse_metadata_fields(raw if isinstance(raw, dict) else {})
        result["format"] = "a1111"
        result["normalized"].update(_safe(obj["parameters"]))
        return result
    if any(
        key in obj for key in ("Comment", "workflow", "Software", "UserComment")
    ) or _is_api_graph(obj.get("prompt")):
        result = parse_metadata_fields(obj)
        if result["format"] != "unknown":
            return result
    if _is_api_graph(obj):
        return parse_metadata_fields({"prompt": stripped})
    if isinstance(obj.get("nodes"), list) and "links" in obj:
        return parse_metadata_fields({"workflow": stripped})
    if "tag" in obj or ("artist" in obj and "prompt" in obj):
        result = parse_metadata_fields({"nai_request": content})
        result["format"] = "novelai"
        result["request"] = _safe(obj)
        mapped = {**obj, "prompt": obj.get("tag", obj.get("prompt", ""))}
        if "negative" in obj:
            mapped["uc"] = obj["negative"]
        if "cfg" in obj:
            mapped["cfg_rescale"] = obj["cfg"]
        _novelai(mapped, result)
        return _safe(result)
    if any(key in obj for key in ("uc", "v4_prompt", "cfg_rescale", "request_type")):
        return parse_metadata_fields({"Software": "NovelAI", "Comment": stripped})
    if "Steps" in obj or "CFG scale" in obj:
        result = parse_metadata_fields({"parameters_json": content})
        result["format"] = "a1111"
        _a1111_parameters(obj, result["normalized"])
        result["normalized"]["parameters"] = _safe(obj)
        return _safe(result)
    if obj.get("format") in {"novelai", "comfyui", "a1111", "unknown"} and isinstance(
        obj.get("normalized"), dict
    ):
        raw = obj.get("raw", {})
        result = parse_metadata_fields(raw if isinstance(raw, dict) else {})
        result["format"] = obj["format"]
        result["normalized"].update(_safe(obj["normalized"]))
        return result
    raise ValueError("无法识别 JSON 参数格式")
