"""ComfyUI workflow and task Page API, separated from plugin registration."""

import asyncio
import copy
import re
import uuid
from collections.abc import Callable
from typing import Any

from astrbot.api.web import error_response, json_response
from astrbot.api.web import request as web_request

from ..config import RuntimeSettings
from ..gallery.store import GenerationStore
from ..models import ImageProvider, browser_safe_integers
from ..providers.executor import ProviderError
from ..providers.comfyui.client import ComfyExecutionError


class ComfyAPI:
    """Use explicit live dependencies; settings are read again for each request."""

    def __init__(
        self,
        *,
        get_settings: Callable[[], RuntimeSettings],
        store: GenerationStore,
        get_service: Callable[[], Any],
        get_runtime: Callable[[], Any],
        serialize_result: Callable[..., dict[str, Any]],
    ):
        self.get_settings = get_settings
        self.store = store
        self.get_service = get_service
        self.get_runtime = get_runtime
        self.serialize_result = serialize_result

    def _comfy_provider(self, body):
        if not isinstance(body, dict):
            raise ValueError("请求体必须是对象")
        provider = (
            ImageProvider.from_mapping(body["provider"])
            if isinstance(body.get("provider"), dict)
            else self.get_settings().provider(
                str(
                    body.get("provider_id")
                    or str(body.get("model_ref", "")).split(":")[0]
                )
            )
        )
        if provider is None or provider.kind != "comfyui":
            raise ValueError("请选择 ComfyUI 服务商")
        return provider

    def _saved_comfy_provider(self, body):
        provider_id = str(
            body.get("provider_id") or str(body.get("model_ref") or "").split(":")[0]
        )
        provider = self.get_settings().provider(provider_id)
        if provider is None or provider.kind != "comfyui" or not provider.base_url:
            raise ValueError("请选择已保存并启用的 ComfyUI 服务商")
        return provider

    def _temporary_comfy_model(self, provider, value):
        if not isinstance(value, dict):
            raise ValueError("临时工作流配置必须是对象")
        raw = copy.deepcopy(value)
        model_id = str(raw.get("id") or "")
        if not re.fullmatch(r"temporary_[0-9a-f]{32}", model_id) or any(
            item.id == model_id for item in provider.models
        ):
            model_id = "temporary_" + uuid.uuid4().hex
        raw.update(id=model_id, name=str(raw.get("name") or "临时工作流"))
        if not raw.get("comfyui"):
            raise ValueError("临时工作流缺少可执行的 API 图")
        temporary_provider = ImageProvider.from_mapping(
            {**provider.public_dict(), "models": [raw], "discovered_models": []}
        )
        model = temporary_provider.models[0]
        if not model.comfyui.get("outputs"):
            raise ValueError("请为临时工作流选择至少一个结果节点")
        return model

    async def _gallery_comfy_import(self, body):
        from ..providers.comfyui.workflows import normalize_workflow
        from ..providers.comfyui.imports import import_result

        providers = [
            item
            for item in self.get_settings().providers
            if item.enabled and item.kind == "comfyui" and item.base_url
        ]
        if not providers:
            raise ValueError(
                "没有已保存并启用的 ComfyUI 服务商，请先在设置中添加或启用服务商"
            )
        generation_id = str(body.get("generation_id") or "")
        image_id = str(body.get("image_id") or "")
        if generation_id:
            context = await self.store.generation_image_context(generation_id, image_id)
            if not context or not context.get("images"):
                raise ValueError("图库记录或所选图片不存在")
            image = context["images"][0]
        else:
            info = await self.store.gallery_image_info(image_id, include_preview=False)
            if not info:
                raise ValueError("图库图片不存在")
            image, context = info["image"], info["detail_fields"]
            generation_id = image["generation_id"]
        if image.get("allowed_actions", {}).get("reference") is False:
            raise ValueError("此外部图库未允许复用图片中的工作流")
        snapshot = (image.get("supplemental") or {}).get("comfyui")
        metadata = image.get("metadata") or {}
        config, historical, errors = None, False, []
        for candidate, is_snapshot in ((snapshot, True), (metadata.get("raw"), False)):
            if not candidate:
                continue
            try:
                config = normalize_workflow(candidate)
                historical = is_snapshot
                break
            except ValueError as exc:
                errors.append(str(exc))
        if config is None:
            try:
                raw = await self.store.read_workflow_image(image["id"])
            except (ValueError, OSError) as exc:
                if any("界面" in message for message in errors):
                    raise ValueError(
                        "图片仅包含界面工作流，缺少可执行的 ComfyUI API 图；请在 ComfyUI 中导出 API 格式"
                    ) from exc
                raise ValueError(
                    "没有可用的工作流快照，且原图无法读取：" + str(exc)
                ) from exc
            try:
                config = await asyncio.to_thread(import_result, raw)
            except ValueError as exc:
                raise ValueError("图片中的工作流无法用于执行：" + str(exc)) from exc
        matched_provider = (
            next(
                (item for item in providers if item.id == context.get("provider_id")),
                None,
            )
            if context.get("provider_kind") == "comfyui"
            else None
        )
        matched = (
            next(
                (
                    item
                    for item in matched_provider.models
                    if item.id == context.get("model") and item.comfyui.get("api_graph")
                ),
                None,
            )
            if matched_provider
            else None
        )
        reference_inputs = [
            item
            for item in config.get("bindings", {}).values()
            if item.get("source") == "reference"
        ]
        references, warnings = [], []
        if historical and reference_inputs and not matched:
            references = await self.store.stage_generation_references(
                generation_id, strict=True
            )
            required_count = (
                max(int(item.get("reference_index", 0)) for item in reference_inputs)
                + 1
            )
            if len(references) != required_count:
                references = []
                warnings.append(
                    "历史参考图或蒙版未完整保留，请按工作流输入顺序重新补充。"
                )
        raw_model = (
            matched.public_dict()
            if matched
            else {
                "id": "gallery_workflow",
                "name": str(
                    (
                        snapshot.get("workflow_name")
                        if isinstance(snapshot, dict)
                        else None
                    )
                    or "图库工作流"
                ),
                "native_batch_size": 1,
                "max_concurrent_requests": 8,
            }
        )
        raw_model.update(
            comfyui=config,
            parameters=config.get("parameters_schema")
            or raw_model.get("parameters")
            or {},
        )
        return config, {
            "model": raw_model,
            "historical_snapshot": historical,
            "matched_model_ref": f"{matched_provider.id}:{matched.id}"
            if matched
            else "",
            "providers": [
                {"id": item.id, "name": item.name or item.id} for item in providers
            ],
            "references": references,
            "warnings": warnings,
        }

    async def _api_comfy_import(self):
        from ..providers.comfyui.client import normalize_workflow
        from ..providers.comfyui.imports import import_result
        from ..providers.comfyui.workflows import migrate_fixed_outputs

        try:
            parameters = tool = None
            historical = False
            extra = {}
            files = await web_request.files()
            if files:
                upload = files.get("file") or next(iter(files.values()))
                raw = await upload.read(30 * 1024 * 1024 + 1)
                if len(raw) > 30 * 1024 * 1024:
                    raise ValueError("工作流图片或 JSON 不得超过 30 MiB")
                config = await asyncio.to_thread(import_result, raw)
            else:
                body = await web_request.json(default={})
                if not isinstance(body, dict):
                    raise ValueError("请求体必须是对象")
                if "temporary_model" in body:
                    provider = self._saved_comfy_provider(body)
                    model = self._temporary_comfy_model(
                        provider, body["temporary_model"]
                    )
                    config, parameters, tool = (
                        model.comfyui,
                        model.parameters,
                        model.tool,
                    )
                    extra = {
                        "model": model.public_dict(),
                        "model_ref": f"{provider.id}:{model.id}",
                        "temporary": True,
                    }
                elif body.get("image_id") or body.get("generation_id"):
                    config, extra = await self._gallery_comfy_import(body)
                    historical = extra["historical_snapshot"]
                    parameters, tool = (
                        extra["model"].get("parameters"),
                        extra["model"].get("tool"),
                    )
                else:
                    config = body.get("content", body.get("comfyui", body))
                    parameters, tool = body.get("parameters"), body.get("tool")
                config = normalize_workflow(config)
            migrated = migrate_fixed_outputs(
                config, parameters, tool, prefer_graph_values=historical
            )
            result = import_result(
                migrated["comfyui"], parameters=migrated["parameters"]
            )
            if extra.get("model"):
                extra["model"].update(
                    comfyui=migrated["comfyui"],
                    parameters=browser_safe_integers(migrated["parameters"]),
                    tool=browser_safe_integers(migrated["tool"]),
                )
                if not extra.get("temporary"):
                    extra["model"].update(result["capabilities"])
            result.update(extra)
            result.update(
                parameters=browser_safe_integers(migrated["parameters"]),
                tool=browser_safe_integers(migrated["tool"]),
            )
            return json_response(result)
        except (ValueError, OSError) as exc:
            return error_response(str(exc), status_code=400)

    async def _api_comfy_inspect(self):
        from ..providers.comfyui.client import ComfyClient, ComfyExecutionError

        try:
            body = await web_request.json(default={})
            provider = self._comfy_provider(body)
            config = (
                body.get("comfyui")
                or provider.get_model(
                    str(body.get("model_ref", "")).split(":")[-1]
                ).comfyui
            )
            result = await ComfyClient(self.get_service().executor.session).inspect(
                provider, config
            )
            return json_response(result)
        except (ValueError, ComfyExecutionError) as exc:
            return error_response(str(exc), status_code=400)

    async def _api_comfy_submit(self):
        from ..providers.comfyui.client import normalize_workflow

        try:
            body = await web_request.json(default={})
            if not isinstance(body, dict):
                raise ValueError("请求体必须是对象")
            provider = self._saved_comfy_provider(body)
            temporary = "temporary_model" in body
            if temporary:
                model = self._temporary_comfy_model(provider, body["temporary_model"])
                config = model.comfyui
            else:
                model_id = str(
                    body.get("model_ref") or body.get("model") or provider.model
                ).split(":")[-1]
                model = next(
                    (item for item in provider.models if item.id == model_id), None
                )
                if model is None:
                    raise ValueError(
                        "ComfyUI 工作流不存在，可从图库选择临时使用或添加工作流"
                    )
                config = normalize_workflow(body.get("comfyui") or model.comfyui)
            # Historical bindings define the required input slots even if the
            # current template has changed mode or reference count since then.
            effective_model = ImageProvider.from_mapping(
                {
                    **provider.public_dict(),
                    "models": [{**model.public_dict(), "comfyui": config}],
                }
            ).models[0]
            mode = str(body.get("mode") or "text2img")
            if mode not in {"text2img", "img2img"} or not effective_model.supports(
                mode
            ):
                raise ValueError("所选工作流的输入绑定不支持当前生图模式")
            reference_ids = body.get("reference_ids") or []
            if isinstance(reference_ids, str):
                reference_ids = [reference_ids]
            if not isinstance(reference_ids, list):
                raise ValueError("参考图编号必须是数组")
            if reference_ids and not effective_model.img2img:
                raise ValueError("所选工作流没有参考图输入绑定")
            references = await self.store.load_staged_references(
                [str(item or "") for item in reference_ids],
                max_images=effective_model.max_reference_images,
                reject_excess=True,
            )
            job = await self.get_runtime().submit(
                provider=provider,
                model=model,
                temporary=temporary,
                references=references,
                comfyui=config,
                mode=mode,
                prompt=str(body.get("prompt") or ""),
                negative_prompt=str(body.get("negative_prompt") or ""),
                size=str(body.get("size") or ""),
                count=body.get("count"),
                parameters=body.get("parameters") or {},
                source="webui",
            )
            return json_response({"job": self.get_runtime().public_job(job)})
        except (ValueError, ProviderError) as exc:
            return error_response(str(exc), status_code=400)

    async def _api_comfy_jobs(self):
        runtime = self.get_runtime()
        job_id = str(web_request.query.get("id") or "")
        if not job_id:
            return json_response(
                {
                    "jobs": [
                        runtime.public_job(job)
                        for job in await runtime.store.list_jobs(
                            include_children=False, queue_only=True
                        )
                    ]
                }
            )
        job = await runtime.store.get_job(job_id)
        if not job:
            return error_response("ComfyUI 任务不存在", status_code=404)
        public = runtime.public_job(job)
        if job["status"] in {"succeeded", "partial"}:
            try:
                result = await runtime.result(job)
                detail = (
                    await self.store.generation_detail(
                        result.generation_id, include_assets=False
                    )
                    if result.generation_id
                    else None
                )
                public["result"] = self.serialize_result(result, download_detail=detail)
            except (ValueError, ProviderError, OSError) as exc:
                public["error"] = str(exc)
        return json_response({"job": public})

    async def _api_comfy_cancel(self):
        try:
            body = await web_request.json(default={})
            runtime = self.get_runtime()
            job = await runtime.cancel(str(body.get("id") or ""))
            return json_response({"job": runtime.public_job(job)})
        except (ValueError, ProviderError, ComfyExecutionError) as exc:
            return error_response(str(exc), status_code=400)

    async def _api_comfy_resume(self):
        try:
            body = await web_request.json(default={})
            runtime = self.get_runtime()
            job = await runtime.manager.resume(str(body.get("id") or ""))
            return json_response({"job": runtime.public_job(job)})
        except (ValueError, ProviderError) as exc:
            return error_response(str(exc), status_code=400)

    async def _api_comfy_dismiss(self):
        try:
            body = await web_request.json(default={})
            if not isinstance(body, dict):
                raise ValueError("请求体必须是 JSON 对象")
            runtime = self.get_runtime()
            job = await runtime.store.dismiss_job(str(body.get("id") or ""))
            return json_response({"job": runtime.public_job(job)})
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
