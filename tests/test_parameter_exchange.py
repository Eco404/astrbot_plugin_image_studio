from __future__ import annotations

import copy
import json
from dataclasses import replace

import pytest

from astrbot_plugin_image_studio.config import HistorySettings, RuntimeSettings
from astrbot_plugin_image_studio.image_metadata import (
    parse_metadata_fields,
    parse_parameter_text,
)
from astrbot_plugin_image_studio.models import ImageProvider
from astrbot_plugin_image_studio.parameter_exchange import (
    export_parameters,
    resolve_parameters,
)


def test_comfyui_import_copy_keeps_condition_structure_and_summary_warning():
    conditions = {
        "2:0": {"operation": "text", "texts": ["a mountain"]},
        "3:0": {"operation": "text", "texts": ["morning sunlight"]},
        "4:0": {"operation": "combine", "inputs": [{"ref": "2:0"}, {"ref": "3:0"}]},
    }
    content = {
        "format": "image_studio",
        "version": 1,
        "generation_engine": "comfyui",
        "has_request_snapshot": False,
        "data": {"mode": "text2img", "prompt": "a mountain\nmorning sunlight"},
        "metadata": {
            "format": "comfyui",
            "warnings": ["多个阶段使用不同的正向条件"],
            "normalized": {
                "prompt_status": "summary",
                "condition_nodes": conditions,
                "stages": [{"node_id": "5", "positive_conditioning": "4:0"}],
                "outputs": [{"node_id": "7", "kind": "save", "stage_ids": ["5"]}],
            },
        },
        "supplemental": {},
    }
    result = resolve_parameters(
        json.dumps(content), settings(), "nai:nai-diffusion-4-5-full"
    )
    assert result["draft"]["prompt"] == content["data"]["prompt"]
    assert result["unmapped"]["condition_nodes"] == conditions
    assert result["unmapped"]["outputs"] == content["metadata"]["normalized"]["outputs"]
    assert any("不等价于原始条件" in warning for warning in result["warnings"])
    assert "多个阶段使用不同的正向条件" in result["warnings"]


def test_fenced_studio_parameters_preserve_large_integer_and_import_data():
    content = json.dumps(
        {
            "format": "image_studio",
            "version": 1,
            "generation_engine": "nai",
            "has_request_snapshot": False,
            "data": {
                "model_ref": "nai:nai-diffusion-4-5-full",
                "parameters": {"seed": 9007199254740993},
            },
            "metadata": {"normalized": {"steps": 20, "cfg_rescale": 0}},
            "supplemental": {
                "overrides": {
                    "negative_prompt": "",
                    "parameters": {"artist": "manual artist"},
                }
            },
        }
    )
    direct = resolve_parameters(content, settings())
    fenced = resolve_parameters(f"```json\n{content}\n```", settings())
    assert direct == fenced
    assert direct["unmapped"]["seed"] == "9007199254740993"
    assert direct["draft"]["parameters"]["cfg"] == 0
    assert direct["draft"]["parameters"]["artist"] == "manual artist"


@pytest.mark.parametrize("key", ["metadata", "supplemental", "data"])
def test_malformed_studio_envelope_has_clear_error(key):
    envelope = {"format": "image_studio", "version": 1, "data": {}, key: [1]}
    with pytest.raises(ValueError):
        resolve_parameters(json.dumps(envelope), settings())


