from __future__ import annotations

from astrbot_plugin_image_gen.config import normalize_webui_settings, runtime_settings
from astrbot_plugin_image_gen.models import GenerationRequest, ImageProvider
from astrbot_plugin_image_gen.providers import _openai_payload


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


def test_runtime_settings_filters_disabled_provider() -> None:
    settings, errors = runtime_settings(
        {
            "enabled": True,
            "enable_llm_tool": True,
            "max_concurrent_generations": 2,
            "webui_managed": {
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
        }
    )

    assert errors == []
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
