from __future__ import annotations

import json
import math

import pytest
from astrbot_plugin_image_studio.config import HistorySettings, RuntimeSettings
from astrbot_plugin_image_studio.models import ImageProvider
from astrbot_plugin_image_studio.novelai_inputs import prepare_advanced
from astrbot_plugin_image_studio.parameter_exchange import resolve_parameters
from astrbot_plugin_image_studio.tests.test_novelai_provider import request

V45 = "nai-diffusion-4-5-full"
V5 = "nai-diffusion-5-full"


def settings(*models):
    provider = ImageProvider.from_mapping(
        {
            "id": "official",
            "kind": "novelai_official",
            "models": list(models) or [{"id": V45}, {"id": V5}],
        }
    )
    return RuntimeSettings(True, (provider,), HistorySettings(True, 100, 100, True), 0)


def wire(parameters, *, model=V45, action="generate"):
    return json.dumps(
        {
            "input": "mountain landscape",
            "model": model,
            "action": action,
            "parameters": {
                "width": 832,
                "height": 1216,
                "steps": 28,
                "uc": "blurry",
                **parameters,
            },
        }
    )


def caption(text, x=0.5, y=0.5):
    return {"char_caption": text, "centers": [{"x": x, "y": y}]}


def character_parameters():
    return {
        "v4_prompt": {
            "caption": {
                "base_caption": "mountain landscape",
                "char_captions": [caption("red hair", 0.1), caption("blue hair", 0.9)],
            },
            "use_coords": True,
        },
        "v4_negative_prompt": {
            "caption": {
                "base_caption": "blurry",
                "char_captions": [caption("hat", 0.1), caption("glasses", 0.9)],
            }
        },
    }


def precise_parameters():
    return {
        "director_reference_images": [
            "PRIVATE_IMAGE_A" * 1000,
            "PRIVATE_IMAGE_B" * 1000,
        ],
        "director_reference_descriptions": [
            {"caption": {"base_caption": "character", "char_captions": []}},
            {"caption": {"base_caption": "character&style", "char_captions": []}},
        ],
        "director_reference_strength_values": [0.7, 0.4],
        "director_reference_secondary_strength_values": [0.2, 0.6],
        "director_reference_information_extracted": [1, 1],
    }


@pytest.mark.parametrize("model", [V45, V5])
def test_official_characters_pair_captions_and_positions_in_original_order(model):
    result = resolve_parameters(wire(character_parameters(), model=model), settings())
    parameters = result["draft"]["parameters"]
    assert parameters["characters"] == [
        {"prompt": "red hair", "negative_prompt": "hat", "x": 0.1, "y": 0.5},
        {"prompt": "blue hair", "negative_prompt": "glasses", "x": 0.9, "y": 0.5},
    ]
    assert parameters["use_coords"] is True
    assert "characters" not in result["unmapped"]


def test_official_ambiguous_character_positions_are_not_silently_collapsed():
    parameters = character_parameters()
    parameters["v4_prompt"]["caption"]["char_captions"][0]["centers"].append(
        {"x": 0.5, "y": 0.5}
    )
    result = resolve_parameters(wire(parameters), settings())
    assert result["draft"]["parameters"]["characters"] == []
    assert (
        result["unmapped"]["characters"]["prompt"][0]["centers"]
        == parameters["v4_prompt"]["caption"]["char_captions"][0]["centers"]
    )
    assert any("无法安全配对" in warning for warning in result["warnings"])


def test_precise_reference_metadata_selects_img2img_without_copying_image_bytes():
    result = resolve_parameters(wire(precise_parameters()), settings())
    draft = result["draft"]
    assert draft["mode"] == "img2img"
    assert draft["model_ref"] == f"official:{V45}"
    assert draft["references"] == []
    assert draft["parameters"]["reference_mode"] == "precise"
    assert draft["parameters"]["reference_settings"] == [
        {
            "type": "character",
            "strength": 0.7,
            "fidelity": 0.8,
            "information_extracted": 1,
        },
        {
            "type": "character_style",
            "strength": 0.4,
            "fidelity": 0.4,
            "information_extracted": 1,
        },
    ]
    assert "PRIVATE_IMAGE" not in json.dumps(result)
    assert any("原始编号顺序" in warning for warning in result["warnings"])