def settings(*, duplicate: bool = False, empty: bool = False) -> RuntimeSettings:
    nai_schema = {
        "style": {
            "type": "preset",
            "default": "custom",
            "choices": ["custom", "galgame"],
        },
        "artist": {"type": "text", "default": "default artist"},
        "steps": {"type": "integer", "min": 1, "max": 28, "default": 24},
        "scale": {"type": "number", "min": 0, "max": 20, "default": 6},
        "cfg": {"type": "number", "min": 0, "max": 1, "default": 0.3},
        "sm": {"type": "boolean", "default": True},
        "seed": {"type": "integer", "default": 0},
        "sampler": {
            "type": "select",
            "default": "k_euler_ancestral",
            "choices": ["k_euler", "k_euler_ancestral"],
        },
        "size": {
            "type": "select",
            "default": "方图",
            "choices": ["竖图", "横图", "方图"],
        },
        "count": {"type": "integer", "default": 1, "min": 1, "max": 4},
    }
    providers = [
        ImageProvider.from_mapping(
            {
                "id": "nai",
                "name": "NAI",
                "kind": "nai_direct",
                "base_url": "https://example.test",
                "models": [
                    {
                        "id": "nai-diffusion-4-5-full",
                        "name": "NAI V4.5",
                        "supports_text2img": True,
                        "supports_img2img": True,
                        "max_reference_images": 1,
                        "supports_negative_prompt": True,
                        "negative_prompt_default": "default negative",
                        "parameters": nai_schema,
                    }
                ],
            }
        ),
        ImageProvider.from_mapping(
            {
                "id": "other",
                "name": "Other",
                "kind": "custom_json",
                "base_url": "https://example.test",
                "models": [
                    {
                        "id": "plain-image",
                        "supports_text2img": True,
                        "supports_negative_prompt": False,
                        "parameters": {
                            "cfg_scale": {
                                "type": "number",
                                "min": 0,
                                "max": 20,
                                "default": 7,
                            },
                            "cfg_rescale": {
                                "type": "number",
                                "min": 0,
                                "max": 1,
                                "default": 0,
                            },
                            "seed": {"type": "integer", "default": 0},
                            "size": {"type": "text", "default": "1024x1024"},
                        },
                    }
                ],
            }
        ),
    ]
    if duplicate:
        value = providers[0].public_dict()
        value["id"] = "nai-copy"
        providers.append(ImageProvider.from_mapping(value))
    return RuntimeSettings(
        True,
        tuple([] if empty else providers),
        HistorySettings(True, 100, 256, False),
        1,
    )


def detail(*, source: str = "webui", metadata: dict | None = None) -> dict:
    return {
        "id": "generation-1",
        "source": source,
        "mode": "text2img",
        "provider_id": "nai",
        "provider_kind": "nai_direct",
        "model": "nai-diffusion-4-5-full",
        "original_prompt": "artist, mountain",
        "generation_engine": "nai",
        "parameters": {
            "negative_prompt": "",
            "size": "竖图",
            "count": 2,
            "parameters": {
                "artist": "",
                "style": "galgame",
                "cfg": 0,
                "scale": 6,
                "sm": False,
                "steps": 26,
            },
        },
        "images": [
            {
                "id": "image-1",
                "download_filename": "20260905120000_t2i_nai.png",
                "metadata": metadata or {},
            }
        ],
        "supplemental": {},
    }


def image_settings() -> RuntimeSettings:
    configuration = settings()
    provider = ImageProvider.from_mapping(
        {
            "id": "natural",
            "name": "Natural",
            "kind": "openai_images",
            "base_url": "https://example.test",
            "models": [
                {
                    "id": "natural-image",
                    "supports_text2img": True,
                    "supports_img2img": True,
                    "max_reference_images": 4,
                }
            ],
        }
    )
    return replace(configuration, providers=(*configuration.providers, provider))


def test_studio_export_roundtrip_preserves_explicit_request_values() -> None:
    original = detail()
    snapshot = copy.deepcopy(original)
    exported = export_parameters(original)
    result = resolve_parameters(exported["content"], settings())
    assert original == snapshot
    assert not result["requires_model_selection"]
    draft = result["draft"]
    assert draft["model_ref"] == "nai:nai-diffusion-4-5-full"
    assert draft["negative_prompt"] == ""
    assert draft["size"] == "竖图"
    assert draft["count"] == 2
    assert draft["parameters"]["artist"] == ""
    assert draft["parameters"]["style"] == "galgame"
    assert draft["parameters"]["cfg"] == 0
    assert draft["parameters"]["sm"] is False


def test_nai_request_export_preserves_style_empty_artist_and_zero() -> None:
    exported = export_parameters(detail(), format_name="nai")
    payload = json.loads(exported["content"])
    assert payload["style"] == "galgame"
    assert payload["artist"] == ""
    assert payload["negative"] == ""
    assert payload["cfg"] == 0
    assert payload["sm"] is False
    resolved = resolve_parameters(exported["content"], settings())
    assert resolved["draft"]["parameters"]["cfg"] == 0
    assert resolved["draft"]["negative_prompt"] == ""


def test_novelai_guidance_and_rescale_have_distinct_nai_targets() -> None:
    content = json.dumps(
        {
            "prompt": "mountain",
            "uc": "blurry",
            "model": "nai-diffusion-4-5-full",
            "scale": 8,
            "cfg_rescale": 0.1,
        }
    )
    result = resolve_parameters(content, settings())
    values = result["draft"]["parameters"]
    assert values["scale"] == 8
    assert values["cfg"] == 0.1
    assert values["artist"] == ""


def test_nai_to_other_provider_does_not_confuse_cfg_semantics() -> None:
    content = '{"tag":"mountain","scale":6,"cfg":0.3}'
    result = resolve_parameters(content, settings(), "other:plain-image")
    values = result["draft"]["parameters"]
    assert values["cfg_scale"] == 6
    assert values["cfg_rescale"] == 0.3


