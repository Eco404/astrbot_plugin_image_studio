import asyncio
import json
from types import SimpleNamespace

from astrbot_plugin_image_studio.backend.config import normalize_webui_settings
from astrbot_plugin_image_studio.backend.observability.integration import (
    observe_api,
    observe_tool,
)
from astrbot_plugin_image_studio.backend.observability.store import (
    StructuredLogStore,
    log_context,
)


def test_operation_error_and_request_share_context(tmp_path):
    async def run():
        logs = StructuredLogStore(tmp_path, level="DEBUG")
        plugin = SimpleNamespace(_logs=logs)

        async def handler():
            with log_context(operation_id="nested-id"):
                await logs.record("INFO", "request", "request")
            return SimpleNamespace(
                status_code=400, body=b'{"message":"Invalid parameter"}'
            )

        await observe_api(plugin, handler, "studio/generate", ["POST"])()
        items = (await logs.query())["items"]
        assert {item["category"] for item in items} == {"operation", "request"}
        assert len({item["operation_id"] for item in items}) == 1
        assert any(item["data"].get("error") == "Invalid parameter" for item in items)
        assert (
            next(item for item in items if item["category"] == "operation")["level"]
            == "WARNING"
        )
        await logs.clear()
        await observe_api(plugin, handler, "logs/clear", ["POST"])()
        assert not (await logs.query(category="operation"))["items"]

    asyncio.run(run())


def test_tool_returned_validation_failure_is_logged_without_event(tmp_path):
    async def run():
        logs = StructuredLogStore(tmp_path, level="DEBUG")
        plugin = SimpleNamespace(_logs=logs)

        @observe_tool
        async def tool(self, event, model_ref=""):
            return SimpleNamespace(
                isError=True,
                content=[
                    SimpleNamespace(
                        type="text",
                        text=json.dumps(
                            {"status": "validation_failed", "error": "Unknown model"}
                        ),
                    )
                ],
            )

        await tool(
            plugin, {"private_user_message": "must not log"}, model_ref="bad:model"
        )
        items = (await logs.query())["items"]
        assert any(
            item["level"] == "WARNING"
            and item["data"].get("status") == "validation_failed"
            for item in items
        )
        assert "private_user_message" not in json.dumps(items)
        assert len({item["operation_id"] for item in items}) == 1
        logs.configure(enabled=False, level="DEBUG", retention_days=7)
        await tool(plugin, None)
        assert (await logs.query())["total"] == len(items)

    asyncio.run(run())


def test_logging_defaults_and_disabled_settings_survive_normalization():
    defaults, errors = normalize_webui_settings({})
    assert not errors
    assert defaults["logging"] == {
        "enabled": True,
        "level": "INFO",
        "retention_days": 7,
    }
    custom, errors = normalize_webui_settings(
        {"logging": {"enabled": False, "level": "debug", "retention_days": 0}}
    )
    assert not errors
    assert custom["logging"] == {
        "enabled": False,
        "level": "DEBUG",
        "retention_days": 0,
    }
    assert normalize_webui_settings(
        {"logging": {"level": "unknown", "retention_days": -1}}
    )[1]
