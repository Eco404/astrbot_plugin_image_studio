"""Gallery browsing, asset actions, exports and external source status API."""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import Any

from astrbot.api.web import error_response, file_response, json_response
from astrbot.api.web import request as web_request
from starlette.background import BackgroundTask

from ..gallery.errors import (
    ExternalDeleteError,
    ExternalPermissionError,
)
from ..media.display import DisplayImageError

LOG_TAG = "[ImageStudio]"


class GalleryAPI:
    def __init__(
        self, *, store, get_settings, get_service, get_external, run_maintenance=None
    ):
        self.store = store
        self.get_settings = get_settings
        self.get_service = get_service
        self.get_external = get_external
        self.run_maintenance = run_maintenance
        self.exports = {}

    async def _api_storage_health(self) -> Any:
        report = await self.store.maintenance_report()
        report["retention"] = await self.store.retention_status(
            self.get_settings().history
        )
        report["external_sources"] = await self.get_external().status()
        return json_response(report)

    async def _api_external_status(self) -> Any:
        return json_response(
            {
                "types": self.get_external().source_types(),
                "sources": await self.get_external().status(),
            }
        )

    async def _api_external_scan(self) -> Any:
        body = await web_request.json(default={})
        if not isinstance(body, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)
        try:
            result = await self.get_external().request_scan(
                str(body.get("source_id") or "nai")
            )
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        return json_response(result)

    async def _api_gallery_as_reference(self) -> Any:
        body = await web_request.json(default={})
        if not isinstance(body, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)
        try:
            result = await self.store.stage_gallery_reference(
                str(body.get("image_id") or "")
            )
        except ExternalPermissionError as exc:
            return error_response(str(exc), status_code=403)
        except (ValueError, OSError) as exc:
            return error_response(str(exc), status_code=400)
        return json_response(result)

    async def _api_gallery_favorite(self) -> Any:
        body = await web_request.json(default={})
        if isinstance(body, dict) and "generation_ids" in body:
            if body.get("action") != "toggle":
                return error_response("批量收藏操作必须为 toggle", status_code=400)
            try:
                return json_response(
                    await self.store.toggle_favorites(body["generation_ids"])
                )
            except ExternalPermissionError as exc:
                return error_response(str(exc), status_code=403)
            except ValueError as exc:
                return error_response(str(exc), status_code=400)
        if not isinstance(body, dict) or not isinstance(body.get("favorite"), bool):
            return error_response("收藏状态必须是布尔值", status_code=400)
        try:
            result = await self.store.set_favorite(
                str(body.get("generation_id") or ""), body["favorite"]
            )
            return json_response(result)
        except ExternalPermissionError as exc:
            return error_response(str(exc), status_code=403)
        except ValueError as exc:
            return error_response(str(exc), status_code=400)

    async def _api_gallery_favorite_status(self) -> Any:
        body = await web_request.json(default={})
        if not isinstance(body, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)
        try:
            return json_response(
                await self.store.favorite_status(body.get("generation_ids"))
            )
        except ValueError as exc:
            return error_response(str(exc), status_code=400)

    async def _api_gallery_delete_images(self) -> Any:
        body = await web_request.json(default={})
        if not isinstance(body, dict) or not isinstance(body.get("image_ids"), list):
            return error_response("请选择需要删除的图片", status_code=400)
        if not 1 <= len(body["image_ids"]) <= 100:
            return error_response("请选择 1 至 100 张图片", status_code=400)
        try:
            preview = await self.store.external_action_preview(
                [str(body.get("generation_id") or "")], "delete"
            )
            if not preview["allowed"]:
                return error_response(
                    "；".join(item["message"] for item in preview["denied"]),
                    status_code=403,
                )
            if preview["external_count"] and body.get("confirm_external") is not True:
                return error_response(
                    "本次删除包含外部资源，将删除来源插件中的原图，请确认后重试",
                    status_code=409,
                )
            result = await self.store.delete_images(
                str(body.get("generation_id") or ""), body["image_ids"]
            )
            return json_response(result)
        except ExternalDeleteError as exc:
            details = exc.as_dict()
            if details.get("generation_deleted"):
                return json_response(
                    {
                        "deleted": body["image_ids"],
                        "remaining": 0,
                        "generation_deleted": True,
                        "errors": [details],
                    }
                )
            return error_response(str(exc), status_code=409)
        except ExternalPermissionError as exc:
            return error_response(str(exc), status_code=403)
        except ValueError as exc:
            return error_response(str(exc), status_code=400)

    async def _api_storage_maintenance(self) -> Any:
        body = await web_request.json(default={})
        if not isinstance(body, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)
        if self.run_maintenance is not None:
            report = await self.run_maintenance(deep=bool(body.get("deep", False)))
        else:
            report = await self.store.run_maintenance(
                self.get_settings().history,
                preview_max_edge=self.get_settings().asset_preview_max_edge,
                preview_quality=self.get_settings().asset_preview_quality,
                deep=bool(body.get("deep", False)),
            )
        return json_response(report)

    @staticmethod
    def _gallery_request_filters() -> dict[str, Any]:
        filters = {
            "query": web_request.query.get("query", ""),
            "provider_id": web_request.query.get("provider_id", ""),
            "mode": web_request.query.get("mode", ""),
            "source": web_request.query.get("source", ""),
            "generation_engine": web_request.query.get("generation_engine", ""),
            "favorite": web_request.query.get("favorite", ""),
            "sort": web_request.query.get("sort", "created"),
            "limit": web_request.query.get("limit", 24),
            "offset": web_request.query.get("offset", 0),
        }
        for key in ("provider_ids", "modes", "sources", "generation_engines"):
            if key in web_request.query:
                filters[key] = web_request.query.get(key)
        return filters

    async def _api_gallery_list(self) -> Any:
        filters = self._gallery_request_filters()
        filters["light"] = str(web_request.query.get("light", "")).lower() in {
            "1",
            "true",
            "yes",
        }
        try:
            payload = await self.store.list_generations(filters)
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        retention = await self.store.gallery_retention_status(
            self.get_settings().history
        )
        candidates = set(retention.get("candidate_ids", []))
        for item in payload["items"]:
            item["cleanup_warning"] = item["id"] in candidates
        payload["retention"] = retention
        return json_response(payload)

    async def _api_gallery_detail(self, generation_id: str) -> Any:
        light = str(web_request.query.get("light", "")).lower() in {"1", "true", "yes"}
        revision = await self.store.gallery_revision() if light else ""
        include_assets = str(web_request.query.get("assets", "1")).lower() not in {
            "0",
            "false",
            "no",
        }
        detail = await self.store.generation_detail(
            generation_id,
            include_assets=include_assets,
            light=light,
        )
        if detail is None:
            return error_response("生成记录不存在", status_code=404)
        if light:
            detail["gallery_revision"] = revision
        return json_response(detail)

    async def _api_gallery_assets(self, generation_id: str) -> Any:
        detail = await self.store.generation_detail(generation_id, include_assets=True)
        if detail is None:
            return error_response("生成记录不存在", status_code=404)
        return json_response(
            {"images": detail["images"], "references": detail["references"]}
        )

    async def _api_gallery_image_sequence(self) -> Any:
        try:
            revision = await self.store.gallery_revision()
            items = await self.store.gallery_image_sequence(
                self._gallery_request_filters()
            )
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        return json_response(
            {"items": items, "total": len(items), "revision": revision}
        )

    async def _api_gallery_image(self, image_id: str) -> Any:
        detail = str(web_request.query.get("detail", "preview")).strip().lower()
        if detail not in {"preview", "display", "original"}:
            return error_response(
                "图片读取方式仅支持 preview、display 或 original", status_code=400
            )
        try:
            options = (
                {"max_edge": web_request.query.get("max_edge", 1536)}
                if detail == "display"
                else {}
            )
            image = await self.store.gallery_image_data(
                image_id, detail=detail, **options
            )
        except DisplayImageError as exc:
            return error_response(str(exc), status_code=422)
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        if image is None:
            return error_response("生成图片不存在", status_code=404)
        return json_response(image)

    async def _api_gallery_image_download(self, image_id: str) -> Any:
        try:
            image = await self.store.gallery_image_file(image_id)
        except ValueError as exc:
            return error_response(str(exc), status_code=403)
        if image is None:
            return error_response("生成图片不存在", status_code=404)
        path, mime_type, filename = image
        return file_response(path, filename=filename, content_type=mime_type)

    async def _api_gallery_image_info(self, image_id: str) -> Any:
        image = await self.store.gallery_image_info(
            image_id,
            include_preview=str(web_request.query.get("include_preview", "1")).lower()
            not in {"0", "false", "no"},
        )
        if image is None:
            return error_response("生成图片不存在", status_code=404)
        return json_response(image)

    async def _api_gallery_reference_image(self, reference_id: str) -> Any:
        image = await self.store.gallery_reference_image(reference_id)
        if image is None:
            return error_response("参考图片不存在", status_code=404)
        return json_response(image)

    async def _api_gallery_reproduce(self, generation_id: str) -> Any:
        try:
            body = await web_request.json(default={})
            image_id = str(body.get("image_id") or "") if isinstance(body, dict) else ""
            plan = await self.get_service().reproduction_plan(generation_id, image_id)
        except ValueError as exc:
            return error_response(str(exc), status_code=404)
        return json_response(plan)

    async def _api_gallery_delete(self) -> Any:
        body = await web_request.json(default={})
        ids = body.get("ids") if isinstance(body, dict) else []
        if not isinstance(ids, list) or not 1 <= len(ids) <= 200:
            return error_response("请选择 1 至 200 条生成记录", status_code=400)
        try:
            preview = await self.store.external_action_preview(ids, "delete")
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        if not preview["allowed"]:
            return error_response(
                "；".join(item["message"] for item in preview["denied"]),
                status_code=403,
            )
        if preview["external_count"] and body.get("confirm_external") is not True:
            return error_response(
                "本次删除包含外部资源，将删除来源插件中的原图，请确认后重试",
                status_code=409,
            )
        try:
            return json_response(await self.store.delete_generations(ids))
        except ValueError as exc:
            return error_response(str(exc), status_code=403)

    async def _api_gallery_delete_preview(self) -> Any:
        body = await web_request.json(default={})
        if not isinstance(body, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)
        action = str(body.get("action") or "delete")
        limit = 1000 if action == "favorite" else 200
        if not isinstance(body.get("ids"), list) or not 1 <= len(body["ids"]) <= limit:
            return error_response(f"请选择 1 至 {limit} 条生成记录", status_code=400)
        try:
            return json_response(
                await self.store.external_action_preview(body.get("ids"), action)
            )
        except ValueError as exc:
            return error_response(str(exc), status_code=400)

    async def _api_reference_delete(self) -> Any:
        body = await web_request.json(default={})
        reference_id = (
            str(body.get("reference_id") or "") if isinstance(body, dict) else ""
        )
        if not await self.store.delete_reference(reference_id):
            return error_response("参考图不存在或已删除", status_code=404)
        return json_response({"reference_id": reference_id})

    async def _api_gallery_export(self) -> Any:
        body = await web_request.json(default={})
        ids = body.get("ids") if isinstance(body, dict) else []
        if not isinstance(ids, list):
            return error_response("ids 必须是列表", status_code=400)
        try:
            path = await self.store.export_generations(
                [str(item or "") for item in ids]
            )
        except ExternalPermissionError as exc:
            return error_response(str(exc), status_code=403)
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        await self.store.cleanup_exports()
        export_id = uuid.uuid4().hex
        self.exports[export_id] = (path, time.time())
        return json_response(
            {"download_endpoint": f"gallery/export/{export_id}", "filename": path.name}
        )

    async def _api_download_export(self, export_id: str) -> Any:
        item = self.exports.get(export_id)
        if item is None:
            return error_response("导出文件已过期，请重新导出", status_code=404)
        path, created_at = item
        if time.time() - created_at > 3600 or not path.is_file():
            self.exports.pop(export_id, None)
            await asyncio.to_thread(_remove_export_file, path)
            return error_response("导出文件已过期，请重新导出", status_code=404)
        response = file_response(
            path,
            filename=path.name,
            content_type="application/zip",
        )
        self.exports.pop(export_id, None)
        response.background = BackgroundTask(_remove_export_file, path)
        return response


def _remove_export_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
