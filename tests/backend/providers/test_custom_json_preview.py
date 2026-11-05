"""Request previews match execution without contacting a provider or saving drafts."""

from __future__ import annotations

import asyncio
import copy
import json

import httpx
import pytest

from astrbot_plugin_image_studio.backend.gallery.store import GenerationStore
from astrbot_plugin_image_studio.backend.generation.preview import (
    EXAMPLE_PROMPT,
    _EXAMPLE_PNG,
    preview_custom_request,
    sanitize_preview,
)
from astrbot_plugin_image_studio.backend.generation.service import (
    ImageGenerationService,
)
from astrbot_plugin_image_studio.backend.models import ImageProvider, ReferenceImage
from astrbot_plugin_image_studio.backend.providers.executor import ProviderExecutor
from astrbot_plugin_image_studio.tests.backend.config.test_parameter_policy import (
    policy_settings,
)
from astrbot_plugin_image_studio.tests.backend.providers.test_agnes_provider import (
    Session,
)
from astrbot_plugin_image_studio.tests.support.webui_harness import create_app


def draft():
    return {
        "id": "policy",
        "kind": "custom_json",
        "base_url": "https://preview.invalid/v2",
        "generate_path": "/create",
        "edit_path": "/edit",
        "proxy": "http://proxy.invalid:7890",
        "api_key": "private-api-token",
        "custom_headers": '{"X-Route":"private-header-token", "Accept":"application/json"}',
        "request_template": json.dumps({"fallback": "{{prompt}}", "n": "{{count}}"}),
        "request_templates": {
            "text2img": json.dumps(
                {
                    "model": "{{model}}",
                    "prompt": "{{prompt}}",
                    "n": "{{n}}",
                    "size": "{{size}}",
                    "quality": "{{quality}}",
                    "negative": "{{negative_prompt}}",
                }
            ),
            "img2img": json.dumps(
                {
                    "model": "{{model}}",
                    "prompt": "{{prompt}}",
                    "n": "{{count}}",
                    "size": "{{size}}",
                    "quality": "{{quality}}",
                    "image": "{{references[0]}}",
                    "negative": "{{negative_prompt}}",
                }
            ),
        },
        "models": [
            {
                "id": "paint",
                "name": "Preview model",
                "supports_text2img": True,
                "supports_img2img": True,
                "max_reference_images": 2,
                "supports_negative_prompt": True,
                "negative_prompt_default": "blurred image",
                "native_batch_size": 2,
                "max_concurrent_requests": 1,
                "parameters": {
                    "copies": {
                        "type": "integer",
                        "request_key": "count",
                        "default": 5,
                        "min": 1,
                        "max": 8,
                        "mode_overrides": {"img2img": {"default": 3}},
                    },
                    "canvas": {
                        "type": "text",
                        "request_key": "size",
                        "default": "1024x1024",
                        "mode_overrides": {"img2img": {"default": "768x1024"}},
                    },
                    "detail": {
                        "type": "select",
                        "request_key": "quality",
                        "default": "standard",
                        "choices": ["standard", "high"],
                        "mode_overrides": {"img2img": {"default": "high"}},
                    },
                },
            }
        ],
    }


@pytest.mark.parametrize(
    "mode,count,repeats", [("text2img", 5, [2, 1]), ("img2img", 3, [1, 1])]
)
def test_preview_matches_real_http_arguments_defaults_mapping_and_batches(
    tmp_path, mode, count, repeats
):
    configured = draft()
    original = copy.deepcopy(configured)
    preview = preview_custom_request(configured, 0, mode)
    assert configured == original
    assert preview["requested_count"] == count
    assert [batch["repeat"] for batch in preview["batches"]] == repeats
    assert [batch["count"] for batch in preview["batches"]] == [2, 1]
    assert preview["request_count"] == sum(repeats)
    assert preview["batches"][0]["body"]["quality"] == (
        "standard" if mode == "text2img" else "high"
    )
    assert preview["batches"][0]["body"]["size"] == (
        "1024x1024" if mode == "text2img" else "768x1024"
    )
    assert preview["batches"][0]["body"]["negative"] == "blurred image"
    assert preview["batches"][0]["headers"]["Authorization"] == "[redacted]"
    assert preview["batches"][0]["headers"]["X-Route"] == "[redacted]"
    assert preview["batches"][0]["headers"]["Accept"] == "application/json"
    if mode == "img2img":
        assert "示例参考图" in preview["batches"][0]["body"]["image"]
        assert "base64," not in json.dumps(preview)

    async def run():
        provider = ImageProvider.from_mapping(configured)
        session = Session()
        store = GenerationStore(tmp_path)
        await store.initialize()
        service = ImageGenerationService(
            settings=policy_settings(provider),
            executor=ProviderExecutor(session),
            store=store,
        )
        try:
            await service.generate(
                mode=mode,
                provider_id=provider.id,
                model_ref="policy:paint",
                prompt=EXAMPLE_PROMPT,
                negative_prompt=provider.models[0].negative_prompt_default,
                references=(
                    ReferenceImage("preview", "example.png", _EXAMPLE_PNG, "image/png"),
                )
                * 2
                if mode == "img2img"
                else (),
            )
            expanded = [
                batch for batch in preview["batches"] for _ in range(batch["repeat"])
            ]
            assert len(session.calls) == len(expanded)
            for batch, (method, url, options) in zip(expanded, session.calls):
                assert method.upper() == batch["method"]
                assert sanitize_preview(
                    {
                        "url": url,
                        "headers": options["headers"],
                        "proxy": options["proxy"],
                        "body": options["json"],
                    },
                    configured,
                ) == {key: batch[key] for key in ("url", "headers", "proxy", "body")}
        finally:
            await store.close()

    asyncio.run(run())


