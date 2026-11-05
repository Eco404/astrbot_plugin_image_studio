"""Custom JSON templates select a mode and fail before submitting bad bindings."""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

from astrbot_plugin_image_studio.backend.models import (
    GenerationRequest,
    ImageProvider,
    ReferenceImage,
)
from astrbot_plugin_image_studio.backend.providers.custom_json import (
    _substitute_template,
    build_payload,
)
from astrbot_plugin_image_studio.backend.providers.executor import (
    ProviderError,
    ProviderExecutor,
)
from astrbot_plugin_image_studio.tests.backend.providers.test_agnes_provider import (
    Session,
    png,
)


def provider(**changes):
    return ImageProvider.from_mapping(
        {
            "id": "custom",
            "kind": "custom_json",
            "base_url": "https://custom.invalid",
            "model": "custom-image",
            "supports_img2img": True,
            **changes,
        }
    )


def request(mode="text2img", **changes):
    values = {
        "mode": mode,
        "provider_id": "custom",
        "prompt": "literal {{unexpanded_user_text}}",
        "model": "custom-image",
        "size": "1024x1024",
        "count": 2,
    }
    values.update(changes)
    return GenerationRequest(**values)


@pytest.mark.parametrize("mode", ["text2img", "img2img"])
def test_mode_template_controls_reference_placement_and_real_sent_body(mode):
    configured = provider(
        request_template=json.dumps({"unused": "legacy"}),
        request_templates={
            "text2img": json.dumps({"model": "{{model}}", "prompt": "{{prompt}}"}),
            "img2img": json.dumps(
                {
                    "model": "{{model}}",
                    "prompt": "{{prompt}}",
                    "extra_body": {"image": "{{references}}"},
                }
            ),
        },
    )
    image = png()
    submitted = request(
        mode, references=(ReferenceImage("ref", "ref.png", image, "image/png"),)
    )
    session = Session()
    asyncio.run(ProviderExecutor(session).generate(configured, submitted))
    method, endpoint, options = session.calls[0]
    assert method == "post"
    assert endpoint == "https://custom.invalid/v1/images/" + (
        "generations" if mode == "text2img" else "edits"
    )
    assert "data" not in options
    assert options["json"] == build_payload(configured, submitted)
    assert options["json"]["prompt"] == submitted.prompt
    assert "unused" not in options["json"]
    if mode == "img2img":
        assert options["json"]["extra_body"]["image"] == [
            "data:image/png;base64," + base64.b64encode(image).decode()
        ]
    else:
        assert "extra_body" not in options["json"]


def test_mode_template_falls_back_to_legacy_template():
    configured = provider(
        request_template='{"amount": "{{count}}", "samples": "{{n}}"}',
        request_templates={"img2img": '{"edited": true}'},
    )
    assert build_payload(configured, request()) == {"amount": 2, "samples": 2}
    assert build_payload(configured, request("img2img")) == {"edited": True}


def test_empty_templates_keep_legacy_json_default_body():
    configured = provider(request_template="", request_templates={"text2img": ""})
    assert build_payload(configured, request()) == {
        "model": "custom-image",
        "prompt": "literal {{unexpanded_user_text}}",
        "size": "1024x1024",
        "n": 2,
        "references": [],
    }


def test_structured_placeholders_preserve_types_without_recursive_expansion():
    defaults = {
        "objects": [{"value": 12, "nullable": None, "enabled": False}],
        "text": "{{references[0]}}",
    }
    configured = provider(
        request_template=json.dumps(
            {
                "first_image": "{{references[0]}}",
                "parameters": "{{options}}",
                "integer": "{{options.objects[0].value}}",
                "null": "{{options.objects[0].nullable}}",
                "boolean": "{{options.objects[0].enabled}}",
                "embedded": "size={{size}}; enabled={{options.objects[0].enabled}}",
                "schema_default": "{{options.text}}",
            }
        )
    )
    body = build_payload(
        configured,
        request(
            "img2img",
            parameters={"options": defaults},
            references=(ReferenceImage("ref", "ref.png", png(), "image/png"),),
        ),
    )
    assert body["first_image"].startswith("data:image/png;base64,")
    assert body["parameters"] == defaults
    assert body["integer"] == 12 and not isinstance(body["integer"], str)
    assert body["null"] is None
    assert body["boolean"] is False
    assert body["embedded"] == "size=1024x1024; enabled=false"
    assert body["schema_default"] == "{{references[0]}}"


@pytest.mark.parametrize(
    "template,message",
    [
        ("not json", "不是合法 JSON"),
        ("[]", "必须为 JSON 对象"),
        ('"{{prompt}}"', "必须为 JSON 对象"),
        ("null", "必须为 JSON 对象"),
        ('{"value":"{{missing}}"}', "不存在"),
        ('{"value":"prefix {{missing}} suffix"}', "不存在"),
        ('{"value":"{{references[0]}}"}', "不存在"),
        ('{"value":"{{references[-1]}}"}', "格式无效"),
        ('{"value":"{{references[9999999999999999999999999]}}"}', "不存在"),
        ('{"value":"{{prompt.upper}}"}', "不存在"),
        ('{"value":"{{prompt.upper()}}"}', "格式无效"),
        ('{"value":"{{prompt}} {{missing}"}', "未正确闭合"),
        ('{"value":"{{prompt"}', "未正确闭合"),
        ('{"value":"prompt}}"}', "未正确闭合"),
        ('{"value":"{{{prompt}}}"}', "未正确闭合"),
        ('{"value":"{{}}"}', "格式无效"),
        ('{"value":"{{prompt|default}}"}', "格式无效"),
        ('{"{{prompt}}":"dynamic key"}', "不能用于字段名"),
    ],
)
def test_invalid_binding_fails_before_http_request(template, message):
    session = Session()
    configured = provider(request_templates={"text2img": template})
    with pytest.raises(ProviderError, match=message) as caught:
        asyncio.run(ProviderExecutor(session).generate(configured, request()))
    assert caught.value.request_status == "not_submitted"
    assert session.calls == []


def test_list_index_is_bounded_even_with_large_input_array():
    with pytest.raises(ProviderError, match="不存在"):
        _substitute_template("{{values[10000]}}", {"values": list(range(10001))})


def test_schema_value_json_is_interpolated_without_python_repr():
    assert _substitute_template("prefix: {{options}}", {"options": [True, None]}) == (
        "prefix: [true, null]"
    )
