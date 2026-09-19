"""Regression checks against reviewed official payload and image semantics.

All provider responses are simulated; these tests never use an account.
"""

from __future__ import annotations

import asyncio
import base64
import io

import pytest
from PIL import Image

from astrbot_plugin_image_studio.backend.providers.novelai.protocol import (
    NOVELAI_MODEL_IDS,
)
from astrbot_plugin_image_studio.backend.providers.executor import ProviderExecutor
from astrbot_plugin_image_studio.tests.backend.providers.test_novelai_advanced import (
    prepare,
    reference,
)
from astrbot_plugin_image_studio.tests.backend.providers.test_novelai_provider import (
    Response,
    Session,
    entry,
    provider,
    request,
)


@pytest.mark.parametrize("model", NOVELAI_MODEL_IDS)
def test_default_euler_request_explicitly_selects_modern_compatibility(model):
    payload, _, _ = prepare(model)
    params = payload["parameters"]
    assert params["sampler"] == "k_euler_ancestral"
    assert params["deliberate_euler_ancestral_bug"] is False
    assert params["prefer_brownian"] is True
    payload, _, _ = prepare(model, parameters={"sampler": "k_euler"})
    assert "deliberate_euler_ancestral_bug" not in payload["parameters"]
    assert "prefer_brownian" not in payload["parameters"]


@pytest.mark.parametrize("model", NOVELAI_MODEL_IDS)
@pytest.mark.parametrize("sampler", ["k_dpm_fast", "k_dpmpp_3m_sde"])
def test_schedule_omission_depends_on_actual_model(model, sampler):
    payload, _, _ = prepare(model, parameters={"sampler": sampler})
    assert payload["parameters"].get("noise_schedule") == (
        "karras" if model.startswith("nai-diffusion-5-") else None
    )
    payload, _, _ = prepare(
        model,
        refs=(reference(), reference("white")),
        parameters={"sampler": sampler, "reference_mode": "inpaint"},
    )
    assert payload["parameters"].get("noise_schedule") == (
        "karras" if model == "nai-diffusion-5-full" else None
    )


@pytest.mark.parametrize("model", NOVELAI_MODEL_IDS)
@pytest.mark.parametrize("inpaint", [False, True])
def test_transparent_base_matches_model_alpha_capability(model, inpaint):
    source = Image.new("RGBA", (64, 64), (240, 20, 10, 0))
    source.putpixel((1, 0), (0, 0, 0, 128))
    raw = io.BytesIO()
    source.save(raw, "PNG")
    base = reference(raw=raw.getvalue())
    payload, _, _ = prepare(
        model,
        refs=(base, reference("white")) if inpaint else (base,),
        parameters={"reference_mode": "inpaint"} if inpaint else {},
    )
    alpha = payload["model"].startswith("nai-diffusion-5-")
    with Image.open(
        io.BytesIO(base64.b64decode(payload["parameters"]["image"]))
    ) as image:
        assert image.mode == ("RGBA" if alpha else "RGB")
        assert image.getpixel((0, 0)) == (
            (240, 20, 10, 0) if alpha else (255, 255, 255)
        )
        assert image.getpixel((1, 0)) == ((0, 0, 0, 128) if alpha else (127, 127, 127))
    assert base.data == raw.getvalue(), "source asset must remain unmodified"


@pytest.mark.parametrize("model", NOVELAI_MODEL_IDS)
def test_inpaint_defaults_ignore_ordinary_img2img_strength(model):
    payload, effective, _ = prepare(
        model,
        refs=(reference(), reference("white")),
        parameters={"reference_mode": "inpaint", "strength": 0.7, "noise": 0.2},
    )
    assert effective["inpaint_strength"] == 1
    assert "img2img" not in payload["parameters"]
    for key in ("strength", "noise", "extra_noise_seed", "inpaint_strength"):
        assert key not in payload["parameters"]
    payload, effective, _ = prepare(model, refs=(reference(),))
    assert payload["parameters"]["strength"] == 0.7
    assert "inpaint_strength" not in effective


@pytest.mark.parametrize("strength", [0, 0.4, 1])
def test_inpaint_strength_is_independent_and_preserves_explicit_zero(strength):
    payload, effective, _ = prepare(
        refs=(reference(), reference("white")),
        parameters={
            "reference_mode": "inpaint",
            "inpaint_strength": strength,
            "strength": 0.7,
            "color_correct": False,
        },
    )
    assert effective["inpaint_strength"] == strength
    if strength < 1:
        assert payload["parameters"]["img2img"] == {
            "strength": strength,
            "color_correct": False,
        }
    else:
        assert "img2img" not in payload["parameters"]


@pytest.mark.parametrize("mode", ["text2img", "img2img"])
def test_provider_uses_the_configured_path_for_the_workspace_mode(mode):
    session = Session(Response({"images": [entry()]}))
    configured = provider(generate_path="/custom/generate", edit_path="/custom/edit")
    asyncio.run(
        ProviderExecutor(session).generate(
            configured,
            request(mode=mode, references=(reference(),) if mode == "img2img" else ()),
        )
    )
    assert session.calls[0][1] == configured.base_url + (
        "/custom/edit" if mode == "img2img" else "/custom/generate"
    )
