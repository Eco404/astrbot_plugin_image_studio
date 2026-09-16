"""Native ComfyUI protocol client with explicit workflow and task boundaries."""

from __future__ import annotations

import asyncio
import inspect
import io
import json
import time
import uuid
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import aiohttp
from PIL import Image, ImageOps

from .workflows import (
    FIXED_OUTPUT_POLICY,
    clear_execution_cache_markers,
    graph_fingerprint,
    inspect_workflow,
    is_link,
    normalize_workflow,
    prepare_graph,
)
from ...models import GeneratedImage

MAX_JSON_BYTES = 32 * 1024 * 1024
MAX_IMAGE_BYTES = 64 * 1024 * 1024
MAX_BATCH_BYTES = 256 * 1024 * 1024
MAX_OUTPUT_IMAGES = 256
MAX_IMAGE_PIXELS = 64_000_000


class ComfyExecutionError(RuntimeError):
    """Actionable errors preserve task identity and any valid partial output."""

    def __init__(
        self,
        message: str,
        *,
        status: int = 0,
        history: dict | None = None,
        images: tuple[GeneratedImage, ...] = (),
        failures: tuple = (),
        prompt_id: str = "",
        node_errors: dict | None = None,
        unknown_submission: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.history = history or {}
        self.images = images
        self.failures = failures
        self.prompt_id = prompt_id
        self.node_errors = node_errors or {}
        self.unknown_submission = unknown_submission


def _headers(provider: Any, *, multipart: bool = False) -> dict[str, str]:
    headers: dict[str, str] = {}
    text = str(getattr(provider, "custom_headers", "") or "").strip()
    if text:
        try:
            values = json.loads(text)
        except ValueError:
            values = {}
            for line in text.splitlines():
                key, separator, value = line.partition(":")
                if separator and key.strip():
                    values[key.strip()] = value.strip()
        if isinstance(values, dict):
            headers = {
                str(key): str(value)
                for key, value in values.items()
                if str(key).strip()
            }
    if getattr(provider, "api_key", "") and not any(
        key.lower() == "authorization" for key in headers
    ):
        headers["Authorization"] = "Bearer " + provider.api_key
    if multipart:
        headers = {
            key: value
            for key, value in headers.items()
            if key.lower() != "content-type"
        }
    return headers


def _url(provider: Any, path: str) -> str:
    base = str(provider.base_url or "").strip().rstrip("/")
    parts = urlsplit(base)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise ComfyExecutionError(
            "请填写有效的 ComfyUI HTTP 地址；认证请使用密钥或自定义请求头"
        )
    return base + "/" + path.lstrip("/")


def _errors_text(errors: Any) -> str:
    if not isinstance(errors, dict):
        return ""
    pieces = []
    for node_id, item in list(errors.items())[:20]:
        if not isinstance(item, dict):
            continue
        messages = []
        for error in (item.get("errors") or [])[:5]:
            if not isinstance(error, dict):
                continue
            extra = error.get("extra_info") or {}
            field = extra.get("input_name", "") if isinstance(extra, dict) else ""
            description = str(
                error.get("details")
                or error.get("message")
                or error.get("type")
                or "校验失败"
            )[:500]
            messages.append((f"{field}：" if field else "") + description)
        pieces.append(
            f"节点 #{node_id} {item.get('class_type', '')}：" + "；".join(messages)
        )
    return "；".join(pieces)


async def _callback(callback: Any, value: Any) -> None:
    if callback is not None:
        result = callback(value)
        if inspect.isawaitable(result):
            await result


def _history_errors(entry: dict) -> str:
    status = entry.get("status") or {}
    messages = status.get("messages") or [] if isinstance(status, dict) else []
    errors = []
    for message in messages:
        if not isinstance(message, (tuple, list)) or not message:
            continue
        if message[0] in {"execution_error", "execution_interrupted"}:
            detail = (
                message[1] if len(message) > 1 and isinstance(message[1], dict) else {}
            )
            if message[0] == "execution_interrupted":
                errors.append("任务已中断")
            else:
                errors.append(
                    f"节点 #{detail.get('node_id', '?')} {detail.get('node_type', '')}："
                    + str(
                        detail.get("exception_message")
                        or detail.get("exception_type")
                        or "执行失败"
                    )[:1200]
                )
    if errors:
        return "；".join(errors)
    if isinstance(status, dict) and status.get("status_str") in {
        "error",
        "failed",
        "cancelled",
        "interrupted",
    }:
        return "工作流执行失败或已取消"
    return ""


def _mask_png(data: bytes) -> bytes:
    with Image.open(io.BytesIO(data)) as source:
        if source.width * source.height > MAX_IMAGE_PIXELS:
            raise ValueError("蒙版像素数量超过上限")
        mask = ImageOps.exif_transpose(source).convert("L")
        encoded = Image.new("RGBA", mask.size, "white")
        encoded.putalpha(ImageOps.invert(mask))
        buffer = io.BytesIO()
        encoded.save(buffer, format="PNG")
        return buffer.getvalue()


def _image_mime(data: bytes) -> str:
    with Image.open(io.BytesIO(data)) as decoded:
        if decoded.width * decoded.height > MAX_IMAGE_PIXELS:
            raise ValueError("输出图片像素数量超过上限")
        mime = Image.MIME.get(decoded.format, "")
        decoded.verify()
        return mime


class ComfyClient:
    """Use configured origins only; never retry an uncertain POST /prompt."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session

    async def _request(
        self,
        provider: Any,
        method: str,
        path: str,
        *,
        image: bool = False,
        **kwargs: Any,
    ) -> Any:
        limit = MAX_IMAGE_BYTES if image else MAX_JSON_BYTES
        timeout = min(max(float(getattr(provider, "timeout_seconds", 180)), 5), 600)
        multipart = "data" in kwargs
        try:
            async with self.session.request(
                method,
                _url(provider, path),
                headers=_headers(provider, multipart=multipart),
                proxy=getattr(provider, "proxy", "") or None,
                timeout=aiohttp.ClientTimeout(total=timeout),
                allow_redirects=False,
                **kwargs,
            ) as response:
                raw = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    raw.extend(chunk)
                    if len(raw) > limit:
                        raise ComfyExecutionError(
                            "ComfyUI 返回的图片或元数据超过大小上限",
                            status=response.status,
                        )
                status = response.status
                if image and 200 <= status < 300:
                    return bytes(raw)
                try:
                    payload = json.loads(raw) if raw else {}
                except (ValueError, RecursionError) as exc:
                    raise ComfyExecutionError(
                        f"ComfyUI 返回非 JSON 响应（HTTP {status}）", status=status
                    ) from exc
                if not 200 <= status < 300:
                    node_errors = (
                        payload.get("node_errors", {})
                        if isinstance(payload, dict)
                        else {}
                    )
                    error = (
                        payload.get("error", {}) if isinstance(payload, dict) else {}
                    )
                    message = (
                        error.get("message", "")
                        if isinstance(error, dict)
                        else str(error)
                    )
                    detail = _errors_text(node_errors) or str(message)[:1000]
                    raise ComfyExecutionError(
                        f"ComfyUI 请求失败（HTTP {status}）"
                        + (f"：{detail}" if detail else ""),
                        status=status,
                        node_errors=node_errors,
                    )
                if not isinstance(payload, dict):
                    raise ComfyExecutionError(
                        "ComfyUI 响应应为 JSON 对象", status=status
                    )
                return payload
        except ComfyExecutionError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            # URL, headers and exception repr may contain credentials. Do not
            # echo transport exception text in public job/error records.
            raise ComfyExecutionError(
                "ComfyUI 连接失败或请求超时，请检查服务地址、代理和网络"
            ) from exc

    async def system_stats(self, provider: Any) -> dict:
        return await self._request(provider, "GET", "/system_stats")

    async def object_info(self, provider: Any) -> dict:
        return await self._request(provider, "GET", "/object_info")

    async def inspect(self, provider: Any, config: Any) -> dict:
        """Validate all submitted nodes, with unknown custom behavior explicit."""
        config = normalize_workflow(config)
        local = inspect_workflow(config)
        graph = config["api_graph"]
        definitions = await self.object_info(provider)
        issues, models = [], []
        deferred = {
            (target["node_id"], target["input_name"])
            for binding in config["bindings"].values()
            if binding["source"] == "reference"
            for target in binding["targets"]
        }
        random_seed_targets = {
            (target["node_id"], target["input_name"])
            for binding in config["bindings"].values()
            if binding["source"] == "seed"
            for target in binding["targets"]
        }

        def issue(
            code: str,
            node_id: str,
            field: str,
            message: str,
            severity: str = "error",
            **extra: Any,
        ) -> None:
            issues.append(
                {
                    "code": code,
                    "node_id": node_id,
                    "class_type": graph[node_id]["class_type"],
                    "input_name": field,
                    "message": message,
                    "severity": severity,
                    **extra,
                }
            )

        output_ids = []
        for item in local["nodes"]:
            node_id, kind = item["id"], item["class_type"]
            node = graph[node_id]
            definition = definitions.get(kind)
            if not isinstance(definition, dict):
                issue(
                    "missing_node_type",
                    node_id,
                    "",
                    "目标 ComfyUI 未提供该节点；可能未安装、加载失败或版本不兼容",
                )
                continue
            item["display_name"] = definition.get("display_name") or item["title"]
            item["description"] = definition.get("description", "")
            item["input_definitions"] = definition.get("input", {})
            item["output_node"] = bool(definition.get("output_node"))
            if item["output_node"]:
                output_ids.append(node_id)
            spec = definition.get("input") or {}
            required = spec.get("required") or {}
            optional = spec.get("optional") or {}
            declared = {**required, **optional}
            for name in required:
                if name not in node["inputs"]:
                    issue(
                        "required_input_missing", node_id, name, f"缺少必填输入 {name}"
                    )
            for name, value in node["inputs"].items():
                field = declared.get(name)
                if field is None:
                    issue(
                        "unknown_input",
                        node_id,
                        name,
                        f"目标节点未声明输入 {name}，可能是动态输入或版本差异",
                        "warning",
                    )
                    continue
                if not isinstance(field, (tuple, list)) or not field:
                    issue(
                        "unverified_input",
                        node_id,
                        name,
                        "此自定义输入无法提前校验",
                        "warning",
                    )
                    continue
                expected = field[0]
                options = (
                    field[1] if len(field) > 1 and isinstance(field[1], dict) else {}
                )
                if is_link(value):
                    upstream, slot = str(value[0]), value[1]
                    if upstream not in graph:
                        issue(
                            "missing_link_node",
                            node_id,
                            name,
                            f"连线来源节点 #{upstream} 不存在",
                        )
                        continue
                    upstream_def = definitions.get(graph[upstream]["class_type"], {})
                    output_types = upstream_def.get("output", [])
                    if isinstance(output_types, list) and output_types:
                        if slot >= len(output_types):
                            issue(
                                "invalid_output_slot",
                                node_id,
                                name,
                                f"来源节点 #{upstream} 没有第 {slot} 个输出",
                            )
                        elif (
                            isinstance(expected, str)
                            and expected != "*"
                            and output_types[slot] != "*"
                            and expected != output_types[slot]
                        ):
                            issue(
                                "link_type_mismatch",
                                node_id,
                                name,
                                f"输入需要 {expected}，来源节点输出 {output_types[slot]}",
                                "warning",
                            )
                    continue
                if isinstance(expected, list):
                    is_model = name.endswith(("_name", "_model")) and (
                        "loader" in kind.lower()
                        or any(
                            part in name
                            for part in (
                                "ckpt",
                                "lora",
                                "vae",
                                "clip",
                                "unet",
                                "diffusion",
                            )
                        )
                    )
                    if is_model:
                        models.append(
                            {
                                "node_id": node_id,
                                "input_name": name,
                                "value": value,
                                "options": expected,
                                "available": value in expected,
                            }
                        )
                    if (node_id, name) in deferred:
                        continue
                    if value not in expected:
                        issue(
                            "missing_model" if is_model else "value_not_in_list",
                            node_id,
                            name,
                            f"目标节点不提供当前值：{str(value)[:200]}",
                            options=expected,
                        )
                elif isinstance(expected, str) and expected in {
                    "INT",
                    "FLOAT",
                    "BOOLEAN",
                    "STRING",
                }:
                    valid = (
                        (isinstance(value, int) and not isinstance(value, bool))
                        if expected == "INT"
                        else (
                            isinstance(value, (int, float))
                            and not isinstance(value, bool)
                        )
                        if expected == "FLOAT"
                        else isinstance(value, bool)
                        if expected == "BOOLEAN"
                        else isinstance(value, str)
                    )
                    if not valid:
                        issue(
                            "invalid_input_type", node_id, name, f"输入需要 {expected}"
                        )
                    elif expected in {"INT", "FLOAT"}:
                        if value == -1 and (node_id, name) in random_seed_targets:
                            # The editor checks the template. Explicit plugin
                            # seed inputs resolve -1 before the execution graph
                            # is checked again and submitted to ComfyUI.
                            continue
                        for limit, comparison in (
                            ("min", lambda a, b: a < b),
                            ("max", lambda a, b: a > b),
                        ):
                            if isinstance(
                                options.get(limit), (int, float)
                            ) and comparison(value, options[limit]):
                                issue(
                                    "input_out_of_range",
                                    node_id,
                                    name,
                                    f"输入超出范围：{limit}={options[limit]}",
                                )
                else:
                    issue(
                        "unverified_input",
                        node_id,
                        name,
                        "此自定义输入将由 ComfyUI 执行校验",
                        "warning",
                    )
            if (
                node["class_type"] == "LoadImage"
                and "image" in node["inputs"]
                and (node_id, "image") not in deferred
            ):
                value = node["inputs"]["image"]
                field = declared.get("image", [])
                if isinstance(value, str) and not (
                    field and isinstance(field[0], list)
                ):
                    issue(
                        "unverified_image",
                        node_id,
                        "image",
                        "工作流引用服务器输入图片；生成前需确认文件仍存在或绑定参考图",
                        "warning",
                    )
        for node_id in config["outputs"]:
            if (
                node_id not in output_ids
                and graph[node_id]["class_type"] in definitions
            ):
                issue("invalid_output_node", node_id, "", "选定节点未声明为输出节点")
        if not output_ids and not any(
            item["code"] == "missing_node_type" for item in issues
        ):
            issues.append(
                {
                    "code": "no_output",
                    "node_id": "",
                    "input_name": "",
                    "severity": "error",
                    "message": "工作流没有可执行的输出节点",
                }
            )
        # API graphs must be acyclic; otherwise ComfyUI may fail recursively.
        active, done = set(), set()

        def visit(node_id: str) -> bool:
            stack = [(node_id, False)]
            while stack:
                current, finished = stack.pop()
                if finished:
                    active.discard(current)
                    done.add(current)
                    continue
                if current in active:
                    return True
                if current in done or current not in graph:
                    continue
                active.add(current)
                stack.append((current, True))
                stack.extend(
                    (str(value[0]), False)
                    for value in graph[current]["inputs"].values()
                    if is_link(value, graph)
                )
            return False

        if any(visit(node_id) for node_id in graph if node_id not in done):
            issues.append(
                {
                    "code": "cyclic_graph",
                    "node_id": "",
                    "input_name": "",
                    "severity": "error",
                    "message": "API 工作流存在循环连线",
                }
            )
        for suggested in local["suggested_bindings"].values():
            target = suggested["targets"][0]
            node_definition = definitions.get(target["class_type"], {})
            input_definitions = node_definition.get("input", {})
            declared = {
                **input_definitions.get("required", {}),
                **input_definitions.get("optional", {}),
            }
            definition = declared.get(target["input_name"])
            if not isinstance(definition, (list, tuple)) or not definition:
                continue
            if isinstance(definition[0], list) and suggested["type"] != "image":
                suggested["type"] = "select"
                suggested["options"] = definition[0]
            elif definition[0] == "INT":
                suggested["integer"] = True
            if len(definition) > 1 and isinstance(definition[1], dict):
                suggested.update(
                    {
                        key: definition[1][key]
                        for key in ("min", "max", "step")
                        if key in definition[1]
                    }
                )
        return {
            **local,
            "outputs": output_ids,
            "issues": issues,
            "models": models,
            "object_info": {
                kind: definitions[kind]
                for kind in {node["class_type"] for node in graph.values()}
                if kind in definitions
            },
            "status": "blocked"
            if any(item["severity"] == "error" for item in issues)
            else "warning"
            if issues
            else "ready",
        }

    async def upload_references(
        self, provider: Any, references: Any, *, config: Any = None
    ) -> list[str]:
        mask_indices, image_indices = set(), set()
        if config is not None:
            for binding in normalize_workflow(config)["bindings"].values():
                if binding["source"] == "reference":
                    (mask_indices if binding["type"] == "mask" else image_indices).add(
                        binding["reference_index"]
                    )
            if mask_indices & image_indices:
                raise ComfyExecutionError(
                    "同一参考图不能同时绑定为图片和蒙版；请分别添加对应参考项"
                )
        paths = []
        for index, reference in enumerate(references):
            if not reference.data or len(reference.data) > MAX_IMAGE_BYTES:
                raise ComfyExecutionError(f"第 {index + 1} 张参考图为空或超过 64 MiB")
            data, mime = reference.data, reference.mime_type
            if index in mask_indices:
                # Native LoadImage exposes MASK as 1-alpha. Our mask inputs use
                # white=edit / black=keep, so encode luminance as inverse alpha.
                try:
                    data, mime = await asyncio.to_thread(_mask_png, data), "image/png"
                except Exception as exc:
                    raise ComfyExecutionError(f"第 {index + 1} 张蒙版无法读取") from exc
            suffix = {
                "image/png": ".png",
                "image/jpeg": ".jpg",
                "image/webp": ".webp",
            }.get(mime, ".png")
            form = aiohttp.FormData()
            form.add_field(
                "image",
                data,
                filename=f"image-studio-{uuid.uuid4().hex}{suffix}",
                content_type=mime,
            )
            form.add_field("type", "input")
            form.add_field("overwrite", "false")
            response = await self._request(provider, "POST", "/upload/image", data=form)
            name, folder = (
                str(response.get("name", "")),
                str(response.get("subfolder", "")),
            )
            if (
                not name
                or name.startswith(("/", "\\"))
                or ".." in name.replace("\\", "/").split("/")
                or ".." in folder.replace("\\", "/").split("/")
            ):
                raise ComfyExecutionError(
                    f"第 {index + 1} 张参考图上传返回的文件名无效"
                )
            paths.append("/".join(part for part in (folder.strip("/"), name) if part))
        return paths

    async def submit(
        self,
        provider: Any,
        graph: dict,
        workflow: dict | None = None,
        *,
        client_id: str = "",
        prompt_id: str = "",
    ) -> dict:
        graph = normalize_workflow(graph)["api_graph"]
        # Also guard direct submissions that bypass prepare_graph. Normalizing
        # above made a copy, leaving the imported workflow and its history intact.
        clear_execution_cache_markers(graph)
        payload: dict[str, Any] = {
            "prompt": graph,
            "client_id": client_id or "image-studio-" + uuid.uuid4().hex,
        }
        if prompt_id:
            try:
                uuid.UUID(prompt_id)
            except ValueError as exc:
                raise ValueError("ComfyUI 任务编号必须是 UUID") from exc
            payload["prompt_id"] = prompt_id
        if workflow:
            payload["extra_data"] = {"extra_pnginfo": {"workflow": workflow}}
        try:
            response = await self._request(provider, "POST", "/prompt", json=payload)
        except ComfyExecutionError as exc:
            if not exc.status or 200 <= exc.status < 300:
                exc.unknown_submission = True
                exc.prompt_id = prompt_id
                exc.args = (
                    str(exc) + "；提交结果未知，请先查询该任务，勿自动重复生成",
                )
            raise
        remote_id = str(response.get("prompt_id") or "")
        if not remote_id:
            raise ComfyExecutionError(
                "ComfyUI 未返回任务编号；提交结果未知，请勿自动重试",
                prompt_id=prompt_id,
                unknown_submission=True,
            )
        return {
            "prompt_id": remote_id,
            "node_errors": response.get("node_errors") or {},
            "number": response.get("number"),
        }

    async def get_history(self, provider: Any, prompt_id: str) -> dict:
        response = await self._request(
            provider, "GET", "/history/" + quote(prompt_id, safe="")
        )
        entry = response.get(prompt_id)
        return entry if isinstance(entry, dict) else {}

    async def wait(
        self,
        provider: Any,
        prompt_id: str,
        *,
        timeout_seconds: float | None = None,
        on_progress: Any = None,
        poll_interval: float = 1.0,
        client_id: str = "",
    ) -> dict:
        deadline = time.monotonic() + max(
            float(timeout_seconds or getattr(provider, "timeout_seconds", 180)), 1
        )
        ws, receiver = None, None

        async def listen() -> None:
            async for message in ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    event = json.loads(message.data)
                except (ValueError, TypeError):
                    continue
                if not isinstance(event, dict) or not isinstance(
                    event.get("data"), dict
                ):
                    continue
                data = event["data"]
                # Old servers omit IDs from progress events; do not attribute
                # those events to this job when sharing a server.
                if str(data.get("prompt_id") or "") != prompt_id:
                    continue
                if event.get("type") in {
                    "progress",
                    "executing",
                    "execution_start",
                    "execution_cached",
                }:
                    await _callback(
                        on_progress,
                        {
                            "status": "running",
                            "event": event["type"],
                            "prompt_id": prompt_id,
                            **data,
                        },
                    )

        try:
            if client_id and hasattr(self.session, "ws_connect"):
                parts = urlsplit(_url(provider, "/ws"))
                ws_url = urlunsplit(
                    (
                        "wss" if parts.scheme == "https" else "ws",
                        parts.netloc,
                        parts.path,
                        "",
                        "",
                    )
                )
                try:
                    ws = await asyncio.wait_for(
                        self.session.ws_connect(
                            ws_url,
                            params={"clientId": client_id},
                            headers=_headers(provider),
                            proxy=getattr(provider, "proxy", "") or None,
                            heartbeat=20,
                            max_msg_size=2 * 1024 * 1024,
                        ),
                        timeout=min(5, max(0.1, deadline - time.monotonic())),
                    )
                    receiver = asyncio.create_task(listen())
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                    pass
            last_state = ""
            while time.monotonic() < deadline:
                try:
                    entry = await asyncio.wait_for(
                        self.get_history(provider, prompt_id),
                        max(0.01, deadline - time.monotonic()),
                    )
                    if entry:
                        error = _history_errors(entry)
                        if error:
                            raise ComfyExecutionError(
                                "ComfyUI " + error, history=entry, prompt_id=prompt_id
                            )
                        status = entry.get("status") or {}
                        if (
                            status.get("completed")
                            or status.get("status_str") in {"success", "completed"}
                            or not status
                            and "outputs" in entry
                        ):
                            return entry
                    queue = await asyncio.wait_for(
                        self._request(provider, "GET", "/queue"),
                        max(0.01, deadline - time.monotonic()),
                    )
                    running = any(
                        isinstance(item, list)
                        and len(item) > 1
                        and str(item[1]) == prompt_id
                        for item in queue.get("queue_running", [])
                    )
                    pending = any(
                        isinstance(item, list)
                        and len(item) > 1
                        and str(item[1]) == prompt_id
                        for item in queue.get("queue_pending", [])
                    )
                    state = "running" if running else "queued" if pending else "waiting"
                    if state != last_state:
                        await _callback(
                            on_progress, {"status": state, "prompt_id": prompt_id}
                        )
                        last_state = state
                except ComfyExecutionError as exc:
                    if exc.history or exc.status in {400, 401, 403}:
                        raise
                    # History/queue reads are safe to retry after connection
                    # loss. Never turn this into a second submission.
                    await _callback(
                        on_progress, {"status": "reconnecting", "prompt_id": prompt_id}
                    )
                except asyncio.TimeoutError:
                    break
                await asyncio.sleep(
                    min(max(poll_interval, 0.01), max(0, deadline - time.monotonic()))
                )
            raise ComfyExecutionError(
                "等待 ComfyUI 任务超时；任务可能仍在服务器排队或执行，可恢复查询，请勿自动重新提交",
                prompt_id=prompt_id,
            )
        finally:
            if receiver:
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)
            if ws:
                await ws.close()

    async def download_outputs(
        self,
        provider: Any,
        history: dict,
        outputs: Any = (),
        *,
        output_limit: int | None = None,
    ) -> tuple[GeneratedImage, ...]:
        if output_limit is not None and (
            isinstance(output_limit, bool)
            or not isinstance(output_limit, int)
            or output_limit < 1
        ):
            raise ValueError("工作流图片收集额度必须为正整数")
        available = history.get("outputs") or {}
        if not isinstance(available, dict):
            raise ComfyExecutionError("ComfyUI 返回的输出结构无效", history=history)
        selected = list(dict.fromkeys(str(item) for item in outputs)) or list(available)
        images, failures, seen = [], [], set()
        index, total, allocated = 0, 0, 0
        for node_id in selected:
            if output_limit is not None and allocated >= output_limit:
                break
            node = available.get(node_id) or {}
            entries = node.get("images") or [] if isinstance(node, dict) else []
            if not entries:
                if outputs:
                    failures.append((index + 1, f"输出节点 #{node_id} 未返回图片"))
                continue
            for item in entries:
                if output_limit is not None and allocated >= output_limit:
                    break
                index += 1
                if index > MAX_OUTPUT_IMAGES:
                    failures.append((index, "输出图片超过 256 张上限"))
                    break
                if not isinstance(item, dict) or not item.get("filename"):
                    failures.append((index, f"输出节点 #{node_id} 的图片描述无效"))
                    continue
                params = {
                    "filename": str(item["filename"]),
                    "subfolder": str(item.get("subfolder", "")),
                    "type": str(item.get("type") or "output"),
                }
                identity = tuple(params.values())
                if identity in seen:
                    continue
                seen.add(identity)
                if params["type"] not in {"output", "temp", "input"}:
                    failures.append((index, f"输出节点 #{node_id} 的图片目录类型无效"))
                    continue
                # Reserve result positions before downloading. A failed download
                # does not pull an extra image from beyond this execution's quota.
                allocated += 1
                try:
                    raw = await self._request(
                        provider, "GET", "/view", params=params, image=True
                    )
                    total += len(raw)
                    if total > MAX_BATCH_BYTES:
                        raise ComfyExecutionError("本批 ComfyUI 输出超过 256 MiB 上限")
                    try:
                        mime = await asyncio.to_thread(_image_mime, raw)
                    except Exception as exc:
                        raise ComfyExecutionError("输出文件不是有效图片") from exc
                    if mime not in {
                        "image/png",
                        "image/jpeg",
                        "image/webp",
                        "image/gif",
                    }:
                        raise ComfyExecutionError("ComfyUI 返回了不支持的图片格式")
                    images.append(
                        GeneratedImage(
                            data=raw,
                            mime_type=mime,
                            response_index=index,
                            effective_parameters={"comfyui_output_node": node_id},
                        )
                    )
                except ComfyExecutionError as exc:
                    failures.append((index, f"输出节点 #{node_id}：{exc}"))
                    if total > MAX_BATCH_BYTES:
                        break
            if index > MAX_OUTPUT_IMAGES or total > MAX_BATCH_BYTES:
                break
        if failures or not images:
            raise ComfyExecutionError(
                "ComfyUI 图片结果不完整："
                + (
                    "；".join(f"第 {i} 项 {reason}" for i, reason in failures)
                    or "指定输出没有图片"
                ),
                images=tuple(images),
                failures=tuple(failures),
                history=history,
            )
        return tuple(images)

    async def cancel(self, provider: Any, prompt_id: str) -> dict:
        if not prompt_id:
            raise ValueError("缺少 ComfyUI 任务编号")
        try:
            response = await self._request(
                provider,
                "POST",
                "/api/jobs/" + quote(prompt_id, safe="") + "/cancel",
                json={},
            )
            return {"status": "cancel_requested", "targeted": True, **response}
        except ComfyExecutionError as exc:
            if exc.status not in {404, 405}:
                raise
        # Older servers cannot safely stop one running task. Removing one
        # queued task is always scoped; never call the global /interrupt.
        await self._request(provider, "POST", "/queue", json={"delete": [prompt_id]})
        return {
            "status": "queued_removed_running_unchanged",
            "targeted": True,
            "message": "已移除该排队任务；此版本不支持定向停止正在执行的任务",
        }

    async def execute(
        self,
        provider: Any,
        request: Any,
        *,
        config: Any = None,
        on_prepared: Any = None,
        on_submitted: Any = None,
        on_progress: Any = None,
        resume_id: str = "",
        timeout_seconds: float | None = None,
        client_id: str = "",
        prompt_id: str = "",
    ) -> tuple[GeneratedImage, ...]:
        config = normalize_workflow(
            config if config is not None else provider.get_model(request.model).comfyui
        )
        client_id = client_id or "image-studio-" + uuid.uuid4().hex
        node_errors = {}
        if resume_id:
            remote_id = resume_id
        else:
            pending_references = [
                f"image-studio-pending-{index}.png"
                for index in range(len(request.references))
            ]
            preview_graph = prepare_graph(config, request, pending_references)
            # Validate the values that will actually be submitted, including
            # explicit replacement models; only pending uploads are deferred.
            preview_config = {
                **config,
                "api_graph": preview_graph,
                "api_graph_json": json.dumps(preview_graph, ensure_ascii=False),
                "bindings": {
                    key: binding
                    for key, binding in config["bindings"].items()
                    if binding["source"] != "reference"
                    or binding["reference_index"] < len(request.references)
                },
            }
            report = await self.inspect(provider, preview_config)
            if report["status"] == "blocked":
                errors = [
                    item for item in report["issues"] if item["severity"] == "error"
                ]
                raise ComfyExecutionError(
                    "工作流依赖检查失败："
                    + "；".join(
                        f"节点 #{item['node_id']} {item.get('input_name', '')}：{item['message']}"
                        for item in errors
                    )
                )
            references = await self.upload_references(
                provider, request.references, config=config
            )
            graph = prepare_graph(config, request, references)
            await _callback(
                on_prepared,
                {
                    "api_graph": graph,
                    "api_graph_json": json.dumps(graph, ensure_ascii=False),
                    "workflow": config["workflow"],
                    "workflow_json": config["workflow_json"],
                    "uploaded_references": references,
                    "outputs": config["outputs"],
                    "fingerprint": graph_fingerprint(graph),
                },
            )
            response = await self.submit(
                provider,
                graph,
                config["workflow"],
                client_id=client_id,
                prompt_id=prompt_id,
            )
            remote_id, node_errors = response["prompt_id"], response["node_errors"]
            await _callback(on_submitted, response)
        execution_error = None
        try:
            history = await self.wait(
                provider,
                remote_id,
                timeout_seconds=timeout_seconds,
                client_id=client_id,
                on_progress=on_progress,
            )
        except ComfyExecutionError as exc:
            if not exc.history:
                raise
            execution_error, history = exc, exc.history
        try:
            images = await self.download_outputs(
                provider,
                history,
                config["outputs"],
                **(
                    {
                        "output_limit": min(
                            request.count,
                            provider.get_model(request.model).native_batch_size,
                        )
                    }
                    if config.get("execution_policy") == FIXED_OUTPUT_POLICY
                    else {}
                ),
            )
        except ComfyExecutionError as exc:
            exc.prompt_id = remote_id
            if execution_error:
                exc.args = (str(execution_error) + "；" + str(exc),)
            if node_errors:
                exc.node_errors = node_errors
                exc.args = (str(exc) + "；" + _errors_text(node_errors),)
            raise
        if execution_error or node_errors:
            raise ComfyExecutionError(
                str(execution_error)
                if execution_error
                else "部分工作流输出未通过校验：" + _errors_text(node_errors),
                images=images,
                history=history,
                prompt_id=remote_id,
                node_errors=node_errors,
            )
        return images
