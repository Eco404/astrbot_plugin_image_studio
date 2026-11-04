"""Agnes wire format follows its documented mode-specific examples."""

from __future__ import annotations

import asyncio
import base64
import io
import json
from dataclasses import replace

import pytest
from PIL import Image

from astrbot_plugin_image_studio.backend.models import (
    GenerationRequest,
    ImageProvider,
    ReferenceImage,
)
from astrbot_plugin_image_studio.backend.providers.agnes import (
    AGNES_BASE_URL,
    AGNES_IMAGE_PATH,
    AGNES_MODEL_ID,
    AGNES_RATIOS,
    AGNES_SIZES,
    agnes_model_presets,
    agnes_parameter_schema,
    build_payload,
)
from astrbot_plugin_image_studio.backend.providers.executor import (
    ProviderError,
    ProviderExecutor,
)


def png(color="orange"):
    target = io.BytesIO()
    Image.new("RGB", (16, 16), color).save(target, "PNG")
    return target.getvalue()


def provider(**changes):
    return ImageProvider.from_mapping(
        {
            "id": "agnes",
            "kind": "agnes",
            "api_key": "offline-test-key",
            "base_url": AGNES_BASE_URL,
            "model": AGNES_MODEL_ID,
            "generate_path": AGNES_IMAGE_PATH,
            # Deliberately incompatible: the adapter must use generations for
            # both modes, including providers migrated from a generic adapter.
            "edit_path": "/images/edits",
            **changes,
        }
    )


def request(**changes):
    return replace(
        GenerationRequest(
            mode="text2img",
            provider_id="agnes",
            prompt="a small orange cat",
            model=AGNES_MODEL_ID,
            size="2k",
            parameters={"ratio": "16:9"},
        ),
        **changes,
    )


class Response:
    def __init__(self, payload=None, *, status=200, raw=None, content_type=None):
        self.status = status
        self.raw = json.dumps(payload).encode() if raw is None else raw
        self.headers = {"Content-Type": content_type or "application/json"}

    async def read(self):
        return self.raw

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass


class Session:
    def __init__(self, *, response=None, downloaded=None):
        self.calls = []
        self.image = png()
        self.response = response or Response(
            {"data": [{"b64_json": base64.b64encode(self.image).decode()}]}
        )
        self.downloaded = downloaded or Response(
            raw=self.image, content_type="image/png"
        )

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        return self.response

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        return self.downloaded


@pytest.mark.parametrize("mode", ["text2img", "img2img"])
@pytest.mark.parametrize("response_format", ["url", "b64_json"])
def test_mode_response_switch_and_image_placement(mode, response_format):
    references = tuple(
        ReferenceImage(str(index), f"{index}.png", png(color), "image/png")
        for index, color in enumerate(("red", "blue"))
    )
    original_parameters = {
        "response_format": response_format,
        "ratio": "16:9",
        "n": 9,
        "count": 9,
        "return_base64": False,
        "references": ["stale-top-level-image"],
        "extra_body": {"image": ["stale-image"], "response_format": "stale"},
    }
    configured = provider()
    session = Session()
    result = asyncio.run(
        ProviderExecutor(session).generate(
            configured,
            request(mode=mode, references=references, parameters=original_parameters),
        )
    )
    assert result[0].data == session.image
    method, endpoint, sent = session.calls[0]
    assert method == "post"
    assert endpoint == AGNES_BASE_URL + AGNES_IMAGE_PATH
    assert sent["headers"]["Authorization"] == "Bearer offline-test-key"
    assert sent["headers"]["Content-Type"] == "application/json"
    assert sent["timeout"].total == configured.timeout_seconds
    assert sent["proxy"] is None
    body = sent["json"]
    assert body["model"] == AGNES_MODEL_ID
    assert body["prompt"] == "a small orange cat"
    assert body["size"] == "2K"
    assert body["ratio"] == "16:9"
    assert not {"n", "count", "image", "references", "response_format"} & body.keys()
    extra = body.get("extra_body", {})
    if mode == "img2img":
        assert extra["image"] == [
            "data:image/png;base64," + base64.b64encode(item.data).decode()
            for item in references
        ]
        assert extra["response_format"] == response_format
        assert "return_base64" not in body
    elif response_format == "b64_json":
        assert body["return_base64"] is True
        assert "image" not in extra and "response_format" not in extra
    else:
        assert extra == {"response_format": "url"}
        assert "return_base64" not in body
    # Mode-specific sanitization must not mutate model defaults or the request.
    assert original_parameters["extra_body"]["image"] == ["stale-image"]
    assert original_parameters["return_base64"] is False


