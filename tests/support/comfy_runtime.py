"""Reusable isolated comfy runtime fixtures; no test-module imports."""

from __future__ import annotations

import asyncio

import copy

import io


from types import SimpleNamespace


from astrbot_plugin_image_studio.backend.generation import comfyui_runtime


from astrbot_plugin_image_studio.backend.generation.comfyui_runtime import ComfyRuntime

from astrbot_plugin_image_studio.backend.config import HistorySettings, RuntimeSettings

from astrbot_plugin_image_studio.backend.models import (
    GeneratedImage,
    ImageProvider,
)


from astrbot_plugin_image_studio.backend.generation.service import (
    ImageGenerationService,
)

from astrbot_plugin_image_studio.backend.gallery.store import GenerationStore

from PIL import Image


def image(color):
    output = io.BytesIO()
    Image.new("RGB", (24, 32), color).save(output, "PNG")
    return GeneratedImage(output.getvalue(), "image/png")


def workflow(*, reference_count=0, prompt_bound=False, count_bound=False):
    graph = {
        "1": {"class_type": "CLIPTextEncode", "inputs": {"text": "fixed prompt"}},
        "2": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": 512, "height": 512, "batch_size": 3},
        },
        "3": {
            "class_type": "KSampler",
            "inputs": {"seed": 42, "positive": ["1", 0], "latent_image": ["2", 0]},
        },
        "4": {
            "class_type": "SaveImage",
            "inputs": {"images": ["3", 0], "filename_prefix": "result"},
        },
    }
    bindings = {}
    if prompt_bound:
        bindings["prompt"] = {
            "source": "prompt",
            "type": "text",
            "node_id": "1",
            "input_name": "text",
            "required": True,
        }
    if count_bound:
        bindings["count"] = {
            "source": "count",
            "type": "number",
            "node_id": "2",
            "input_name": "batch_size",
        }
    for index in range(reference_count):
        node_id = str(index + 10)
        graph[node_id] = {"class_type": "LoadImage", "inputs": {"image": "old.png"}}
        bindings[f"reference_{index}"] = {
            "source": "reference",
            "type": "image",
            "node_id": node_id,
            "input_name": "image",
            "reference_index": index,
        }
    return {
        "api_graph": graph,
        "bindings": bindings,
        "outputs": ["4"],
        "workflow": {"nodes": [], "links": []},
    }


def configured_provider(config=None, **changes):
    return ImageProvider.from_mapping(
        {
            "id": "comfy",
            "name": "Local Comfy",
            "kind": "comfyui",
            "base_url": "http://comfy.invalid:8188",
            "api_key": "secret-not-in-jobs",
            "models": [
                {
                    "id": "workflow",
                    "name": "Saved workflow",
                    "comfyui": config or workflow(),
                    "tool": {"enabled": True, "max_reference_images": 8},
                }
            ],
            **changes,
        }
    )


class FakeComfyClient:
    def __init__(self):
        self.calls = []
        self.images = (image("red"), image("blue"), image("green"))
        self.history = {
            "status": {"completed": True, "status_str": "success"},
            "outputs": {"4": {"images": [{"filename": "result.png"}]}},
        }
        self.wait_started = asyncio.Event()
        self.release_wait = asyncio.Event()
        self.release_wait.set()
        self.wait_error = None
        self.download_error = None
        self.submit_error = None
        self.cancel_result = {"cancelled": True}
        self.cancel_hook = None
        self.issues = []
        self.upload_started = asyncio.Event()
        self.release_upload = asyncio.Event()
        self.release_upload.set()

    async def inspect(self, provider, config):
        self.calls.append(("inspect", copy.deepcopy(config)))
        return {"issues": copy.deepcopy(self.issues)}

    async def upload_references(self, provider, references, *, config):
        self.calls.append(("upload", tuple(references)))
        self.upload_started.set()
        await self.release_upload.wait()
        return [f"uploaded-{index}.png" for index in range(len(references))]

    async def submit(self, provider, graph, ui_workflow, *, client_id):
        self.calls.append(("submit", copy.deepcopy(graph), client_id))
        if self.submit_error:
            raise self.submit_error
        return {"prompt_id": f"remote-{client_id}", "node_errors": {}}

    async def wait(self, provider, remote_id, *, on_progress, client_id):
        self.calls.append(("wait", remote_id, client_id))
        await on_progress({"status": "running", "value": 1, "max": 10})
        self.wait_started.set()
        await self.release_wait.wait()
        if self.wait_error:
            raise self.wait_error
        return copy.deepcopy(self.history)

    async def download_outputs(self, provider, history, outputs, *, output_limit=None):
        self.calls.append(
            ("download", copy.deepcopy(history), list(outputs), output_limit)
        )
        if self.download_error:
            raise self.download_error
        return self.images[:output_limit] if output_limit is not None else self.images

    async def cancel(self, provider, remote_id):
        self.calls.append(("cancel", remote_id))
        if self.cancel_hook:
            await self.cancel_hook()
        return self.cancel_result


async def runtime_fixture(
    tmp_path, monkeypatch, *, config=None, history=True, native=1, count_default=1
):
    client = FakeComfyClient()
    monkeypatch.setattr(comfyui_runtime, "ComfyClient", lambda _: client)
    provider = configured_provider(config)
    if native != 1 or count_default != 1:
        raw = provider.public_dict()
        raw["models"][0]["native_batch_size"] = native
        raw["models"][0]["native_batch_size_source"] = "manual"
        raw["models"][0]["parameters"]["count"]["default"] = count_default
        provider = ImageProvider.from_mapping(raw)
    settings = RuntimeSettings(
        enable_llm_tool=True,
        providers=(provider,),
        history=HistorySettings(history, 100, 0, True, True),
        revision=0,
        default_page_text2img_model_ref="comfy:workflow",
        default_page_img2img_model_ref="comfy:workflow",
        default_tool_text2img_model_ref="comfy:workflow",
        default_tool_img2img_model_ref="comfy:workflow",
    )
    store = GenerationStore(tmp_path)
    await store.initialize()
    service = ImageGenerationService(
        settings=settings, executor=SimpleNamespace(session=object()), store=store
    )
    runtime = ComfyRuntime(service)
    service.comfy_runtime = runtime
    await runtime.start()
    return runtime, service, provider, client


async def submit(runtime, provider, **changes):
    return await runtime.submit(
        provider=provider,
        model=provider.models[0],
        mode="text2img",
        prompt="",
        **changes,
    )
