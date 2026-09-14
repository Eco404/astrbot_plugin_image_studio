from __future__ import annotations

import copy

import pytest
from astrbot_plugin_image_studio.models import ImageProvider
from astrbot_plugin_image_studio.novelai_catalog import MODEL_NAMES, model_capabilities


def model(model_id, parameters=None):
    return ImageProvider.from_mapping(
        {
            "id": "offline",
            "kind": "novelai_official",
            "models": [{"id": model_id, "parameters": parameters or {}}],
        }
    ).models[0]


@pytest.mark.parametrize("model_id", MODEL_NAMES)
def test_new_models_have_separate_inpaint_and_img2img_strengths(model_id):
    current = model(model_id)
    assert current.parameters["strength"]["default"] == 0.7
    infill = current.parameters["inpaint_strength"]
    assert infill["default"] == 1
    assert infill["modes"] == ["img2img"]
    assert infill["record_in_history"] and infill["refill_from_history"]
    assert infill["webui_visible"]


@pytest.mark.parametrize("model_id", MODEL_NAMES)
def test_each_exposed_sampler_has_a_compatible_noise_schedule(model_id):
    current = model(model_id)
    caps = model_capabilities(model_id)
    assert current.parameters["sampler"]["choices"] == caps["samplers"]
    for sampler in current.parameters["sampler"]["choices"]:
        assert set(caps["sampler_noise_schedules"][sampler]) & set(
            current.parameters["noise_schedule"]["choices"]
        )
    if model_id.startswith("nai-diffusion-5-"):
        assert "k_dpm_2" not in caps["samplers"]
        assert all(
            value == ["karras"] for value in caps["sampler_noise_schedules"].values()
        )
    else:
        assert caps["sampler_noise_schedules"]["k_dpm_2"] == [
            "exponential",
            "polyexponential",
        ]


@pytest.mark.parametrize(
    "model_id", ["nai-diffusion-5-full", "nai-diffusion-5-curated"]
)
def test_v5_defaults_to_straight_alpha_without_requesting_transparent_background(
    model_id,
):
    current = model(model_id)
    assert current.parameters["straight_alpha"]["default"] is True
    assert current.parameters["straight_alpha"]["label"] == "直通 Alpha"
    assert current.parameters["tag_hint_transparent_background"]["default"] is False


def test_saved_defaults_and_behavior_survive_schema_updates():
    original = copy.deepcopy(model("nai-diffusion-5-full").parameters)
    original.pop("inpaint_strength")
    original["strength"]["default"] = 0.45
    original["sampler"]["default"] = "k_euler"
    original["straight_alpha"].update(
        label="透明通道输出",
        default=False,
        webui_visible=False,
        record_in_history=False,
        refill_from_history=False,
    )
    before = copy.deepcopy(original)
    current = model("nai-diffusion-5-full", original)
    assert original == before
    assert current.parameters["strength"]["default"] == 0.45
    assert current.parameters["inpaint_strength"]["default"] == 1
    assert current.parameters["sampler"]["default"] == "k_euler"
    alpha = current.parameters["straight_alpha"]
    assert alpha["label"] == "直通 Alpha"
    assert alpha["default"] is False
    assert not alpha["webui_visible"] and not alpha["record_in_history"]
    assert not alpha["refill_from_history"]


def test_saved_renamed_sampler_gets_current_supported_options():
    original = copy.deepcopy(model("nai-diffusion-5-curated").parameters)
    original["my_sampler"] = original.pop("sampler")
    original["my_sampler"].update(request_key="sampler", default="k_dpm_2")
    current = model("nai-diffusion-5-curated", original)
    sampler = current.parameters["my_sampler"]
    assert "sampler" not in current.parameters
    assert "k_dpm_2" not in sampler["choices"]
    assert sampler["default"] == "k_euler_ancestral"
