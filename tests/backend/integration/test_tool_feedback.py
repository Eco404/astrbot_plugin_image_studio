"""Tool contracts explain the actual operation and its failure boundary."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import jsonschema
import pytest
from astrbot.core.provider.register import llm_tools
from astrbot.core.agent.tool import ToolSet
from astrbot_plugin_image_studio.main import ImageStudioPlugin
from astrbot_plugin_image_studio.backend.models import ImageProvider, ReferenceImage
from astrbot_plugin_image_studio.backend.providers.errors import ProviderError
from astrbot_plugin_image_studio.tests.backend.integration.test_service_and_tool import (
    PNG,
    ToolEvent,
    settings,
)
from astrbot_plugin_image_studio.tests.support.comfy_runtime import runtime_fixture


def payload(result):
    return json.loads(result.content[0].text)


def plugin(service):
    instance = object.__new__(ImageStudioPlugin)
    instance._settings = settings()
    instance._service = service
    return instance


@pytest.mark.parametrize(
    "messages",
    [
        [{"type": "image", "asset_id": "image-id"}],
        [{"type": "plain", "text": "必要说明"}, {"type": "file", "path": "result.png"}],
    ],
)
def test_nested_delivery_schema_accepts_documented_messages(messages):
    schema = llm_tools.get_func("image_studio_send_output").parameters
    jsonschema.validate({"destination": "session", "messages": messages}, schema)
    parameter_view = llm_tools.get_full_tool_set().get_param_only_tool_set()
    assert parameter_view.get_tool("image_studio_send_output").parameters == schema


@pytest.mark.parametrize(
    "arguments",
    [
        {"destination": "session", "messages": ['{type: "image", asset_id: "x"}']},
        {"destination": "session", "messages": ["<private_companion_split/>"]},
        {"destination": "session", "messages": [{"asset_id": "x"}]},
        {"destination": "session", "messages": [{"type": "picture", "asset_id": "x"}]},
        {"destination": "session", "messages": []},
        {"messages": [{"type": "image", "asset_id": "x"}]},
    ],
)
def test_nested_delivery_schema_rejects_observed_invalid_arguments(arguments):
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            arguments, llm_tools.get_func("image_studio_send_output").parameters
        )


def test_reference_schema_and_registered_tool_doc_explain_automatic_inputs():
    tool = llm_tools.get_func("image_studio_generate")
    schema = tool.parameters
    jsonschema.validate(
        {"mode": "img2img", "references": [{"path": "attachment.jpg"}]}, schema
    )
    jsonschema.validate({"mode": "img2img"}, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"references": ["attachment.jpg"]}, schema)
    assert "引用消息图片" in tool.description
    assert "不必先用 Python 或 Shell" in tool.description
    assert set(schema["properties"]["references"]["items"]["properties"]) == {
        "asset_id",
        "path",
    }


def test_nested_contract_survives_host_provider_schema_conversion():
    tool = llm_tools.get_func("image_studio_send_output")
    tools = ToolSet([tool])
    schemas = [
        tools.openai_schema()[0]["function"]["parameters"],
        tools.anthropic_schema()[0]["input_schema"],
        tools.google_schema()["function_declarations"][0]["parameters"],
    ]
    for schema in schemas:
        assert schema["required"] == ["destination", "messages"]
        item = schema["properties"]["messages"]["items"]
        assert item["required"] == ["type"]
        assert item["properties"]["type"]["enum"] == [
            "plain",
            "image",
            "record",
            "video",
            "file",
        ]
        assert set(item["properties"]) == {
            "type",
            "text",
            "asset_id",
            "path",
            "url",
            "name",
        }


@pytest.mark.parametrize("parameter_only", [False, True])
@pytest.mark.parametrize(
    "tool_name,accepted,rejected",
    [
        (
            "image_studio_view_asset",
            [
                {"asset_ids": ["image-id"]},
                {"asset_ids": ["image-id"] * 8, "detail": "preview"},
                {"asset_ids": ["image-id"], "detail": "original"},
            ],
            [
                {},
                {"asset_ids": []},
                {"asset_ids": ["image-id"] * 9},
                {"asset_ids": "image-id"},
                {"asset_ids": [1]},
                {"asset_ids": ["image-id"], "detail": "invalid"},
            ],
        ),
        (
            "image_studio_task",
            [
                {},
                {"task_id": "job-id"},
                {"task_id": "job-id", "wait_seconds": 0},
                {"task_id": "job-id", "wait_seconds": 0.5},
                {"task_id": "job-id", "wait_seconds": 30},
            ],
            [
                {"wait_seconds": -0.1},
                {"wait_seconds": 30.1},
                {"wait_seconds": "30"},
                {"wait_seconds": True},
                {"wait_seconds": None},
            ],
        ),
    ],
)
def test_asset_and_task_bounds_survive_host_schema_conversion(
    parameter_only, tool_name, accepted, rejected
):
    tools = ToolSet([llm_tools.get_func(tool_name)])
    if parameter_only:
        tools = tools.get_param_only_tool_set()
    schemas = [
        tools.openai_schema()[0]["function"]["parameters"],
        tools.anthropic_schema()[0]["input_schema"],
        tools.google_schema()["function_declarations"][0]["parameters"],
    ]
    for schema in schemas:
        for arguments in accepted:
            jsonschema.validate(arguments, schema)
        for arguments in rejected:
            with pytest.raises(jsonschema.ValidationError):
                jsonschema.validate(arguments, schema)


@pytest.mark.parametrize(
    "tasks", [[], [{"task_id": "saved-task", "status": "running"}]]
)
def test_task_list_reports_its_comfyui_scope_without_submitting(tasks):
    async def run():
        scopes = []

        async def list_agent(**kwargs):
            scopes.append(kwargs["scope_id"])
            return tasks

        instance = plugin(
            SimpleNamespace(comfy_runtime=SimpleNamespace(list_agent=list_agent))
        )
        result = await instance.image_studio_task(ToolEvent())
        body = payload(result)
        assert not result.isError and body["tasks"] == tasks
        assert len(scopes) == 1
        assert "本次查询未提交生图" in body["message"]
        if tasks:
            assert "列表中的 task_id" in body["next_action"]
        else:
            assert "没有可查询的 ComfyUI 任务" in body["message"]
            assert "不包含其他服务商" in body["next_action"]
            assert "按 task_id" not in body["next_action"]

    asyncio.run(run())


@pytest.mark.parametrize(
    "error,expected_status,expected_request_status",
    [
        (ValueError("参数不在允许范围"), "validation_failed", "not_submitted"),
        (
            ProviderError("模板无效", request_status="not_submitted"),
            "validation_failed",
            "not_submitted",
        ),
        (
            ProviderError("HTTP 400", request_status="rejected"),
            "upstream_rejected",
            "rejected",
        ),
        (ProviderError("响应无法解析"), "generation_unknown", "unknown"),
        (TimeoutError(), "generation_unknown", "unknown"),
    ],
)
def test_tool_failures_retain_submission_evidence(
    error, expected_status, expected_request_status
):
    async def run():
        calls = []

        async def generate(**kwargs):
            calls.append(kwargs)
            raise error

        instance = plugin(SimpleNamespace(generate=generate))
        event = ToolEvent()
        await instance.image_studio_get_capabilities(event)
        result = await instance.image_studio_generate(
            event, prompt="tree", mode="text2img"
        )
        body = payload(result)
        assert result.isError and body["status"] == expected_status
        assert body["request_status"] == expected_request_status
        assert len(calls) == 1
        if expected_request_status == "rejected":
            assert "不要自动重试相同请求" in body["next_action"]
            assert "去掉参考图" in body["next_action"]
        elif expected_request_status == "unknown":
            assert "未调用生图接口" not in body["message"]
            assert "仅包含 ComfyUI" in body["next_action"]

    asyncio.run(run())


def test_query_precondition_fails_before_calling_service():
    async def run():
        instance = plugin(SimpleNamespace())
        result = await instance.image_studio_generate(
            ToolEvent(), prompt="tree", mode="text2img"
        )
        body = payload(result)
        assert result.isError
        assert body["status"] == "validation_failed"
        assert body["request_status"] == "not_submitted"
        assert "必须先调用 image_studio_get_capabilities" in body["error"]

    asyncio.run(run())


def test_comfy_result_read_error_does_not_claim_submission_did_not_happen(
    tmp_path, monkeypatch
):
    async def run():
        runtime, service, _, client = await runtime_fixture(tmp_path, monkeypatch)
        client.release_wait.clear()

        async def fail_to_inspect(*args, **kwargs):
            raise ValueError("模拟任务提交后的读取错误")

        monkeypatch.setattr(runtime, "inspect_agent", fail_to_inspect)
        instance = plugin(service)
        instance._settings = service.settings
        event = ToolEvent()
        try:
            await instance.image_studio_get_capabilities(event)
            result = await instance.image_studio_generate(event, mode="text2img")
            body = payload(result)
            assert result.isError and body["status"] == "generation_unknown"
            assert body["request_status"] == "unknown"
            listed = payload(await instance.image_studio_task(event))
            assert len(listed["tasks"]) == 1
            assert "不要重新提交" in body["next_action"]
        finally:
            client.release_wait.set()
            await runtime.close()
            await service.store.close()

    asyncio.run(run())


@pytest.mark.parametrize("failure", [None, ValueError("参考图为空或超过 20 MB 上限")])
def test_bad_reference_identifies_input_without_submitting(failure):
    async def run():
        read_paths = []

        async def reference_from_media_ref(path, **kwargs):
            read_paths.append(path)
            if path == "first.png":
                return ReferenceImage("first", path, PNG, "image/png")
            if failure:
                raise failure
            return None

        instance = plugin(
            SimpleNamespace(reference_from_media_ref=reference_from_media_ref)
        )
        image_provider = ImageProvider.from_mapping(
            {
                "id": "test-provider",
                "kind": "openai_images",
                "base_url": "https://example.test",
                "models": [
                    {
                        "id": "test-image",
                        "supports_img2img": True,
                        "max_reference_images": 8,
                    }
                ],
            }
        )
        instance._settings = replace(instance._settings, providers=(image_provider,))
        event = ToolEvent()
        await instance.image_studio_get_capabilities(event)
        result = await instance.image_studio_generate(
            event,
            mode="img2img",
            model_ref="test-provider:test-image",
            prompt="tree",
            references=[{"path": "first.png"}, {"path": "wrong-extension.png"}],
        )
        body = payload(result)
        assert result.isError
        assert body.get("reference_index") == 1 and body["reference_field"] == "path", (
            body
        )
        assert body["request_status"] == "not_submitted"
        assert "references[1].path" in body["error"]
        assert "20 MB" in body["error"] if failure else "扩展名" in body["error"]
        assert read_paths == ["first.png", "wrong-extension.png"]

    asyncio.run(run())
