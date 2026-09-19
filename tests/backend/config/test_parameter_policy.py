from __future__ import annotations

import asyncio
import copy
import io
import json
from dataclasses import replace

import pytest
from PIL import Image, PngImagePlugin

from astrbot_plugin_image_studio.backend.config import HistorySettings, RuntimeSettings
from astrbot_plugin_image_studio.backend.models import (
    GeneratedImage,
    ImageProvider,
    ReferenceImage,
)
from astrbot_plugin_image_studio.backend.parameters.exchange import (
    export_parameters,
    request_snapshot,
    resolve_parameters,
)
from astrbot_plugin_image_studio.backend.generation.service import (
    ImageGenerationService,
    _parameters_for_model,
)
from astrbot_plugin_image_studio.backend.gallery.store import GenerationStore


FLAGS = ("webui_visible", "record_in_history", "refill_from_history")


def policy_provider(parameters=None, *, tool=None, kind="custom_json"):
    return ImageProvider.from_mapping(
        {
            "id": "policy",
            "name": "Policy test provider",
            "kind": kind,
            "base_url": "https://example.invalid",
            "models": [
                {
                    "id": "paint",
                    "supports_text2img": True,
                    "supports_negative_prompt": True,
                    "negative_prompt_default": "page negative",
                    "parameters": parameters or {"quality": {"default": "high"}},
                    "tool": tool or {},
                    "native_batch_size": 4,
                    "max_concurrent_requests": 3,
                }
            ],
        }
    )


def policy_settings(provider):
    return RuntimeSettings(
        enable_llm_tool=True,
        providers=(provider,),
        history=HistorySettings(True, 100, 100, False),
        revision=0,
        default_page_text2img_model_ref="policy:paint",
        default_tool_text2img_model_ref="policy:paint",
    )


def studio_content(parameters=None, **data):
    return json.dumps(
        {
            "format": "image_studio",
            "version": 1,
            "generation_engine": "custom_json",
            "has_request_snapshot": True,
            "data": {
                "model_ref": "policy:paint",
                "model": "paint",
                "mode": "text2img",
                "prompt": "one mountain",
                "parameters": parameters or {},
                **data,
            },
            "metadata": {},
            "supplemental": {},
        }
    )


def image_with_metadata():
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("Software", "NovelAI")
    metadata.add_text(
        "Comment",
        json.dumps({"prompt": "upstream mountain", "seed": 123, "steps": 27}),
    )
    stream = io.BytesIO()
    Image.new("RGB", (32, 24), "green").save(stream, "PNG", pnginfo=metadata)
    return GeneratedImage(stream.getvalue(), "image/png")


class CapturingExecutor:
    def __init__(self):
        self.requests = []
        self.image = image_with_metadata()

    async def generate(self, _provider, request):
        self.requests.append(request)
        return (self.image,) * request.count


@pytest.mark.parametrize("flag", FLAGS)
def test_schema_policy_preserves_explicit_false_and_defaults_omitted_flags_true(flag):
    provider = policy_provider(
        {
            "omitted": {"type": "text", "default": "a"},
            "disabled": {"type": "text", "default": "b", flag: False},
        }
    )
    reloaded = ImageProvider.from_mapping(provider.public_dict())
    schema = reloaded.models[0].parameters
    assert schema["disabled"][flag] is False
    for name in FLAGS:
        assert schema["omitted"].get(name, True) is True


@pytest.mark.parametrize(
    "kind", ["openai_images", "gemini", "nai_direct", "custom_json"]
)
def test_injected_count_opts_out_of_refill_but_user_count_omission_means_true(kind):
    injected = policy_provider(kind=kind).models[0].parameters["count"]
    assert injected["refill_from_history"] is False
    custom = (
        policy_provider(
            {"copies": {"type": "integer", "default": 2, "request_key": "n"}},
            kind=kind,
        )
        .models[0]
        .parameters["copies"]
    )
    assert custom.get("refill_from_history", True) is True


