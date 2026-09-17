from __future__ import annotations

import gzip
import io
import json
import math

import pytest
from astrbot_plugin_image_studio.backend.metadata import parser as image_metadata
from PIL import Image, PngImagePlugin

MAGIC = b"stealth_pngcomp"
PARAMETERS = {
    "prompt": "a landscape",
    "uc": "blurry",
    "seed": 123456,
    "steps": 28,
    "scale": 5,
    "sampler": "k_euler_ancestral",
    "request_type": "PromptGenerateRequest",
}


def metadata(**parameters):
    return {
        "Software": "NovelAI",
        "Source": "NovelAI Diffusion V4.5",
        "Comment": json.dumps({**PARAMETERS, **parameters}),
    }


def compressed_frame(payload: bytes, *, bit_length: int | None = None) -> bytes:
    compressed = gzip.compress(payload, mtime=0)
    length = len(compressed) * 8 if bit_length is None else bit_length
    return MAGIC + length.to_bytes(4, "big") + compressed


def png(*, stealth=None, frame=None, fields=None, mode="RGBA", size=None):
    if stealth is not None:
        frame = compressed_frame(json.dumps(stealth).encode("utf-8"))
    if frame is None:
        frame = b""
    # Deliberately non-square and not byte aligned: row-major extraction fails.
    width, height = size or (61, max(32, math.ceil(len(frame) * 8 / 61)))
    image = Image.new(mode, (width, height), (20, 40, 60, 255)[: len(mode)])
    pixels = image.load()
    for position in range(min(len(frame) * 8, width * height)):
        x, y = divmod(position, height)
        pixel = list(pixels[x, y])
        pixel[-1] = (pixel[-1] & 0xFE) | (
            (frame[position // 8] >> (7 - position % 8)) & 1
        )
        pixels[x, y] = tuple(pixel)
    pnginfo = PngImagePlugin.PngInfo()
    for key, value in (fields or {}).items():
        pnginfo.add_text(key, value)
    output = io.BytesIO()
    image.save(output, "PNG", pnginfo=pnginfo)
    return output.getvalue()


def test_reads_official_column_major_alpha_payload_without_changing_image():
    fields = metadata()
    source = png(stealth=fields)
    original = bytes(source)
    result = image_metadata.parse_image_metadata(source)
    assert result["format"] == "novelai"
    assert result["parser_version"] == image_metadata.PARSER_VERSION
    assert result["normalized"]["seed"] == PARAMETERS["seed"]
    assert result["normalized"]["mode"] == "text2img"
    assert result["normalized"]["model"] == fields["Source"]
    assert result["raw"]["Comment"] == fields["Comment"]
    assert result["raw"]["StealthMetadata"] == {
        "protocol": "stealth_pngcomp",
        "fields": fields,
    }
    assert result["warnings"] == []
    assert source == original


@pytest.mark.parametrize("mode", ["RGB", "RGBA"])
@pytest.mark.parametrize("size", [(3, 3), (61, 32)])
def test_plain_png_has_no_invented_metadata_or_warning(mode, size):
    result = image_metadata.parse_image_metadata(png(mode=mode, size=size))
    assert result["format"] == "unknown"
    assert result["normalized"] == {
        "mode": "unknown",
        "file_dimensions": {"width": size[0], "height": size[1]},
    }
    assert result["warnings"] == []
    assert "StealthMetadata" not in result["raw"]


def test_rgb_blue_channel_lsb_is_not_treated_as_alpha_metadata():
    result = image_metadata.parse_image_metadata(png(stealth=metadata(), mode="RGB"))
    assert result["format"] == "unknown"
    assert result["warnings"] == []


def test_missing_regular_comment_is_supplied_without_invalid_comment_warning():
    result = image_metadata.parse_image_metadata(
        png(stealth=metadata(), fields={"Software": "NovelAI"})
    )
    assert result["format"] == "novelai"
    assert result["normalized"]["seed"] == PARAMETERS["seed"]
    assert result["warnings"] == []


def test_regular_fields_win_conflicts_and_missing_parameters_are_supplemented():
    ordinary_comment = json.dumps({"prompt": "ordinary prompt", "steps": 12})
    hidden = metadata(prompt="hidden prompt", seed=987)
    result = image_metadata.parse_image_metadata(
        png(
            stealth=hidden,
            fields={"Software": "NovelAI", "Comment": ordinary_comment},
        )
    )
    assert result["normalized"]["prompt"] == "ordinary prompt"
    assert result["normalized"]["steps"] == 12
    assert result["normalized"]["seed"] == 987
    assert result["raw"]["Comment"] == ordinary_comment
    assert result["raw"]["StealthMetadata"]["fields"]["Comment"] == hidden["Comment"]
    assert any("Comment 不一致" in message for message in result["warnings"])


@pytest.mark.parametrize("hidden_as_object", [False, True])
def test_equivalent_comment_json_has_no_conflict_warning(hidden_as_object):
    hidden = metadata()
    hidden["Comment"] = (
        PARAMETERS
        if hidden_as_object
        else json.dumps(PARAMETERS, sort_keys=True, separators=(",", ":"))
    )
    ordinary_comment = json.dumps(PARAMETERS, indent=2)
    result = image_metadata.parse_image_metadata(
        png(stealth=hidden, fields={"Software": "NovelAI", "Comment": ordinary_comment})
    )
    assert result["raw"]["Comment"] == ordinary_comment
    assert result["warnings"] == []


@pytest.mark.parametrize(
    "frame",
    [
        MAGIC,  # No complete length header.
        MAGIC + (0).to_bytes(4, "big"),
        MAGIC + (7).to_bytes(4, "big"),
        MAGIC + (2**32 - 8).to_bytes(4, "big"),
        MAGIC + (24).to_bytes(4, "big") + b"bad",
        # A gzip header followed by an invalid DEFLATE block (zlib.error).
        MAGIC
        + (88).to_bytes(4, "big")
        + b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03\xff",
        compressed_frame(b'{"Software":"NovelAI"}')[:-6],
        compressed_frame(b"not json"),
        compressed_frame(b"[]"),
        compressed_frame(b"\xff"),
    ],
)
def test_invalid_payload_never_prevents_reading_regular_metadata(frame):
    # Exact capacity for the deliberately truncated header case.
    size = (len(MAGIC), 8) if frame == MAGIC else None
    result = image_metadata.parse_image_metadata(
        png(frame=frame, size=size, fields=metadata(seed=42))
    )
    assert result["format"] == "novelai"
    assert result["normalized"]["seed"] == 42
    assert "StealthMetadata" not in result["raw"]
    assert any("隐写元数据无效" in message for message in result["warnings"])


def test_rejects_declared_payload_larger_than_image_capacity():
    frame = compressed_frame(json.dumps(metadata()).encode())
    result = image_metadata.parse_image_metadata(png(frame=frame, size=(20, 20)))
    assert result["format"] == "unknown"
    assert any("超过图片容量" in message for message in result["warnings"])


def test_gzip_expansion_is_bounded_at_existing_metadata_limit():
    payload = json.dumps({"padding": "x" * image_metadata.MAX_METADATA_BYTES}).encode()
    result = image_metadata.parse_image_metadata(
        png(frame=compressed_frame(payload), fields=metadata(seed=42))
    )
    assert result["normalized"]["seed"] == 42
    assert "StealthMetadata" not in result["raw"]
    assert any("解压后超过 4 MiB" in message for message in result["warnings"])


def test_does_not_mix_distinct_generation_formats():
    ordinary = "ordinary picture\nSteps: 20, Sampler: Euler, Seed: 7, Size: 512x512"
    result = image_metadata.parse_image_metadata(
        png(stealth=metadata(), fields={"parameters": ordinary})
    )
    assert result["format"] == "a1111"
    assert result["normalized"]["seed"] == 7
    assert "model" not in result["normalized"]
    assert result["raw"]["StealthMetadata"]["fields"]["Software"] == "NovelAI"
    assert any("生成格式不同" in message for message in result["warnings"])


def test_combined_metadata_limit_preserves_ordinary_data(monkeypatch):
    ordinary = metadata(seed=42)
    hidden = {**metadata(seed=9), "Padding": "x" * 700}
    monkeypatch.setattr(image_metadata, "MAX_METADATA_BYTES", 1500)
    result = image_metadata.parse_image_metadata(png(stealth=hidden, fields=ordinary))
    assert result["normalized"]["seed"] == 42
    assert result["raw"] == ordinary
    assert any("合并后的图片元数据" in message for message in result["warnings"])


def test_existing_pixel_limit_is_checked_before_stealth_read(monkeypatch):
    monkeypatch.setattr(image_metadata, "MAX_PIXELS", 100)
    with pytest.raises(ValueError, match="6400 万像素"):
        image_metadata.parse_image_metadata(png(stealth=metadata()))
