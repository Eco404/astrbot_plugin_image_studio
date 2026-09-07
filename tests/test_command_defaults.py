from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from astrbot_plugin_image_studio.config import HistorySettings, RuntimeSettings
from astrbot_plugin_image_studio.models import (
    GeneratedImage,
    ImageProvider,
    ReferenceImage,
)
from astrbot_plugin_image_studio.service import ImageGenerationService


class Recorder:
    def __init__(self):
        self.requests = []
        self.history = []
        self.discarded = []

    async def generate(self, provider, request):
        self.requests.append((provider, request))
        return (GeneratedImage(b"test-image", "image/png"),)

    async def record_success(self, **values):
        self.history.append(values)
        return "record"

    async def discard_staged_references(self, references):
        self.discarded.append(references)


def configured_provider(provider_id="alpha", model_id="paint", **model_changes):
    return ImageProvider.from_mapping(
        {
            "id": provider_id,
            "kind": "custom_json",
            "models": [
                {
                    "id": model_id,
                    "supports_text2img": True,
                    "supports_img2img": True,
                    "max_reference_images": 2,
                    "supports_negative_prompt": True,
                    "negative_prompt_default": "model negative default",
                    "parameters": {
                        "count": {"type": "integer", "default": 6, "min": 1, "max": 8},
                        "steps": {"type": "integer", "default": 0},
                    },
                    "tool": {
                        "enabled": True,
                        "parameters": {
                            "count": {"default_override": 3, "exposed": True}
                        },
                    },
                    **model_changes,
                }
            ],
        }
    )


def service(providers=None, default="alpha:paint", image_default="alpha:paint"):
    recorder = Recorder()
    settings = RuntimeSettings(
        enable_llm_tool=True,
        providers=tuple(providers or [configured_provider()]),
        history=HistorySettings(True, 100, 0, True),
        revision=0,
        default_page_text2img_model_ref=default,
        default_page_img2img_model_ref=image_default,
        default_tool_text2img_model_ref="alpha:paint",
    )
    return ImageGenerationService(
        settings=settings, executor=recorder, store=recorder
    ), recorder


async def generate(subject, **options):
    return await subject.generate(
        **{
            "mode": "text2img",
            "provider_id": "",
            "prompt": "test",
            "source": "command",
            **options,
        }
    )


def test_command_defaults_come_from_model_and_explicit_empty_negative_stays_empty():
    async def run():
        subject, recorder = service()
        default = await generate(subject)
        assert default.request.count == 6
        assert default.request.negative_prompt == "model negative default"
        assert default.request.parameters == {"steps": 0}
        assert default.request.selection_source == "mode_default"
        cleared = await generate(subject, negative_prompt="", count=1)
        assert cleared.request.negative_prompt == "" and cleared.request.count == 1
        override = await generate(subject, negative_prompt=" explicit ")
        assert override.request.negative_prompt == "explicit"
        assert len(recorder.history) == 3
        assert len(recorder.requests) == 13
        assert all(request.count == 1 for _, request in recorder.requests)

    asyncio.run(run())


@pytest.mark.parametrize(
    "descriptor,count,expected",
    [
        ({"default": 6, "max": 8}, None, 6),
        ({"default": 6, "max": 8}, 20, 8),
        ({"default": 20, "max": 8}, None, 8),
        ({"default": 0, "max": 8}, None, 1),
        ({"default": 0, "min": 2, "max": 8}, None, 2),
        ({"default": 2, "min": 3, "max": 8}, 1, 3),
        ({"default": 6}, None, 6),
        ({}, None, 1),
        ({}, 20, 16),
    ],
)
def test_command_count_uses_model_limits_without_global_four_cap(
    descriptor, count, expected
):
    async def run():
        subject, _ = service(
            [
                configured_provider(
                    parameters={"count": {"type": "integer", **descriptor}}
                )
            ]
        )
        result = await generate(subject, count=count)
        assert result.request.count == expected

    asyncio.run(run())


def test_command_count_without_schema_uses_default_total_limit():
    async def run():
        subject, _ = service([configured_provider(parameters={})])
        assert (await generate(subject)).request.count == 1
        assert (await generate(subject, count=9)).request.count == 9

    asyncio.run(run())


@pytest.mark.parametrize(
    "name,request_key",
    [("count", "n"), ("n", "n"), ("quantity", "n"), ("quantity", "count")],
)
def test_command_count_alias_defaults_and_parameters_do_not_leak(name, request_key):
    async def run():
        subject, _ = service(
            [
                configured_provider(
                    parameters={
                        name: {
                            "type": "integer",
                            "request_key": request_key,
                            "default": 7,
                            "max": 9,
                        }
                    }
                )
            ]
        )
        default = await generate(subject)
        assert default.request.count == 7 and default.request.parameters == {}
        clamped = await generate(subject, parameters={name: 22})
        assert clamped.request.count == 9 and clamped.request.parameters == {}
        explicit = await generate(subject, count=2, parameters={name: 22})
        assert explicit.request.count == 2 and explicit.request.parameters == {}

    asyncio.run(run())