def test_webui_visibility_does_not_change_tool_exposure_or_runtime_overrides():
    provider = policy_provider(
        {
            "private_panel": {
                "type": "integer",
                "default": 2,
                "webui_visible": False,
            },
            "visible_panel": {"type": "integer", "default": 3},
        },
        tool={
            "parameters": {
                "private_panel": {"exposed": True},
                "visible_panel": {"exposed": False},
            }
        },
    )
    model = provider.models[0]
    assert "private_panel" in model.llm_exposed_parameter_names
    assert "visible_panel" not in model.llm_exposed_parameter_names
    for source in ("webui", "command", "llm_tool"):
        actual = _parameters_for_model({"private_panel": 7}, model, source=source)
        assert actual["private_panel"] == 7


@pytest.mark.parametrize("source", ["webui", "command", "llm_tool"])
def test_record_policy_filters_mapped_and_core_values_without_modifying_wire_or_image(
    tmp_path, source
):
    async def run():
        provider = policy_provider(
            {
                "guidance": {
                    "type": "number",
                    "default": 6,
                    "request_key": "scale",
                    "record_in_history": False,
                },
                "canvas": {
                    "type": "text",
                    "default": "1024x1024",
                    "request_key": "size",
                    "record_in_history": False,
                },
                "copies": {
                    "type": "integer",
                    "default": 1,
                    "request_key": "count",
                    "record_in_history": False,
                },
                "seed": {"type": "integer", "default": 1, "record_in_history": False},
                "steps": {"type": "integer", "default": 24},
            }
        )
        executor = CapturingExecutor()
        store = GenerationStore(tmp_path)
        await store.initialize()
        service = ImageGenerationService(
            settings=policy_settings(provider), executor=executor, store=store
        )
        result = await service.generate(
            mode="text2img",
            provider_id="policy",
            model_ref="policy:paint",
            prompt="mountain",
            parameters={"guidance": 9, "seed": 5, "steps": 26},
            size="768x1024",
            count=2,
            source=source,
        )
        assert executor.requests[0].parameters["scale"] == 9
        assert executor.requests[0].parameters["seed"] == 5
        assert executor.requests[0].size == "768x1024"
        assert executor.requests[0].count == 2
        assert result.images[0].data == executor.image.data
        detail = await store.generation_detail(
            result.generation_id, include_assets=False
        )
        saved = detail["parameters"]
        assert "size" not in saved and "count" not in saved
        assert saved["parameters"] == {"steps": 26}
        assert detail["images"][0]["metadata"]["normalized"]["seed"] == 123
        assert next(store.assets_dir.rglob("*.png")).read_bytes() == executor.image.data
        assert "parameter_policy" not in saved
        assert "parameter_policy" not in detail["supplemental"]
        copied = json.loads(export_parameters(detail)["content"])["data"]
        assert "size" not in copied and "count" not in copied
        assert "seed" not in copied["parameters"]
        assert "scale" not in copied["parameters"]
        draft = await service.reproduction_plan(result.generation_id)
        assert draft["parameters"]["seed"] == 1
        assert draft["parameters"]["guidance"] == 6
        assert draft["parameters"]["steps"] == 26
        assert draft["size"] == "1024x1024"
        assert draft["count"] == 1
        current = provider.public_dict()
        current["models"][0]["parameters"]["seed"].update(
            record_in_history=True, default=9
        )
        service.update_settings(
            replace(service.settings, providers=(ImageProvider.from_mapping(current),))
        )
        later = await service.reproduction_plan(result.generation_id)
        assert later["parameters"]["seed"] == 9
        assert detail["images"][0]["metadata"]["normalized"]["seed"] == 123

    asyncio.run(run())


def test_request_snapshot_does_not_synthesize_omitted_values_from_defaults_or_metadata():
    detail = {
        "id": "generation",
        "source": "webui",
        "mode": "text2img",
        "provider_id": "policy",
        "provider_kind": "nai_direct",
        "model": "paint",
        "original_prompt": "mountain",
        "parameters": {"parameters": {"steps": 24}},
        "images": [
            {
                "id": "image",
                "metadata": {
                    "format": "novelai",
                    "normalized": {"seed": 123, "width": 832, "height": 1216},
                },
            }
        ],
    }
    snapshot = request_snapshot(detail)
    assert all(key not in snapshot for key in ("size", "count", "negative_prompt"))
    assert snapshot["parameters"] == {"steps": 24}
    exported = json.loads(export_parameters(detail, format_name="nai")["content"])
    assert "size" not in exported and "seed" not in exported