def test_precise_settings_without_embedded_originals_can_still_preserve_input_order():
    parameters = precise_parameters()
    parameters.pop("director_reference_images")
    result = resolve_parameters(wire(parameters), settings())
    assert [
        item["type"] for item in result["draft"]["parameters"]["reference_settings"]
    ] == ["character", "character_style"]
    assert result["draft"]["references"] == []


def test_misaligned_precise_arrays_are_reported_without_reordering_or_image_leak():
    parameters = precise_parameters()
    parameters["director_reference_strength_values"] = [0.5]
    result = resolve_parameters(wire(parameters), settings())
    assert result["draft"]["parameters"]["reference_settings"] == []
    assert result["unmapped"]["director_reference_strength_values"] == [0.5]
    assert "PRIVATE_IMAGE" not in json.dumps(result)
    assert any("数量与逐图设置不一致" in warning for warning in result["warnings"])


@pytest.mark.parametrize("model", [V45, V5])
def test_infill_maps_inpainting_variant_to_base_model_and_rebuilds_reference_roles(
    model,
):
    result = resolve_parameters(
        wire(
            {
                "image": "PRIVATE_BASE",
                "mask": "PRIVATE_MASK",
                "strength": 0.8,
                "color_correct": False,
            },
            model=model + "-inpainting",
            action="infill",
        ),
        settings(),
    )
    assert result["draft"]["model_ref"] == f"official:{model}"
    assert result["draft"]["mode"] == "img2img"
    assert result["draft"]["parameters"]["reference_mode"] == "inpaint"
    assert result["draft"]["parameters"]["reference_settings"] == [
        {"type": "base"},
        {"type": "mask"},
    ]
    assert result["draft"]["parameters"]["inpaint_strength"] == 0.8
    assert result["draft"]["parameters"]["strength"] == 0.7
    assert result["draft"]["parameters"]["color_correct"] is False
    assert "PRIVATE_" not in json.dumps(result)
    assert any("第 1 张底图、第 2 张蒙版" in warning for warning in result["warnings"])


@pytest.mark.parametrize("strength", [0, 0.4, 1])
def test_official_nested_infill_strength_is_restored_including_explicit_zero(strength):
    result = resolve_parameters(
        wire(
            {
                "image": "PRIVATE_BASE",
                "mask": "PRIVATE_MASK",
                "img2img": {"strength": strength, "color_correct": False},
            },
            model=V5 + "-inpainting",
            action="infill",
        ),
        settings(),
    )
    assert result["draft"]["parameters"]["inpaint_strength"] == strength
    assert result["draft"]["parameters"]["strength"] == 0.7
    assert result["draft"]["parameters"]["color_correct"] is False
    assert "img2img" not in result["unmapped"]


def test_official_infill_without_img2img_influence_uses_full_redraw_default():
    result = resolve_parameters(
        wire(
            {"image": "PRIVATE_BASE", "mask": "PRIVATE_MASK"},
            model=V45 + "-inpainting",
            action="infill",
        ),
        settings(),
    )
    assert result["draft"]["parameters"]["inpaint_strength"] == 1


@pytest.mark.parametrize(
    "historical,expected",
    [
        ({"strength": 0}, 0),
        ({"strength": 0.4}, 0.4),
        ({"strength": 0.7, "inpaint_strength": 0}, 0),
    ],
)
def test_legacy_infill_history_migrates_strength_without_overriding_new_control(
    historical, expected
):
    result = resolve_parameters(
        json.dumps(
            {
                "format": "image_studio",
                "version": 1,
                "generation_engine": "novelai",
                "has_request_snapshot": True,
                "data": {
                    "model": V45,
                    "mode": "img2img",
                    "parameters": {"reference_mode": "inpaint", **historical},
                },
            }
        ),
        settings(),
        for_reproduction=True,
    )
    assert result["draft"]["parameters"]["inpaint_strength"] == expected
    assert result["draft"]["parameters"]["strength"] == 0.7


