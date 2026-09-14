from __future__ import annotations

import asyncio
import base64
import io
import json
import zipfile
from dataclasses import replace

import aiohttp
import pytest
from astrbot_plugin_image_studio import novelai
from astrbot_plugin_image_studio.models import (
    GenerationRequest,
    ImageProvider,
    ReferenceImage,
)
from astrbot_plugin_image_studio.providers import (
    ProviderError,
    ProviderExecutor,
    ProviderPartialResponseError,
)
from PIL import Image, PngImagePlugin

SECRET = "pst-exact-account-token"


def png(color="red", size=(64, 64), seed=123):
    output = io.BytesIO()
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", json.dumps({"seed": seed, "prompt": "metadata prompt"}))
    Image.new("RGB", size, color).save(output, "PNG", pnginfo=info)
    return output.getvalue()


def request(**changes):
    return replace(
        GenerationRequest(
            mode="text2img",
            provider_id="official",
            prompt="a cat",
            negative_prompt="blur",
            model="nai-diffusion-5-full",
            size="1024x1024",
            parameters={"seed": 42},
        ),
        **changes,
    )


def provider(**changes):
    return ImageProvider.from_mapping(
        {
            "id": "official",
            "name": "NovelAI",
            "kind": "novelai_official",
            "base_url": "https://image.novelai.net",
            "api_key": SECRET,
            "model": "nai-diffusion-5-full",
            "generate_path": "/ai/generate-image",
            **changes,
        }
    )


class Stream:
    def __init__(self, raw):
        self.raw = raw
        self.read_chunks = 0

    async def iter_chunked(self, size):
        for start in range(0, len(self.raw), size):
            self.read_chunks += 1
            yield self.raw[start : start + size]


class Response:
    def __init__(
        self,
        payload=None,
        *,
        status=201,
        raw=None,
        content_type="application/json",
        headers=None,
    ):
        self.status = status
        self.headers = {"Content-Type": content_type, **(headers or {})}
        self.content = Stream(json.dumps(payload).encode() if raw is None else raw)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class Session:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        if self.error:
            raise self.error
        return self.response

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        if self.error:
            raise self.error
        return self.response


def entry(data=None, **fields):
    return {"image": base64.b64encode(data or png()).decode(), **fields}


@pytest.mark.parametrize("model", novelai.NOVELAI_MODEL_IDS)
def test_builds_v45_and_v5_nested_prompts_without_proxy_fields(model):
    payload, effective = novelai.build_generation_payload(request(), model)
    params = payload["parameters"]
    assert payload["action"] == "generate"
    assert payload["model"] == model
    assert payload["input"] == params["v4_prompt"]["caption"]["base_caption"] == "a cat"
    assert (
        params["negative_prompt"]
        == params["v4_negative_prompt"]["caption"]["base_caption"]
        == "blur"
    )
    assert params["params_version"] == 4
    assert params["seed"] == effective["seed"] == 42
    assert params["width"] == params["height"] == 1024
    assert params["n_samples"] == effective["count"] == 1
    assert params["qualityToggle"] is False and params["add_original_image"] is False
    assert effective["size"] == "1024x1024"
    for key in ("token", "tag", "nocache", "use_new_shared_trial", "image"):
        assert key not in params and key not in effective
    assert "v4_prompt" not in effective and "prompt" not in effective


def test_random_seed_resolves_before_request_and_snapshots_actual_value(monkeypatch):
    monkeypatch.setattr(novelai.secrets, "randbelow", lambda _limit: 321)
    payload, effective = novelai.build_generation_payload(
        request(parameters={"seed": -1}), "nai-diffusion-5-full"
    )
    assert payload["parameters"]["seed"] == effective["seed"] == 321


@pytest.mark.parametrize(
    "changes, expected",
    [
        ({"parameters": {"seed": True}}, "seed"),
        ({"parameters": {"seed": 2**32}}, "seed"),
        ({"parameters": {"scale": "nan"}}, "scale"),
        ({"parameters": {"cfg_rescale": 2}}, "cfg_rescale"),
        ({"parameters": {"image_format": "jpeg"}}, "image_format"),
        ({"parameters": {"params_version": 5}}, "params_version"),
        ({"parameters": {"token": SECRET}}, "token"),
        ({"parameters": {"cfg": 5}}, "cfg"),
        ({"parameters": {"strength": 0.5}}, "仅用于图生图"),
        ({"size": "竖图"}, "尺寸"),
        ({"size": "1000x1000"}, "64 倍数"),
        ({"size": "65536x65536"}, "像素"),
        ({"count": 0}, "count"),
    ],
)
def test_invalid_explicit_parameters_fail_before_network(changes, expected):
    session = Session()
    with pytest.raises(ProviderError, match=expected) as error:
        asyncio.run(ProviderExecutor(session).generate(provider(), request(**changes)))
    assert SECRET not in str(error.value)
    assert session.calls == []


