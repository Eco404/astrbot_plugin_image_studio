from __future__ import annotations

import asyncio
import json

import httpx
import mcp.types
import pytest
from astrbot.api.message_components import Image, Plain

from astrbot_plugin_image_studio.tests.backend.integration.test_command_invocation import (
    CommandEvent,
)
from astrbot_plugin_image_studio.tests.backend.providers.test_nai_batches import (
    Executor,
)
from astrbot_plugin_image_studio.tests.backend.integration.test_service_and_tool import (
    ToolEvent,
)
from astrbot_plugin_image_studio.tests.support.webui_harness import create_app

MODEL_REF = "nai:nai-diffusion-4-5-full"


@pytest.mark.parametrize("source", ["webui", "command", "llm_tool"])
def test_partial_batch_reaches_caller_with_images_and_warning(tmp_path, source):
    async def run():
        app = await create_app(tmp_path, seed=False)
        plugin = app.state.plugin
        plugin._service.executor = Executor(failures={2})

        async def no_references(*_args, **_kwargs):
            return ()

        if source == "webui":
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/astrbot_plugin_image_studio/studio/generate",
                    json={
                        "prompt": "mountain landscape",
                        "model_ref": MODEL_REF,
                        "count": 3,
                    },
                )
                assert response.status_code == 200
                result = response.json()
                warning = result["warning"]
                assert len(result["images"]) == 2
                assert result["generation_id"]
        elif source == "command":
            plugin._command_reference_sources = no_references
            event = CommandEvent(
                f"/istudio mountain landscape --model {MODEL_REF} --n 3"
            )
            messages = [item async for item in plugin.image_gen(event)]
            assert len(messages) == 1
            chain = messages[0]["chain"]
            warning = "".join(item.text for item in chain if isinstance(item, Plain))
            assert len([item for item in chain if isinstance(item, Image)]) == 2
        else:
            plugin._event_references = no_references
            event = ToolEvent()
            capabilities = await plugin.image_studio_get_capabilities(
                event, query_type="model", mode="text2img", model_refs=[MODEL_REF]
            )
            assert not capabilities.isError
            result = await plugin.image_studio_generate(
                event,
                prompt="mountain landscape",
                mode="text2img",
                model_ref=MODEL_REF,
                parameters={"count": 3},
            )
            assert not result.isError
            assert json.loads(result.content[0].text)["status"] == "partial"
            warning = "".join(
                item.text
                for item in result.content
                if isinstance(item, mcp.types.TextContent)
            )
            assert (
                len(
                    [
                        item
                        for item in result.content
                        if isinstance(item, mcp.types.ImageContent)
                    ]
                )
                == 2
            )
            assert warning.count('"asset_id":') == 2
        assert "成功 2 次" in warning and "失败 1 次" in warning
        assert "实际返回 2 张" in warning
        assert "第 2 次请求" in warning and "upstream failure 2" in warning
        assert "请勿自动重试整个批次" in warning
        assert len(plugin._service.executor.calls) == 3
        gallery = await plugin.store.list_generations({})
        assert gallery["total"] == 1
        detail = await plugin.store.generation_detail(gallery["items"][0]["id"])
        assert detail["source"] == source
        assert len(detail["images"]) == 2
        assert detail["supplemental"]["batch"]["failed"] == 1

    asyncio.run(run())
