from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from astrbot.api.message_components import Image, Reply
from astrbot_plugin_image_studio import main as plugin_main
from astrbot_plugin_image_studio.main import ImageStudioPlugin, _parse_command
from astrbot_plugin_image_studio.models import (
    GeneratedImage,
    ImageProvider,
    ReferenceImage,
)
from astrbot_plugin_image_studio.tests.test_service_and_tool import PNG, ToolEvent


class CommandEvent(ToolEvent):
    def __init__(self, text, messages=(), request_refs=()):
        super().__init__()
        self.message_str = text
        self.messages = list(messages)
        self._extras["provider_request"] = SimpleNamespace(
            image_urls=list(request_refs), extra_user_content_parts=[]
        )

    def get_messages(self):
        return self.messages

    def plain_result(self, text):
        return {"text": text}

    def chain_result(self, chain):
        return {"chain": chain}


class CommandService:
    def __init__(self, data_by_path=None, limit=3, selection_error=None):
        self.data_by_path = data_by_path or {}
        self.reads = []
        self.generated = []
        self.selected = []
        self.selection_error = selection_error
        self.provider = ImageProvider.from_mapping(
            {
                "id": "provider",
                "kind": "openai_images",
                "base_url": "https://example.test",
                "models": [
                    {
                        "id": "model",
                        "supports_text2img": True,
                        "supports_img2img": True,
                        "max_reference_images": limit,
                    }
                ],
            }
        )

    def resolve_command_model(self, **options):
        self.selected.append(options)
        if self.selection_error:
            raise ValueError(self.selection_error)
        return self.provider, self.provider.models[0]

    async def reference_from_media_ref(self, path, *, workspace_root=None):
        self.reads.append(path)
        value = self.data_by_path.get(path)
        if isinstance(value, Exception):
            raise value
        if value is None:
            return None
        return ReferenceImage(path, path, value, "image/png")

    async def generate(self, **options):
        self.generated.append(options)
        if options["mode"] == "img2img" and not options["references"]:
            raise ValueError("未读取到可用参考图")
        return SimpleNamespace(images=(GeneratedImage(PNG, "image/png"),), warning="")


def plugin(service=None):
    result = object.__new__(ImageStudioPlugin)
    result._service = service
    result.context = SimpleNamespace(_db=None)
    result._settings = SimpleNamespace(
        history=SimpleNamespace(record_invocation_identity=False)
    )
    return result


@pytest.fixture
def references(monkeypatch):
    calls = {"images": [], "quoted": 0, "workspace": 0}
    quoted = []

    async def convert(image):
        calls["images"].append(image.file)
        if image.file.startswith("broken-"):
            raise ValueError("cannot resolve message image")
        return image.file

    async def extract(_event):
        calls["quoted"] += 1
        return list(quoted)

    async def workspace(_event, _context):
        calls["workspace"] += 1
        return None

    monkeypatch.setattr(Image, "convert_to_file_path", convert)
    monkeypatch.setattr(plugin_main, "extract_quoted_message_images", extract)
    monkeypatch.setattr(plugin_main, "_event_workspace_root", workspace)
    return calls, quoted


def invoke(instance, event):
    async def run():
        return [result async for result in instance.image_gen(event)]

    return asyncio.run(run())


def test_command_parser_preserves_model_defaults_and_quoted_strings():
    options = _parse_command(
        '/img "blue sky" --param-steps 24 --param-cfg=0.3 --param-style "soft light"'
    )
    assert options["prompt"] == "blue sky"
    assert options["count"] is None
    assert options["negative_prompt"] is None
    assert options["parameters"] == {"steps": "24", "cfg": "0.3", "style": "soft light"}
    assert options["mode_explicit"] is False
    assert _parse_command("/img sky --negative ''")["negative_prompt"] == ""