def test_unknown_model_is_not_silently_substituted():
    with pytest.raises(ValueError, match="尚未支持模型"):
        novelai.build_generation_payload(request(), "nai-diffusion-future")


def test_img2img_resizes_base_image_without_data_url_or_snapshot_binary():
    image = png(size=(128, 64))
    ref = ReferenceImage("reference", "input.png", image, "image/png")
    req = request(
        mode="img2img",
        size="64x128",
        references=(ref,),
        parameters={"seed": 8, "strength": 0.6, "noise": 0.1},
    )
    payload, effective = novelai.build_generation_payload(req, req.model)
    assert payload["action"] == "img2img"
    params = payload["parameters"]
    assert params["strength"] == 0.6 and params["noise"] == 0.1
    assert params["extra_noise_seed"] == effective["extra_noise_seed"] == 8
    with Image.open(io.BytesIO(base64.b64decode(params["image"]))) as base:
        assert base.size == (64, 128) and base.format == "PNG"
    assert not params["image"].startswith("data:")
    assert "image" not in effective and "references" not in effective
    assert ref.data == image


@pytest.mark.parametrize("refs", [(), (1, 2)])
def test_img2img_requires_exactly_one_base_image(refs):
    with pytest.raises(ValueError, match="一张底图|一张参考图"):
        novelai.build_generation_payload(
            request(mode="img2img", references=refs), "nai-diffusion-5-full"
        )


def test_img2img_can_be_explicitly_disabled_on_model():
    session = Session()
    with pytest.raises(ProviderError, match="尚未启用图生图"):
        asyncio.run(
            ProviderExecutor(session).generate(
                provider(supports_img2img=False), request(mode="img2img")
            )
        )
    assert not session.calls


@pytest.mark.parametrize("status", [200, 201])
def test_json_http_accepts_all_results_and_preserves_bytes_and_indexes(status):
    red, blue = png("red", seed=81), png("blue", seed=23)
    session = Session(
        Response(
            {"images": [entry(red, index=7, seed=81), entry(blue, index=2, seed=23)]},
            status=status,
        )
    )
    images = asyncio.run(
        ProviderExecutor(session).generate(
            provider(
                custom_headers='{"authorization":"wrong", "accept":"wrong", "X-Test":"yes"}'
            ),
            request(count=2),
        )
    )
    assert [image.data for image in images] == [red, blue]
    assert [image.response_index for image in images] == [7, 2]
    assert [image.effective_parameters["seed"] for image in images] == [81, 23]
    [(method, url, options)] = session.calls
    assert method == "post" and url == "https://image.novelai.net/ai/generate-image"
    assert options["allow_redirects"] is False
    assert options["headers"] == {
        "Authorization": f"Bearer {SECRET}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Test": "yes",
    }
    assert options["json"]["parameters"]["n_samples"] == 2
    assert SECRET not in json.dumps([image.effective_parameters for image in images])


def test_invalid_response_entry_retains_other_originals_and_reports_position():
    session = Session(
        Response(
            {
                "images": [
                    entry(index=0, seed=32),
                    {"index": 1, "image": "invalid"},
                    entry(index=2, seed=98),
                ]
            }
        )
    )
    with pytest.raises(ProviderPartialResponseError) as error:
        asyncio.run(ProviderExecutor(session).generate(provider(), request(count=3)))
    assert len(error.value.images) == 2
    assert [image.response_index for image in error.value.images] == [0, 2]
    assert error.value.failures[0][0] == 2
    assert "Base64" in error.value.failures[0][1]
    assert len(session.calls) == 1


def test_repeated_image_bytes_are_distinct_results_and_unknown_batch_seeds_not_invented():
    raw = json.dumps({"images": [entry(index=0), entry(index=1)]}).encode()
    images, failures = novelai.parse_generation_response(
        raw, "application/json", {"seed": 5, "count": 2}
    )
    assert len(images) == 2 and not failures
    assert all("seed" not in image.effective_parameters for image in images)


def test_invalid_seed_and_repeated_index_keep_decodable_image_and_report_missing_items():
    raw = json.dumps(
        {"images": [entry(index=5, seed=7), entry(index=5, seed=True)]}
    ).encode()
    images, failures = novelai.parse_generation_response(
        raw, "application/json", {"seed": 42}, expected_count=3
    )
    assert len(images) == 2
    assert images[0].response_index == 5 and images[1].response_index is None
    assert "seed" not in images[1].effective_parameters
    assert [position for position, _reason in failures] == [2, 3]
    assert "图片已保留" in failures[0][1]
    assert "未返回" in failures[1][1]


