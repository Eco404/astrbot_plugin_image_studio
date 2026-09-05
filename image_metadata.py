from __future__ import annotations

import io
import json
import math
import re
import warnings
from typing import Any

from PIL import Image

PARSER_VERSION = 1
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
                    (305, "Software"),
                    (306, "DateTime"),
                    (36867, "DateTimeOriginal"),
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
    result = {
        "format": "unknown",
        "parser_version": PARSER_VERSION,
        "raw": raw,
        "normalized": {"mode": "unknown"},
        "warnings": [],
    }
    if width > 0 and height > 0:
        result["normalized"]["file_dimensions"] = {"width": width, "height": height}
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
    links = {}
    for link in workflow.get("links", []):
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
        "CLIPTextEncode": ("text",),
        "CheckpointLoaderSimple": ("ckpt_name",),
        "UNETLoader": ("unet_name", "weight_dtype"),
        "LoraLoader": ("lora_name", "strength_model", "strength_clip"),
        "LoraLoaderModelOnly": ("lora_name", "strength_model"),
        "EmptyLatentImage": ("width", "height", "batch_size"),
        "EmptySD3LatentImage": ("width", "height", "batch_size"),
        "LatentUpscaleBy": ("upscale_method", "scale_by"),
        "VAELoader": ("vae_name",),
    }
    graph = {}
    for node in nodes:
        if not isinstance(node, dict) or "id" not in node:
            continue
        kind = node.get("type", "")
        inputs = {}
        widgets = node.get("widgets_values", [])
        if isinstance(widgets, list):
            inputs.update(zip(widget_names.get(kind, ()), widgets))
        for entry in node.get("inputs", []):
            if isinstance(entry, dict) and str(entry.get("link")) in links:
                inputs[entry.get("name", "")] = links[str(entry["link"])]
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

    def link(value: Any) -> str | None:
        if isinstance(value, list) and len(value) == 2 and isinstance(value[1], int):
            identifier = str(value[0])
            if identifier in graph:
                return identifier
        return None

    def inputs(identifier: str) -> dict:
        value = graph[identifier].get("inputs", {})
        return value if isinstance(value, dict) else {}

    roots = [
        key
        for key, node in graph.items()
        if node.get("class_type")
        in {
            "SaveImage",
            "PreviewImage",
            "SaveAnimatedWEBP",
            "SaveAnimatedPNG",
            "SaveImageWebsocket",
        }
    ]
    if not roots:
        _warning(
            result,
            "未找到已知成图输出节点，完整工作流已保留，无法可靠确定参与生成的参数",
        )
        return
    order: list[str] = []
    visited: set[str] = set()

    def visit(identifier: str, active: set[str]) -> None:
        if identifier in active or len(active) > MAX_DEPTH:
            raise ValueError("ComfyUI 工作流存在循环或依赖层数过多")
        if identifier in visited:
            return
        for value in inputs(identifier).values():
            dependency = link(value)
            if dependency is not None:
                visit(dependency, active | {identifier})
        visited.add(identifier)
        order.append(identifier)

    for root in roots:
        visit(root, set())
    if len(roots) > 1:
        _warning(
            result,
            "工作流包含多个成图输出，图片未指明对应输出；摘要包含这些输出的依赖节点",
        )

    def resolve(value: Any, active: frozenset[str] = frozenset()) -> Any:
        identifier = link(value)
        if identifier is None:
            return value if not isinstance(value, (dict, list)) else None
        if identifier in active or len(active) > MAX_DEPTH:
            return None
        node = graph[identifier]
        kind = node.get("class_type", "")
        data = inputs(identifier)
        active = active | {identifier}
        if kind in {
            "CLIPTextEncode",
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

    def latent(value: Any, active: frozenset[str] = frozenset()) -> dict:
        identifier = link(value)
        if identifier is None or identifier in active or len(active) > MAX_DEPTH:
            return {}
        kind = graph[identifier].get("class_type", "")
        data = inputs(identifier)
        active = active | {identifier}
        if kind in {"EmptyLatentImage", "EmptySD3LatentImage", "EmptyFlux2LatentImage"}:
            return {
                key: resolve(data[key]) for key in ("width", "height") if key in data
            } | {"mode": "text2img"}
        if kind == "LoadImage":
            return {"mode": "img2img"}
        for key in ("latent_image", "samples", "pixels", "image", "images"):
            if key in data and link(data[key]):
                values = latent(data[key], active)
                if kind == "LatentUpscaleBy":
                    factor = resolve(data.get("scale_by"))
                    for dimension in ("width", "height"):
                        if isinstance(
                            values.get(dimension), (int, float)
                        ) and isinstance(factor, (int, float)):
                            values[dimension] = round(values[dimension] * factor)
                elif kind == "LatentUpscale":
                    for dimension in ("width", "height"):
                        if data.get(dimension):
                            values[dimension] = resolve(data[dimension])
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
            "positive": "prompt",
            "negative": "negative_prompt",
            "start_at_step": "start_at_step",
            "end_at_step": "end_at_step",
        }.items():
            if source in data:
                value = resolve(data[source])
                if value is not None:
                    stage[destination] = value
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
            "prompt",
            "negative_prompt",
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
    normalized["output_nodes"] = roots


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