@pytest.mark.parametrize(
    "configuration,content",
    [
        (settings(empty=True), '{"tag":"mountain","model":"nai-diffusion-4-5-full"}'),
        (
            settings(duplicate=True),
            '{"tag":"mountain","model":"nai-diffusion-4-5-full"}',
        ),
        (settings(duplicate=True), '{"tag":"mountain"}'),
        (settings(), '{"tag":"mountain","model":"unavailable-model"}'),
    ],
)
def test_missing_unknown_or_ambiguous_model_requires_explicit_selection(
    configuration: RuntimeSettings, content: str
) -> None:
    result = resolve_parameters(content, configuration)
    assert result["requires_model_selection"]
    assert result["draft"]["model_ref"] == ""
    assert result["draft"]["prompt"] == "mountain"


def test_missing_model_does_not_discard_unmapped_parameters() -> None:
    result = resolve_parameters(
        '{"tag":"mountain","steps":99,"custom_option":{"enabled":false}}',
        settings(empty=True),
    )
    assert result["requires_model_selection"]
    assert result["unmapped"]["steps"] == 99
    assert result["unmapped"]["custom_option"] == {"enabled": False}


def test_missing_model_selects_only_unique_compatible_source() -> None:
    result = resolve_parameters('{"tag":"mountain","cfg":0.3}', settings())
    assert result["requires_model_selection"] is False
    assert result["draft"]["model_ref"] == "nai:nai-diffusion-4-5-full"
    assert result["selection_reason"] == "source_unique"
    assert any("唯一" in warning for warning in result["warnings"])
    assert resolve_parameters(
        '{"Steps":30,"CFG scale":7,"prompt":"scene"}', settings()
    )["requires_model_selection"]


@pytest.mark.parametrize("mode", ["img2img", "i2i", "image2image"])
def test_paste_explicit_image_mode_selects_supported_model(mode: str) -> None:
    content = json.dumps(
        {
            "format": "image_studio",
            "version": 1,
            "generation_engine": "openai_images",
            "data": {"prompt": "mountain", "mode": mode},
        }
    )
    result = resolve_parameters(content, image_settings())
    assert result["draft"]["mode"] == "img2img"
    assert result["draft"]["model_ref"] == "natural:natural-image"
    assert result["selection_reason"] == "source_unique"
    assert any("参考图" in warning for warning in result["warnings"])
    assert not any("未确定生成模式" in warning for warning in result["warnings"])


def test_paste_model_ref_and_explicit_mode_override_default_mode() -> None:
    content = json.dumps(
        {
            "format": "image_studio",
            "version": 1,
            "data": {
                "model_ref": "natural:natural-image",
                "mode": "img2img",
                "prompt": "mountain",
            },
        }
    )
    result = resolve_parameters(content, image_settings())
    assert result["draft"]["mode"] == "img2img"
    assert result["selection_reason"] == "model_ref"
    invalid = json.dumps(
        {
            "format": "image_studio",
            "version": 1,
            "data": {
                "model_ref": "other:plain-image",
                "mode": "img2img",
                "prompt": "mountain",
            },
        }
    )
    unresolved = resolve_parameters(invalid, settings())
    assert unresolved["requires_model_selection"]
    assert unresolved["draft"]["mode"] == "img2img"


def test_original_novelai_action_determines_mode_without_interface_hint() -> None:
    content = json.dumps(
        {
            "prompt": "scene",
            "uc": "bad",
            "model": "nai-diffusion-4-5-full",
            "action": "img2img",
        }
    )
    result = resolve_parameters(content, image_settings())
    assert result["draft"]["mode"] == "img2img"
    assert result["requires_model_selection"]
    assert result["selection_reason"] == ""


def test_unavailable_explicit_model_reference_never_falls_back_to_source() -> None:
    content = json.dumps(
        {
            "format": "image_studio",
            "version": 1,
            "generation_engine": "nai",
            "data": {"model_ref": "deleted:gone-model", "prompt": "scene"},
        }
    )
    result = resolve_parameters(content, settings())
    assert result["requires_model_selection"]
    assert result["draft"]["model_ref"] == ""


def test_out_of_range_and_unknown_values_are_retained_without_clamping() -> None:
    content = json.dumps(
        {
            "tag": "mountain",
            "model": "nai-diffusion-4-5-full",
            "steps": 99,
            "cfg": -1,
            "sampler": "future_sampler",
            "custom_option": {"enabled": False},
        }
    )
    result = resolve_parameters(content, settings())
    assert result["draft"]["parameters"]["steps"] == 24
    assert result["draft"]["parameters"]["cfg"] == 0.3
    assert result["unmapped"] == {
        "steps": 99,
        "cfg": -1,
        "sampler": "future_sampler",
        "custom_option": {"enabled": False},
    }
    assert result["warnings"]


