"""Isolated real-API WebUI harness; generated fixtures never touch deployment data."""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import re
import tempfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageDraw, PngImagePlugin

from astrbot.api.web import PluginRequest, bind_request_context
from astrbot_plugin_image_studio.config import (
    normalize_webui_settings,
    runtime_settings,
)
from astrbot_plugin_image_studio.main import ImageStudioPlugin, PLUGIN_NAME
from astrbot_plugin_image_studio.models import GeneratedImage, GenerationRequest
from astrbot_plugin_image_studio.service import ImageGenerationService
from astrbot_plugin_image_studio.storage import GenerationStore


def fixture_image(index: int = 0, *, novelai: bool = False) -> bytes:
    width, height = ((576, 768), (768, 576), (640, 640))[index % 3]
    palette = [(170, 204, 206), (194, 198, 175), (208, 181, 190), (169, 190, 211)]
    image = Image.new("RGB", (width, height), palette[index % len(palette)])
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, height * 0.62, width, height), fill=(89 + index % 50, 130, 120))
    draw.polygon(
        [
            (0, height * 0.67),
            (width * 0.36, height * 0.24),
            (width * 0.68, height * 0.68),
        ],
        fill=(100, 129 + index % 40, 152),
    )
    draw.polygon(
        [
            (width * 0.28, height * 0.7),
            (width * 0.73, height * 0.37),
            (width, height * 0.73),
        ],
        fill=(114, 143, 136 + index % 40),
    )
    draw.line(
        [(0, height * 0.85), (width, height * 0.72)],
        fill=(202, 216, 195),
        width=5 + index % 12,
    )
    output = io.BytesIO()
    pnginfo = PngImagePlugin.PngInfo()
    if novelai:
        pnginfo.add_text("Software", "NovelAI")
        pnginfo.add_text(
            "Comment",
            json.dumps(
                {
                    "prompt": f"mountain landscape, daylight, composition {index}",
                    "uc": "low quality",
                    "model": "nai-diffusion-4-5-full",
                    "steps": 26,
                    "scale": 6,
                    "cfg_rescale": 0.3,
                    "seed": index + 100,
                    "width": width,
                    "height": height,
                    "sampler": "k_euler_ancestral",
                    "request_type": "PromptGenerateRequest",
                }
            ),
        )
    image.save(output, "PNG", pnginfo=pnginfo)
    return output.getvalue()


class TestConfig(dict):
    async def save_config_async(self) -> None:
        return None


class FakeExecutor:
    async def generate(
        self, provider: Any, request: GenerationRequest
    ) -> tuple[GeneratedImage, ...]:
        return tuple(
            GeneratedImage(
                fixture_image(index + 75, novelai=provider.kind == "nai_direct"),
                "image/png",
            )
            for index in range(2)
        )


