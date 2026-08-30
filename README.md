# Image Studio 生图插件

`astrbot_plugin_image_gen` 为 AstrBot 提供统一的多服务商生图能力，支持指令调用、LLM 工具调用和 WebUI 测试。

插件内置三个 WebUI 板块：

- **生图**：先选择“文生图 / 图生图”，再从支持该模式的模型中选择一个，工作区会按模型参数 schema 动态生成。
- **画廊**：搜索和筛选历史记录，查看完整参数，复现生成配置，导出或批量删除记录。
- **设置**：管理插件开关、并发限制、历史保留策略和生图服务商。

## 安装

将本目录放到 `AstrBot/data/plugins/astrbot_plugin_image_gen`，启用插件后，从插件详情页打开 Image Studio 页面。

WebUI 是插件的主要设置入口。AstrBot 原生插件设置页只显示基础开关和并发限制；WebUI 中的服务商与历史设置仍然读写同一份 AstrBot 插件配置文件，存放在隐藏的 `webui_managed` 配置组中。

## 支持的服务商类型

当前提供以下适配器：

- `openai_images`：OpenAI Images API，以及兼容 `/images/generations` 和 `/images/edits` 的服务。
- `gemini`：Gemini `generateContent` 图片输出，支持内联参考图。
- `nai_direct`：兼容 NAI `GET /generate` 的文生图服务。
- `custom_json`：可自定义请求体模板和图片响应提取路径的 JSON 接口。

服务商只负责连接配置：类型、地址、路径、请求头、鉴权和超时。模型配置单独位于“模型配置”区域，同一服务商可以添加多个模型，并分别声明文生图、图生图、反向提示词和最大参考图数量。被停用、配置不完整或没有支持当前模式模型的服务商不会出现在生图页面的模型列表中。

模型参数是可扩展的 JSON schema，前端不需要为新模型增加代码。每个参数可以包含 `type`（`text`、`number`、`select`、`boolean`、`json`）、`label`、`default`、`request_key`、`min`、`max`、`step` 和 `choices`。例如：

```json
{
  "steps": {"type": "number", "label": "步数", "default": 28, "min": 1, "max": 60, "request_key": "steps"},
  "sampler": {"type": "select", "label": "采样器", "default": "k_euler", "choices": ["k_euler", "k_dpmpp_2m"]}
}
```

OpenAI Images 会预填尺寸、数量、质量、背景和输出格式；Gemini 会预填画面比例和图片尺寸；NAI 会预填尺寸、采样器、步数、Scale、CFG、噪声调度和种子。`custom_json` 可用同一套控件描述任意新参数，未识别的参数会按 `request_key` 原样传给自定义接口。

## 反向提示词支持

反向提示词不是所有生图接口的通用参数：

- NAI 直连使用 `negative` 参数，默认支持。
- OpenAI Images 标准接口没有专用反向提示词参数，插件不会向该接口发送 `negative_prompt`。
- Gemini `generateContent` 图片输出没有专用反向提示词字段，限制内容应写入正向提示词。
- 自定义 JSON 服务商默认关闭该字段；确认目标模型对应接口明确支持后，可在模型配置中开启，插件才会注入 `negative_prompt`。

生图页面会根据当前服务商能力禁用或启用反向提示词输入，后端也会再次校验，避免把不受支持的字段发送给标准接口。

## 指令与 Agent 工具

指令示例：

```text
/image_gen 一张山间湖泊的影棚风格摄影 --provider my-openai --size 1024x1024
/image_gen 重新绘制这张图片 --mode img2img --ref /path/from/astrbot/temp/tool_images/file.png
```

LLM 工具名为 `image_gen_generate`。工具成功后会返回 MCP `ImageContent`，而不只是文本路径。AstrBot 会缓存图片，并把它加入后续支持视觉输入的 Agent 步骤，因此 Agent 可以继续查看、判断和处理生成结果。

## 历史与参考图

- 是否保留历史、最大记录数和最大图片容量均可在 WebUI 设置。
- 结果图会作为画廊记录保存；参考图只显示在对应生成记录的详情中，不会单独成为画廊卡片。
- 参考图可以在详情中单独删除，删除后不会影响结果图和请求参数。
- 即使历史参考图没有保留，仍可恢复已有参数并进入生图页面；界面会明确提示需要重新补充参考图。
- 画廊导出文件不会包含 API 密钥、鉴权请求头或其他疑似凭据字段。

## 数据位置

- 插件配置：AstrBot 的插件配置文件，其中包括 WebUI 可见的 API 密钥。
- 历史数据库：`data/plugin_data/astrbot_plugin_image_gen/history.sqlite3`。
- 结果图、缩略图和保留的参考图：同一插件数据目录下的对应子目录。

## 开发验证

在仓库目录中使用 AstrBot 虚拟环境运行：

```bash
conda run -n astrbot python -m pytest -q tests
conda run -n astrbot ruff check .
conda run -n astrbot ruff format --check .
node --check pages/image-studio/app.js
```

真实生图还需要在 WebUI 中配置可用端点、模型和凭据，并对目标服务商进行连接测试。
