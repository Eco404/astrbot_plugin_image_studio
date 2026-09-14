from __future__ import annotations

import asyncio
import base64
import io
import json
import math
from dataclasses import replace

import aiohttp
import pytest
from astrbot_plugin_image_studio.config import HistorySettings, RuntimeSettings
from astrbot_plugin_image_studio.models import ImageProvider, ReferenceImage
from astrbot_plugin_image_studio.novelai import (
    NOVELAI_MODEL_IDS,
    prepare_generation_payload,
)
from astrbot_plugin_image_studio.parameter_exchange import (
    export_parameters,
    resolve_parameters,
)
from astrbot_plugin_image_studio.providers import ProviderError, ProviderExecutor
from astrbot_plugin_image_studio.providers import ProviderPartialResponseError
from astrbot_plugin_image_studio.service import ImageGenerationService
from astrbot_plugin_image_studio.storage import GenerationStore
from astrbot_plugin_image_studio.tests.test_novelai_provider import (
    SECRET,
    Response,
    Session,
    entry,
    png,
    provider,
    request,
)
from PIL import Image

V45 = "nai-diffusion-4-5-full"
V45_CURATED = "nai-diffusion-4-5-curated"
V5 = "nai-diffusion-5-full"
V5_CURATED = "nai-diffusion-5-curated"
ENCODED_VIBE = b"\x01\x02offline-vibe-embedding\x00\xfe"


def reference(color="red", size=(64, 64), *, raw=None, name="input"):
    return ReferenceImage(
        name, name + ".png", png(color, size) if raw is None else raw, "image/png"
    )


def prepare(model=V45, *, refs=(), parameters=None, mode=None, size="64x64"):
    req = request(
        model=model,
        mode=mode or ("img2img" if refs else "text2img"),
        references=refs,
        size=size,
        parameters={"seed": 42, **(parameters or {})},
    )
    return prepare_generation_payload(req, model)


class RecordingSession:
    """An offline transport routing encoding separately from image generation."""

    def __init__(self, *, encoding=None, encode_error=None):
        self.encoding = encoding
        self.encode_error = encode_error
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.endswith("/ai/encode-vibe"):
            if self.encode_error:
                raise self.encode_error
            return self.encoding or Response(
                raw=ENCODED_VIBE, content_type="application/octet-stream"
            )
        return Response({"images": [entry(seed=42)]})

    @property
    def encode_calls(self):
        return [call for call in self.calls if call[0].endswith("/ai/encode-vibe")]

    @property
    def generate_calls(self):
        return [call for call in self.calls if call[0].endswith("/ai/generate-image")]


@pytest.mark.parametrize("model", NOVELAI_MODEL_IDS)
def test_all_official_models_have_text_and_single_base_protocol(model):
    payload, effective, vibes = prepare(model)
    assert payload["model"] == model
    assert payload["action"] == "generate"
    assert payload["parameters"]["params_version"] == 4
    assert payload["parameters"]["steps"] == 23
    assert payload["parameters"]["scale"] == (
        7 if model.startswith("nai-diffusion-5-") else 5
    )
    assert vibes == []
    assert not any(key in effective for key in ("image", "v4_prompt", "mask"))
    payload, effective, vibes = prepare(model, refs=(reference(),))
    assert payload["action"] == "img2img"
    assert effective["reference_settings"] == [{"type": "base"}]
    assert payload["parameters"]["image"]
    assert vibes == []