def test_short_response_is_partial_success_without_repeating_generation():
    session = Session(Response({"images": [entry(seed=8)]}))
    with pytest.raises(ProviderPartialResponseError) as error:
        asyncio.run(ProviderExecutor(session).generate(provider(), request(count=2)))
    assert len(error.value.images) == 1
    assert error.value.failures == ((2, "上游未返回本次请求中的这张图片"),)
    assert len(session.calls) == 1


def test_accepts_image_data_url_and_preserves_single_input_seed():
    item = entry()
    item["image"] = "data:image/png;base64," + item["image"]
    images, failures = novelai.parse_generation_response(
        json.dumps({"images": [item]}).encode(), "application/json", {"seed": 90}
    )
    assert not failures and images[0].effective_parameters["seed"] == 90


def test_zip_preserves_all_image_bytes_without_extracting_names(tmp_path, monkeypatch):
    output = io.BytesIO()
    red, blue = png("red"), png("blue")
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("../../escape.png", red)
        archive.writestr("image_1.png", blue)
    monkeypatch.chdir(tmp_path)
    images, failures = novelai.parse_generation_response(
        output.getvalue(), "application/zip", {"seed": 5, "count": 2}
    )
    assert not failures and [image.data for image in images] == [red, blue]
    assert [image.response_index for image in images] == [0, 1]
    assert all("seed" not in image.effective_parameters for image in images)
    assert list(tmp_path.iterdir()) == []


def test_zip_bad_entry_retains_good_original():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("image.png", png())
        archive.writestr("broken.png", b"not an image")
    images, failures = novelai.parse_generation_response(
        output.getvalue(), "application/zip", {}
    )
    assert len(images) == 1 and failures[0][0] == 2


def test_header_readable_but_truncated_image_does_not_count_as_success():
    output = io.BytesIO()
    Image.new("RGB", (64, 64), "red").save(output, "JPEG")
    truncated = output.getvalue()[:-20]
    with Image.open(io.BytesIO(truncated)) as image:
        assert image.size == (64, 64)
    images, failures = novelai.parse_generation_response(
        json.dumps({"images": [entry(truncated)]}).encode(), "application/json", {}
    )
    assert not images and "损坏" in failures[0][1]


def test_zip_uncompressed_size_and_decoded_pixel_limits(monkeypatch):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("image.png", png())
    monkeypatch.setattr(novelai, "MAX_UNCOMPRESSED_BYTES", 20)
    with pytest.raises(ValueError, match="解压总大小"):
        novelai.parse_generation_response(output.getvalue(), "application/zip", {})
    monkeypatch.setattr(novelai, "MAX_UNCOMPRESSED_BYTES", 100000)
    monkeypatch.setattr(novelai, "MAX_IMAGE_PIXELS", 100)
    images, failures = novelai.parse_generation_response(
        json.dumps({"images": [entry()]}).encode(), "application/json", {}
    )
    assert not images and "像素" in failures[0][1]


@pytest.mark.parametrize(
    "raw, kind",
    [
        (b"broken", "application/json"),
        (b"PK\x03\x04broken", "application/zip"),
        (b'{"images": []}', "application/json"),
        (b"[]", "application/json"),
    ],
)
def test_rejects_structurally_invalid_responses(raw, kind):
    with pytest.raises(ValueError):
        novelai.parse_generation_response(raw, kind, {})


@pytest.mark.parametrize(
    "status, label",
    [
        (302, "非成功"),
        (400, "参数"),
        (401, "Token"),
        (402, "额度"),
        (403, "权限"),
        (409, "任务"),
        (422, "参数"),
        (429, "限流"),
        (500, "服务异常"),
    ],
)
def test_http_error_never_retries_or_echoes_sensitive_upstream_body(status, label):
    response = Response({"error": SECRET}, status=status)
    session = Session(response)
    with pytest.raises(ProviderError, match=label) as error:
        asyncio.run(ProviderExecutor(session).generate(provider(), request()))
    assert SECRET not in str(error.value)
    assert len(session.calls) == 1
    if status not in {400, 422}:
        assert response.content.read_chunks == 0


def test_invalid_parameter_reports_sanitized_upstream_detail():
    response = Response(
        {
            "message": f"parameters.noise_schedule is incompatible with sampler; token={SECRET}"
        },
        status=400,
    )
    with pytest.raises(ProviderError) as error:
        asyncio.run(ProviderExecutor(Session(response)).generate(provider(), request()))
    assert "noise_schedule" in str(error.value)
    assert "sampler" in str(error.value)
    assert SECRET not in str(error.value)