def test_duplicate_and_incomplete_draft_ids_are_selected_by_index():
    configured = draft()
    second = copy.deepcopy(configured["models"][0])
    second["parameters"]["detail"]["default"] = "high"
    configured["models"] = [{"id": ""}, configured["models"][0], second]
    preview = preview_custom_request(configured, 2, "text2img")
    assert preview["batches"][0]["body"]["quality"] == "high"
    assert (
        preview_custom_request(configured, 1, "text2img")["batches"][0]["body"][
            "quality"
        ]
        == "standard"
    )


def test_preview_populates_configured_reference_slots_for_multi_image_templates():
    configured = draft()
    configured["request_templates"]["img2img"] = json.dumps(
        {
            "first": "{{references[0]}}",
            "second": "{{references[1]}}",
            "all": "{{references}}",
        }
    )
    preview = preview_custom_request(configured, 0, "img2img")
    body = preview["batches"][0]["body"]
    assert "示例参考图" in body["first"] and "示例参考图" in body["second"]
    assert len(body["all"]) == 2
    assert any("2 张示例图" in note for note in preview["notes"])


def test_empty_mode_template_falls_back_and_empty_all_uses_real_default_body():
    configured = draft()
    configured["request_templates"] = {"text2img": ""}
    assert set(
        preview_custom_request(configured, 0, "text2img")["batches"][0]["body"]
    ) == {"fallback", "n"}
    configured["request_template"] = ""
    body = preview_custom_request(configured, 0, "text2img")["batches"][0]["body"]
    assert body["n"] == 2
    assert body["quality"] == "standard"
    assert body["references"] == []
    assert "copies" not in body and "canvas" not in body and "detail" not in body


def test_preview_hides_credentials_even_copied_into_arbitrary_fields_and_urls():
    configured = draft()
    configured["base_url"] = "https://username:private-url-password@preview.invalid"
    configured["generate_path"] = "/create?api_key=private-query-token&format=json"
    configured["proxy"] = "http://proxy-user:private-proxy-password@proxy.invalid:7890"
    configured["request_template"] = ""
    configured["request_templates"] = {
        "text2img": json.dumps(
            {
                "api_key": "hardcoded-template-key",
                "nested": {"access_token": "nested-secret"},
                "plain": "private-api-token",
                "header_copy": "private-header-token",
                "url_password_copy": "private-url-password",
                "url": "https://other.invalid/result?token=private-other-token",
            }
        )
    }
    preview = preview_custom_request(configured, 0, "text2img")
    output = json.dumps(preview)
    for secret in (
        "private-api-token",
        "private-header-token",
        "private-url-password",
        "private-proxy-password",
        "private-query-token",
        "private-other-token",
        "hardcoded-template-key",
        "nested-secret",
    ):
        assert secret not in output
    assert "format=json" in preview["batches"][0]["url"]
    assert "[redacted]" in output


@pytest.mark.parametrize(
    "change,index,mode,message",
    [
        ({"models": []}, 0, "text2img", "添加模型"),
        ({}, True, "text2img", "有效的预览模型"),
        ({}, 2, "text2img", "有效的预览模型"),
        ({}, 0, "other", "预览模式"),
        ({"kind": "openai_images"}, 0, "text2img", "自定义 JSON"),
        ({"models": [{"id": ""}]}, 0, "text2img", "模型的 ID"),
        (
            {"models": [{"id": "paint", "supports_img2img": False}]},
            0,
            "img2img",
            "不支持",
        ),
    ],
)
def test_preview_rejects_invalid_selections(change, index, mode, message):
    with pytest.raises(ValueError, match=message):
        preview_custom_request({**draft(), **change}, index, mode)


def test_preview_api_has_no_generation_or_persistence_side_effects(
    tmp_path, monkeypatch
):
    async def run():
        app = await create_app(tmp_path, seed=False)
        plugin = app.state.plugin
        plugin._logs.enabled = False
        settings_before = copy.deepcopy(plugin._studio_settings)
        files_before = {
            path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()
        }

        def forbidden(*_args, **_kwargs):
            raise AssertionError(
                "preview must not initialize or use a generation service"
            )

        monkeypatch.setattr(plugin, "_service_or_raise", forbidden)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            endpoint = "/astrbot_plugin_image_studio/provider/preview"
            response = await client.post(
                endpoint,
                json={"provider": draft(), "model_index": 0, "mode": "img2img"},
            )
            assert response.status_code == 200
            assert response.headers["Cache-Control"] == "no-store"
            assert response.json()["mode"] == "img2img"
            invalid = draft()
            invalid["request_templates"]["text2img"] = '{"bad":"{{private-api-token}}"}'
            failed = await client.post(
                endpoint,
                json={"provider": invalid, "model_index": 0, "mode": "text2img"},
            )
            assert failed.status_code == 400
            assert "private-api-token" not in failed.text
            assert (await client.post(endpoint, json=[])).status_code == 400
        assert plugin._studio_settings == settings_before
        assert plugin._service.executor.calls == []
        assert {
            path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()
        } == files_before
        await plugin.store.close()

    asyncio.run(run())