def test_single_reference_stays_array_and_legacy_size_is_preserved():
    body = build_payload(
        provider(),
        request(
            mode="img2img",
            size="1024x768",
            references=(ReferenceImage("one", "one.png", png(), "image/png"),),
        ),
    )
    assert len(body["extra_body"]["image"]) == 1
    assert isinstance(body["extra_body"]["image"], list)
    assert body["size"] == "1024x768"


def test_url_output_download_reuses_provider_proxy_without_auth_leak():
    session = Session(
        response=Response({"data": [{"url": "https://cdn.invalid/result.png"}]})
    )
    configured = provider(proxy="http://proxy.invalid:7890")
    result = asyncio.run(ProviderExecutor(session).generate(configured, request()))
    assert result[0].data == session.image
    assert [call[0] for call in session.calls] == ["post", "get"]
    for _, _, options in session.calls:
        assert options["proxy"] == configured.proxy
    assert "headers" not in session.calls[1][2]


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"mode": "img2img"}, "至少一张参考图片"),
        ({"count": 2}, "每次请求生成一张"),
        ({"size": "5K"}, "尺寸"),
        ({"size": "0x1024"}, "尺寸"),
        ({"parameters": {"ratio": "5:7"}}, "宽高比"),
        ({"parameters": {"response_format": "base64"}}, "返回格式"),
        ({"parameters": {"extra_body": "not an object"}}, "必须为对象"),
    ],
)
def test_invalid_request_is_not_submitted(changes, message):
    session = Session()
    with pytest.raises(ProviderError, match=message) as caught:
        asyncio.run(ProviderExecutor(session).generate(provider(), request(**changes)))
    assert caught.value.request_status == "not_submitted"
    assert session.calls == []


@pytest.mark.parametrize(
    "status,expected", [(400, "rejected"), (429, "rejected"), (408, "unknown"), (503, "unknown")]
)
def test_upstream_error_preserves_submission_status(status, expected):
    session = Session(response=Response({"error": "failed"}, status=status))
    with pytest.raises(ProviderError, match=f"HTTP {status}") as caught:
        asyncio.run(ProviderExecutor(session).generate(provider(), request()))
    assert caught.value.request_status == expected
    assert len(session.calls) == 1


def test_catalog_is_builtin_and_does_not_make_a_remote_models_request():
    session = Session()
    result = asyncio.run(ProviderExecutor(session).discover_models(provider()))
    assert session.calls == []
    assert len(result) == 1
    model = result[0]
    assert model["id"] == AGNES_MODEL_ID
    assert model["supports_text2img"] and model["supports_img2img"]
    assert not model["supports_negative_prompt"]
    assert model["native_batch_size"] == 1
    assert model["capability_source"] == "builtin"
    assert model["parameters"]["size"]["choices"] == list(AGNES_SIZES)
    assert model["parameters"]["ratio"]["choices"] == list(AGNES_RATIOS)
    model["parameters"]["size"]["choices"].append("bad-size")
    assert "bad-size" not in agnes_model_presets()[0]["parameters"]["size"]["choices"]


def test_default_schema_is_independent_and_contains_only_supported_controls():
    schema = agnes_parameter_schema()
    assert set(schema) == {"size", "ratio", "count", "response_format"}
    assert schema["size"]["default"] == "1K"
    assert schema["count"]["default"] == 1
    assert schema["count"]["max"] == 16
    schema["size"]["choices"].clear()
    assert agnes_parameter_schema()["size"]["choices"] == list(AGNES_SIZES)


def test_config_default_and_native_batch_limit():
    configured = ImageProvider.from_mapping(
        {"kind": "agnes", "models": [{"id": AGNES_MODEL_ID, "native_batch_size": 8}]}
    )
    assert configured.base_url == AGNES_BASE_URL
    assert configured.generate_path == configured.edit_path == AGNES_IMAGE_PATH
    assert configured.edit_request_format == "json_data_url"
    model = configured.models[0]
    assert model.img2img
    assert model.native_batch_size == 1
    assert model.native_batch_size_source == "fixed"
    assert model.parameters["size"]["default"] == "1K"


@pytest.mark.parametrize("source", ["webui", "llm_tool", "command"])
def test_count_is_split_into_independent_generation_requests(source):
    from astrbot_plugin_image_studio.tests.backend.providers.test_nai_batches import (
        generate,
        provider as make_provider,
        service,
    )

    async def run():
        configured = make_provider(kind="agnes")
        session = Session()
        subject = service([configured], executor=ProviderExecutor(session))
        result = await generate(subject, count=3, source=source)
        assert result.request.count == len(result.images) == 3
        assert len(session.calls) == 3
        for method, endpoint, options in session.calls:
            assert method == "post"
            assert endpoint == AGNES_BASE_URL + AGNES_IMAGE_PATH
            assert options["json"]["size"] == "1K"
            assert "n" not in options["json"] and "count" not in options["json"]

    asyncio.run(run())