@pytest.mark.parametrize(
    "payload",
    [
        {"message": "Recaptcha token is required for trial generation"},
        {"error": {"message": "Invalid reCAPTCHA token for trial generation"}},
    ],
)
def test_trial_captcha_error_explains_account_flow_without_parameter_advice(payload):
    session = Session(Response(payload, status=400))
    with pytest.raises(ProviderError, match="免费试用需要官方验证码验证") as error:
        asyncio.run(ProviderExecutor(session).generate(provider(), request()))
    assert "尚未接入试用验证码流程" in str(error.value)
    assert "参数无效" not in str(error.value)
    assert "检查模型" not in str(error.value)
    assert len(session.calls) == 1


@pytest.mark.parametrize(
    "error", [TimeoutError(), aiohttp.ClientConnectionError("sensitive wire data")]
)
def test_generation_transport_failures_do_not_retry(error):
    session = Session(error=error)
    with pytest.raises(ProviderError, match="不会自动重试") as exc:
        asyncio.run(ProviderExecutor(session).generate(provider(), request()))
    assert len(session.calls) == 1 and "sensitive" not in str(exc.value)


def test_http_declared_response_limit_is_checked_before_read():
    response = Response(
        {}, headers={"Content-Length": str(novelai.MAX_RESPONSE_BYTES + 1)}
    )
    with pytest.raises(ProviderError, match="大小上限"):
        asyncio.run(ProviderExecutor(Session(response)).generate(provider(), request()))
    assert response.content.read_chunks == 0


def test_http_stream_limit_applies_without_content_length(monkeypatch):
    from astrbot_plugin_image_studio import providers

    monkeypatch.setattr(providers, "MAX_RESPONSE_BYTES", 10)
    response = Response(raw=b"A" * 100)
    with pytest.raises(ProviderError, match="大小上限"):
        asyncio.run(ProviderExecutor(Session(response)).generate(provider(), request()))


def test_subscription_uses_pat_get_and_keeps_usage_separate_from_anlas():
    session = Session(
        Response(
            {
                "active": True,
                "tier": 3,
                "trainingStepsLeft": {
                    "fixedTrainingStepsLeft": 100,
                    "purchasedTrainingSteps": 250,
                },
                "usage": {
                    "percent": 120,
                    "isNegative": False,
                    "timeUntilNextPercent": 0,
                },
                "private": SECRET,
            },
            status=200,
        )
    )
    quota = asyncio.run(ProviderExecutor(session).fetch_quota(provider()))
    assert quota["remaining"] == 350 and quota["subscription_anlas"] == 100
    assert quota["purchased_anlas"] == 250 and quota["subscription_active"] is True
    assert "enabled" not in quota
    assert quota["usage"] == {
        "percent": 120,
        "is_negative": False,
        "time_until_next_percent": 0,
    }
    [(method, url, options)] = session.calls
    assert method == "get" and url == "https://image.novelai.net/user/subscription"
    assert options["headers"]["Authorization"] == "Bearer " + SECRET
    assert options["allow_redirects"] is False and options["timeout"].total == 15
    assert SECRET not in json.dumps(quota)


def test_missing_subscription_fields_stay_unknown_and_zero_percent_is_not_exhaustion():
    quota = novelai.parse_subscription({"active": False, "usage": {"percent": 0}})
    assert quota["remaining"] is None and quota["tier"] is None
    assert quota["subscription_anlas"] is None and quota["purchased_anlas"] is None
    assert quota["subscription_active"] is False
    assert "enabled" not in quota
    assert quota["usage"] == {
        "percent": 0,
        "is_negative": None,
        "time_until_next_percent": None,
    }


def test_partial_balance_does_not_fabricate_total_and_usage_can_be_negative():
    quota = novelai.parse_subscription(
        {
            "active": True,
            "trainingStepsLeft": {"purchasedTrainingSteps": 250},
            "usage": {"percent": -4.5, "isNegative": True, "timeUntilNextPercent": 123},
        }
    )
    assert quota["remaining"] is None and quota["purchased_anlas"] == 250
    assert quota["usage"]["percent"] == -4.5


@pytest.mark.parametrize("payload", [{}, [], {"active": 1}])
def test_subscription_rejects_invalid_top_level_without_exposing_body(payload):
    with pytest.raises(ProviderError, match="响应格式无效"):
        asyncio.run(
            ProviderExecutor(Session(Response(payload, status=200))).fetch_quota(
                provider()
            )
        )


def test_discovery_explicitly_rejects_static_model_list_as_remote():
    session = Session()
    with pytest.raises(ProviderError, match="内置模型预设"):
        asyncio.run(ProviderExecutor(session).discover_models(provider()))
    assert session.calls == []