@pytest.mark.parametrize(
    "text", ["/img --help", "image_gen --help", "/image_gen --help"]
)
def test_help_requires_no_service_reference_or_upstream_io(text, references):
    options = _parse_command(text)
    assert options["help"] is True
    instance = plugin()
    event = CommandEvent(text, [Image(file="broken-never.png")])
    result = invoke(instance, event)
    assert len(result) == 1 and "--" in result[0]["text"]
    assert references[0] == {"images": [], "quoted": 0, "workspace": 0}


@pytest.mark.parametrize(
    "text", ["/img --help sky --n 2", "/img sky --n 2 --help", "/img sky --help --n=2"]
)
def test_mixed_help_token_is_ignored_without_consuming_following_argument(text):
    options = _parse_command(text)
    assert options.get("help", False) is False
    assert options["prompt"] == "sky"
    assert options["count"] == 2


@pytest.mark.parametrize("key", ["provider", "model", "mode", "ref", "n"])
@pytest.mark.parametrize("suffix", ["", " ''", "=", " --size 512x512"])
def test_explicit_known_option_missing_or_empty_value_rejected(key, suffix):
    with pytest.raises(ValueError):
        _parse_command(f"/img sky --{key}{suffix}")


@pytest.mark.parametrize("value", ["nonsense", "1.5", "0", "-2", "NaN"])
def test_invalid_explicit_count_rejected_instead_of_becoming_one(value):
    with pytest.raises(ValueError):
        _parse_command(f"/img sky --n={value}")


def test_command_no_images_keeps_text_mode_and_forwards_none_defaults(references):
    service = CommandService()
    result = invoke(plugin(service), CommandEvent("/img blue sky"))
    assert "chain" in result[0]
    request = service.generated[0]
    assert request["mode"] == "text2img"
    assert request["count"] is None and request["negative_prompt"] is None
    assert request["references"] == () and request["source"] == "command"
    assert not service.reads


@pytest.mark.parametrize(
    "limit,expected",
    [
        (1, ["direct.png"]),
        (3, ["direct.png", "quoted.png", "core.png"]),
        (5, ["direct.png", "quoted.png", "core.png", "request.png", "explicit.png"]),
    ],
)
def test_direct_before_earlier_reply_then_quoted_core_request_and_explicit_references(
    references, limit, expected
):
    calls, quoted = references
    quoted.extend(["core.png"])
    paths = ["direct.png", "quoted.png", "core.png", "request.png", "explicit.png"]
    service = CommandService({path: PNG + path.encode() for path in paths}, limit=limit)
    event = CommandEvent(
        "/img repaint --ref explicit.png",
        [Reply(id="quote", chain=[Image(file="quoted.png")]), Image(file="direct.png")],
        request_refs=["request.png"],
    )
    result = invoke(plugin(service), event)
    assert "chain" in result[0]
    request = service.generated[0]
    assert request["mode"] == "img2img"
    assert [image.filename for image in request["references"]] == expected
    assert calls["images"][0] == "direct.png"


def test_content_duplicates_do_not_consume_reference_slots(references):
    service = CommandService(
        {
            "first.png": PNG,
            "same-other-path.png": PNG,
            "second.png": PNG + b"second",
            "third.png": PNG + b"third",
            "fourth.png": PNG + b"fourth",
        },
        limit=3,
    )
    event = CommandEvent(
        "/img edit",
        [
            Image(file="first.png"),
            Image(file="same-other-path.png"),
            Image(file="second.png"),
            Image(file="third.png"),
            Image(file="fourth.png"),
        ],
    )
    assert "chain" in invoke(plugin(service), event)[0]
    assert [image.filename for image in service.generated[0]["references"]] == [
        "first.png",
        "second.png",
        "third.png",
    ]
    assert "fourth.png" not in service.reads


def test_command_sources_keep_direct_reply_core_request_and_required_order(references):
    async def run():
        first = Image(file="direct.png")
        quote = Image(file="quoted.png")
        references[1].append("core.png")
        event = CommandEvent(
            "/img repaint",
            [Reply(id="quoted", chain=[quote]), first],
            request_refs=["request.png"],
        )
        sources = await plugin()._command_reference_sources(event, ["explicit.png"])
        assert sources == [
            (first, False),
            (quote, False),
            ("core.png", False),
            ("request.png", False),
            ("explicit.png", True),
        ]
        assert not references[0]["images"] and references[0]["workspace"] == 0

    asyncio.run(run())


