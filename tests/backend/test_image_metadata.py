from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image, PngImagePlugin

from astrbot_plugin_image_studio.backend.metadata.parser import (
    MAX_METADATA_BYTES,
    PARSER_VERSION,
    parse_image_metadata,
    parse_metadata_fields,
    parse_parameter_text,
)


def encoded_image(
    format: str = "PNG", *, fields: dict | None = None, comment: bytes | None = None
) -> bytes:
    output = io.BytesIO()
    kwargs = {}
    if fields:
        pnginfo = PngImagePlugin.PngInfo()
        for key, value in fields.items():
            pnginfo.add_text(key, value)
        kwargs["pnginfo"] = pnginfo
    if comment is not None:
        exif = Image.Exif()
        exif[37510] = comment
        kwargs["exif"] = exif
    Image.new("RGB", (24, 32), "white").save(output, format, **kwargs)
    return output.getvalue()


def api_graph() -> dict:
    return {
        "1": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": "example.safetensors"},
        },
        "2": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": "positive", "clip": ["1", 1]},
        },
        "3": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": "negative", "clip": ["1", 1]},
        },
        "4": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": 832, "height": 1216},
        },
        "5": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["1", 0],
                "positive": ["2", 0],
                "negative": ["3", 0],
                "latent_image": ["4", 0],
                "seed": 18446744073709551615,
                "steps": 28,
                "cfg": 5.5,
                "sampler_name": "euler",
                "scheduler": "normal",
                "denoise": 1,
            },
        },
        "6": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["5", 0], "vae": ["1", 2]},
        },
        "7": {"class_type": "SaveImage", "inputs": {"images": ["6", 0]}},
    }


def test_novelai_preserves_raw_and_different_file_dimensions() -> None:
    parameters = {
        "prompt": "artist, a landscape",
        "uc": "low quality",
        "seed": 123,
        "width": 832,
        "height": 1216,
        "scale": 6,
        "cfg_rescale": 0.3,
        "steps": 26,
        "sampler": "k_euler_ancestral",
        "request_type": "PromptGenerateRequest",
    }
    comment = json.dumps(parameters)
    result = parse_image_metadata(
        encoded_image(fields={"Software": "NovelAI", "Comment": comment})
    )
    assert result["format"] == "novelai"
    assert result["raw"]["Comment"] == comment
    normalized = result["normalized"]
    assert normalized["file_dimensions"] == {"width": 24, "height": 32}
    assert (normalized["width"], normalized["height"]) == (832, 1216)
    assert normalized["guidance_scale"] == 6
    assert normalized["cfg_rescale"] == 0.3
    assert normalized["negative_prompt"] == "low quality"
    assert normalized["mode"] == "text2img"


@pytest.mark.parametrize("image_format", ["JPEG", "WEBP"])
@pytest.mark.parametrize(
    "encoding,header",
    [
        ("utf-16-be", b"UNICODE\x00"),
        ("utf-16-le", b"UNICODE\x00"),
        ("utf-16", b"UNICODE\x00"),
        ("utf-8", b"ASCII\x00\x00\x00"),
    ],
)
def test_exif_infotext_decoding(
    image_format: str, encoding: str, header: bytes
) -> None:
    text = 'a landscape, <lora:light:0.7>\nNegative prompt: blurry, distorted\nSteps: 30, Sampler: DPM++ 2M, Schedule type: Karras, CFG scale: 7, Seed: 18446744073709551615, Size: 1440x1440, Model: "Model, with comma", Denoising strength: 0.63'
    result = parse_image_metadata(
        encoded_image(image_format, comment=header + text.encode(encoding))
    )
    assert result["format"] == "a1111"
    normalized = result["normalized"]
    assert normalized["model"] == "Model, with comma"
    assert normalized["seed"] == "18446744073709551615"
    assert normalized["mode"] == "unknown"
    assert normalized["loras"] == [{"name": "light", "strength": 0.7}]
    assert normalized["width"] == 1440
    assert normalized["file_dimensions"]["width"] == 24
    assert result["raw"]["UserComment"] == text


