"""UI synchronization survives submission, execution writeback and recovery."""

from __future__ import annotations

import asyncio
import copy
import io
import json

import pytest
from astrbot_plugin_image_studio.backend.models import GeneratedImage, ReferenceImage
from astrbot_plugin_image_studio.backend.providers.comfyui.client import (
    ComfyClient,
    image_workflow_snapshot,
)
from astrbot_plugin_image_studio.backend.providers.comfyui.runtime import ComfyRuntime
from astrbot_plugin_image_studio.backend.providers.comfyui.workflows import (
    normalize_workflow,
)
from astrbot_plugin_image_studio.tests.backend.test_comfy_provider import (
    provider,
    request,
)
from astrbot_plugin_image_studio.tests.backend.test_comfy_runtime import (
    image,
    runtime_fixture,
    workflow,
)
from astrbot_plugin_image_studio.tests.backend.test_comfy_ui_sync import fixture, node
from PIL import Image, PngImagePlugin


def png_with_workflow(graph, ui):
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("prompt", json.dumps(graph))
    metadata.add_text("workflow", json.dumps(ui))
    buffer = io.BytesIO()
    Image.new("RGB", (24, 24), "green").save(buffer, "PNG", pnginfo=metadata)
    return GeneratedImage(buffer.getvalue(), "image/png")


def random_submission():
    config = fixture()
    config["api_graph"]["2"]["inputs"]["seed"] = -1
    node(config, 2)["widgets_values"][0] = -1
    config["workflow_json"] = json.dumps(config["workflow"])
    return config


def test_actual_seed_and_display_writebacks_are_kept_without_touching_image_bytes():
    submitted = random_submission()
    original = copy.deepcopy(submitted)
    graph, ui = (
        copy.deepcopy(submitted["api_graph"]),
        copy.deepcopy(submitted["workflow"]),
    )
    graph["2"]["inputs"]["seed"] = 7788
    graph["2"]["is_changed"] = [7788]
    node({"workflow": ui}, 2)["widgets_values"][0] = 7788
    node({"workflow": ui}, 3)["widgets_values"] = [["newly evaluated text"]]
    output = png_with_workflow(graph, ui)
    content = output.data
    saved = image_workflow_snapshot(output, submitted)
    assert saved["api_graph"]["2"]["inputs"]["seed"] == 7788
    assert "is_changed" not in saved["api_graph"]["2"]
    assert node(saved, 2)["widgets_values"][0] == 7788
    assert node(saved, 3)["widgets_values"] == [["newly evaluated text"]]
    assert json.loads(saved["workflow_json"]) == saved["workflow"]
    assert submitted == original
    assert output.data is content
    assert not saved["workflow_sync_warnings"]


@pytest.mark.parametrize(
    "alteration", ["prompt", "connection", "class", "missing", "input_is_changed"]
)
def test_stale_or_unrelated_returned_prompt_cannot_replace_submission(alteration):
    submitted = random_submission()
    graph = copy.deepcopy(submitted["api_graph"])
    graph["2"]["inputs"]["seed"] = 8899
    if alteration == "prompt":
        graph["1"]["inputs"]["text"] = "stale text"
    elif alteration == "connection":
        graph["3"]["inputs"]["anything"] = ["2", 0]
    elif alteration == "class":
        graph["2"]["class_type"] = "OtherSeed"
    elif alteration == "input_is_changed":
        graph["2"]["inputs"]["is_changed"] = "must remain a real input"
    else:
        graph.pop("1")
    saved = image_workflow_snapshot(
        png_with_workflow(graph, submitted["workflow"]), submitted
    )
    assert saved["api_graph"] == submitted["api_graph"]
    assert saved["workflow"] == submitted["workflow"]
    assert any("实际随机种子" in text for text in saved["workflow_sync_warnings"])


def test_valid_seed_does_not_make_unrelated_ui_widget_changes_authoritative():
    submitted = random_submission()
    graph, ui = (
        copy.deepcopy(submitted["api_graph"]),
        copy.deepcopy(submitted["workflow"]),
    )
    graph["2"]["inputs"]["seed"] = 9999
    node({"workflow": ui}, 2)["widgets_values"][0] = 9999
    node({"workflow": ui}, 1)["widgets_values"] = ["unrelated stale prompt"]
    saved = image_workflow_snapshot(png_with_workflow(graph, ui), submitted)
    assert node(saved, 2)["widgets_values"][0] == 9999
    assert node(saved, 1)["widgets_values"] == node(submitted, 1)["widgets_values"]


