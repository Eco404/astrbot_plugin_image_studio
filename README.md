# Image Studio 生图插件

`astrbot_plugin_image_studio` 为 AstrBot 提供统一的多服务商生图能力，支持指令调用、LLM 工具调用和 WebUI 测试。

插件内置三个 WebUI 板块：

- **生图**：先选择“文生图 / 图生图”，再从支持该模式的模型中选择一个，工作区会按模型参数 schema 动态生成。
- **画廊**：搜索和筛选历史记录，查看完整参数，复现生成配置，导出或批量删除记录。
- **设置**：管理历史策略、Provider 并发、服务商、模型能力和 LLM 工具参数。

## 安装

将本目录放到 `AstrBot/data/plugins/astrbot_plugin_image_studio`，启用插件后，从插件详情页打开 Image Studio 页面。

WebUI 是插件的主要设置入口。AstrBot 原生插件设置页只保留“启用 LLM 生图工具”；服务商、模型和历史设置由插件写入数据目录中的 `studio_config.json`。

“默认值”可以分别设置页面/指令和 LLM 工具使用的文生图、图生图模型。尺寸、数量等生成参数不设置全局默认值，而是使用所选模型参数 schema 中的默认配置。

## 支持的服务商类型

当前提供以下适配器：

- `openai_images`：OpenAI Images API，以及兼容 `/images/generations` 和 `/images/edits` 的服务。
- `gemini`：Gemini `generateContent` 图片输出，支持内联参考图。
- `nai_direct`：兼容 `astrbot_plugin_nai_image` 使用的第三方 `nai.sta1n.cn` 协议，默认请求 `GET https://nai.sta1n.cn/generate`。这里的 Token 是该站申请的 `toUserId`，并非 NovelAI 官方 API 凭据。
- `custom_json`：可自定义请求体模板和图片响应提取路径的 JSON 接口。

服务商只负责连接配置：类型、地址、路径、请求头、鉴权、超时和该 Provider 的并发上限。服务商允许在没有模型时保存；“获取模型”会尽力读取远程模型列表和能力，读取不到的能力使用保守预填并允许手动调整。模型配置单独位于“模型配置”区域，同一服务商可以添加多个模型，并分别声明文生图、图生图、反向提示词和最大参考图数量。

模型参数是可扩展的 JSON schema，前端不需要为新模型增加代码。每个参数可以包含 `type`（`text`、`textarea`、`number`、`select`、`boolean`、`json`、`preset`）、`label`、`default`、`request_key`、`min`、`max`、`step` 和 `choices`。`preset` 可通过 `target` 和选项中的 `fill` 联动填写另一个参数；配合 `ui_only: true` 时只参与界面交互，不会发送给上游。例如：

```json
{
  "steps": {"type": "number", "label": "步数", "default": 28, "min": 1, "max": 60, "request_key": "steps"},
  "sampler": {"type": "select", "label": "采样器", "default": "k_euler", "choices": ["k_euler", "k_dpmpp_2m"]}
}
```

OpenAI Images 会预填尺寸、数量、质量、背景和输出格式；Gemini 会预填画面比例和图片尺寸；NAI 第三方 GET 会预填绘画风格、画师串、中文尺寸、采样器、步数、Scale、CFG Rescale、噪声调度和默认反向提示词，并固定发送 `nocache=1`。NAI 的画师串默认留空，绘画风格默认显示“自定义”。其 `scale` 是 Prompt Guidance，范围 `1–20`、默认 `6`；`cfg` 会映射为 `cfg_rescale`，范围 `0–1`、插件默认 `0.3`；当前第三方接口将 `steps` 限制为 `1–28`。采样器默认 `k_euler_ancestral`。

## 反向提示词支持

反向提示词不是所有生图接口的通用参数：

- NAI 第三方 GET 使用 `negative` 查询参数，默认支持。
- OpenAI Images 标准接口没有专用反向提示词参数，插件不会向该接口发送 `negative_prompt`。
- Gemini `generateContent` 图片输出没有专用反向提示词字段，限制内容应写入正向提示词。
- 自定义 JSON 服务商默认关闭该字段；确认目标模型对应接口明确支持后，可在模型配置中开启，插件才会注入 `negative_prompt`。

