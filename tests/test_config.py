from __future__ import annotations

from astrbot_plugin_image_gen.config import normalize_webui_settings, runtime_settings


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