@pytest.mark.parametrize("count", [0, -1, True, "abc", 1.5, float("inf")])
def test_command_invalid_explicit_count_fails_without_generation_or_history(count):
    async def run():
        subject, recorder = service()
        with pytest.raises(ValueError, match="数量"):
            await generate(subject, count=count)
        assert recorder.requests == recorder.history == recorder.discarded == []

    asyncio.run(run())


def test_command_model_without_negative_support_discards_default_and_override():
    async def run():
        subject, _ = service([configured_provider(supports_negative_prompt=False)])
        assert (await generate(subject)).request.negative_prompt == ""
        assert (
            await generate(subject, negative_prompt="discarded")
        ).request.negative_prompt == ""

    asyncio.run(run())


@pytest.mark.parametrize(
    "options,expected",
    [
        ({"provider_id": "missing"}, "指定服务商"),
        ({"provider_id": "disabled"}, "指定服务商"),
        ({"model": "missing"}, "指定模型"),
        ({"model_ref": "alpha:missing"}, "指定模型"),
        ({"model": "disabled:paint"}, "指定模型"),
        ({"provider_id": "alpha", "model": "beta:paint"}, "不属于"),
        ({"provider_id": "beta"}, "默认模型不属于"),
        ({"model": "paint"}, "多个服务商"),
        ({"model": "alpha:editing"}, "不支持"),
    ],
)
def test_command_selection_rejects_invalid_mismatched_and_ambiguous_models(
    options, expected
):
    async def run():
        alpha = configured_provider()
        editing = configured_provider(
            model_id="editing", supports_text2img=False
        ).models[0]
        providers = [
            replace(alpha, models=alpha.models + (editing,)),
            configured_provider("beta"),
            replace(configured_provider("disabled"), enabled=False),
        ]
        subject, recorder = service(providers)
        with pytest.raises(ValueError, match=expected):
            await generate(subject, **options)
        assert recorder.requests == recorder.history == recorder.discarded == []

    asyncio.run(run())


@pytest.mark.parametrize(
    "default,expected",
    [("", "未设置"), ("alpha:missing", "已失效"), ("disabled:paint", "已失效")],
)
def test_command_invalid_page_default_does_not_fall_back(default, expected):
    async def run():
        subject, recorder = service(default=default)
        with pytest.raises(ValueError, match=expected):
            await generate(subject)
        assert recorder.requests == recorder.history == []
        explicit = await generate(subject, model="alpha:paint")
        assert explicit.provider.id == "alpha"

    asyncio.run(run())


def test_command_explicit_and_default_selection_share_read_only_resolution():
    async def run():
        subject, recorder = service(
            [configured_provider(), configured_provider("beta")]
        )
        assert (
            subject.resolve_command_model(mode="text", provider_id="alpha")[0].id
            == "alpha"
        )
        assert (
            subject.resolve_command_model(mode="text2img", model="beta:paint")[0].id
            == "beta"
        )
        assert (
            subject.resolve_command_model(
                mode="text2img", provider_id="beta", model="paint"
            )[0].id
            == "beta"
        )
        assert recorder.requests == recorder.history == []
        explicit = await generate(subject, provider_id="beta", model="paint")
        assert (
            explicit.provider.id == "beta"
            and explicit.request.selection_source == "explicit"
        )

    asyncio.run(run())


def test_command_image_mode_uses_its_page_default_and_reference_limit():
    async def run():
        subject, _ = service(
            [configured_provider(), configured_provider("beta")],
            image_default="beta:paint",
        )
        references = tuple(
            ReferenceImage(
                id=str(index),
                filename=f"{index}.png",
                mime_type="image/png",
                data=b"reference",
            )
            for index in range(4)
        )
        result = await generate(subject, mode="img2img", references=references)
        assert result.provider.id == "beta"
        assert result.request.references == references[:2]

    asyncio.run(run())


def test_webui_and_llm_follow_model_count_limits_and_negative_behavior():
    async def run():
        subject, _ = service()
        page = await generate(subject, source="webui")
        tool = await generate(subject, source="llm_tool")
        assert page.request.count == 6 and page.request.negative_prompt == ""
        assert tool.request.count == 3 and tool.request.negative_prompt == ""
        assert (await generate(subject, source="webui", count=50)).request.count == 8
        assert (await generate(subject, source="llm_tool", count=50)).request.count == 8
        assert (
            await generate(subject, source="webui", model="missing")
        ).request.model == "paint"

    asyncio.run(run())