@pytest.mark.parametrize("model", [V45, V45_CURATED])
def test_precise_maps_each_role_and_inverse_fidelity_and_uses_padded_input(model):
    refs = tuple(reference(size=(128, 64), name=str(index)) for index in range(3))
    settings = [
        {
            "type": "character",
            "strength": 0.2,
            "fidelity": 0.8,
            "information_extracted": 1,
        },
        {
            "type": "style",
            "strength": 0.4,
            "fidelity": 0.6,
            "information_extracted": 0.9,
        },
        {
            "type": "character_style",
            "strength": 0.9,
            "fidelity": 0.1,
            "information_extracted": 0.8,
        },
    ]
    payload, effective, vibes = prepare(
        model,
        refs=refs,
        parameters={"reference_mode": "precise", "reference_settings": settings},
    )
    parameters = payload["parameters"]
    assert payload["action"] == "generate"
    assert "image" not in parameters
    assert [
        description["caption"]["base_caption"]
        for description in parameters["director_reference_descriptions"]
    ] == ["character", "style", "character&style"]
    assert parameters["director_reference_strength_values"] == [0.2, 0.4, 0.9]
    assert parameters["director_reference_secondary_strength_values"] == pytest.approx(
        [0.2, 0.4, 0.9]
    )
    assert parameters["director_reference_information_extracted"] == [1, 0.9, 0.8]
    assert effective["reference_settings"] == settings
    assert "director_reference_images" not in effective
    assert vibes == []
    for encoded in parameters["director_reference_images"]:
        with Image.open(io.BytesIO(base64.b64decode(encoded))) as image:
            assert image.size == (1536, 1024)
            assert image.format == "PNG"
            assert max(image.convert("RGB").getpixel((10, 10))) < 10
            assert image.convert("RGB").getpixel((768, 512))[0] > 240


@pytest.mark.parametrize("model", [V5, V5_CURATED])
@pytest.mark.parametrize("reference_mode", ["precise", "vibe"])
def test_v5_rejects_unsupported_reference_modes(model, reference_mode):
    with pytest.raises(ValueError, match="不支持参考用途"):
        prepare(
            model, refs=(reference(),), parameters={"reference_mode": reference_mode}
        )


@pytest.mark.parametrize("model", [V5, V5_CURATED])
@pytest.mark.parametrize("role", ["character", "style", "character_style", "vibe"])
def test_v5_reference_roles_cannot_bypass_mode_capabilities(model, role):
    with pytest.raises(ValueError, match="不支持"):
        prepare(
            model,
            refs=(reference(), reference()),
            parameters={"reference_settings": [{"type": "base"}, {"type": role}]},
        )


@pytest.mark.parametrize("model", NOVELAI_MODEL_IDS)
def test_inpainting_selects_actual_model_and_keeps_mask_separate_from_base(model):
    payload, effective, vibes = prepare(
        model,
        refs=(reference(), reference("white", name="mask")),
        parameters={"reference_mode": "inpaint", "strength": 0.8},
    )
    expected_model = V45_CURATED if model == V5_CURATED else model
    assert payload["model"] == expected_model + "-inpainting"
    assert payload["action"] == "infill"
    assert effective["reference_settings"] == [{"type": "base"}, {"type": "mask"}]
    assert payload["parameters"]["img2img"]["strength"] == 0.8
    assert payload["parameters"]["image"] != payload["parameters"]["mask"]
    with Image.open(
        io.BytesIO(base64.b64decode(payload["parameters"]["mask"]))
    ) as mask:
        assert mask.size == (64, 64)
        assert mask.convert("L").getextrema() == (255, 255)
    assert "image" not in effective and "mask" not in effective
    assert vibes == []


def test_explicit_mask_base_precise_input_order_is_preserved():
    refs = (
        reference("white", name="mask"),
        reference("blue", name="character"),
        reference("red", name="base"),
    )
    payload, effective, vibes = prepare(
        refs=refs,
        parameters={
            "reference_mode": "inpaint",
            "reference_settings": [
                {"type": "mask"},
                {"type": "style"},
                {"type": "base"},
            ],
        },
    )
    assert [item["type"] for item in effective["reference_settings"]] == [
        "mask",
        "style",
        "base",
    ]
    assert (
        payload["parameters"]["director_reference_descriptions"][0]["caption"][
            "base_caption"
        ]
        == "style"
    )
    with Image.open(
        io.BytesIO(base64.b64decode(payload["parameters"]["image"]))
    ) as base:
        assert base.convert("RGB").getpixel((32, 32)) == (255, 0, 0)
    assert not vibes


