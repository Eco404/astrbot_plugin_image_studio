"""Keep actionable Image Studio contracts through AstrBot's two-stage schemas."""

import jsonschema
import pytest
from astrbot.core.agent.tool import ToolSet
from astrbot.core.provider.register import llm_tools

from astrbot_plugin_image_studio.main import ImageStudioPlugin


@pytest.mark.parametrize(
    "tool_name",
    ["image_studio_get_capabilities", "image_studio_generate"],
)
def test_parameter_only_tools_keep_contract_across_host_provider_formats(tool_name):
    assert callable(getattr(ImageStudioPlugin, tool_name))
    original = llm_tools.get_func(tool_name)
    tools = ToolSet([original]).get_param_only_tool_set()
    parameter_tool = tools.get_tool(tool_name)
    assert parameter_tool.description == ""
    assert parameter_tool.parameters == original.parameters
    assert parameter_tool.parameters is not original.parameters
    schemas = [
        tools.openai_schema()[0]["function"]["parameters"],
        tools.anthropic_schema()[0]["input_schema"],
        tools.google_schema()["function_declarations"][0]["parameters"],
    ]
    for schema in schemas:
        assert schema["description"] == original.parameters["description"]
        assert set(schema["properties"]) == set(original.parameters["properties"])
        assert schema.get("required", []) == original.parameters.get("required", [])
        for name, definition in original.parameters["properties"].items():
            assert (
                schema["properties"][name]["description"] == definition["description"]
            )


def test_parameter_only_query_explains_selection_and_query_only_requests():
    tool = (
        llm_tools.get_full_tool_set()
        .get_param_only_tool_set()
        .get_tool("image_studio_get_capabilities")
    )
    schema = tool.parameters
    guidance = schema["description"]
    selection = schema["properties"]["query_type"]["description"]
    assert "不生成图片" in guidance
    assert "search/providers 仅提供目录" in guidance
    assert "query_type=model 和 model_refs" in guidance
    assert "用户要求生图时" in guidance and "image_studio_generate" in guidance
    assert "用户只询问模型信息时" in guidance
    assert "用户未指定模型时优先使用" in selection
    assert "默认模型不支持所需功能" in selection
    assert "普通画风或画面描述不是更换理由" in selection
    assert "search 搜索用户指定的名称，选定后再用 model 查询参数" in selection


def test_parameter_only_generate_keeps_reuse_scope_and_attachment_guidance():
    tool = (
        llm_tools.get_full_tool_set()
        .get_param_only_tool_set()
        .get_tool("image_studio_generate")
    )
    schema = tool.parameters
    guidance = schema["description"]
    assert "生成前须调用 image_studio_get_capabilities" in guidance
    assert "当前这条用户消息" in guidance and "同一模型和模式" in guidance
    assert "修改 prompt 或 parameters 的值无需重查" in guidance
    assert "新的用户消息" in guidance and "插件设置变化后须重新查询" in guidance
    assert "也须先查询该默认模型" in schema["properties"]["model_ref"]["description"]
    references = schema["properties"]["references"]["description"]
    assert "直接使用附件提供的完整路径" in references
    assert "不必先用 Python 或 Shell" in references
    assert "引用消息图片" in references and "本次模型请求附件" in references


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"query_type": "default", "mode": ""},
        {"query_type": "model", "model_refs": ["provider:model", "missing-id"]},
        {"query_type": "model", "model_refs": ["provider:model"] * 20},
        {"query_type": "search", "limit": 50, "offset": 0},
    ],
)
def test_capability_schema_keeps_optional_fields_and_per_item_id_resolution(arguments):
    jsonschema.validate(
        arguments,
        llm_tools.get_func("image_studio_get_capabilities").parameters,
    )


@pytest.mark.parametrize(
    "arguments",
    [
        {"query_type": "unknown"},
        {"mode": "edit"},
        {"model_refs": "provider:model"},
        {"model_refs": []},
        {"model_refs": ["provider:model"] * 21},
        {"model_refs": [12]},
        {"limit": 0},
        {"limit": 51},
        {"limit": 2.5},
        {"offset": -1},
    ],
)
def test_capability_schema_exposes_existing_enum_and_batch_bounds(arguments):
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            arguments,
            llm_tools.get_func("image_studio_get_capabilities").parameters,
        )
