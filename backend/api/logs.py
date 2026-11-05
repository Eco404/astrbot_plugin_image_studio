"""Structured log query and explicit clear endpoints for the Page API."""

from __future__ import annotations

from astrbot.api.web import error_response, json_response
from astrbot.api.web import request as web_request


class LogsAPI:
    def __init__(self, *, get_logs):
        self.get_logs = get_logs

    async def _api_logs_query(self):
        try:
            args = web_request.query
            response = json_response(
                await self.get_logs().query(
                    category=str(args.get("category", args.get("type", ""))),
                    level=str(args.get("level", "")),
                    date_from=str(args.get("date_from", "")),
                    date_to=str(args.get("date_to", "")),
                    page=int(args.get("page", 1)),
                    page_size=int(args.get("page_size", 50)),
                )
            )
            response.headers["Cache-Control"] = "no-store"
            return response
        except (ValueError, TypeError):
            return error_response(
                "日志筛选条件无效，请检查类型、等级、日期和页码", status_code=400
            )
        except OSError:
            return error_response("日志文件暂时无法读取", status_code=500)

    async def _api_logs_clear(self):
        body = await web_request.json(default={})
        if not isinstance(body, dict) or body.get("confirm") is not True:
            return error_response("清空日志需要再次确认", status_code=400)
        try:
            # Deliberately do not append a clear-operation event afterward.
            return json_response(await self.get_logs().clear())
        except OSError:
            return error_response("日志未能完全清空，请稍后重试", status_code=500)