生图页面会根据当前服务商能力禁用或启用反向提示词输入，后端也会再次校验，避免把不受支持的字段发送给标准接口。支持反向提示词的模型还可以在“工具配置”中单独控制“向 LLM 暴露反向提示词”：关闭后，LLM 能力查询不会返回该参数，LLM 也不能覆盖它，但后端仍会使用模型配置中的默认反向提示词。

## 指令与 Agent 工具

指令示例：

```text
/image_gen 一张山间湖泊的影棚风格摄影 --provider my-openai --size 1024x1024
/image_gen 重新绘制这张图片 --mode img2img --ref /path/from/astrbot/temp/tool_images/file.png
```

LLM 工具包括 `image_studio_get_capabilities`、`image_studio_generate`、`image_studio_view_asset` 和 `image_studio_send_output`。每次生成前都必须先查询能力。未指定模型或模型类型的常规请求首先使用 `query_type=default`；`mode` 可以省略，此时同时返回文生图和图生图默认模型，也可以指定 `text2img` 或 `img2img` 只获取对应默认。如果默认模型支持用户明确要求的模式、参考图数量和参数，就直接生成，不再查询全部模型。普通主体、画风、构图和文字描述可以通过提示词表达，不属于模型能力缺口。只有默认模型存在明确能力缺口或不可用、用户要求比较模型，或者指定了模型类型但不知道具体 `model_ref` 时，才使用 `query_type=all`，并尽量携带相同的 `mode`。明确指定模型时使用 `query_type=model` 和完整 `model_ref`。

查询结果会明确自然语言或 NAI tag 提示词格式，并只列出当前模型允许 LLM 使用的动态参数；一次查询授权只供对应模型和模式生成一次。查询 `all` 后可以直接使用选中的模型，不需要再次查询 `model`。

`image_studio_generate` 只公开 `prompt`、`mode`、`model_ref`、`parameters` 和 `references`。Provider 由完整 `model_ref` 确定；尺寸、数量和 `negative_prompt` 等模型参数只有在能力查询返回时才能放入 `parameters`。LLM 仍传入未暴露或模型不支持的字段时，插件会静默丢弃并使用模型或工具配置的默认值；已暴露字段仍按 schema 校验。

LLM 应主动选择 `mode=text2img` 或 `mode=img2img`。文生图不会因为消息中带有图片而自动切换模式；图生图应在 `references` 中使用 `{ "asset_id": "..." }` 引用 Image Studio 资产，或使用 `{ "path": "workspace/input.png" }` 引用当前 workspace 图片。只有图生图没有提供具体引用，或 LLM 连模式也没有填写时，后端才自动读取当前消息及引用消息图片作为容错。非空 `references` 只使用明确提供的内容，不与消息图片合并。指令调用继续自动读取当前消息及引用消息中的图片；没有显式指定模式时，检测到图片会自动使用图生图。

所有生成图都进入统一的内容寻址资产库。LLM 只会获得 `asset_id`、MIME 类型和原图大小，不会获得插件私有路径。设置页“图片资产”支持三种返回方式：

- `轻量预览`：默认方式，向 LLM 返回与画廊共用的 WebP 预览；可配置最大边长和质量。
- `仅返回资产信息`：不自动返回 `ImageContent`，速度最快；确实需要观察画面时再调用 `image_studio_view_asset`。
- `完整原图`：保持原行为，适合必须进行像素级检查的任务，但多模态请求可能明显变慢。

`image_studio_view_asset` 默认只加载轻量预览，也可显式请求完整原图。`asset_id` 必须拥有当前会话的有效租约，不能跨会话读取。查看、发送或复制资产会续期软过期时间，但不能突破最长 7 天的硬期限。由于 AstrBot Core 会在同一 Agent 流程中持续携带已返回的 `ImageContent`，轻量预览仍可能被重复发送，但请求体会比完整原图小得多。Core 自动生成的 `data/temp/tool_images` 路径只是临时视觉缓存，不是稳定的 Image Studio 资产引用。

