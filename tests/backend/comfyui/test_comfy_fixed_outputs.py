"""Fixed execution budgets keep graph inputs intact and never replenish output."""

from __future__ import annotations

import asyncio

import pytest

from astrbot_plugin_image_studio.backend.providers.comfyui.client import (
    ComfyClient,
    ComfyExecutionError,
)
from astrbot_plugin_image_studio.tests.backend.comfyui.test_comfy_batches import (
    fixture,
    submit,
)
from astrbot_plugin_image_studio.tests.backend.comfyui.test_comfy_provider import (
    Response,
    Session,
    png,
    provider,
)


@pytest.mark.parametrize(
    "native,count,actual,quotas,kept",
    [
        (2, 8, [1, 1, 1, 1], [2, 2, 2, 2], 4),
        (2, 8, [3, 3, 3, 3], [2, 2, 2, 2], 8),
        (2, 5, [2, 2, 2], [2, 2, 1], 5),
        (2, 5, [1, 1, 1], [2, 2, 1], 3),
        (2, 5, [1, 3, 3], [2, 2, 1], 4),
        (4, 1, [4], [1], 1),
        (1, 1, [3], [1], 1),
    ],
)
def test_fixed_execution_plan(
    native, count, actual, quotas, kept, tmp_path, monkeypatch
):
    async def run():
        runtime, service, configured, client = await fixture(
            tmp_path, monkeypatch, native=native
        )
        client.output_counts = actual
        try:
            job = await submit(runtime, configured, count)
            completed = await asyncio.wait_for(runtime.manager.wait(job["id"]), 5)
            assert completed["status"] == "succeeded", completed
            result = await runtime.result(completed)
            assert len(result.images) == kept
            assert result.request.count == count
            assert bool(result.warning) is (kept < count)
            submitted = [call[1] for call in client.calls if call[0] == "submit"]
            assert len(submitted) == len(quotas)
            assert all(graph["2"]["inputs"]["batch_size"] == 3 for graph in submitted)
            assert sorted(client.collection_limits) == sorted(quotas)
            assert (
                len(
                    (await service.store.generation_detail(result.generation_id))[
                        "images"
                    ]
                )
                == kept
            )
            if len(quotas) > 1:
                assert completed["result"]["batch_plan"]["quotas"] == quotas
            else:
                assert completed["result"]["collection_limit"] == quotas[0]
        finally:
            await runtime.close()

    asyncio.run(run())


def history_outputs():
    def entries(*names):
        return {"images": [{"filename": name, "type": "output"} for name in names]}

    return {
        "outputs": {
            "first": entries("one.png", "two.png"),
            "second": entries("three.png", "four.png"),
        }
    }


def test_output_quota_is_shared_across_selected_nodes_and_applied_before_download():
    session = Session([Response(raw=png()) for _ in range(3)])
    images = asyncio.run(
        ComfyClient(session).download_outputs(
            provider(), history_outputs(), ["second", "first"], output_limit=3
        )
    )
    assert len(images) == 3
    assert [call[2]["params"]["filename"] for call in session.calls] == [
        "three.png",
        "four.png",
        "one.png",
    ]
    assert [image.effective_parameters["comfyui_output_node"] for image in images] == [
        "second",
        "second",
        "first",
    ]


def test_download_failure_does_not_take_another_result_beyond_reserved_positions():
    session = Session([Response(raw=b"missing", status=404), Response(raw=png())])
    with pytest.raises(ComfyExecutionError) as caught:
        asyncio.run(
            ComfyClient(session).download_outputs(
                provider(), history_outputs(), ["first", "second"], output_limit=2
            )
        )
    assert len(caught.value.images) == 1
    assert len(session.calls) == 2
    assert [call[2]["params"]["filename"] for call in session.calls] == [
        "one.png",
        "two.png",
    ]


def test_short_output_is_returned_without_downloader_error():
    session = Session([Response(raw=png()) for _ in range(2)])
    images = asyncio.run(
        ComfyClient(session).download_outputs(
            provider(), history_outputs(), ["first"], output_limit=8
        )
    )
    assert len(images) == 2
    assert len(session.calls) == 2


def test_legacy_download_without_quota_retains_all_images():
    session = Session([Response(raw=png()) for _ in range(4)])
    images = asyncio.run(
        ComfyClient(session).download_outputs(
            provider(), history_outputs(), ["first", "second"]
        )
    )
    assert len(images) == 4
