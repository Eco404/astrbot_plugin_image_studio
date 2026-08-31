from __future__ import annotations

import asyncio
import json

from astrbot_plugin_image_studio.config import (
    load_studio_settings,
    normalize_webui_settings,
    runtime_settings,
    save_studio_settings,
)
from astrbot_plugin_image_studio.models import GenerationRequest, ImageProvider
from astrbot_plugin_image_studio.providers import _nai_query, _openai_payload


def test_webui_settings_normalize_provider_and_history() -> None:
    normalized, errors = normalize_webui_settings(
        {
            "providers": [
                {
                    "id": "local-openai",
                    "name": "Local OpenAI",
                    "kind": "openai_images",
                    "base_url": "https://example.test/v1",
                    "model": "gpt-image-1",
                    "api_key": "visible-in-webui",
                    "supports_text2img": True,
                    "supports_img2img": True,
                    "max_reference_images": 2,
                }
            ],
            "history": {"max_records": "12", "max_megabytes": "256"},
        }
    )

    assert errors == []
    assert normalized["history"]["max_records"] == 12
    assert normalized["providers"][0]["api_key"] == "visible-in-webui"
    assert normalized["history"]["record_invocation_identity"] is False


def test_history_can_enable_invocation_identity_snapshots() -> None:
    normalized, errors = normalize_webui_settings(
        {"history": {"record_invocation_identity": True}}
    )

    assert errors == []
    assert normalized["history"]["record_invocation_identity"] is True


def test_provider_without_models_is_a_valid_saved_draft() -> None:
    normalized, errors = normalize_webui_settings(
        {
            "providers": [
                {
                    "id": "draft",
                    "name": "Draft",
                    "kind": "openai_images",
                    "base_url": "https://example.test/v1",
                    "models": [],
                }
            ]
        }
    )

    assert errors == []
    assert normalized["providers"][0]["models"] == []


def test_empty_tool_choice_descriptions_are_removed() -> None:
    normalized, errors = normalize_webui_settings(
        {
            "providers": [
                {
                    "id": "custom",
                    "name": "Custom",
                    "kind": "custom_json",
                    "base_url": "https://example.test",
                    "models": [
                        {
                            "id": "image-model",
                            "tool": {
                                "parameters": {
                                    "quality": {
                                        "exposed": True,
                                        "choice_descriptions": {},
                                    },
                                    "format": {
                                        "exposed": True,
                                        "choice_descriptions": {
                                            "png": "无损格式",
                                            "empty": "",
                                        },
                                    },
                                }
                            },
                        }
                    ],
                }
            ]
        }
    )

    assert errors == []
    parameters = normalized["providers"][0]["models"][0]["tool"]["parameters"]
    assert "choice_descriptions" not in parameters["quality"]
    assert parameters["format"]["choice_descriptions"] == {"png": "无损格式"}


def test_generation_default_models_are_scoped_without_global_parameters() -> None:
    normalized, errors = normalize_webui_settings(
        {
            "generation_defaults": {
                "page": {
                    "text2img_model_ref": "page:text",
                    "img2img_model_ref": "page:image",
                },
                "tool": {
                    "text2img_model_ref": "tool:text",
                    "img2img_model_ref": "tool:image",
                },
                "size": "2048x2048",
                "count": 4,
            }
        }
    )

    assert errors == []
    assert normalized["generation_defaults"] == {
        "page": {
            "text2img_model_ref": "page:text",
            "img2img_model_ref": "page:image",
        },
        "tool": {
            "text2img_model_ref": "tool:text",
            "img2img_model_ref": "tool:image",
        },
    }


def test_plugin_owned_settings_round_trip_atomically(tmp_path) -> None:
    settings = {
        "providers": [],
        "revision": 4,
        "history": {"max_records": 12},
    }

    asyncio.run(save_studio_settings(tmp_path, settings))
    loaded, errors = load_studio_settings(tmp_path)

    assert errors == []
    assert loaded["revision"] == 4
    assert loaded["history"]["max_records"] == 12
    assert json.loads((tmp_path / "studio_config.json").read_text())["revision"] == 4
    assert (tmp_path / "studio_config.json").stat().st_mode & 0o777 == 0o600


def test_runtime_settings_filters_disabled_provider() -> None:
    settings, errors = runtime_settings(
        {
            "enabled": False,
            "enable_llm_tool": True,
        },
        {
            "providers": [
                {
                    "id": "disabled",
                    "name": "Disabled",
                    "enabled": False,
                    "kind": "openai_images",
                    "base_url": "https://example.test",
                    "model": "gpt-image-1",
                }
            ]
        },
    )

    assert errors == []
    assert not hasattr(settings, "enabled")
    assert settings.provider("disabled") is None
    assert settings.providers_for_mode("text2img") == []


def test_negative_prompt_capability_defaults_to_nai_only() -> None:
    openai, openai_errors = normalize_webui_settings(
        {
            "providers": [
                {
                    "id": "openai",
                    "name": "OpenAI",
                    "kind": "openai_images",
                    "base_url": "https://example.test",
                    "model": "gpt-image-1",
                }
            ]
        }
    )
    nai, nai_errors = normalize_webui_settings(
        {
            "providers": [
                {
                    "id": "nai",
                    "name": "NAI",
                    "kind": "nai_direct",
                    "base_url": "https://example.test",
                    "model": "nai-diffusion",
                }
            ]
        }
    )
    assert openai_errors == [] and nai_errors == []
    assert openai["providers"][0]["supports_negative_prompt"] is False
    assert nai["providers"][0]["supports_negative_prompt"] is True