`image_studio_send_output` 只操作当前会话，`destination=session` 可按顺序投递中途文字、Image Studio 图片资产、workspace 媒体或 HTTP(S) 媒体；`destination=workspace` 只把 Image Studio `asset_id` 复制到当前 local/sandbox workspace，并返回后续 Python、Shell 或其他工具可用的路径。它不接受目标会话或用户 ID。进入 Image Studio 工作流后，插件会尽力从当前请求隐藏 AstrBot 原生 `send_message_to_user`，避免两套发送说明冲突。

`image_studio_generate` 返回的是可继续处理的工作流资产，单次生图成功不代表整个用户任务已经完成。Agent 可以继续多次生图、改图、拼接或制作 GIF。`image_studio_send_output` 只负责投递产物或有意的中途通知，不会终止本轮 Agent；中途消息可以包含图片和文字，但已经发送的文字不应在后续重复。任务完成后应停止调用工具，直接输出一条非空的普通 assistant 文本回复；不能把最终正文塞进发送工具后以空响应结束。

## 历史与参考图

- 是否保留历史、最大记录数和最大图片容量均可在 WebUI 设置。
- 开启“记录调用来源身份”后，指令和 LLM 工具记录平台、群聊/私聊、群 ID、群名称快照、用户 ID 和用户昵称；WebUI 生图没有聊天身份，不记录这些字段。名称仅是生成时快照，筛选和匹配仍使用 ID。
- 生成图和保留的参考图按内容 SHA-256 共享同一份文件；相同图片被多次使用不会重复占用历史空间。
- 结果图会作为画廊记录保存；参考图只显示在对应生成记录的详情中，不会单独成为画廊卡片。
- 参考图可以在详情中单独删除，删除后不会影响结果图和请求参数。
- 即使历史参考图没有保留，仍可恢复已有参数并进入生图页面；界面会明确提示需要重新补充参考图。
- 画廊导出采用平铺 ZIP：每张图片与一个同名 JSON 成对保存，基础名称为 `YYYYMMDDHHMMSS_t2i|i2i_模型`；多图或重名时追加序号。
- 导出 JSON 不会包含 API 密钥、鉴权请求头或其他疑似凭据字段；ZIP 下载响应完成后会立即从插件数据目录删除。
- 插件启动时会执行一次存储维护，此后每小时检查数据库关系、租约、孤立原图/预览和暂存文件；设置页可以立即执行普通检查或重新计算全部原图 SHA-256 的深度检查。

## 数据位置

- AstrBot 插件配置：只保存 `enable_llm_tool`。
- Image Studio 配置：`data/plugin_data/astrbot_plugin_image_studio/studio_config.json`，包括 Provider 密钥、模型和历史策略。
- 历史数据库：`data/plugin_data/astrbot_plugin_image_studio/history.sqlite3`。
- 去重后的原图资源：`data/plugin_data/astrbot_plugin_image_studio/history/assets`。
- 画廊和 Agent 共用的内容寻址预览：`data/plugin_data/astrbot_plugin_image_studio/history/thumbnails`。
- sandbox 投递短期暂存：`data/plugin_data/astrbot_plugin_image_studio/delivery_staging`，发送后立即删除，每小时维护负责清理异常残留。
- 历史数据库中的来源身份字段：`context_type`、`platform_name`、`platform_id`、`group_id`、`group_name`、`user_id`、`user_name`。

## 开发验证

在仓库目录中使用 AstrBot 虚拟环境运行：

```bash
conda run -n astrbot python -m pytest -q tests
conda run -n astrbot ruff check .
conda run -n astrbot ruff format --check .
node --check pages/image-studio/app.js
```

真实生图还需要在 WebUI 中配置可用端点、模型和凭据，并对目标服务商进行连接测试。
