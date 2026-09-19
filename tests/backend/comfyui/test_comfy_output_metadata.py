"""Returned PNGs retain pixels and restore the final seed in both widget forms."""

from __future__ import annotations

import copy
import io
import json
import struct
import zlib
from dataclasses import replace

import pytest
from astrbot_plugin_image_studio.backend.providers.comfyui.client import (
    prepare_submission_workflow,
)
from astrbot_plugin_image_studio.backend.providers.comfyui.global_seed import (
    global_seed_workflow_writeback,
)
from astrbot_plugin_image_studio.backend.providers.comfyui.output_metadata import (
    prepare_output,
)
from astrbot_plugin_image_studio.tests.backend.comfyui.test_comfy_global_seed import (
    fixture as global_fixture,
)
from astrbot_plugin_image_studio.tests.backend.comfyui.test_comfy_global_seed import (
    returned as global_returned,
)
from astrbot_plugin_image_studio.tests.support.comfy_runtime import image
from astrbot_plugin_image_studio.tests.backend.comfyui.test_comfy_sync_execution import (
    node,
    random_submission,
)
from PIL import Image, PngImagePlugin


def chunks(data):
    result, offset = [], 8
    while offset < len(data):
        length = struct.unpack_from(">I", data, offset)[0]
        end = offset + length + 12
        result.append((data[offset + 4 : offset + 8], data[offset:end]))
        offset = end
    return result


def png(graph, ui, *, text_kind="plain"):
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("prompt", json.dumps(graph), zip=True)
    metadata.add_text("Author", "preserve author")
    metadata.add_text("parameters", "other tool metadata")
    if text_kind == "international":
        metadata.add_itxt("workflow", json.dumps(ui, ensure_ascii=False), zip=True)
    else:
        metadata.add_text("workflow", json.dumps(ui), zip=text_kind == "compressed")
    buffer = io.BytesIO()
    Image.new("RGB", (32, 24), "blue").save(buffer, "PNG", pnginfo=metadata)
    return replace(image("blue"), data=buffer.getvalue())


def setup(*, named_only=False):
    submitted = random_submission()
    seed = node(submitted, 2)
    seed["widgets_values_named"] = {"seed": -1, "control_after_generate": "fixed"}
    if named_only:
        seed.pop("widgets_values")
    display = node(submitted, 3)
    display["widgets_values_named"] = {"text": "current runtime snapshot"}
    submitted["workflow_json"] = json.dumps(submitted["workflow"])
    returned_graph = copy.deepcopy(submitted["api_graph"])
    returned_graph["2"]["inputs"]["seed"] = 987654321
    returned_ui = copy.deepcopy(submitted["workflow"])
    returned_seed = node({"workflow": returned_ui}, 2)
    if not named_only:
        returned_seed["widgets_values"][0] = 987654321
    node({"workflow": returned_ui}, 3)["widgets_values"] = [["本次显示内容"]]
    return submitted, returned_graph, returned_ui


@pytest.mark.parametrize("text_kind", ["plain", "compressed", "international"])
@pytest.mark.parametrize("named_only", [False, True])
def test_png_download_restores_actual_seed_and_display_in_named_mode_without_reencoding(
    text_kind, named_only
):
    submitted, graph, ui = setup(named_only=named_only)
    before = copy.deepcopy(submitted)
    output = png(graph, ui, text_kind=text_kind)
    result = prepare_output(output, submitted)
    assert result.data != output.data
    with Image.open(io.BytesIO(result.data)) as restored:
        restored_ui = json.loads(restored.text["workflow"])
        assert json.loads(restored.text["prompt"]) == graph
        assert restored.text["Author"] == "preserve author"
        assert restored.text["parameters"] == "other tool metadata"
        assert restored.getpixel((0, 0)) == (0, 0, 255)
    assert (
        node({"workflow": restored_ui}, 2)["widgets_values_named"]["seed"] == 987654321
    )
    assert (
        node({"workflow": restored_ui}, 3)["widgets_values_named"]["text"]
        == "本次显示内容"
    )
    original_chunks = [raw for kind, raw in chunks(output.data) if kind == b"IDAT"]
    assert [
        raw for kind, raw in chunks(result.data) if kind == b"IDAT"
    ] == original_chunks

    # Every other ancillary chunk and prompt payload is copied verbatim.
    def non_workflow(data):
        return [
            raw
            for kind, raw in chunks(data)
            if not (
                kind in {b"tEXt", b"zTXt", b"iTXt"}
                and raw[8:].startswith(b"workflow\x00")
            )
        ]

    assert non_workflow(result.data) == non_workflow(output.data)
    snapshot = result.effective_parameters["_comfyui"]
    assert snapshot["workflow"] == restored_ui
    assert not snapshot.get("workflow_sync_warnings")
    assert submitted == before