def executable(*, references=0):
    config = workflow(prompt_bound=True, reference_count=references)
    config["api_graph"]["91"] = {
        "class_type": "Seed (rgthree)",
        "inputs": {"seed": 123},
    }
    config["api_graph"]["3"]["inputs"]["seed"] = ["91", 0]
    config["workflow"]["nodes"] = [
        {"id": 1, "type": "CLIPTextEncode", "widgets_values": ["fixed prompt"]},
        {"id": 91, "type": "Seed (rgthree)", "widgets_values": [123, "", "", ""]},
        *[
            {
                "id": 10 + index,
                "type": "LoadImage",
                "widgets_values": ["old.png", "image"],
            }
            for index in range(references)
        ],
    ]
    config["input_overrides"] = [{"node_id": "91", "input_name": "seed", "value": -1}]
    return config


@pytest.mark.parametrize("persisted_tracking", [True, False])
def test_fallback_execute_submits_and_records_final_bound_values_after_uploads(
    persisted_tracking,
):
    class Client(ComfyClient):
        async def inspect(self, _provider, _config):
            return {"status": "ready", "issues": []}

        async def upload_references(self, _provider, references, *, config):
            assert len(references) == 1
            return ["server/new-name.png"]

        async def submit(self, _provider, graph, ui, **_kwargs):
            self.graph, self.ui = copy.deepcopy(graph), copy.deepcopy(ui)
            assert node({"workflow": ui}, 1)["widgets_values"] == ["a dog"]
            assert node({"workflow": ui}, 91)["widgets_values"][0] == -1
            assert (
                node({"workflow": ui}, 10)["widgets_values"][0] == "server/new-name.png"
            )
            return {"prompt_id": "test", "node_errors": {}}

        async def wait(self, *_args, **_kwargs):
            return {}

        async def download_outputs(self, *_args, **_kwargs):
            graph, ui = copy.deepcopy(self.graph), copy.deepcopy(self.ui)
            graph["91"]["inputs"]["seed"] = 112233
            node({"workflow": ui}, 91)["widgets_values"][0] = 112233
            return (png_with_workflow(graph, ui),)

    config = executable(references=1)
    if not persisted_tracking:
        config.pop("input_overrides")
        config["api_graph"]["91"]["inputs"]["seed"] = -1
    original = copy.deepcopy(config)
    prepared = []
    client = Client(object())
    images = asyncio.run(
        client.execute(
            provider(),
            request(
                references=(
                    ReferenceImage("test", "test.png", image("red").data, "image/png"),
                )
            ),
            config=config,
            on_prepared=prepared.append,
        )
    )
    assert node(prepared[0], 91)["widgets_values"][0] == -1
    snapshot = images[0].effective_parameters["_comfyui"]
    assert snapshot["api_graph"]["91"]["inputs"]["seed"] == 112233
    assert node(snapshot, 91)["widgets_values"][0] == 112233
    assert config == original


def test_durable_submission_persists_synced_ui_and_recovers_without_resubmission(
    tmp_path, monkeypatch
):
    async def run():
        config = executable()
        runtime, service, selected, client = await runtime_fixture(
            tmp_path, monkeypatch, config=config
        )
        client.release_wait.clear()
        original_submit = client.submit
        submitted_ui = []

        async def capture(provider, graph, ui, *, client_id):
            submitted_ui.append(copy.deepcopy(ui))
            current = copy.deepcopy(graph)
            returned_ui = copy.deepcopy(ui)
            current["91"]["inputs"]["seed"] = 3456
            node({"workflow": returned_ui}, 91)["widgets_values"][0] = 3456
            client.images = (png_with_workflow(current, returned_ui),)
            return await original_submit(provider, graph, ui, client_id=client_id)

        client.submit = capture
        recovered = None
        try:
            job = await runtime.submit(
                provider=selected,
                model=selected.models[0],
                mode="text2img",
                prompt="changed prompt",
            )
            await asyncio.wait_for(client.wait_started.wait(), 2)
            pending = await runtime.store.get_job(job["id"])
            assert node(pending["result"], 91)["widgets_values"][0] == -1
            assert node(pending["result"], 1)["widgets_values"] == ["changed prompt"]
            await runtime.close()
            client.release_wait.set()
            recovered = ComfyRuntime(service)
            await recovered.start()
            done = await recovered.manager.wait(job["id"])
            assert done["status"] == "succeeded", done
            restored = await recovered.result(done)
            snapshot = restored.images[0].effective_parameters["_comfyui"]
            assert node(snapshot, 91)["widgets_values"][0] == 3456
            assert node(snapshot, 1)["widgets_values"] == ["changed prompt"]
            assert len(submitted_ui) == 1
            assert node(selected.models[0].comfyui, 91)["widgets_values"][0] == 123
        finally:
            if recovered:
                await recovered.close()
            await runtime.close()

    asyncio.run(run())