def test_unhandled_nested_infill_parameters_remain_visible_for_manual_review():
    result = resolve_parameters(
        wire(
            {"img2img": {"strength": 0.6, "noise": 0.2, "image": "PRIVATE_BASE"}},
            action="infill",
        ),
        settings(),
    )
    assert result["draft"]["parameters"]["inpaint_strength"] == 0.6
    assert result["unmapped"]["img2img"] == {"noise": 0.2}
    assert "PRIVATE_BASE" not in json.dumps(result)


@pytest.mark.parametrize(
    "prompt,expected",
    [
        ("a cat, transparent background", "a cat, transparent background"),
        ("Transparent Background, a cat", "Transparent Background, a cat"),
        (
            "a cat, transparent background, white fur",
            "a cat, transparent background, white fur",
        ),
        (
            "a cat beside a transparent background panel",
            "a cat beside a transparent background panel, transparent background",
        ),
    ],
)
def test_raw_official_transparent_prompt_refill_does_not_expand_existing_tag_twice(
    prompt, expected
):
    raw = json.loads(wire({"tag_hint_transparent_background": True}, model=V5))
    raw["input"] = prompt
    draft = resolve_parameters(json.dumps(raw), settings())["draft"]
    assert draft["prompt"] == prompt
    for _ in range(2):
        fields, _, _, _, _, expanded = prepare_advanced(
            request(prompt=draft["prompt"], parameters=draft["parameters"]),
            V5,
            832,
            1216,
        )
        assert expanded == expected
        assert fields["v4_prompt"]["caption"]["base_caption"] == expected
        raw["input"] = expanded
        draft = resolve_parameters(json.dumps(raw), settings())["draft"]


def test_disabled_transparency_hint_does_not_remove_explicit_prompt_tags():
    prompt = "a cat, transparent background"
    fields, _, _, _, _, expanded = prepare_advanced(
        request(prompt=prompt, parameters={"tag_hint_transparent_background": False}),
        V5,
        832,
        1216,
    )
    assert expanded == prompt
    assert fields["tag_hint_transparent_background"] is False


def test_vibe_metadata_retains_per_image_strength_and_information_without_embeddings():
    result = resolve_parameters(
        wire(
            {
                "reference_image_multiple": ["PRIVATE_VIBE_A", "PRIVATE_VIBE_B"],
                "reference_strength_multiple": [0.4, 0.6],
                "reference_information_extracted_multiple": [0.8, 1],
                "normalize_reference_strength_multiple": False,
            }
        ),
        settings(),
    )
    parameters = result["draft"]["parameters"]
    assert result["draft"]["mode"] == "img2img"
    assert parameters["reference_mode"] == "vibe"
    assert parameters["reference_settings"] == [
        {"type": "vibe", "strength": 0.4, "information_extracted": 0.8},
        {"type": "vibe", "strength": 0.6, "information_extracted": 1},
    ]
    assert parameters["normalize_reference_strength_multiple"] is False
    assert "PRIVATE_VIBE" not in json.dumps(result)


def test_v5_precise_reference_is_unmapped_with_an_explicit_model_support_warning():
    result = resolve_parameters(wire(precise_parameters(), model=V5), settings())
    assert result["draft"]["parameters"]["reference_mode"] == "img2img"
    assert result["draft"]["parameters"]["reference_settings"] == []
    assert result["unmapped"]["reference_mode"] == "precise"
    assert len(result["unmapped"]["reference_settings"]) == 2
    assert any(
        "不支持 precise" in warning and "不能直接复现" in warning
        for warning in result["warnings"]
    )