@pytest.mark.parametrize(
    "policy,expected",
    [
        ({}, 27),
        ({"webui_visible": False}, 24),
        ({"refill_from_history": False}, 24),
        ({"webui_visible": False, "refill_from_history": True}, 24),
        ({"record_in_history": False}, 27),
    ],
)
def test_reproduction_uses_current_visibility_and_refill_not_current_record_flag(
    policy, expected
):
    provider = policy_provider(
        {"steps": {"type": "integer", "default": 24, "min": 1, "max": 28, **policy}},
        tool={"parameters": {"steps": {"exposed": True, "default_override": 20}}},
    )
    result = resolve_parameters(
        studio_content({"steps": 27}),
        policy_settings(provider),
        for_reproduction=True,
    )
    assert result["draft"]["parameters"]["steps"] == expected


def test_reproduction_rechecks_changed_schema_for_the_same_old_record(tmp_path):
    async def run():
        initial = policy_provider({"steps": {"type": "integer", "default": 24}})
        store = GenerationStore(tmp_path)
        await store.initialize()
        service = ImageGenerationService(
            settings=policy_settings(initial), executor=CapturingExecutor(), store=store
        )
        generated = await service.generate(
            mode="text2img",
            provider_id="policy",
            prompt="mountain",
            parameters={"steps": 27},
        )
        before = await store.generation_detail(
            generated.generation_id, include_assets=False
        )
        for flags, expected in (
            ({"webui_visible": False}, 22),
            ({"webui_visible": True, "refill_from_history": False}, 22),
            ({"webui_visible": True, "record_in_history": False}, 27),
        ):
            current = policy_provider(
                {"steps": {"type": "integer", "default": 22, **flags}}
            )
            service.update_settings(replace(service.settings, providers=(current,)))
            draft = await service.reproduction_plan(generated.generation_id)
            assert draft["parameters"]["steps"] == expected
        after = await store.generation_detail(
            generated.generation_id, include_assets=False
        )
        assert after == before

    asyncio.run(run())


@pytest.mark.parametrize("count_key", ["count", "copies"])
def test_count_refill_is_schema_driven_but_model_scheduling_is_always_current(
    count_key,
):
    descriptor = {"type": "integer", "default": 2, "request_key": "count"}
    for allowed, expected in ((False, 2), (True, 5)):
        provider = policy_provider(
            {count_key: {**descriptor, "refill_from_history": allowed}}
        )
        resolved = resolve_parameters(
            studio_content(count=5, native_batch_size=99, max_concurrent_requests=99),
            policy_settings(provider),
            for_reproduction=True,
        )
        draft = resolved["draft"]
        assert draft["count"] == expected
        assert draft.get("native_batch_size", provider.models[0].native_batch_size) == 4
        assert (
            draft.get(
                "max_concurrent_requests", provider.models[0].max_concurrent_requests
            )
            == 3
        )
        assert "native_batch_size" not in draft["parameters"]
        assert "max_concurrent_requests" not in draft["parameters"]


@pytest.mark.parametrize(
    "descriptor,value",
    [
        ({"type": "integer", "default": 2, "min": 0}, 0),
        ({"type": "boolean", "default": True}, False),
        ({"type": "text", "default": "default"}, ""),
        ({"type": "select", "default": "default", "choices": [None, "default"]}, None),
    ],
)
def test_reproduction_preserves_legal_falsey_values(descriptor, value):
    result = resolve_parameters(
        studio_content({"option": value}),
        policy_settings(policy_provider({"option": descriptor})),
        for_reproduction=True,
    )
    assert "option" in result["draft"]["parameters"]
    assert result["draft"]["parameters"]["option"] == value


@pytest.mark.parametrize("value", [29, -1, True, "bad", 2.5, None])
def test_incompatible_historical_values_warn_and_leave_current_defaults(value):
    result = resolve_parameters(
        studio_content({"steps": value}),
        policy_settings(
            policy_provider(
                {"steps": {"type": "integer", "min": 1, "max": 28, "default": 24}}
            )
        ),
        for_reproduction=True,
    )
    assert result["draft"]["parameters"]["steps"] == 24
    assert result["unmapped"]["steps"] == value
    assert any("steps" in warning for warning in result["warnings"])