def harness_settings() -> dict:
    value, errors = normalize_webui_settings(
        {
            "revision": 1,
            "history": {
                "enabled": True,
                "max_records": 40,
                "max_megabytes": 256,
                "retain_reference_images": True,
            },
            "generation_defaults": {
                "page": {
                    "text2img_model_ref": "natural:studio-image",
                    "img2img_model_ref": "natural:studio-image",
                }
            },
            "providers": [
                {
                    "id": "natural",
                    "name": "自然语言测试",
                    "kind": "openai_images",
                    "base_url": "https://example.test/v1",
                    "models": [
                        {
                            "id": "studio-image",
                            "name": "自然语言图像",
                            "supports_text2img": True,
                            "supports_img2img": True,
                            "max_reference_images": 4,
                            "supports_negative_prompt": False,
                            "parameters": {
                                "size": {
                                    "type": "select",
                                    "default": "1024x1024",
                                    "choices": ["1024x1024", "1024x1536", "1536x1024"],
                                },
                                "count": {
                                    "type": "integer",
                                    "default": 2,
                                    "min": 1,
                                    "max": 4,
                                },
                                "quality": {
                                    "type": "select",
                                    "default": "auto",
                                    "choices": ["auto", "high", "medium", "low"],
                                },
                            },
                        }
                    ],
                },
                {
                    "id": "nai",
                    "name": "NAI 测试",
                    "kind": "nai_direct",
                    "base_url": "https://example.test",
                    "models": [
                        {
                            "id": "nai-diffusion-4-5-full",
                            "name": "NAI V4.5",
                            "supports_text2img": True,
                            "supports_img2img": False,
                            "supports_negative_prompt": True,
                            "negative_prompt_default": "low quality, blurry",
                            "parameters": {
                                "style": {
                                    "type": "preset",
                                    "default": "custom",
                                    "target": "artist",
                                    "choices": [
                                        {
                                            "value": "custom",
                                            "label": "自定义",
                                            "text": "",
                                        },
                                        {
                                            "value": "landscape",
                                            "label": "风景",
                                            "text": "landscape art",
                                        },
                                    ],
                                },
                                "artist": {"type": "text", "default": ""},
                                "size": {
                                    "type": "select",
                                    "default": "竖图",
                                    "choices": ["竖图", "横图", "方图"],
                                },
                                "count": {
                                    "type": "integer",
                                    "default": 1,
                                    "min": 1,
                                    "max": 4,
                                },
                                "steps": {
                                    "type": "integer",
                                    "default": 26,
                                    "min": 1,
                                    "max": 28,
                                },
                                "scale": {
                                    "type": "number",
                                    "default": 6,
                                    "min": 0,
                                    "max": 20,
                                },
                                "cfg": {
                                    "type": "number",
                                    "default": 0.3,
                                    "min": 0,
                                    "max": 1,
                                    "step": 0.05,
                                },
                                "seed": {"type": "integer", "default": 0},
                                "sampler": {
                                    "type": "select",
                                    "default": "k_euler_ancestral",
                                    "choices": ["k_euler_ancestral", "k_euler"],
                                },
                            },
                        }
                    ],
                },
            ],
        }
    )
    if errors:
        raise ValueError(errors)
    return value


BRIDGE_JS = r"""
(() => {
  let sequence = 0;
  const pending = new Map();
  window.addEventListener('message', event => {
    if (event.source !== window.parent || event.data?.harnessReply === undefined) return;
    const item = pending.get(event.data.harnessReply);
    if (!item) return;
    pending.delete(event.data.harnessReply);
    clearTimeout(item.timeout);
    event.data.error ? item.reject(new Error(event.data.error)) : item.resolve(event.data.result);
  });
  const call = (method, path, data, filename) => new Promise((resolve, reject) => {
    const id = ++sequence;
    const timeout = setTimeout(() => { pending.delete(id); reject(new Error('测试页面通信超时')); }, 15000);
    pending.set(id, {resolve, reject, timeout});
    window.parent.postMessage({harnessCall: id, method, path, data, filename}, '*');
  });
  window.AstrBotPluginPage = {
    ready: async () => ({theme: 'light', pluginName: 'astrbot_plugin_image_studio'}),
    apiGet: (path, params) => call('GET', path, params),
    apiPost: (path, body) => call('POST', path, body),
    upload: (path, file) => call('UPLOAD', path, file),
    download: (path, params, filename) => call('DOWNLOAD', path, params, filename),
    onThemeChange: () => () => {},
    getTheme: () => 'light',
  };
})();
"""