@pytest.mark.parametrize(
    "policy", [{"webui_visible": False}, {"refill_from_history": False}]
)
def test_new_fields_still_obey_current_schema_refill_controls(policy):
    current = settings(
        {
            "id": V45,
            "parameters": {"characters": {"type": "json", "default": [], **policy}},
        }
    )
    result = resolve_parameters(
        wire(character_parameters()), current, for_reproduction=True
    )
    assert result["draft"]["parameters"]["characters"] == []


def test_exact_snapshot_never_recovers_unrecorded_fields_from_embedded_metadata():
    result = resolve_parameters(
        json.dumps(
            {
                "format": "image_studio",
                "version": 1,
                "generation_engine": "novelai",
                "has_request_snapshot": True,
                "data": {
                    "model": V45,
                    "prompt": "mountain",
                    "mode": "text2img",
                    "parameters": {},
                },
                "metadata": {"raw": {"Comment": json.dumps(character_parameters())}},
            }
        ),
        settings(),
        for_reproduction=True,
    )
    assert result["draft"]["parameters"]["characters"] == []


def test_unresolved_model_draft_also_excludes_embedded_images():
    result = resolve_parameters(
        json.dumps(
            {
                "format": "image_studio",
                "version": 1,
                "generation_engine": "novelai",
                "has_request_snapshot": True,
                "data": {
                    "model": "unknown-model",
                    "parameters": {
                        "director_reference_images": ["PRIVATE_IMAGE"],
                        "custom": {"image": "PRIVATE_NESTED"},
                    },
                },
            }
        ),
        settings(),
    )
    assert result["requires_model_selection"]
    assert "PRIVATE_" not in json.dumps(result)


@pytest.mark.parametrize(
    "action,expected",
    [
        ("img2img", ["base", "character", "character_style"]),
        ("infill", ["base", "mask", "character", "character_style"]),
    ],
)
def test_precise_reference_combines_with_base_and_inpainting_in_canonical_upload_order(
    action, expected
):
    parameters = {**precise_parameters(), "image": "PRIVATE_BASE"}
    if action == "infill":
        parameters["mask"] = "PRIVATE_MASK"
    result = resolve_parameters(wire(parameters, action=action), settings())
    assert [
        item["type"] for item in result["draft"]["parameters"]["reference_settings"]
    ] == expected
    assert "reference_settings" not in result["unmapped"]
    assert "PRIVATE_" not in json.dumps(result)


def test_base_and_vibe_settings_preserve_the_base_first():
    result = resolve_parameters(
        wire(
            {
                "image": "PRIVATE_BASE",
                "reference_image_multiple": ["PRIVATE_VIBE"],
                "reference_strength_multiple": [0.7],
            },
            action="img2img",
        ),
        settings(),
    )
    assert result["draft"]["parameters"]["reference_settings"] == [
        {"type": "base"},
        {"type": "vibe", "strength": 0.7},
    ]


def test_precise_and_vibe_combination_cannot_silently_drop_the_vibe():
    result = resolve_parameters(
        wire(
            {
                **precise_parameters(),
                "reference_image_multiple": ["PRIVATE_VIBE"],
                "reference_strength_multiple": [0.7],
            }
        ),
        settings(),
    )
    assert result["draft"]["parameters"]["reference_settings"] == []
    assert result["unmapped"]["reference_combination"][
        "reference_strength_multiple"
    ] == [0.7]
    assert any("组合不受支持" in warning for warning in result["warnings"])
    assert "PRIVATE_" not in json.dumps(result)


def test_imported_gallery_metadata_restores_advanced_fields_but_manual_values_win():
    characters = [
        {"prompt": "manual character", "negative_prompt": "hat", "x": 0.5, "y": 0.5}
    ]
    result = resolve_parameters(
        json.dumps(
            {
                "format": "image_studio",
                "version": 1,
                "generation_engine": "novelai",
                "has_request_snapshot": False,
                "data": {
                    "model": V45,
                    "mode": "text2img",
                    "prompt": "manual prompt",
                    "parameters": {},
                },
                "metadata": {
                    "format": "novelai",
                    "raw": {
                        "Comment": json.dumps(
                            {**character_parameters(), "uc": "blurry", "steps": 28}
                        )
                    },
                },
                "supplemental": {
                    "overrides": {"parameters": {"characters": characters}}
                },
            }
        ),
        settings(),
    )
    assert result["draft"]["prompt"] == "manual prompt"
    assert result["draft"]["parameters"]["characters"] == characters


