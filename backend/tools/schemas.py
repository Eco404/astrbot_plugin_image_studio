"""Parameter contracts retained by AstrBot's full and skills-like tool schemas."""

from copy import deepcopy


def with_image_arguments(handler):
    """Enrich this plugin's just-registered tool after AstrBot parses its docstring."""
    from astrbot.core.provider.register import llm_tools

    tool = llm_tools.get_func(handler.__name__)
    parameters = deepcopy(tool.parameters)
    properties = parameters["properties"]
    if handler.__name__ == "image_studio_get_capabilities":
        parameters["description"] = (
            "本工具只查询模型或工作流，不生成图片。search/providers 仅提供目录，"
            "选定模型后须用 query_type=model 和 model_refs 查询其参数。"
            "用户要求生图时，取得可用模型的参数后继续调用 image_studio_generate；"
            "用户只询问模型信息时，按查询结果答复。"
        )
        properties["query_type"]["enum"] = [
            "default",
            "model",
            "providers",
            "search",
            "all",
        ]
        properties["mode"]["enum"] = ["", "text2img", "img2img"]
        properties["model_refs"].update(
            minItems=1, maxItems=20, items={"type": "string"}
        )
        properties["limit"].update(type="integer", minimum=1, maximum=50)
        properties["offset"].update(type="integer", minimum=0)
    elif handler.__name__ == "image_studio_generate":
        parameters["description"] = (
            "生成前须调用 image_studio_get_capabilities 查询目标模型在所选模式下的参数和提示词要求。"
            "处理当前这条用户消息期间，插件设置未变时，同一模型和模式可复用已经成功的查询；"
            "修改 prompt 或 parameters 的值无需重查。"
            "新的用户消息、尚未查询的模型或模式，或插件设置变化后须重新查询。"
        )
        properties["mode"]["enum"] = ["text2img", "img2img"]
        properties["references"].update(
            maxItems=8,
            items={
                "type": "object",
                "properties": {
                    "asset_id": {
                        "type": "string",
                        "description": "本插件返回的图片编号；与 path 二选一。",
                    },
                    "path": {
                        "type": "string",
                        "description": "消息附件提供的完整路径或当前工作区图片路径；保留原文件名和扩展名，与 asset_id 二选一。",
                    },
                },
                "additionalProperties": False,
            },
        )
    elif handler.__name__ == "image_studio_send_output":
        parameters["required"] = ["destination", "messages"]
        properties["destination"]["enum"] = ["session", "workspace"]
        properties["messages"].update(
            minItems=1,
            maxItems=40,
            items={
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["plain", "image", "record", "video", "file"],
                        "description": "消息类型；workspace 仅支持 image。",
                    },
                    "text": {
                        "type": "string",
                        "description": "plain 必填，仅用于必要附言；完整最终正文直接回复。",
                    },
                    "asset_id": {
                        "type": "string",
                        "description": "image 使用本插件返回的图片编号；与 path、url 三选一。workspace 必须使用此字段。",
                    },
                    "path": {
                        "type": "string",
                        "description": "session 发送工作区中的媒体文件；与 asset_id、url 三选一。",
                    },
                    "url": {
                        "type": "string",
                        "description": "session 发送 HTTP/HTTPS 媒体地址；与 asset_id、path 三选一。",
                    },
                    "name": {"type": "string", "description": "可选的文件名称。"},
                },
                "required": ["type"],
                "additionalProperties": False,
            },
        )
    tool.parameters = parameters
    return handler