@pytest.mark.parametrize(
    "parameters,refs,match",
    [
        ({"reference_mode": "inpaint"}, (reference(),), "底图.*蒙版"),
        ({"reference_mode": "inpaint"}, (reference(), reference("black")), "没有白色"),
        (
            {"reference_mode": "inpaint"},
            (reference(), reference("white", size=(128, 64))),
            "尺寸必须",
        ),
        (
            {
                "reference_mode": "inpaint",
                "reference_settings": [
                    {"type": "base"},
                    {"type": "mask"},
                    {"type": "vibe"},
                ],
            },
            (reference(), reference("white"), reference()),
            "不支持 Vibe",
        ),
        (
            {
                "reference_mode": "precise",
                "reference_settings": [{"type": "character"}, {"type": "vibe"}],
            },
            (reference(), reference()),
            "不能.*混用",
        ),
        (
            {"reference_settings": [{"type": "base"}, {"type": "base"}]},
            (reference(), reference()),
            "最多包含一张底图",
        ),
    ],
)
def test_invalid_reference_combinations_or_masks_fail_locally(parameters, refs, match):
    with pytest.raises(ValueError, match=match):
        prepare(refs=refs, parameters=parameters)


@pytest.mark.parametrize(
    "model,maximum", [(V45, 6), (V45_CURATED, 6), (V5, 32), (V5_CURATED, 32)]
)
def test_character_capacity_matches_each_model(model, maximum):
    characters = [{"prompt": f"character {index}"} for index in range(maximum)]
    payload, effective, _ = prepare(model, parameters={"characters": characters})
    assert (
        len(payload["parameters"]["v4_prompt"]["caption"]["char_captions"]) == maximum
    )
    assert len(effective["characters"]) == maximum
    with pytest.raises(ValueError, match=f"最多支持 {maximum}"):
        prepare(model, parameters={"characters": characters + [{"prompt": "extra"}]})


@pytest.mark.parametrize(
    "model,expected", [(V45, {"x": 0.3, "y": 0.9}), (V5, {"x": 0.22, "y": 0.98})]
)
def test_character_positions_and_negative_captions_remain_paired(model, expected):
    characters = [
        {"prompt": "red hair", "negative_prompt": "hat", "x": 0.22, "y": 0.98},
        {"prompt": "blue hair", "negative_prompt": "glasses", "x": 0.5, "y": 0.5},
    ]
    payload, effective, _ = prepare(
        model, parameters={"characters": characters, "use_coords": True}
    )
    parameters = payload["parameters"]
    positive = parameters["v4_prompt"]["caption"]["char_captions"]
    negative = parameters["v4_negative_prompt"]["caption"]["char_captions"]
    assert parameters["v4_prompt"]["use_coords"] is True
    assert [item["char_caption"] for item in positive] == ["red hair", "blue hair"]
    assert [item["char_caption"] for item in negative] == ["hat", "glasses"]
    assert positive[0]["centers"] == negative[0]["centers"] == [expected]
    assert effective["characters"][0]["x"] == expected["x"]
    assert effective["characters"][0]["y"] == expected["y"]


def test_v5_curated_inpainting_enforces_actual_v45_character_capacity():
    with pytest.raises(ValueError, match="最多支持 6"):
        prepare(
            V5_CURATED,
            refs=(reference(), reference("white")),
            parameters={
                "reference_mode": "inpaint",
                "characters": [{"prompt": str(index)} for index in range(7)],
            },
        )


@pytest.mark.parametrize("model", [V5, V5_CURATED])
def test_v5_transparency_parameters_and_prompt_hint(model):
    payload, effective, _ = prepare(
        model,
        parameters={"straight_alpha": True, "tag_hint_transparent_background": True},
    )
    assert payload["parameters"]["straight_alpha"] is True
    assert payload["parameters"]["tag_hint_transparent_background"] is True
    assert payload["input"] == "a cat, transparent background"
    assert effective["straight_alpha"] is True
    assert effective["tag_hint_transparent_background"] is True


@pytest.mark.parametrize("model", [V45, V45_CURATED, V5_CURATED])
@pytest.mark.parametrize("key", ["straight_alpha", "tag_hint_transparent_background"])
def test_actual_v45_model_rejects_v5_transparency(model, key):
    refs = (reference(), reference("white")) if model == V5_CURATED else ()
    parameters = {key: True, **({"reference_mode": "inpaint"} if refs else {})}
    with pytest.raises(ValueError, match="不支持透明背景"):
        prepare(model, refs=refs, parameters=parameters)