def test_comfyui_workflow_export_is_byte_exact_text_for_large_seeds() -> None:
    workflow = '{ "nodes": [], "links": [], "extra": {"seed":18446744073709551615} }\n'
    api = '{"1":{"class_type":"SaveImage","inputs":{}}}'
    metadata = {
        "format": "comfyui",
        "raw": {"workflow": workflow, "prompt": api},
        "normalized": {},
    }
    original = detail(source="import", metadata=metadata)
    assert export_parameters(original, format_name="workflow")["content"] == workflow
    assert export_parameters(original, format_name="comfy_api")["content"] == api


def test_a1111_full_copy_can_be_pasted_back() -> None:
    text = "mountain\nNegative prompt: blurry\nSteps: 30, Sampler: DPM++ 2M, CFG scale: 8, Seed: 18446744073709551615, Size: 1440x1440, Model: plain-image"
    metadata = parse_metadata_fields({"parameters": text})
    exported = export_parameters(
        detail(source="import", metadata=metadata), format_name="a1111"
    )
    parsed = parse_parameter_text(exported["content"])
    assert parsed["normalized"] == metadata["normalized"]
    result = resolve_parameters(exported["content"], settings())
    assert not result["requires_model_selection"]
    assert result["draft"]["parameters"]["cfg_scale"] == 8
    assert result["unmapped"]["seed"] == "18446744073709551615"
    assert result["unmapped"]["negative_prompt"] == "blurry"


def test_import_studio_copy_applies_manual_supplemental_values() -> None:
    metadata = parse_metadata_fields(
        {
            "Software": "NovelAI",
            "Comment": json.dumps(
                {
                    "prompt": "embedded",
                    "uc": "original negative",
                    "model": "nai-diffusion-4-5-full",
                    "steps": 26,
                    "seed": 8,
                    "width": 832,
                    "height": 1216,
                    "scale": 6,
                }
            ),
        }
    )
    original = detail(source="import", metadata=metadata)
    original.update(
        provider_id="",
        provider_kind="",
        original_prompt="manual prompt",
        parameters={},
        generation_engine="novelai",
    )
    original["supplemental"] = {
        "overrides": {
            "prompt": "manual prompt",
            "negative_prompt": "manual negative",
            "parameters": {"steps": 20, "seed": 9},
        },
        "display_parameters": {
            **metadata["normalized"],
            "negative_prompt": "manual negative",
            "steps": 20,
            "seed": 9,
        },
    }
    result = resolve_parameters(export_parameters(original)["content"], settings())
    assert result["draft"]["prompt"] == "manual prompt"
    assert result["draft"]["negative_prompt"] == "manual negative"
    assert result["draft"]["parameters"]["steps"] == 20
    assert result["draft"]["parameters"]["seed"] == 9
    assert result["draft"]["size"] == "竖图"


def test_import_studio_copy_retains_embedded_negative_when_not_manually_overridden() -> (
    None
):
    metadata = parse_metadata_fields(
        {
            "Software": "NovelAI",
            "Comment": '{"prompt":"embedded","uc":"embedded negative","model":"nai-diffusion-4-5-full","steps":26}',
        }
    )
    original = detail(source="import", metadata=metadata)
    original.update(
        parameters={},
        provider_id="",
        provider_kind="",
        original_prompt="embedded",
        generation_engine="novelai",
    )
    result = resolve_parameters(export_parameters(original)["content"], settings())
    assert result["draft"]["negative_prompt"] == "embedded negative"


def test_partial_novelai_metadata_model_name_and_characters_are_retained() -> None:
    characters = [{"char_caption": "one character", "centers": [{"x": 0.2, "y": 0.5}]}]
    parameters = {
        "model_name": "nai-diffusion-4-5-full",
        "uc": "bad",
        "v4_prompt": {
            "caption": {"base_caption": "scene", "char_captions": characters}
        },
    }
    result = resolve_parameters(json.dumps(parameters), settings())
    assert not result["requires_model_selection"]
    assert result["draft"]["prompt"] == "scene"
    assert result["unmapped"]["characters"]["prompt"] == characters


def test_image_choice_export_cannot_access_other_record_image() -> None:
    with pytest.raises(ValueError, match="不属于"):
        export_parameters(detail(), image_id="not-in-this-record")