def test_hidden_or_non_refillable_parameters_without_defaults_stay_unset():
    provider = policy_provider(
        {
            "hidden": {"type": "text", "webui_visible": False},
            "not_restored": {"type": "text", "refill_from_history": False},
            "optional": {"type": "text"},
        }
    )
    result = resolve_parameters(
        studio_content({"hidden": "old", "not_restored": "old", "removed": "old"}),
        policy_settings(provider),
        for_reproduction=True,
    )
    assert all(
        name not in result["draft"]["parameters"]
        for name in ("hidden", "not_restored", "optional", "removed")
    )
    assert result["unmapped"]["removed"] == "old"
    assert result["warnings"]


def test_plain_paste_ignores_refill_policy_but_not_hidden_control_defaults():
    provider = policy_provider(
        {
            "steps": {"type": "integer", "default": 24, "refill_from_history": False},
            "hidden": {"type": "integer", "default": 2, "webui_visible": False},
        }
    )
    result = resolve_parameters(
        studio_content({"steps": 27, "hidden": 9}), policy_settings(provider)
    )
    assert result["draft"]["parameters"]["steps"] == 27
    assert result["draft"]["parameters"]["hidden"] == 2


def preset_parameters(*, record_style=False):
    return {
        "style": {
            "type": "preset",
            "default": "preset",
            "ui_only": True,
            "target": "artist",
            "record_in_history": record_style,
            "choices": [
                {"value": "custom", "label": "Custom"},
                {"value": "preset", "label": "Preset", "fill": "preset artist"},
            ],
        },
        "artist": {"type": "text", "default": ""},
    }


@pytest.mark.parametrize("source", ["webui", "command", "llm_tool"])
@pytest.mark.parametrize("artist", ["user artist", ""])
@pytest.mark.parametrize("artist_first", [False, True])
def test_explicit_artist_wins_over_preset_in_any_input_order(
    source, artist, artist_first
):
    supplied = (
        {"artist": artist, "style": "preset"}
        if artist_first
        else {"style": "preset", "artist": artist}
    )
    result = _parameters_for_model(
        supplied, policy_provider(preset_parameters()).models[0], source=source
    )
    assert result["artist"] == artist
    assert "style" not in result


def test_preset_only_and_configured_preset_still_expand_artist():
    model = policy_provider(preset_parameters()).models[0]
    for supplied in ({}, {"style": "preset"}):
        assert _parameters_for_model(supplied, model)["artist"] == "preset artist"


@pytest.mark.parametrize("record_style", [False, True])
def test_ui_only_values_can_be_recorded_without_becoming_upstream_parameters(
    tmp_path, record_style
):
    async def run():
        provider = policy_provider(preset_parameters(record_style=record_style))
        store = GenerationStore(tmp_path)
        await store.initialize()
        executor = CapturingExecutor()
        service = ImageGenerationService(
            settings=policy_settings(provider), executor=executor, store=store
        )
        result = await service.generate(
            mode="text2img",
            provider_id="policy",
            prompt="mountain",
            parameters={"style": "preset"},
        )
        assert "style" not in executor.requests[0].parameters
        assert executor.requests[0].parameters["artist"] == "preset artist"
        detail = await store.generation_detail(
            result.generation_id, include_assets=False
        )
        saved = detail["parameters"]["parameters"]
        assert ("style" in saved) is record_style
        if record_style:
            assert saved["style"] == "preset"
        assert saved["artist"] == "preset artist"

    asyncio.run(run())


@pytest.mark.parametrize(
    "artist,style", [("", "custom"), ("manual", "custom"), ("preset artist", "preset")]
)
def test_reproduction_preset_is_derived_from_final_artist_without_overwriting_it(
    artist, style
):
    provider = policy_provider(preset_parameters())
    original = studio_content({"artist": artist, "style": "preset"})
    result = resolve_parameters(
        original, policy_settings(provider), for_reproduction=True
    )
    assert result["draft"]["parameters"]["artist"] == artist
    assert result["draft"]["parameters"]["style"] == style
    assert json.loads(original)["data"]["parameters"]["style"] == "preset"