@pytest.mark.parametrize("size", ["832x1216", "1024x1024", "1536x1024"])
def test_variety_boost_scales_sigma_for_output_dimensions(size):
    payload, effective, _ = prepare(size=size, parameters={"variety_boost": True})
    width, height = map(int, size.split("x"))
    assert payload["parameters"]["skip_cfg_above_sigma"] == pytest.approx(
        58 * math.sqrt(((width // 8) * (height // 8)) / (104 * 152))
    )
    assert effective["variety_boost"] is True
    assert "skip_cfg_above_sigma" not in effective


@pytest.mark.parametrize("model", [V5, V5_CURATED])
def test_v5_rejects_variety_boost(model):
    with pytest.raises(ValueError, match="不支持 Variety Boost"):
        prepare(model, parameters={"variety_boost": True})


@pytest.mark.parametrize(
    "model,quality,suffix",
    [
        (V45, "standard", "very aesthetic, masterpiece, no text"),
        (
            V45_CURATED,
            "standard",
            "very aesthetic, masterpiece, no text, -0.8::feet::, rating:general",
        ),
        (V5, "light", "very aesthetic, amazing quality, no text"),
    ],
)
def test_quality_preset_expands_wire_prompt_while_snapshot_keeps_preset(
    model, quality, suffix
):
    payload, effective, _ = prepare(model, parameters={"quality_preset": quality})
    assert payload["input"] == "a cat, " + suffix
    assert (
        payload["parameters"]["v4_prompt"]["caption"]["base_caption"]
        == payload["input"]
    )
    assert effective["quality_preset"] == quality
    assert "prompt" not in effective


def test_plugin_history_reproduction_does_not_duplicate_quality_or_transparency_tags(
    tmp_path,
):
    async def run():
        configured = provider()
        settings = RuntimeSettings(
            True, (configured,), HistorySettings(True, 100, 100, True), 0
        )
        store = GenerationStore(tmp_path)
        await store.initialize()
        session = RecordingSession()
        service = ImageGenerationService(
            settings=settings, executor=ProviderExecutor(session), store=store
        )
        first = await service.generate(
            mode="text2img",
            provider_id=configured.id,
            model_ref=f"{configured.id}:{V5}",
            prompt="a cat",
            parameters={
                "quality_preset": "standard",
                "tag_hint_transparent_background": True,
            },
        )
        detail = await store.generation_detail(first.generation_id)
        copied = export_parameters(detail)
        draft = resolve_parameters(copied["content"], settings, for_reproduction=True)[
            "draft"
        ]
        assert draft["prompt"] == "a cat"
        await service.generate(
            mode=draft["mode"],
            provider_id=draft["provider_id"],
            model_ref=draft["model_ref"],
            prompt=draft["prompt"],
            negative_prompt=draft["negative_prompt"],
            size=draft.get("size"),
            parameters=draft["parameters"],
        )
        original, repeated = [
            call[1]["json"]["input"] for call in session.generate_calls
        ]
        assert original == repeated
        assert repeated.count("masterpiece") == 1
        assert repeated.count("transparent background") == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    "changes,match",
    [
        (
            {
                "parameters": {
                    "reference_mode": "vibe",
                    "characters": [{"prompt": "x"}] * 7,
                }
            },
            "最多支持 6",
        ),
        (
            {
                "parameters": {
                    "reference_mode": "vibe",
                    "reference_settings": [{"type": "vibe", "strength": 2}],
                }
            },
            "参考|reference",
        ),
        (
            {
                "parameters": {
                    "reference_mode": "vibe",
                    "normalize_reference_strength_multiple": "yes",
                }
            },
            "布尔值",
        ),
        ({"parameters": {"reference_mode": "vibe", "unknown": True}}, "不支持参数"),
        ({"size": "65x64"}, "64 倍数"),
        ({"references": (reference(), reference(raw=b"broken image"))}, "不是可读取"),
    ],
)
def test_entire_request_is_validated_before_any_paid_vibe_encoding(changes, match):
    session = RecordingSession()
    req = request(
        model=V45,
        mode="img2img",
        references=(reference(),),
        parameters={"reference_mode": "vibe"},
    )
    req = replace(req, **changes)
    with pytest.raises(ProviderError, match=match):
        asyncio.run(ProviderExecutor(session).generate(provider(model=V45), req))
    assert session.calls == []


def test_successful_vibe_generation_uses_binary_encoding_and_forwards_provider_proxy():
    async def run():
        session = RecordingSession()
        executor = ProviderExecutor(session)
        configured = provider(model=V45, proxy="http://127.0.0.1:7890")
        req = request(
            model=V45,
            mode="img2img",
            references=(reference(), reference("blue")),
            parameters={
                "reference_mode": "vibe",
                "reference_settings": [
                    {"type": "vibe", "strength": 0.8, "information_extracted": 0.7},
                    {"type": "vibe", "strength": 0.4, "information_extracted": 1},
                ],
            },
        )
        images = await executor.generate(configured, req)
        assert len(images) == 1
        assert len(session.encode_calls) == 2
        assert len(session.generate_calls) == 1
        for url, kwargs in session.calls:
            assert kwargs["proxy"] == configured.proxy
            assert kwargs["allow_redirects"] is False
            assert kwargs["headers"]["Authorization"] == "Bearer " + SECRET
        for _, kwargs in session.encode_calls:
            assert kwargs["headers"]["Accept"] == "application/octet-stream"
            assert kwargs["json"]["model"] == V45
            assert kwargs["json"]["image"]
        payload = session.generate_calls[0][1]["json"]["parameters"]
        assert (
            payload["reference_image_multiple"]
            == [base64.b64encode(ENCODED_VIBE).decode()] * 2
        )
        assert payload["reference_strength_multiple"] == pytest.approx([2 / 3, 1 / 3])
        assert payload["normalize_reference_strength_multiple"] is False
        assert (
            images[0].effective_parameters["normalize_reference_strength_multiple"]
            is True
        )
        assert "reference_image_multiple" not in images[0].effective_parameters

    asyncio.run(run())


def test_vibe_cache_is_shared_for_same_account_but_isolates_inputs_model_and_endpoint():
    async def run():
        session = RecordingSession()
        executor = ProviderExecutor(session)
        configured = provider(model=V45)
        payload = {
            "model": V45,
            "image": "offline-image-A",
            "information_extracted": 0.7,
        }
        value = await executor._encode_vibe(configured, payload)
        assert (
            await executor._encode_vibe(
                replace(
                    configured, id="another-provider", proxy="http://127.0.0.1:7890"
                ),
                dict(payload),
            )
            == value
        )
        assert len(session.encode_calls) == 1
        for different_provider, different_payload in (
            (replace(configured, api_key="pst-another-offline-account"), payload),
            (replace(configured, base_url="https://other.invalid"), payload),
            (configured, {**payload, "model": V45_CURATED}),
            (configured, {**payload, "image": "offline-image-B"}),
            (configured, {**payload, "information_extracted": 1}),
        ):
            await executor._encode_vibe(different_provider, different_payload)
        assert len(session.encode_calls) == 6

    asyncio.run(run())


def test_concurrent_identical_vibes_encode_once_and_release_the_per_key_lock():
    async def run():
        started, release = asyncio.Event(), asyncio.Event()

        class BlockedResponse(Response):
            async def __aenter__(self):
                started.set()
                await release.wait()
                return self

        session = RecordingSession(
            encoding=BlockedResponse(
                raw=ENCODED_VIBE, content_type="application/octet-stream"
            )
        )
        executor = ProviderExecutor(session)
        payload = {
            "model": V45,
            "image": "same-offline-image",
            "information_extracted": 0.7,
        }
        tasks = [
            asyncio.create_task(
                executor._encode_vibe(provider(model=V45, id=str(index)), payload)
            )
            for index in range(5)
        ]
        await asyncio.wait_for(started.wait(), 1)
        await asyncio.sleep(0)
        assert len(session.encode_calls) == 1
        release.set()
        values = await asyncio.gather(*tasks)
        assert len(set(values)) == 1
        assert len(session.encode_calls) == 1
        assert not executor._vibe_locks

    asyncio.run(run())


@pytest.mark.parametrize(
    "encoding,error",
    [
        (Response({"message": "Not enough Anlas"}, status=402), None),
        (Response(raw=b"", content_type="application/octet-stream"), None),
        (Response({"encoding": "not-binary"}, status=200), None),
        (
            Response(
                raw=b"<html>upstream error</html>", status=200, content_type="text/html"
            ),
            None,
        ),
        (None, TimeoutError()),
        (None, aiohttp.ClientConnectionError("offline failure")),
    ],
)
def test_failed_vibe_encoding_never_sends_a_generation_request_or_caches_an_embedding(
    encoding, error
):
    async def run():
        session = RecordingSession(encoding=encoding, encode_error=error)
        executor = ProviderExecutor(session)
        req = request(
            model=V45,
            mode="img2img",
            references=(reference(),),
            parameters={"reference_mode": "vibe"},
        )
        with pytest.raises(ProviderError, match="未发送生图请求"):
            await executor.generate(provider(model=V45), req)
        assert len(session.encode_calls) == 1
        assert session.generate_calls == []
        assert not executor._vibe_cache
        assert not executor._vibe_locks

    asyncio.run(run())


def test_failed_encoding_is_not_repeated_by_other_batch_chunks(monkeypatch):
    async def run():
        session = RecordingSession(encode_error=TimeoutError())
        executor = ProviderExecutor(session)
        current_time = [100.0]
        monkeypatch.setattr(
            "astrbot_plugin_image_studio.providers.time.monotonic",
            lambda: current_time[0],
        )
        configured = provider(model=V45)
        req = request(
            model=V45,
            mode="img2img",
            references=(reference(),),
            parameters={"reference_mode": "vibe"},
        )
        results = await asyncio.gather(
            *(executor.generate(configured, req) for _ in range(3)),
            return_exceptions=True,
        )
        assert all(isinstance(item, ProviderError) for item in results)
        assert len(session.encode_calls) == 1
        assert "60 秒" in str(results[-1])
        assert session.generate_calls == []
        current_time[0] += 61
        with pytest.raises(ProviderError):
            await executor.generate(configured, req)
        assert len(session.encode_calls) == 2

    asyncio.run(run())


def test_official_model_id_change_reconciles_schema_preserving_common_policies():
    original = provider(model=V45).public_dict()
    model = original["models"][0]
    model["parameters"]["steps"].update(
        default=31, webui_visible=False, record_in_history=False
    )
    model["parameters"]["reference_mode"]["default"] = "vibe"
    model["parameters"]["noise_schedule"]["default"] = "exponential"
    model["id"] = V5
    changed = ImageProvider.from_mapping(original).models[0]
    assert changed.parameters["steps"]["default"] == 31
    assert changed.parameters["steps"]["webui_visible"] is False
    assert changed.parameters["steps"]["record_in_history"] is False
    assert "variety_boost" not in changed.parameters
    assert "straight_alpha" in changed.parameters
    assert changed.parameters["reference_mode"]["default"] == "img2img"
    assert changed.parameters["noise_schedule"]["default"] == "karras"
    assert changed.max_reference_images == 2


def test_inpaint_service_composites_and_records_actual_fallback_model(tmp_path):
    async def run():
        mask = Image.new("RGB", (64, 64), "black")
        mask.paste("white", (32, 0, 64, 64))
        data = io.BytesIO()
        mask.save(data, "PNG")
        configured = provider(model=V5_CURATED)
        session = Session(Response({"images": [entry(png("blue"), seed=42)]}))
        store = GenerationStore(tmp_path)
        await store.initialize()
        service = ImageGenerationService(
            settings=RuntimeSettings(
                True, (configured,), HistorySettings(True, 20, 20, True), 0
            ),
            executor=ProviderExecutor(session),
            store=store,
        )
        result = await service.generate(
            mode="img2img",
            provider_id=configured.id,
            model_ref=f"{configured.id}:{V5_CURATED}",
            prompt="a robot",
            size="64x64",
            parameters={"reference_mode": "inpaint"},
            references=(reference(), reference(raw=data.getvalue(), name="mask")),
        )
        assert "V4.5 精选版" in result.warning
        with Image.open(io.BytesIO(result.images[0].data)) as output:
            assert output.convert("RGB").getpixel((8, 8)) == (255, 0, 0)
            assert output.convert("RGB").getpixel((56, 8)) == (0, 0, 255)
        detail = await store.generation_detail(result.generation_id)
        effective = detail["images"][0]["supplemental"]["effective_request"][
            "parameters"
        ]
        assert effective["actual_model"] == V45_CURATED + "-inpainting"
        assert effective["actual_action"] == "infill"
        initial = await service.reproduction_plan(result.generation_id)
        assert len(initial["references"]) == 2
        assert initial["references"][0]["width"] == 64
        with store._connect() as connection:
            connection.execute(
                "UPDATE generation_references SET available=0 WHERE generation_id=? AND ordinal=1",
                (result.generation_id,),
            )
        incomplete = await service.reproduction_plan(result.generation_id)
        assert incomplete["references"] == []
        assert any("顺序错位" in warning for warning in incomplete["warnings"])

    asyncio.run(run())


def test_failed_inpaint_composite_preserves_upstream_original_and_reports_partial():
    raw = png("blue")
    session = Session(Response({"images": [entry(raw)]}))
    req = request(
        model=V45,
        mode="img2img",
        size="128x128",
        references=(reference(), reference("white")),
        parameters={"reference_mode": "inpaint"},
    )
    with pytest.raises(ProviderPartialResponseError, match="未完成蒙版合成") as error:
        asyncio.run(ProviderExecutor(session).generate(provider(model=V45), req))
    assert error.value.images[0].data == raw
    assert len(session.calls) == 1


def test_late_base_validation_still_happens_before_any_vibe_encoding():
    session = RecordingSession()
    req = request(
        model=V45,
        mode="img2img",
        references=(reference(), reference(raw=b"not-an-image")),
        parameters={
            "reference_mode": "vibe",
            "reference_settings": [{"type": "vibe"}, {"type": "base"}],
        },
    )
    with pytest.raises(ProviderError, match="图片数据损坏|参考图"):
        asyncio.run(ProviderExecutor(session).generate(provider(model=V45), req))
    assert not session.calls


def test_second_vibe_failure_preserves_first_encoding_but_does_not_generate():
    class SecondEncodingFails(RecordingSession):
        def post(self, url, **kwargs):
            if url.endswith("/ai/encode-vibe") and self.encode_calls:
                self.calls.append((url, kwargs))
                return Response({"message": "insufficient balance"}, status=402)
            return super().post(url, **kwargs)

    async def run():
        session = SecondEncodingFails()
        executor = ProviderExecutor(session)
        req = request(
            model=V45,
            mode="img2img",
            references=(reference(), reference("blue")),
            parameters={"reference_mode": "vibe"},
        )
        with pytest.raises(ProviderError, match="未发送生图请求"):
            await executor.generate(provider(model=V45), req)
        assert len(session.encode_calls) == 2
        assert not session.generate_calls
        assert len(executor._vibe_cache) == 1
        assert not executor._vibe_locks

    asyncio.run(run())


def test_reusing_the_same_vibe_with_changed_strength_does_not_charge_a_second_encoding():
    async def run():
        session = RecordingSession()
        executor = ProviderExecutor(session)
        configured = provider(model=V45)
        original = request(
            model=V45,
            mode="img2img",
            references=(reference(),),
            parameters={
                "reference_mode": "vibe",
                "reference_settings": [{"type": "vibe", "strength": 0.4}],
            },
        )
        await executor.generate(configured, original)
        changed = replace(
            original,
            parameters={
                "reference_mode": "vibe",
                "reference_settings": [{"type": "vibe", "strength": 0.9}],
            },
        )
        await executor.generate(configured, changed)
        assert len(session.encode_calls) == 1
        assert [
            call[1]["json"]["parameters"]["reference_strength_multiple"]
            for call in session.generate_calls
        ] == [[0.4], [0.9]]
        assert SECRET not in json.dumps(list(executor._vibe_cache))

    asyncio.run(run())


@pytest.mark.parametrize("model,information", [(V45, 0.7), (V45_CURATED, 1.0)])
def test_vibe_defaults_and_disabled_normalization_match_reference_intent(
    model, information
):
    payload, effective, vibes = prepare(
        model,
        refs=(reference(), reference("blue")),
        parameters={
            "reference_mode": "vibe",
            "normalize_reference_strength_multiple": False,
        },
    )
    assert [item["information_extracted"] for item in vibes] == [
        information,
        information,
    ]
    assert payload["parameters"]["reference_strength_multiple"] == [0.6, 0.6]
    assert effective["normalize_reference_strength_multiple"] is False
    assert all(
        key not in payload["parameters"]
        for key in (
            "reference_settings",
            "reference_mode",
            "characters",
            "quality_preset",
        )
    )


@pytest.mark.parametrize("model,limit,mode", [(V45, 8, "vibe"), (V5, 2, "inpaint")])
def test_plugin_reference_limit_rejects_whole_batch_without_silent_truncation(
    model, limit, mode
):
    with pytest.raises(ValueError, match=f"最多接受 {limit}"):
        prepare(
            model,
            refs=(reference(),) * (limit + 1),
            parameters={"reference_mode": mode},
        )


@pytest.mark.parametrize(
    "parameter,value,match",
    [
        ("steps", 51, "steps"),
        ("steps", {}, "steps"),
        ("scale", 10.1, "scale"),
        ("scale", [], "scale"),
        ("seed", True, "seed"),
        ("sampler", "new_unknown_sampler", "sampler"),
        ("sampler", "k_dpm_2", "karras|Karras"),
        ("sampler", {}, "sampler"),
        ("image_format", {}, "image_format"),
        ("quality_preset", {}, "质量"),
        ("reference_settings", [{"type": {}}], "type"),
    ],
)
def test_invalid_wire_scalars_and_enums_never_start_paid_encoding(
    parameter, value, match
):
    session = RecordingSession()
    req = request(
        model=V45,
        mode="img2img",
        references=(reference(),),
        parameters={"reference_mode": "vibe", parameter: value},
    )
    with pytest.raises(ProviderError, match=match):
        asyncio.run(ProviderExecutor(session).generate(provider(model=V45), req))
    assert not session.calls


@pytest.mark.parametrize("reference_mode", ["precise", "vibe"])
def test_transparent_reference_pixels_are_matted_black_not_hidden_rgb(reference_mode):
    source = Image.new("RGBA", (64, 64), (255, 0, 0, 0))
    source.paste((0, 0, 255, 255), (16, 16, 48, 48))
    output = io.BytesIO()
    source.save(output, format="PNG")
    payload, _, vibes = prepare(
        refs=(reference(raw=output.getvalue()),),
        parameters={"reference_mode": reference_mode},
    )
    encoded = (
        payload["parameters"]["director_reference_images"][0]
        if reference_mode == "precise"
        else vibes[0]["image"]
    )
    with Image.open(io.BytesIO(base64.b64decode(encoded))) as image:
        assert image.format == "PNG"
        assert image.convert("RGB").getpixel((1, 1)) == (0, 0, 0)
        assert image.convert("RGB").getpixel((image.width // 2, image.height // 2)) == (
            0,
            0,
            255,
        )


@pytest.mark.parametrize("model,expected", [(V45, 0.7), (V5, 0.6)])
def test_character_grid_boundary_matches_official_cell_selection(model, expected):
    payload, effective, _ = prepare(
        model,
        parameters={
            "characters": [
                {"prompt": "subject", "negative_prompt": "hat", "x": 0.6, "y": 0.6}
            ],
            "use_coords": True,
        },
    )
    expected_point = {"x": expected, "y": expected}
    assert payload["parameters"]["v4_prompt"]["caption"]["char_captions"][0][
        "centers"
    ] == [expected_point]
    assert payload["parameters"]["v4_negative_prompt"]["caption"]["char_captions"][0][
        "centers"
    ] == [expected_point]
    assert effective["characters"][0]["x"] == expected


@pytest.mark.parametrize("size,limit", [("704x512", 8), ("640x640", 6), ("768x768", 4)])
def test_native_batch_capacity_is_resolution_dependent(size, limit):
    req = request(model=V45, size=size, count=limit)
    payload, _, _ = prepare_generation_payload(req, V45)
    assert payload["parameters"]["n_samples"] == limit
    with pytest.raises(ValueError, match=f"最多支持 {limit}"):
        prepare_generation_payload(replace(req, count=limit + 1), V45)


def test_output_pixel_limit_is_checked_before_paid_vibe_encoding():
    session = RecordingSession()
    req = request(
        model=V45,
        size="4096x1024",
        mode="img2img",
        references=(reference(),),
        parameters={"reference_mode": "vibe"},
    )
    with pytest.raises(ProviderError, match="像素上限"):
        asyncio.run(ProviderExecutor(session).generate(provider(model=V45), req))
    assert not session.calls


@pytest.mark.parametrize("sampler", ["k_dpm_fast", "k_dpmpp_3m_sde"])
def test_samplers_without_a_noise_schedule_omit_it_on_the_wire(sampler):
    payload, _, _ = prepare(parameters={"sampler": sampler})
    assert payload["parameters"]["sampler"] == sampler
    assert "noise_schedule" not in payload["parameters"]