def test_comfyui_uses_output_ancestors_and_preserves_large_integer_text() -> None:
    graph = api_graph()
    graph["90"] = {
        "class_type": "LoraLoader",
        "inputs": {"lora_name": "unconnected.safetensors", "strength_model": 1},
    }
    graph["91"] = {"class_type": "KSampler", "inputs": {"seed": 99, "steps": 2}}
    text = json.dumps(graph)
    result = parse_metadata_fields({"prompt": text})
    assert result["format"] == "comfyui"
    assert result["raw"]["prompt"] == text
    normalized = result["normalized"]
    assert normalized["seed"] == "18446744073709551615"
    assert normalized["steps"] == 28
    assert normalized["model"] == "example.safetensors"
    assert normalized["mode"] == "text2img"
    assert normalized["width"] == 832
    assert "loras" not in normalized
    assert len(normalized["stages"]) == 1


def test_comfyui_dynamic_prompt_is_not_guessed_and_disabled_loras_excluded() -> None:
    graph = api_graph()
    graph["10"] = {
        "class_type": "Raffle",
        "inputs": {"seed": 12, "text": "not the executed prompt"},
    }
    graph["2"]["inputs"]["text"] = ["10", 0]
    graph["11"] = {
        "class_type": "Power Lora Loader (rgthree)",
        "inputs": {
            "model": ["1", 0],
            "lora_1": {"on": False, "lora": "disabled", "strength": 1},
            "lora_2": {"on": True, "lora": "active", "strength": 0.7},
        },
    }
    graph["5"]["inputs"]["model"] = ["11", 0]
    result = parse_metadata_fields({"prompt": json.dumps(graph)})
    assert "prompt" not in result["normalized"]
    assert [item["name"] for item in result["normalized"]["loras"]] == ["active"]
    assert any(
        "Raffle" in warning and "10" in warning for warning in result["warnings"]
    )


def test_comfyui_multistage_keeps_per_stage_steps() -> None:
    graph = api_graph()
    graph["8"] = {
        "class_type": "LatentUpscaleBy",
        "inputs": {"samples": ["5", 0], "scale_by": 1.5},
    }
    graph["9"] = {
        "class_type": "KSampler",
        "inputs": {
            **graph["5"]["inputs"],
            "latent_image": ["8", 0],
            "steps": 15,
            "denoise": 0.5,
        },
    }
    graph["6"]["inputs"]["samples"] = ["9", 0]
    result = parse_metadata_fields({"prompt": json.dumps(graph)})
    normalized = result["normalized"]
    assert "steps" not in normalized
    assert [stage["steps"] for stage in normalized["stages"]] == [28, 15]
    assert normalized["stages"][1]["width"] == 1248
    assert any("多个采样" in warning for warning in result["warnings"])


def test_unknown_image_does_not_invent_generation_parameters() -> None:
    result = parse_image_metadata(encoded_image())
    assert result["format"] == "unknown"
    assert result["normalized"] == {
        "mode": "unknown",
        "file_dimensions": {"width": 24, "height": 32},
    }
    malformed = parse_metadata_fields({"Software": "NovelAI", "Comment": "{"})
    assert malformed["format"] == "novelai"
    assert malformed["warnings"]


def test_parser_enforces_limits_and_rejects_cycles() -> None:
    with pytest.raises(ValueError, match="4 MiB"):
        parse_metadata_fields({"Comment": "x" * MAX_METADATA_BYTES})
    graph = api_graph()
    graph["5"]["inputs"]["latent_image"] = ["6", 0]
    with pytest.raises(ValueError, match="循环"):
        parse_metadata_fields({"prompt": json.dumps(graph)})
    with pytest.raises(ValueError):
        parse_image_metadata(b"not an image")