def test_sync_warning_keeps_successful_job_successful(tmp_path, monkeypatch):
    async def run():
        config = executable()
        config["workflow"]["nodes"] = []
        runtime, service, _selected, _client = await runtime_fixture(
            tmp_path, monkeypatch, config=config
        )
        try:
            result = await service.generate(
                mode="text2img", provider_id="comfy", model="workflow", prompt="new"
            )
            assert result.images
            assert result.warning
            assert all(
                job["status"] == "succeeded" for job in await runtime.store.list_jobs()
            )
            assert result.images[0].effective_parameters["_comfyui"][
                "workflow_sync_warnings"
            ]
        finally:
            await runtime.close()

    asyncio.run(run())


def test_fallback_resume_preserves_prior_warning_without_preparing_again():
    class Client(ComfyClient):
        async def inspect(self, *_args):
            raise AssertionError("resume must not inspect again")

        async def upload_references(self, *_args, **_kwargs):
            raise AssertionError("resume must not upload again")

        async def submit(self, *_args, **_kwargs):
            raise AssertionError("resume must not resubmit")

        async def wait(self, *_args, **_kwargs):
            return {}

        async def download_outputs(self, *_args, **_kwargs):
            return (image("blue"),)

    config = normalize_workflow(executable())
    config["workflow_sync_warnings"] = ["已提交时无法同步节点 #1", None]
    results = asyncio.run(
        Client(object()).execute(provider(), request(), config=config, resume_id="job")
    )
    snapshot = results[0].effective_parameters["_comfyui"]
    assert "已提交时无法同步节点 #1" in snapshot["workflow_sync_warnings"]
    assert None not in snapshot["workflow_sync_warnings"]
    assert snapshot["api_graph"] == config["api_graph"]


def test_batch_children_keep_distinct_execution_seed_and_ui_snapshots(
    tmp_path, monkeypatch
):
    async def run():
        config = executable()
        runtime, service, selected, client = await runtime_fixture(
            tmp_path, monkeypatch, config=config
        )
        submitted = {}

        async def capture(_provider, graph, ui, *, client_id):
            remote_id = "result-" + client_id
            assert node({"workflow": ui}, 91)["widgets_values"][0] == -1
            seed = 9000 + len(submitted)
            actual, actual_ui = copy.deepcopy(graph), copy.deepcopy(ui)
            actual["91"]["inputs"]["seed"] = seed
            node({"workflow": actual_ui}, 91)["widgets_values"][0] = seed
            submitted[remote_id] = png_with_workflow(actual, actual_ui)
            return {"prompt_id": remote_id, "node_errors": {}}

        async def wait(_provider, remote_id, **_kwargs):
            return {"remote_id": remote_id}

        async def download(_provider, history, _outputs, **_kwargs):
            return (submitted[history["remote_id"]],)

        client.submit, client.wait, client.download_outputs = capture, wait, download
        try:
            result = await service.generate(
                mode="text2img",
                provider_id=selected.id,
                model="workflow",
                prompt="new",
                count=3,
            )
            assert len(result.images) == 3
            snapshots = [
                image.effective_parameters["_comfyui"] for image in result.images
            ]
            assert {
                snapshot["api_graph"]["91"]["inputs"]["seed"] for snapshot in snapshots
            } == {9000, 9001, 9002}
            assert {
                node(snapshot, 91)["widgets_values"][0] for snapshot in snapshots
            } == {9000, 9001, 9002}
            assert not result.warning
        finally:
            await runtime.close()

    asyncio.run(run())
