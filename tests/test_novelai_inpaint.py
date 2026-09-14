from __future__ import annotations

import base64
import gzip
import io
import json

import pytest
from astrbot_plugin_image_studio.image_metadata import parse_image_metadata
from astrbot_plugin_image_studio.models import GeneratedImage
from astrbot_plugin_image_studio.novelai_inpaint import composite_inpaint_results
from PIL import Image, PngImagePlugin


def encode(image, image_format="png", **options):
    output = io.BytesIO()
    if image_format == "webp":
        options.update(lossless=True, exact=True)
    image.save(output, image_format.upper(), **options)
    return output.getvalue()


def payload(source, mask, image_format="png"):
    return {
        "action": "infill",
        "model": "nai-diffusion-5-full-inpainting",
        "parameters": {
            "width": source.width,
            "height": source.height,
            "image": base64.b64encode(encode(source)).decode("ascii"),
            "mask": base64.b64encode(encode(mask)).decode("ascii"),
            "image_format": image_format,
        },
    }


def half_mask(size=(64, 64)):
    mask = Image.new("RGB", size, "black")
    mask.paste("white", (size[0] // 2, 0, size[0], size[1]))
    return mask


@pytest.mark.parametrize("image_format", ["png", "webp"])
def test_composites_only_masked_pixels_and_preserves_alpha_and_result_identity(
    image_format,
):
    source = Image.new("RGBA", (64, 64), (19, 37, 53, 127))
    source.putpixel((0, 0), (21, 43, 67, 0))
    edited = Image.new("RGBA", source.size, (103, 71, 29, 63))
    edited.putpixel((63, 63), (105, 77, 31, 0))
    item = GeneratedImage(
        encode(edited, image_format), f"image/{image_format}", {"seed": 123}, 7
    )
    request = payload(source, half_mask(), image_format)
    original_request = json.dumps(request)
    (result,) = composite_inpaint_results((item,), request)
    with Image.open(io.BytesIO(result.data)) as output:
        rgba = output.convert("RGBA")
        assert (
            rgba.crop((0, 0, 32, 64)).tobytes() == source.crop((0, 0, 32, 64)).tobytes()
        )
        assert (
            rgba.crop((32, 0, 64, 64)).tobytes()
            == edited.crop((32, 0, 64, 64)).tobytes()
        )
        assert output.format.lower() == image_format
    assert result.mime_type == f"image/{image_format}"
    assert result.response_index == 7
    assert result.effective_parameters == {"seed": 123}
    assert result.effective_parameters is item.effective_parameters
    assert json.dumps(request) == original_request


def novelai_fields():
    return {
        "Software": "NovelAI",
        "Description": "角色站在窗边",
        "Source": "NovelAI Diffusion V4.5",
        "Comment": json.dumps(
            {"prompt": "角色站在窗边", "seed": 987, "steps": 23, "uc": "blur"},
            ensure_ascii=False,
        ),
    }


def stealth_png(fields):
    compressed = gzip.compress(json.dumps(fields).encode("utf-8"))
    frame = b"stealth_pngcomp" + (len(compressed) * 8).to_bytes(4, "big") + compressed
    image = Image.new("RGBA", (64, 64), (24, 48, 72, 255))
    for position in range(len(frame) * 8):
        x, y = divmod(position, image.height)
        bit = (frame[position // 8] >> (7 - position % 8)) & 1
        image.putpixel((x, y), (24, 48, 72, 254 | bit))
    return encode(image)


@pytest.mark.parametrize("image_format", ["png", "webp"])
@pytest.mark.parametrize("hidden", [False, True])
def test_preserves_text_and_materializes_stealth_metadata(image_format, hidden):
    fields = novelai_fields()
    if hidden:
        data = stealth_png(fields)
    else:
        info = PngImagePlugin.PngInfo()
        for key, value in fields.items():
            info.add_itxt(key, value)
        data = encode(Image.new("RGBA", (64, 64), "green"), pnginfo=info)
    source = Image.new("RGBA", (64, 64), "blue")
    item = GeneratedImage(data, "image/png", {"seed": 987}, 2)
    (result,) = composite_inpaint_results(
        (item,), payload(source, half_mask(), image_format)
    )
    metadata = parse_image_metadata(result.data)
    assert metadata["format"] == "novelai"
    assert metadata["normalized"]["seed"] == 987
    assert metadata["normalized"]["prompt"] == "角色站在窗边"
    for key, value in fields.items():
        assert metadata["raw"][key] == value
    if hidden:
        if image_format == "png":
            snapshot = json.loads(metadata["raw"]["StealthMetadata"])
        else:
            snapshot = json.loads(metadata["raw"]["UserComment"])["png_text"][
                "StealthMetadata"
            ]
        assert snapshot["fields"] == fields


def test_preserves_original_webp_exif_xmp_and_icc_bytes():
    fields = novelai_fields()
    exif = Image.Exif()
    exif[305] = "NovelAI"
    exif[37510] = b"UNICODE\x00" + json.dumps(fields, ensure_ascii=False).encode(
        "utf-16"
    )
    exif_bytes = exif.tobytes()
    xmp = b"<x:xmpmeta xmlns:x='adobe:ns:meta/'/>"
    icc = b"test-icc-profile"
    data = encode(
        Image.new("RGB", (64, 64), "red"),
        "webp",
        exif=exif_bytes,
        xmp=xmp,
        icc_profile=icc,
    )
    with Image.open(io.BytesIO(data)) as provider_image:
        # Pillow/libwebp strips the optional Exif prefix while making WebP.
        source_exif = provider_image.info["exif"]
    item = GeneratedImage(data, "image/webp")
    request = payload(Image.new("RGB", (64, 64), "blue"), half_mask(), "webp")
    (result,) = composite_inpaint_results((item,), request)
    with Image.open(io.BytesIO(result.data)) as output:
        assert output.info["exif"] == source_exif
        assert output.info["xmp"] == xmp
        assert output.info["icc_profile"] == icc
    assert parse_image_metadata(result.data)["normalized"]["seed"] == 987


def test_png_to_webp_keeps_original_exif_snapshot_when_adding_text_container():
    exif = Image.Exif()
    exif[37510] = b"ASCII\x00\x00\x00original comment"
    exif_bytes = exif.tobytes()
    info = PngImagePlugin.PngInfo()
    info.add_itxt("Software", "NovelAI")
    info.add_itxt("Comment", novelai_fields()["Comment"])
    data = encode(Image.new("RGB", (64, 64), "red"), pnginfo=info, exif=exif_bytes)
    (result,) = composite_inpaint_results(
        (GeneratedImage(data, "image/png"),),
        payload(Image.new("RGB", (64, 64), "blue"), half_mask(), "webp"),
    )
    snapshot = json.loads(parse_image_metadata(result.data)["raw"]["UserComment"])
    assert base64.b64decode(snapshot["original_exif_base64"]) == exif_bytes
    assert snapshot["png_text"]["UserComment"] == "original comment"


def test_rejects_wrong_return_size_and_reports_position():
    good = GeneratedImage(encode(Image.new("RGB", (64, 64), "red")), "image/png")
    wrong = GeneratedImage(encode(Image.new("RGB", (32, 64), "red")), "image/png")
    with pytest.raises(ValueError, match="第 2 张返回图片尺寸"):
        composite_inpaint_results(
            (good, wrong), payload(Image.new("RGB", (64, 64)), half_mask())
        )


@pytest.mark.parametrize("field", ["image", "mask"])
def test_rejects_wrong_input_dimensions(field):
    request = payload(Image.new("RGB", (64, 64)), half_mask())
    request["parameters"][field] = base64.b64encode(
        encode(Image.new("RGB", (32, 64)))
    ).decode()
    image = GeneratedImage(encode(Image.new("RGB", (64, 64))), "image/png")
    with pytest.raises(ValueError, match="与请求尺寸"):
        composite_inpaint_results((image,), request)


def test_non_infill_action_returns_original_tuple_without_reencoding():
    images = (GeneratedImage(b"original bytes", "image/png", {"seed": 123}, 0),)
    assert composite_inpaint_results(images, {"action": "generate"}) is images
