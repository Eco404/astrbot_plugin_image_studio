"""Parse public image generation commands without host or storage dependencies."""

import shlex
from typing import Any

COMMAND_HELP = """Image Studio 生图指令
/istudio <提示词> [参数]

--provider ID：指定服务商
--model ID：指定模型，可用 服务商ID:模型ID
--mode text2img|img2img：文生图或图生图
--size 尺寸：如 1024x1024，NAI 可用 竖图、2K横图
--n 数量：缺省使用模型默认值，超出模型上限时截断
--negative 内容：缺省使用模型默认反向提示词；--negative '' 清空
--ref 路径或URL：可重复填写多张参考图
--param-参数名 值：如 --param-steps 26，值保持字符串

默认模型在 WebUI 设置的“默认值 → 页面”中配置。
未指定模式时，有图片或 --ref 则使用图生图，否则文生图。
参考图顺序：当前消息、引用消息、--ref；去重后按模型上限截断。
指定图生图但没有可用参考图会报错；指定文生图则忽略参考图。
参数可写成 --key=value；包含空格的值请使用英文引号。
单独 /istudio --help 显示此帮助；与其他内容混用时忽略 --help。

示例：/istudio 清晨的山间湖泊
示例：/istudio 重绘这张图 --mode img2img --ref input.png
示例：/istudio '1girl, solo, full body, garden' --provider nai --model nai-diffusion-4-5-full --param-style galgame
""".strip()


def parse_command(raw: str, *, allow_empty_prompt: bool = False) -> dict[str, Any]:
    tokens = shlex.split(raw.strip())
    if tokens and tokens[0] in {"/istudio", "istudio"}:
        tokens.pop(0)
    help_only = tokens == ["--help"]
    tokens = [token for token in tokens if token != "--help"]
    values: dict[str, Any] = {
        "mode": "text2img",
        "provider_id": "",
        "model": "",
        "size": "",
        "negative_prompt": None,
        "count": None,
        "reference_paths": [],
        "mode_explicit": False,
        "parameters": {},
        "help": help_only,
    }
    if help_only:
        values["prompt"] = ""
        return values
    known = {"mode", "provider", "model", "size", "negative", "n", "ref"}
    prompt_parts: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("--") and "=" in token:
            key, value = token[2:].split("=", 1)
        elif token.startswith("--") and index + 1 < len(tokens):
            key, value = token[2:], tokens[index + 1]
            if (key in known or key.startswith("param-")) and value.startswith("--"):
                raise ValueError(f"参数 --{key} 缺少值")
            index += 1
        else:
            if token.startswith("--") and (
                token[2:] in known or token.startswith("--param-")
            ):
                raise ValueError(f"参数 {token} 缺少值")
            prompt_parts.append(token)
            index += 1
            continue
        if key in {"mode", "provider", "model", "size", "ref"} and not value.strip():
            raise ValueError(f"参数 --{key} 不能为空")
        if key == "mode":
            values["mode"] = value
            values["mode_explicit"] = True
        elif key == "provider":
            values["provider_id"] = value
        elif key == "model":
            values["model"] = value
        elif key == "size":
            values["size"] = value
        elif key == "negative":
            values["negative_prompt"] = value
        elif key == "n":
            try:
                values["count"] = int(value)
            except ValueError as exc:
                raise ValueError("参数 --n 必须是正整数") from exc
            if values["count"] <= 0:
                raise ValueError("参数 --n 必须是正整数")
        elif key == "ref":
            values["reference_paths"].append(value)
        elif key.startswith("param-"):
            if key == "param-":
                raise ValueError("--param- 后必须填写参数名")
            values["parameters"][key.removeprefix("param-")] = value
        else:
            prompt_parts.append(token)
        index += 1
    values["prompt"] = " ".join(prompt_parts).strip()
    if not values["prompt"] and not allow_empty_prompt:
        raise ValueError("请填写提示词；使用 /istudio --help 查看指令帮助")
    return values