def test_openai_payload_only_includes_negative_prompt_when_enabled() -> None:
    request = GenerationRequest(
        mode="text2img", provider_id="test", prompt="tree", negative_prompt="fog"
    )
    standard_provider = ImageProvider.from_mapping(
        {
            "id": "standard",
            "name": "Standard",
            "kind": "openai_images",
            "base_url": "https://example.test",
            "model": "gpt-image-1",
        }
    )
    custom_provider = ImageProvider.from_mapping(
        {
            "id": "custom",
            "name": "Custom",
            "kind": "custom_json",
            "base_url": "https://example.test",
            "model": "custom-image",
            "supports_negative_prompt": True,
        }
    )
    assert "negative_prompt" not in _openai_payload(standard_provider, request)
    assert _openai_payload(custom_provider, request)["negative_prompt"] == "fog"


def test_models_are_configured_independently_and_filtered_by_mode() -> None:
    settings, errors = runtime_settings(
        {},
        {
            "providers": [
                {
                    "id": "domestic",
                    "name": "国内服务商",
                    "kind": "custom_json",
                    "base_url": "https://example.test",
                    "models": [
                        {
                            "id": "draw-text",
                            "name": "文生图模型",
                            "supports_text2img": True,
                            "supports_img2img": False,
                        },
                        {
                            "id": "draw-edit",
                            "name": "图生图模型",
                            "supports_text2img": False,
                            "supports_img2img": True,
                            "max_reference_images": 3,
                        },
                    ],
                }
            ]
        },
    )

    assert errors == []
    assert [model.id for _, model in settings.models_for_mode("text2img")] == [
        "draw-text"
    ]
    assert [model.id for _, model in settings.models_for_mode("img2img")] == [
        "draw-edit"
    ]
    assert settings.providers[0].get_model("draw-edit").max_reference_images == 3


def test_img2img_requires_a_positive_reference_limit() -> None:
    settings, errors = runtime_settings(
        {},
        {
            "providers": [
                {
                    "id": "manual",
                    "name": "Manual",
                    "kind": "custom_json",
                    "base_url": "https://example.test",
                    "models": [
                        {
                            "id": "zero-limit",
                            "supports_text2img": True,
                            "supports_img2img": True,
                            "max_reference_images": 0,
                            "capability_source": "manual",
                            "tool": {"max_reference_images": 4},
                        },
                        {
                            "id": "manual-limit",
                            "supports_text2img": True,
                            "supports_img2img": True,
                            "max_reference_images": 2,
                            "capability_source": "manual",
                            "tool": {"max_reference_images": 4},
                        },
                    ],
                }
            ]
        },
    )

    assert errors == []
    assert [model.id for _, model in settings.models_for_mode("img2img")] == [
        "manual-limit"
    ]
    zero_limit = settings.providers[0].get_model("zero-limit")
    manual_limit = settings.providers[0].get_model("manual-limit")
    assert zero_limit.llm_max_reference_images == 0
    assert manual_limit.llm_max_reference_images == 2


def test_provider_transport_defaults_follow_provider_kind() -> None:
    provider = ImageProvider.from_mapping(
        {
            "id": "gemini",
            "name": "Gemini",
            "kind": "gemini",
            "base_url": "https://example.test",
            "model": "gemini-image",
        }
    )

    assert provider.generate_path == "/v1beta/models/{model}:generateContent"
    assert provider.edit_path == "/v1beta/models/{model}:generateContent"
    assert provider.edit_request_format == "json_data_url"


def test_nai_transport_matches_third_party_get_protocol() -> None:
    provider = ImageProvider.from_mapping(
        {
            "id": "nai",
            "name": "NAI third party",
            "kind": "nai_direct",
            "api_key": "to-user-id",
            "models": [
                {
                    "id": "nai-diffusion-4-5-full",
                    "supports_negative_prompt": True,
                }
            ],
        }
    )
    request = GenerationRequest(
        mode="text2img",
        provider_id="nai",
        prompt="1girl, outdoors",
        negative_prompt="bad anatomy",
        model="nai-diffusion-4-5-full",
        size="竖图",
        parameters={
            "artist": "artist:test",
            "steps": 24,
            "scale": 6,
            "cfg": 7,
            "sampler": "k_dpmpp_2m_sde",
            "noise_schedule": "karras",
            "nocache": 0,
        },
    )

    query = _nai_query(provider, request)

    assert provider.base_url == "https://nai.sta1n.cn"
    assert provider.generate_path == "/generate"
    assert query == {
        "artist": "artist:test",
        "steps": "24",
        "scale": "6",
        "cfg": "7",
        "sampler": "k_dpmpp_2m_sde",
        "noise_schedule": "karras",
        "nocache": "1",
        "tag": "1girl, outdoors",
        "token": "to-user-id",
        "model": "nai-diffusion-4-5-full",
        "size": "竖图",
        "negative": "bad anatomy",
    }
