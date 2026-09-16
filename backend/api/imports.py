"""Own staged import tickets and batch lifecycle separately from plugin entry."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import shutil
import time
import uuid
from typing import Any

from astrbot.api import logger
from astrbot.api.web import error_response, json_response
from astrbot.api.web import request as web_request

from ..gallery.errors import (
    ImportDuplicateError,
    ImportEditConflictError,
)
from ..metadata.exchange import export_parameters, resolve_parameters
from ..metadata.parser import parse_metadata_fields

LOG_TAG = "[ImageStudio]"


class ImportsAPI:
    def __init__(self, *, store, get_settings):
        self.store = store
        self.get_settings = get_settings
        self.uploads = {}
        self.groups = {}

    def clear(self):
        self.uploads.clear()
        self.groups.clear()

    async def _api_import_inspect(self) -> Any:
        body = await web_request.json(default={})
        if not isinstance(body, dict) or not isinstance(body.get("metadata"), dict):
            return error_response("元数据必须是对象", status_code=400)
        try:
            if len(json.dumps(body, ensure_ascii=False)) > 4 * 1024 * 1024:
                raise ValueError("元数据不能超过 4 MB")
            width = max(0, min(65535, int(body.get("width") or 0)))
            height = max(0, min(65535, int(body.get("height") or 0)))
            output_node_id = body.get("output_node_id", "")
            if not isinstance(output_node_id, str):
                raise ValueError("ComfyUI 输出节点 ID 必须是字符串")
            result = await asyncio.to_thread(
                parse_metadata_fields,
                body["metadata"],
                width=width,
                height=height,
                output_node_id=output_node_id,
            )
            return json_response(result)
        except (ValueError, TypeError, OverflowError) as exc:
            return error_response(str(exc), status_code=400)

    @staticmethod
    def _import_hash_items(body: Any) -> list[dict[str, str]]:
        items = body.get("items") if isinstance(body, dict) else None
        if not isinstance(items, list) or not 1 <= len(items) <= 100:
            raise ValueError("每批请选择 1 至 100 张图片")
        result, seen = [], set()
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("导入项目格式错误")
            client_id, digest = item.get("client_id"), item.get("sha256")
            if (
                not isinstance(client_id, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", client_id)
                or client_id in seen
            ):
                raise ValueError("导入图片标识无效或重复")
            if not isinstance(digest, str) or not re.fullmatch(
                r"[a-fA-F0-9]{64}", digest
            ):
                raise ValueError(f"图片 {client_id} 缺少有效的 SHA-256，请重新选择图片")
            seen.add(client_id)
            result.append({"client_id": client_id, "sha256": digest.lower()})
        return result

    async def _check_import_items(self, items: list[dict[str, str]]) -> dict[str, Any]:
        hashes = [item["sha256"] for item in items]
        seen, repeated = set(), set()
        for digest in hashes:
            if digest in seen:
                repeated.add(digest)
            seen.add(digest)
        if repeated:
            return {
                "allowed": False,
                "code": "batch_duplicates",
                "duplicate_hashes": sorted(repeated),
                "message": "本次选择包含内容相同的图片，请移除重复图片后重试",
            }
        return await self.store.check_import_hashes(hashes)

    async def _api_import_check(self) -> Any:
        try:
            items = self._import_hash_items(await web_request.json(default={}))
            return json_response(await self._check_import_items(items))
        except ValueError as exc:
            return error_response(str(exc), status_code=400)

    @staticmethod
    def _import_merge_engine(value: Any) -> str:
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 80:
            raise ValueError("请先为待合并图片选择明确的生图来源")
        engine = value.strip()
        if engine.lower() in {"unknown", "mixed"}:
            raise ValueError("未知或混合来源不能合并，请先为图片选择相同的生图来源")
        return "novelai" if engine.lower() in {"nai", "novelai"} else engine

    async def _api_import_merge_targets(self) -> Any:
        try:
            engine = self._import_merge_engine(
                web_request.query.get("generation_engine")
            )
            limit = int(web_request.query.get("limit", 24))
            offset = int(web_request.query.get("offset", 0))
            if not 1 <= limit <= 60 or offset < 0:
                raise ValueError("分页数量须为 1 至 60，偏移量不能为负数")
            result = await self.store.list_import_merge_targets(
                engine,
                limit=limit,
                offset=offset,
                query=str(web_request.query.get("query", ""))[:240],
                sort=str(web_request.query.get("sort", "created")),
            )
            return json_response(result)
        except (ValueError, TypeError, OverflowError) as exc:
            return error_response(str(exc), status_code=400)

    async def _api_import_prepare(self) -> Any:
        body = await web_request.json(default={})
        items = body.get("items") if isinstance(body, dict) else None
        if not isinstance(items, list) or not 1 <= len(items) <= 100:
            return error_response("每批请选择 1 至 100 张图片", status_code=400)
        try:
            hashes = self._import_hash_items(body)
            check = await self._check_import_items(hashes)
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        if not check["allowed"]:
            return json_response(check)
        hashes_by_id = {item["client_id"]: item["sha256"] for item in hashes}
        as_group = body.get("as_group", False)
        if not isinstance(as_group, bool):
            return error_response("图组选项必须是布尔值", status_code=400)
        merge_target_id = body.get("merge_target_id", "")
        if not isinstance(merge_target_id, str) or (
            merge_target_id and not re.fullmatch(r"[a-f0-9]{32}", merge_target_id)
        ):
            return error_response("合并目标图组 ID 无效", status_code=400)
        if as_group and merge_target_id:
            return error_response("新建图组和合并已有图组不能同时选择", status_code=400)
        expected_engine = ""
        if merge_target_id:
            try:
                expected_engine = self._import_merge_engine(
                    body.get("generation_engine")
                )
                target = await self.store.generation_detail(
                    merge_target_id, include_assets=False
                )
                if target is None:
                    raise ValueError("目标图组已被删除或不存在，请重新选择")
                if target.get("source") != "import":
                    raise ValueError("只能合并到手动导入的图片或图组")
                if (
                    self._import_merge_engine(target.get("generation_engine"))
                    != expected_engine
                ):
                    raise ValueError("目标图组与待导入图片的生图来源不一致，请重新选择")
                if len(target.get("images", [])) + len(items) > 100:
                    raise ValueError("合并后图组不能超过 100 张图片")
            except ValueError as exc:
                return error_response(str(exc), status_code=400)
        if as_group and len(items) < 2:
            return error_response("图组至少需要两张图片", status_code=400)
        await self._expire_import_groups()
        now = time.time()
        self.uploads = {
            key: value
            for key, value in self.uploads.items()
            if now - value["created_at"] < 3600
        }
        pending: list[tuple[str, dict[str, Any]]] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                return error_response("导入项目格式错误", status_code=400)
            client_id = str(item.get("client_id") or "")
            overrides = item.get("overrides", {})
            if (
                not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", client_id)
                or client_id in seen
            ):
                return error_response("导入图片标识无效或重复", status_code=400)
            if (
                not isinstance(overrides, dict)
                or len(json.dumps(overrides, ensure_ascii=False)) > 1024 * 1024
            ):
                return error_response("补充参数必须是小于 1 MB 的对象", status_code=400)
            if not isinstance(overrides.get("parameters", {}), dict):
                return error_response("补充参数 parameters 必须是对象", status_code=400)
            if merge_target_id and "generation_engine" in overrides:
                try:
                    if (
                        self._import_merge_engine(overrides["generation_engine"])
                        != expected_engine
                    ):
                        raise ValueError(
                            f"图片 {item.get('filename') or client_id} 的生图来源与本批合并来源不一致"
                        )
                except ValueError as exc:
                    return error_response(str(exc), status_code=400)
            seen.add(client_id)
            pending.append(
                (
                    uuid.uuid4().hex,
                    {
                        "client_id": client_id,
                        "sha256": hashes_by_id[client_id],
                        "created_at": now,
                        "filename": str(item.get("filename") or "import.png")[:240],
                        "overrides": copy.deepcopy(overrides),
                    },
                )
            )
        if len(self.uploads) + len(pending) > 500:
            return error_response(
                "待上传项目过多，请完成已有上传后重试", status_code=429
            )
        if as_group:
            models = [item["overrides"].get("model") for _, item in pending]
            if any(not isinstance(model, str) or not model.strip() for model in models):
                return error_response(
                    "作为图组导入时，每张图片都必须填写模型", status_code=400
                )
            if len({model.strip() for model in models}) != 1:
                details = "；".join(
                    f"{item['filename']}：{model.strip()}"
                    for (_, item), model in zip(pending, models)
                )
                return error_response(
                    f"图组中的模型必须相同，当前模型不一致：{details}", status_code=400
                )
        group_id = uuid.uuid4().hex
        directory = self.store.imports_dir / group_id
        await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=False)
        for ordinal, (token, item) in enumerate(pending):
            item.update(
                group_id=group_id,
                path=directory / f"{ordinal:03d}-{token}.image",
                uploaded=False,
            )
            if as_group:
                item["overrides"]["model"] = item["overrides"]["model"].strip()
        self.groups[group_id] = {
            "created_at": now,
            "directory": directory,
            "items": pending,
            "lock": asyncio.Lock(),
            "result": None,
            "merge_target_id": merge_target_id,
            "expected_engine": expected_engine,
            "mode": "merge" if merge_target_id else "group" if as_group else "separate",
        }
        group_response = {
            "allowed": True,
            "group_id": group_id,
            "commit_endpoint": f"imports/group/{group_id}/commit",
            "cancel_endpoint": f"imports/group/{group_id}/cancel",
        }
        self.uploads.update(pending)
        return json_response(
            {
                **group_response,
                "items": [
                    {
                        "client_id": item["client_id"],
                        "upload_endpoint": f"imports/upload/{token}",
                    }
                    for token, item in pending
                ],
            }
        )

    async def _api_import_upload(self, upload_id: str) -> Any:
        item = self.uploads.get(upload_id)
        if item is None or time.time() - item["created_at"] >= 3600:
            self.uploads.pop(upload_id, None)
            return error_response("导入准备已过期，请重试上传", status_code=410)
        files = await web_request.files()
        upload = files.get("file")
        if upload is None:
            return error_response("缺少图片文件", status_code=400)
        limit = 30 * 1024 * 1024
        if upload.content_length is not None and upload.content_length > limit:
            return error_response("导入图片不能超过 30 MB", status_code=400)
        raw = await upload.read(limit + 1)
        if not raw or len(raw) > limit:
            return error_response("图片为空或超过 30 MB", status_code=400)
        actual_digest = hashlib.sha256(raw).hexdigest()
        if actual_digest != item["sha256"]:
            return json_response(
                {
                    "allowed": False,
                    "code": "hash_mismatch",
                    "client_id": item["client_id"],
                    "expected_sha256": item["sha256"],
                    "actual_sha256": actual_digest,
                    "message": "上传图片内容与查重时不一致，请重新选择或上传原图片",
                }
            )
        if item.get("group_id"):
            group = self.groups.get(item["group_id"])
            if group is None:
                return error_response("导入批次已过期或取消，请重试", status_code=410)
            async with group["lock"]:
                if self.groups.get(item["group_id"]) is not group:
                    return error_response("导入批次已取消，请重试", status_code=410)
                if group["result"] is not None:
                    return json_response(
                        {
                            "allowed": True,
                            "uploaded": True,
                            "group_id": item["group_id"],
                            "committed": True,
                        }
                    )
                try:
                    await self.store.stage_import_file(item["path"], raw)
                    item["uploaded"] = True
                    return json_response(
                        {
                            "allowed": True,
                            "uploaded": True,
                            "group_id": item["group_id"],
                            "client_id": item["client_id"],
                        }
                    )
                except ValueError as exc:
                    return error_response(str(exc), status_code=400)
                except OSError:
                    return error_response(
                        "图片暂存失败，请检查磁盘空间后重试", status_code=500
                    )
        return error_response("导入批次不存在，请重新选择图片", status_code=410)

    async def _expire_import_groups(self) -> None:
        now = time.time()
        for group_id, group in list(self.groups.items()):
            if now - group["created_at"] >= 3600:
                async with group["lock"]:
                    await self._discard_import_group(group_id, group)

    async def _discard_import_group(self, group_id: str, group: dict[str, Any]) -> None:
        """Remove only this server-created import staging directory."""
        directory = group["directory"]
        if (
            directory.parent.resolve() != self.store.imports_dir.resolve()
            or directory.name != group_id
        ):
            raise ValueError("图组暂存路径无效")
        if directory.exists():
            try:
                await asyncio.to_thread(shutil.rmtree, directory)
            except FileNotFoundError:
                pass
        for token, _ in group["items"]:
            self.uploads.pop(token, None)
        self.groups.pop(group_id, None)

    async def _api_import_group_cancel(self, group_id: str) -> Any:
        group = self.groups.get(group_id)
        if group is not None:
            async with group["lock"]:
                await self._discard_import_group(group_id, group)
        return json_response({"cancelled": True})

    @staticmethod
    def _import_batch_result(saved: dict[str, Any]) -> dict[str, Any]:
        return {
            **saved,
            "allowed": True,
            "merged": saved["mode"] == "merge",
            "added_count": saved["added"],
        }

    async def _api_import_group_commit(self, group_id: str) -> Any:
        if not re.fullmatch(r"[a-f0-9]{32}", group_id):
            return error_response("导入批次 ID 无效", status_code=400)
        group = self.groups.get(group_id)
        if group is None or time.time() - group["created_at"] >= 3600:
            previous = await self.store.get_import_batch_result(
                f"import_batch:{group_id}"
            )
            if previous is not None:
                return json_response(self._import_batch_result(previous))
            return error_response("导入批次已过期或取消，请重试", status_code=410)
        async with group["lock"]:
            if (
                self.groups.get(group_id) is not group
                or time.time() - group["created_at"] >= 3600
            ):
                return error_response("导入批次已过期或取消，请重试", status_code=410)
            if group["result"] is not None:
                return json_response(group["result"])
            missing = [
                item["filename"] for _, item in group["items"] if not item["uploaded"]
            ]
            if missing:
                return error_response(
                    f"本批尚有图片未上传：{'、'.join(missing)}", status_code=400
                )
            try:
                saved = await self.store.commit_import_batch(
                    [item for _, item in group["items"]],
                    import_key=f"import_batch:{group_id}",
                    mode=group["mode"],
                    target_id=group.get("merge_target_id", ""),
                    expected_engine=group.get("expected_engine", ""),
                    preview_max_edge=self.get_settings().asset_preview_max_edge,
                    preview_quality=self.get_settings().asset_preview_quality,
                )
                result = self._import_batch_result(saved)
            except ImportDuplicateError as exc:
                await self._discard_import_group(group_id, group)
                return json_response(exc.as_dict())
            except ValueError as exc:
                await self._discard_import_group(group_id, group)
                return error_response(str(exc), status_code=400)
            except OSError:
                return error_response(
                    "导入保存失败，请检查磁盘空间后重试", status_code=500
                )
            group["result"] = result
            for token, _ in group["items"]:
                self.uploads.pop(token, None)
            group["items"] = []
            # A lost response can retry the commit, without keeping another copy of the images.
            try:
                await asyncio.to_thread(shutil.rmtree, group["directory"])
            except OSError:
                logger.warning("%s 导入批次已保存，暂存目录将由维护任务清理", LOG_TAG)
            return json_response(result)

    async def _api_resolve_parameters(self) -> Any:
        body = await web_request.json(default={})
        if not isinstance(body, dict) or not isinstance(body.get("content"), str):
            return error_response("请粘贴参数文本", status_code=400)
        if len(body["content"]) > 4 * 1024 * 1024:
            return error_response("参数文本不能超过 4 MB", status_code=400)
        try:
            result = await asyncio.to_thread(
                resolve_parameters,
                body["content"],
                self.get_settings(),
                str(body.get("model_ref") or ""),
                for_reproduction=body.get("for_reproduction") is True,
            )
            return json_response(result)
        except ValueError as exc:
            return error_response(str(exc), status_code=400)

    async def _api_gallery_import_edit(self, generation_id: str) -> Any:
        try:
            if web_request.method == "GET":
                if "output_node_id" in web_request.query:
                    return json_response(
                        await self.store.project_import_edit_image(
                            generation_id,
                            str(web_request.query.get("image_id", "")),
                            str(web_request.query.get("item_revision", "")),
                            str(web_request.query.get("output_node_id", "")),
                        )
                    )
                return json_response(
                    await self.store.import_edit_snapshot(
                        generation_id,
                        light=str(web_request.query.get("light", "")).lower()
                        in {"1", "true", "yes"},
                        image_id=str(web_request.query.get("image_id", "")),
                        item_revision=str(web_request.query.get("item_revision", "")),
                        include_preview=str(
                            web_request.query.get("include_preview", "1")
                        ).lower()
                        not in {"0", "false", "no"},
                    )
                )
            body = await web_request.json(default={})
            if not isinstance(body, dict) or set(body) != {"revision", "items"}:
                raise ValueError("编辑请求必须包含 revision 和 items")
            return json_response(
                await self.store.edit_import(
                    generation_id, body["revision"], body["items"]
                )
            )
        except ImportEditConflictError as exc:
            return error_response(str(exc), status_code=409)
        except LookupError as exc:
            return error_response(str(exc), status_code=404)
        except (ValueError, TypeError, OverflowError, RecursionError) as exc:
            return error_response(str(exc), status_code=400)

    async def _api_gallery_parameters(self, generation_id: str) -> Any:
        image_id = str(web_request.query.get("image_id") or "")
        try:
            detail = await self.store.generation_image_context(generation_id, image_id)
            if detail is None:
                return error_response("生成记录不存在", status_code=404)
            result = export_parameters(
                detail,
                image_id,
                str(web_request.query.get("format") or "studio"),
            )
            return json_response(result)
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