def test_v5_curated_inpainting_uses_the_underlying_inpainting_character_limit():
    model = "nai-diffusion-5-curated"
    result = resolve_parameters(
        json.dumps(
            {
                "format": "image_studio",
                "version": 1,
                "generation_engine": "novelai",
                "has_request_snapshot": True,
                "data": {
                    "model": model,
                    "mode": "img2img",
                    "parameters": {
                        "reference_mode": "inpaint",
                        "characters": [{"prompt": str(index)} for index in range(7)],
                    },
                },
            }
        ),
        settings({"id": model}),
        for_reproduction=True,
    )
    assert result["draft"]["parameters"]["characters"] == []
    assert len(result["unmapped"]["characters"]) == 7
    assert any("当前模式支持的 6 个" in warning for warning in result["warnings"])


def test_unknown_official_image_model_is_not_silently_replaced_by_available_one():
    result = resolve_parameters(
        wire(character_parameters(), model="nai-diffusion-future"), settings()
    )
    assert result["requires_model_selection"]
    assert result["draft"]["model_ref"] == ""


@pytest.mark.parametrize("value", [-0.1, 1.1, "0.5", True])
def test_invalid_precise_strength_names_the_image_and_field(value):
    parameters = precise_parameters()
    parameters["director_reference_strength_values"][1] = value
    result = resolve_parameters(wire(parameters), settings())
    assert result["draft"]["parameters"]["reference_settings"] == []
    assert any(
        "第 2 张" in warning and "director_reference_strength_values" in warning
        for warning in result["warnings"]
    )


def test_malformed_precise_caption_is_reported_as_an_unknown_type():
    parameters = precise_parameters()
    parameters["director_reference_descriptions"][0]["caption"] = []
    result = resolve_parameters(wire(parameters), settings())
    assert result["draft"]["parameters"]["reference_settings"] == []
    assert any(
        "第 1 张精准参考的类型无法识别" in warning for warning in result["warnings"]
    )


@pytest.mark.parametrize("threshold,enabled", [(58, True), (None, False)])
def test_variety_boost_recognizes_only_the_reviewed_official_threshold(
    threshold, enabled
):
    result = resolve_parameters(wire({"skip_cfg_above_sigma": threshold}), settings())
    assert result["draft"]["parameters"]["variety_boost"] is enabled
    assert "skip_cfg_above_sigma" not in result["unmapped"]


def test_nonstandard_variety_threshold_is_not_approximated():
    result = resolve_parameters(wire({"skip_cfg_above_sigma": 19}), settings())
    assert result["draft"]["parameters"]["variety_boost"] is False
    assert result["unmapped"]["skip_cfg_above_sigma"] == 19
    assert any("阈值 58" in warning for warning in result["warnings"])


@pytest.mark.parametrize("width,height", [(1024, 1024), (1216, 832), (1536, 1024)])
def test_variety_boost_recovers_resolution_scaled_official_threshold(width, height):
    threshold = 58 * math.sqrt(((width // 8) * (height // 8)) / (104 * 152))
    result = resolve_parameters(
        wire({"width": width, "height": height, "skip_cfg_above_sigma": threshold}),
        settings(),
    )
    assert result["draft"]["parameters"]["variety_boost"] is True
    assert "skip_cfg_above_sigma" not in result["unmapped"]


def test_variety_boost_does_not_confuse_baseline_threshold_with_square_resolution():
    result = resolve_parameters(
        wire({"width": 1024, "height": 1024, "skip_cfg_above_sigma": 58}), settings()
    )
    assert result["draft"]["parameters"]["variety_boost"] is False
    assert result["unmapped"]["skip_cfg_above_sigma"] == 58