def test_parameter_text_recognizes_exact_request_and_nai_cfg_mapping() -> None:
    request = {
        "model_ref": "provider:model",
        "prompt": "example",
        "parameters": {"artist": "", "style": "custom", "cfg": 0},
    }
    copied = json.dumps({"format": "image_studio", "version": 1, "data": request})
    result = parse_parameter_text(copied)
    assert result["request"] == request
    assert result["normalized"]["prompt"] == "example"
    nai = parse_parameter_text('{"tag":"example","negative":"bad","scale":6,"cfg":0.3}')
    assert nai["normalized"]["guidance_scale"] == 6
    assert nai["normalized"]["cfg_rescale"] == 0.3
    assert nai["request"]["negative"] == "bad"
    with pytest.raises(ValueError, match="无法识别"):
        parse_parameter_text("this is just a prompt")


def test_parameter_text_workflow_and_a1111_json() -> None:
    assert parse_parameter_text(json.dumps(api_graph()))["format"] == "comfyui"
    result = parse_parameter_text(
        '{"Steps":30,"CFG scale":7,"Size":"1440x1440","prompt":"scene"}'
    )
    assert result["format"] == "a1111"
    assert result["normalized"]["guidance_scale"] == 7
    assert result["normalized"]["width"] == 1440


@pytest.mark.parametrize("image_format", ["WEBP", "JPEG"])
def test_converted_comfyui_exif_labels_keep_original_workflow(
    image_format: str,
) -> None:
    graph = api_graph()
    prompt = json.dumps(graph, ensure_ascii=False, indent=1)
    workflow = '{ "nodes": [], "links": [], "extra": { "seed": 18446744073709551615 } }'
    exif = Image.Exif()
    exif[305] = "PNG2WebP-AI-Metadata-Converter"
    exif[270] = "Workflow: " + workflow
    exif[271] = "Prompt: " + prompt
    output = io.BytesIO()
    Image.new("RGB", (24, 32), "white").save(output, image_format, exif=exif)
    result = parse_image_metadata(output.getvalue())
    assert result["format"] == "comfyui"
    assert result["parser_version"] == PARSER_VERSION == 9
    assert result["raw"]["ImageDescription"] == "Workflow: " + workflow
    assert result["raw"]["Make"] == "Prompt: " + prompt
    assert result["raw"]["workflow"] == workflow
    assert result["raw"]["prompt"] == prompt
    assert result["normalized"]["seed"] == "18446744073709551615"
    assert result["normalized"]["model"] == "example.safetensors"


@pytest.mark.parametrize(
    "tag", ["UserComment", "ImageDescription", "Artist", "Make", "Model"]
)
def test_nested_exif_metadata_preserves_json_member_text(tag: str) -> None:
    graph_text = json.dumps(api_graph(), indent=1)
    workflow_text = (
        '{ "nodes": [], "links": [], "extra": { "seed": 18446744073709551615 } }'
    )
    payload = (
        '{"metadata":{"workflow":' + workflow_text + ',"prompt":' + graph_text + "}}"
    )
    result = parse_metadata_fields({tag: payload})
    assert result["format"] == "comfyui"
    assert result["raw"][tag] == payload
    assert result["raw"]["prompt"] == graph_text
    assert result["raw"]["workflow"] == workflow_text
    assert result["normalized"]["seed"] == "18446744073709551615"


def test_embedded_text_does_not_overwrite_native_parameter_keys() -> None:
    native = json.dumps(api_graph())
    other = api_graph()
    other["5"]["inputs"]["seed"] = 1
    result = parse_metadata_fields(
        {"prompt": native, "Make": "Prompt: " + json.dumps(other)}
    )
    assert result["raw"]["prompt"] == native
    assert result["normalized"]["seed"] == "18446744073709551615"
    unknown = parse_metadata_fields(
        {
            "Make": "Camera Maker",
            "Model": "Example Camera",
            "ImageDescription": "Prompt: a landscape photograph",
        }
    )
    assert unknown["format"] == "unknown"
    assert unknown["normalized"] == {"mode": "unknown"}