def test_import_reproduction_applies_current_policy_but_does_not_change_metadata():
    envelope = json.loads(studio_content())
    envelope["has_request_snapshot"] = False
    envelope["metadata"] = {
        "format": "novelai",
        "normalized": {"steps": 27, "seed": 123},
        "raw": {"Comment": '{"steps":27,"seed":123}'},
    }
    before = copy.deepcopy(envelope)
    provider = policy_provider(
        {
            "steps": {"type": "integer", "default": 24, "webui_visible": False},
            "seed": {"type": "integer", "default": 2, "refill_from_history": False},
        }
    )
    result = resolve_parameters(
        json.dumps(envelope), policy_settings(provider), for_reproduction=True
    )
    assert result["draft"]["parameters"]["steps"] == 24
    assert result["draft"]["parameters"]["seed"] == 2
    assert envelope == before


def test_reproduction_keeps_retained_references_while_applying_current_policy(tmp_path):
    async def run():
        raw = policy_provider(
            {"steps": {"type": "integer", "default": 24, "refill_from_history": False}}
        ).public_dict()
        raw["models"][0].update(supports_img2img=True, max_reference_images=2)
        provider = ImageProvider.from_mapping(raw)
        settings = replace(
            policy_settings(provider), history=HistorySettings(True, 100, 100, True)
        )
        store = GenerationStore(tmp_path)
        await store.initialize()
        service = ImageGenerationService(
            settings=settings, executor=CapturingExecutor(), store=store
        )
        reference = image_with_metadata()
        result = await service.generate(
            mode="img2img",
            provider_id="policy",
            model_ref="policy:paint",
            prompt="repaint mountain",
            parameters={"steps": 27},
            references=(
                ReferenceImage(
                    "reference", "source.png", reference.data, reference.mime_type
                ),
            ),
        )
        draft = await service.reproduction_plan(result.generation_id)
        assert draft["mode"] == "img2img"
        assert draft["reference_available"] is True
        assert len(draft["references"]) == 1
        assert draft["references"][0]["filename"] == "source.png"
        assert draft["references"][0]["preview_data_url"].startswith(
            "data:image/png;base64,"
        )
        assert draft["parameters"]["steps"] == 24
        assert not any("不包含原始参考图" in warning for warning in draft["warnings"])

    asyncio.run(run())


@pytest.mark.parametrize("name", ["negative_prompt", "negative_alias"])
def test_missing_negative_prompt_uses_schema_default_not_separate_model_default(name):
    provider = policy_provider(
        {
            name: {
                "type": "text",
                "request_key": "negative_prompt",
                "default": "schema negative",
                "refill_from_history": False,
            }
        }
    )
    result = resolve_parameters(
        studio_content(), policy_settings(provider), for_reproduction=True
    )
    assert result["draft"]["negative_prompt"] == "schema negative"
    assert result["draft"]["parameters"][name] == "schema negative"


@pytest.mark.parametrize("kind", ["integer", "number"])
@pytest.mark.parametrize("default", [None, 0])
def test_nullable_numeric_value_accepted_by_reproduction_can_be_generated_again(
    kind, default
):
    descriptor = {"type": kind, "default": default}
    if default is not None:
        descriptor["nullable"] = True
    provider = policy_provider({"seed": descriptor})
    result = resolve_parameters(
        studio_content({"seed": None}),
        policy_settings(provider),
        for_reproduction=True,
    )
    assert result["draft"]["parameters"]["seed"] is None
    actual = _parameters_for_model(result["draft"]["parameters"], provider.models[0])
    assert actual["seed"] is None


def test_nonrecorded_custom_parameter_does_not_remove_same_named_record_identity(
    tmp_path,
):
    async def run():
        provider = policy_provider(
            {
                "selection_source": {
                    "type": "text",
                    "default": "custom request value",
                    "record_in_history": False,
                }
            }
        )
        store = GenerationStore(tmp_path)
        await store.initialize()
        service = ImageGenerationService(
            settings=policy_settings(provider),
            executor=CapturingExecutor(),
            store=store,
        )
        result = await service.generate(
            mode="text2img",
            provider_id="policy",
            model_ref="policy:paint",
            prompt="mountain",
        )
        detail = await store.generation_detail(
            result.generation_id, include_assets=False
        )
        assert detail["parameters"]["selection_source"] == "explicit"
        assert "selection_source" not in detail["parameters"]["parameters"]

    asyncio.run(run())