WRAPPER_HTML = r"""<!doctype html><html lang="zh-CN"><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Image Studio 本地验证</title>
<style>html,body{margin:0;width:100%;height:100%;overflow:hidden}iframe{width:100%;height:100%;border:0;display:block}</style></head><body>
<iframe id="studio" src="/ui/index.html" sandbox="allow-scripts allow-forms allow-downloads"></iframe>
<script>
window.addEventListener('message', async event => {
  const frame = document.getElementById('studio');
  if (event.source !== frame.contentWindow || event.data?.harnessCall === undefined) return;
  const request = event.data;
  try {
    const url = new URL('/astrbot_plugin_image_studio/' + String(request.path).replace(/^\/+/, ''), location.origin);
    if (!url.pathname.startsWith('/astrbot_plugin_image_studio/')) throw new Error('测试 API 路径无效');
    const options = {};
    if (request.method === 'POST') { options.method = 'POST'; options.headers = {'Content-Type':'application/json'}; options.body = JSON.stringify(request.data); }
    else if (request.method === 'UPLOAD') { options.method = 'POST'; const form = new FormData(); form.append('file', request.data, request.data.name || 'import.png'); options.body = form; }
    else for (const [key,value] of Object.entries(request.data || {})) if (value !== undefined && value !== null) url.searchParams.set(key,value);
    const response = await fetch(url, options);
    if (!response.ok) { const body = await response.json(); throw new Error(body.message || '请求失败'); }
    let result;
    if (request.method === 'DOWNLOAD') {
      const blob = await response.blob(); const objectUrl = URL.createObjectURL(blob); const anchor = document.createElement('a');
      anchor.href = objectUrl; anchor.download = request.filename || 'download'; anchor.click(); setTimeout(() => URL.revokeObjectURL(objectUrl), 1000); result = {ok: true};
    } else result = await response.json();
    event.source.postMessage({harnessReply: request.harnessCall, result}, '*');
  } catch(error) { event.source.postMessage({harnessReply: request.harnessCall, error: error.message}, '*'); }
});
</script></body></html>"""


async def create_app(data_dir: Path, seed: bool = True) -> FastAPI:
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    plugin = object.__new__(ImageStudioPlugin)
    plugin.config = TestConfig(enable_llm_tool=True)
    plugin.data_dir = data_dir
    plugin.store = GenerationStore(data_dir)
    await plugin.store.initialize()
    plugin._studio_settings = harness_settings()
    plugin._settings, plugin._settings_errors = runtime_settings(
        plugin.config, plugin._studio_settings
    )
    plugin._settings_lock = asyncio.Lock()
    plugin._imports, plugin._exports = {}, {}
    plugin._session, plugin._maintenance_task = None, None
    plugin._service = ImageGenerationService(
        settings=plugin._settings, executor=FakeExecutor(), store=plugin.store
    )
    app = FastAPI()
    app.state.plugin = plugin
    app.state.data_dir = data_dir
    app.state.seed_ids = []

    class Context:
        def register_web_api(
            self, path: str, handler: Any, methods: list[str], description: str
        ) -> None:
            async def endpoint(request: Request) -> Any:
                with bind_request_context(
                    PluginRequest(
                        request,
                        path_params=request.path_params,
                        plugin_name=PLUGIN_NAME,
                        username="local-test",
                    )
                ):
                    return await handler(**request.path_params)

            app.add_api_route(
                re.sub(r"<([^>]+)>", r"{\1}", path),
                endpoint,
                methods=methods,
                description=description,
            )

    plugin.context = Context()
    plugin._register_web_apis()
    if seed:
        for index in range(37):
            provider = plugin._settings.providers[index % 2]
            images = tuple(
                GeneratedImage(
                    fixture_image(
                        index * 3 + ordinal, novelai=provider.kind == "nai_direct"
                    ),
                    "image/png",
                )
                for ordinal in range(3 if index == 0 else 1)
            )
            generation_id = await plugin.store.record_success(
                provider=provider,
                request=GenerationRequest(
                    mode="text2img",
                    provider_id=provider.id,
                    model=provider.models[0].id,
                    prompt=f"山间湖泊与日光，构图 {index + 1}",
                    size="1024x1024",
                    count=len(images),
                    source=("webui", "command", "llm_tool")[index % 3],
                ),
                images=images,
                elapsed_ms=1200,
                history=plugin._settings.history,
            )
            app.state.seed_ids.append(generation_id)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return WRAPPER_HTML

    @app.get("/api/plugin/page/bridge-sdk.js")
    async def bridge() -> Response:
        return Response(BRIDGE_JS, media_type="application/javascript")

    app.mount(
        "/ui",
        StaticFiles(
            directory=Path(__file__).parents[1] / "pages" / "image-studio", html=True
        ),
        name="ui",
    )
    return app


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18765)
    args = parser.parse_args()
    harness_data = Path(tempfile.mkdtemp(prefix="image-studio-webui-"))
    print(f"Isolated test data: {harness_data}", flush=True)
    uvicorn.run(asyncio.run(create_app(harness_data)), host="127.0.0.1", port=args.port)