@pytest.mark.parametrize(
    "change", ["named_seed", "array_seed", "prompt", "links", "layout"]
)
def test_unverifiable_result_keeps_original_bytes_with_warning(change):
    submitted, graph, ui = setup()
    if change == "named_seed":
        node({"workflow": ui}, 2)["widgets_values_named"]["seed"] = 555
    elif change == "array_seed":
        node({"workflow": ui}, 2)["widgets_values"][0] = 555
    elif change == "prompt":
        graph["1"]["inputs"]["text"] = "unrelated prompt"
    elif change == "links":
        ui["links"] = [[1, 1, 0, 2, 0, "INT"]]
    else:
        node({"workflow": ui}, 2)["widgets_values"].append("unknown layout")
    output = png(graph, ui)
    result = prepare_output(output, submitted)
    assert result.data == output.data
    assert any(
        "原文件保持不变" in text
        for text in result.effective_parameters["_comfyui"]["workflow_sync_warnings"]
    )


def test_malformed_png_and_duplicate_metadata_are_preserved():
    submitted, graph, ui = setup()
    valid = png(graph, ui)
    damaged = bytearray(valid.data)
    damaged[-1] ^= 1
    results = [prepare_output(replace(valid, data=bytes(damaged)), submitted)]
    workflow_chunk = next(
        raw
        for kind, raw in chunks(valid.data)
        if kind == b"tEXt" and raw[8:].startswith(b"workflow\x00")
    )
    duplicate = valid.data[:-12] + workflow_chunk + valid.data[-12:]
    results.append(prepare_output(replace(valid, data=duplicate), submitted))
    assert results[0].data == bytes(damaged)
    assert results[1].data == duplicate
    assert all(
        result.effective_parameters["_comfyui"]["workflow_sync_warnings"]
        for result in results
    )


def test_metadata_free_png_is_not_enriched_or_reencoded():
    submitted, _graph, _ui = setup()
    original = image("red")
    result = prepare_output(original, submitted)
    assert result.data is original.data
    with Image.open(io.BytesIO(result.data)) as restored:
        assert not restored.text


def test_webp_retains_bytes_and_exif_with_explicit_format_boundary():
    submitted, graph, ui = setup()
    metadata = Image.Exif()
    metadata[0x010F] = "prompt:" + json.dumps(graph)
    metadata[0x0110] = "workflow:" + json.dumps(ui)
    buffer = io.BytesIO()
    Image.new("RGB", (24, 24), "green").save(buffer, "WEBP", exif=metadata)
    original = replace(image("green"), data=buffer.getvalue(), mime_type="image/webp")
    result = prepare_output(original, submitted)
    assert result.data is original.data
    assert any(
        "仅支持无损同步 PNG" in text
        for text in result.effective_parameters["_comfyui"]["workflow_sync_warnings"]
    )


def test_large_compressed_workflow_is_bounded_without_touching_pixels():
    submitted, graph, ui = setup()
    valid = png(graph, ui)
    payload = b"workflow\x00\x00" + zlib.compress(b" " * (4 * 1024 * 1024 + 1))
    body = b"zTXt" + payload
    chunk = (
        struct.pack(">I", len(payload))
        + body
        + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    )
    malicious = valid.data[:8] + b"".join(
        chunk if kind == b"tEXt" and raw[8:].startswith(b"workflow\x00") else raw
        for kind, raw in chunks(valid.data)
    )
    result = prepare_output(replace(valid, data=malicious), submitted)
    assert result.data == malicious
    assert result.effective_parameters["_comfyui"]["workflow_sync_warnings"]


@pytest.mark.parametrize("action,value", [("randomize", 556677), ("fixed", 12)])
def test_global_seed_hook_recovery_repairs_named_fields_and_expanded_prompt(
    action, value
):
    graph, ui = global_fixture(action=action)
    node({"workflow": ui}, 1)["widgets_values_named"] = {
        "value": 12,
        "mode": True,
        "action": action,
        "last_seed": "previous",
    }
    node({"workflow": ui}, 2)["widgets_values_named"] = {"seed": 33}
    node({"workflow": ui}, 3)["widgets_values_named"] = {
        "text": graph["3"]["inputs"]["text"]
    }
    config = {"api_graph": graph, "workflow": ui}
    submitted_ui = prepare_submission_workflow(config, graph)
    assert not submitted_ui["warnings"]
    assert submitted_ui["workflow"]["seed_widgets"] == {"2": 0}
    submitted = {**config, "workflow": submitted_ui["workflow"]}
    actual = global_returned(graph, value)
    hook_ui = global_seed_workflow_writeback(graph, actual, submitted["workflow"])
    original = png(actual, hook_ui)
    result = prepare_output(original, submitted)
    snapshot = result.effective_parameters["_comfyui"]
    assert not snapshot["workflow_sync_warnings"]
    assert snapshot["api_graph"] == actual
    with Image.open(io.BytesIO(result.data)) as restored:
        corrected_ui = json.loads(restored.text["workflow"])
    assert node({"workflow": corrected_ui}, 1)["widgets_values_named"]["value"] == value
    assert (
        node({"workflow": corrected_ui}, 1)["widgets_values_named"]["last_seed"] == 12
    )
    assert node({"workflow": corrected_ui}, 2)["widgets_values_named"]["seed"] == value
    assert node({"workflow": corrected_ui}, 3)["widgets_values_named"][
        "text"
    ] == "seed=" + str(value)
    assert [raw for kind, raw in chunks(original.data) if kind == b"IDAT"] == [
        raw for kind, raw in chunks(result.data) if kind == b"IDAT"
    ]