def test_original_exif_creation_time_wins_and_preserves_timezone_and_subseconds() -> (
    None
):
    result = parse_metadata_fields(
        {
            "DateTimeOriginal": "2026:09:05 19:20:30",
            "OffsetTimeOriginal": "+08:00",
            "SubSecTimeOriginal": "1234",
            "DateTimeDigitized": "2026:09:06 12:00:00",
            "Creation Time": "2026-09-07T00:00:00Z",
            "DateTime": "2026:09:08 12:00:00",
        }
    )
    normalized = result["normalized"]
    expected = datetime(2026, 9, 5, 11, 20, 30, 123400, tzinfo=timezone.utc).timestamp()
    assert normalized["generated_at"] == expected
    assert normalized["generated_at_source"] == "DateTimeOriginal"
    assert "generated_at_timezone_assumed" not in normalized
    assert result["warnings"] == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("Creation Time", "Sat, 05 Sep 2026 11:20:30 GMT"),
        ("CreationTime", "2026-09-05T19:20:30+08:00"),
        ("DateTimeDigitized", "2026-09-05T11:20:30Z"),
        ("date:create", "2026-09-05T11:20:30+00:00"),
    ],
)
def test_embedded_creation_dates_accept_standard_formats(
    field: str, value: str
) -> None:
    result = parse_metadata_fields({field: value})
    assert (
        result["normalized"]["generated_at"]
        == datetime(2026, 9, 5, 11, 20, 30, tzinfo=timezone.utc).timestamp()
    )
    assert result["normalized"]["generated_at_source"] == field


def test_invalid_creation_date_falls_back_and_timezone_assumption_is_visible() -> None:
    result = parse_metadata_fields(
        {
            "DateTimeOriginal": "0000:00:00 00:00:00",
            "Creation Time": "2026-09-05 11:20:30",
        }
    )
    assert result["normalized"]["generated_at_source"] == "Creation Time"
    assert result["normalized"]["generated_at_timezone_assumed"] is True
    assert any(
        "DateTimeOriginal" in warning and "无效" in warning
        for warning in result["warnings"]
    )
    assert any("UTC" in warning for warning in result["warnings"])
    no_timestamp = parse_metadata_fields(
        {
            "Generation time": "3.9996",
            "lastModified": 1700000000000,
            "DateTimeOriginal": "invalid",
        }
    )
    assert "generated_at" not in no_timestamp["normalized"]


def test_png_creation_time_and_webp_exif_timestamp_are_extracted_from_file() -> None:
    result = parse_image_metadata(
        encoded_image(fields={"Creation Time": "2026-09-05T11:20:30Z"})
    )
    expected = datetime(2026, 9, 5, 11, 20, 30, tzinfo=timezone.utc).timestamp()
    assert result["normalized"]["generated_at"] == expected
    exif = Image.Exif()
    exif[36867] = "2026:09:05 19:20:30"
    exif[36881] = "+08:00"
    output = io.BytesIO()
    Image.new("RGB", (24, 32), "white").save(output, "WEBP", exif=exif)
    result = parse_image_metadata(output.getvalue())
    assert result["normalized"]["generated_at"] == expected
    assert result["raw"]["OffsetTimeOriginal"] == "+08:00"


def test_exif_modification_time_does_not_become_creation_time() -> None:
    result = parse_metadata_fields(
        {
            "DateTime": "2026:09:05 19:20:30",
            "OffsetTime": "+08:00",
            "SubSecTime": "125",
        }
    )
    assert "generated_at" not in result["normalized"]
    assert result["raw"]["DateTime"] == "2026:09:05 19:20:30"
    assert result["warnings"] == []


@pytest.mark.parametrize(
    "name,format,seed",
    [
        ("20260831162558_t2i_nai-diffusion-4-5-full.png", "novelai", 2014091097),
        ("Anima_00001_.png", "comfyui", 457663653203480),
        ("7F7066F37BF383F6E36E4F7F335CCCF6.png", "comfyui", 957843645410562),
        ("Stable Diffusion 149299935.webp", "a1111", 2629817595),
        ("149037466_p0.webp", "comfyui", 457663653203480),
    ],
)
def test_available_user_samples(name: str, format: str, seed: int) -> None:
    sample = Path(__file__).parents[2] / "data" / "image" / name
    if not sample.is_file():
        pytest.skip("本地参考样本不在发布仓库中")
    result = parse_image_metadata(sample.read_bytes())
    assert result["format"] == format
    assert result["normalized"]["seed"] == seed