def test_unreadable_direct_can_use_valid_quoted_input_without_changing_mode(references):
    service = CommandService({"quoted.png": PNG}, limit=1)
    event = CommandEvent(
        "/img repaint",
        [
            Image(file="broken-direct.png"),
            Reply(id="quoted", chain=[Image(file="quoted.png")]),
        ],
    )
    assert "chain" in invoke(plugin(service), event)[0]
    assert service.generated[0]["mode"] == "img2img"
    assert [reference.filename for reference in service.generated[0]["references"]] == [
        "quoted.png"
    ]


@pytest.mark.parametrize(
    "path,value",
    [
        ("missing.png", None),
        ("blocked.png", ValueError("unsafe path")),
        ("broken-direct.png", PNG),
    ],
)
def test_unreadable_automatic_input_never_silently_falls_back_to_text_mode(
    references, path, value
):
    service = CommandService({path: value})
    result = invoke(plugin(service), CommandEvent("/img repaint", [Image(file=path)]))
    assert "参考图" in result[0]["text"]
    assert service.selected[0]["mode"] == "img2img"
    assert all(request["mode"] == "img2img" for request in service.generated)


@pytest.mark.parametrize("reference", ["missing.png", "blocked.png"])
def test_explicit_bad_reference_reports_error(references, reference):
    service = CommandService({"blocked.png": ValueError("outside workspace")})
    result = invoke(plugin(service), CommandEvent(f"/img repaint --ref {reference}"))
    assert "显式参考图" in result[0]["text"]
    assert not service.generated


def test_explicit_bad_reference_is_not_hidden_by_other_valid_input(references):
    service = CommandService({"direct.png": PNG}, limit=3)
    event = CommandEvent("/img repaint --ref missing.png", [Image(file="direct.png")])
    result = invoke(plugin(service), event)
    assert "显式参考图" in result[0]["text"]
    assert service.reads == ["direct.png", "missing.png"]
    assert not service.generated


def test_explicit_text_mode_skips_message_quoted_workspace_and_explicit_references(
    references,
):
    service = CommandService()
    event = CommandEvent(
        "/img sky --mode text2img --ref missing.png",
        [
            Image(file="broken-direct.png"),
            Reply(id="quoted", chain=[Image(file="broken-quote.png")]),
        ],
        request_refs=["request.png"],
    )
    assert "chain" in invoke(plugin(service), event)[0]
    assert service.generated[0]["mode"] == "text2img"
    assert service.generated[0]["references"] == ()
    assert not service.reads
    assert references[0] == {"images": [], "quoted": 0, "workspace": 0}


def test_explicit_img2img_without_any_input_reports_no_reference(references):
    service = CommandService()
    result = invoke(plugin(service), CommandEvent("/img repaint --mode img2img"))
    assert "参考图" in result[0]["text"]
    assert all(request["mode"] == "img2img" for request in service.generated)


def test_model_validation_precedes_image_materialization(references):
    service = CommandService(selection_error="指定模型不存在")
    event = CommandEvent("/img repaint --model unknown", [Image(file="direct.png")])
    result = invoke(plugin(service), event)
    assert "指定模型不存在" in result[0]["text"]
    assert not service.reads and not references[0]["images"]
    assert not service.generated


def test_ordered_loader_mode_does_not_change_original_llm_explicit_precedence(
    references,
):
    async def run():
        service = CommandService({"explicit.png": PNG, "direct.png": PNG + b"direct"})
        instance = plugin(service)
        event = CommandEvent("", [Image(file="direct.png")])
        automatic = await instance._event_references(event, ["explicit.png"])
        assert [item.filename for item in automatic] == ["explicit.png", "direct.png"]
        exact = await instance._event_references(
            event, ["explicit.png"], include_event_references=False
        )
        assert [item.filename for item in exact] == ["explicit.png"]

    asyncio.run(run())
