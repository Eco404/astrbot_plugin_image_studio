# Image Studio 生图插件

`astrbot_plugin_image_studio` 为 AstrBot 提供统一的多服务商生图能力，支持指令调用、LLM 工具调用和 WebUI 测试。

插件内置四个 WebUI 板块：

- **生图**：先选择“文生图 / 图生图”，再从支持该模式的模型中选择一个，工作区会按模型参数 schema 动态生成。
- **画廊**：搜索和筛选历史记录，在详情中连续浏览当前生成及相邻记录的图片，复现生成配置，导出或批量删除记录。手机端点击成图会进入无边框全屏查看，可左右滑动切换、上下滑动退出、捏合或双击缩放；单击图片后可从右下角的圆形按钮下载当前原图。
- **导入**：选择或拖入多张图片，自动读取生成参数，每张图片可独立补充来源、模型和参数，确认后上传入库。
- **设置**：管理历史策略、Provider 并发、服务商、模型能力和 LLM 工具参数。

画廊分页以 24 条为基准，随实际列数补齐整行，最后一页保留真实剩余数量。分页、批量操作、设置保存和详情操作栏始终位于对应页面或浮窗底部；手机端使用紧凑图标按钮。设置存在修改时显示黄色未保存状态，切换页面保留草稿，保存失败不会清空修改。

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
/image_gen 重新绘制这张图片 --mode img2img --ref workspace/input.png
```

LLM 工具包括 `image_studio_get_capabilities`、`image_studio_generate`、`image_studio_view_asset` 和 `image_studio_send_output`。每次生成前都必须先查询能力。未指定模型或模型类型的常规请求首先使用 `query_type=default`；`mode` 可以省略，此时同时返回文生图和图生图默认模型，也可以指定 `text2img` 或 `img2img` 只获取对应默认。如果默认模型支持用户明确要求的模式、参考图数量和参数，就直接生成，不再查询全部模型。普通主体、画风、构图和文字描述可以通过提示词表达，不属于模型能力缺口。只有默认模型存在明确能力缺口或不可用、用户要求比较模型，或者指定了模型类型但不知道具体 `model_ref` 时，才使用 `query_type=all`，并尽量携带相同的 `mode`。明确指定模型时使用 `query_type=model` 和完整 `model_ref`。

查询结果会明确自然语言或 NAI tag 提示词格式，并只列出当前模型允许 LLM 使用的动态参数；一次查询授权只供对应模型和模式生成一次。查询 `all` 后可以直接使用选中的模型，不需要再次查询 `model`。

`image_studio_generate` 只公开 `prompt`、`mode`、`model_ref`、`parameters` 和 `references`。Provider 由完整 `model_ref` 确定；尺寸、数量和 `negative_prompt` 等模型参数只有在能力查询返回时才能放入 `parameters`。LLM 仍传入未暴露或模型不支持的字段时，插件会静默丢弃并使用模型或工具配置的默认值；已暴露字段仍按 schema 校验。

LLM 应主动选择 `mode=text2img` 或 `mode=img2img`。文生图不会因为消息中带有图片而自动切换模式；图生图应在 `references` 中使用 `{ "asset_id": "..." }` 引用 Image Studio 资产，或使用 `{ "path": "workspace/input.png" }` 引用当前 workspace 图片。只有图生图没有提供具体引用，或 LLM 连模式也没有填写时，后端才自动读取当前消息及引用消息图片作为容错。非空 `references` 只使用明确提供的内容，不与消息图片合并。指令调用继续自动读取当前消息及引用消息中的图片；没有显式指定模式时，检测到图片会自动使用图生图。

所有生成图都进入统一的内容寻址资产库。LLM 只会获得 `asset_id`、MIME 类型和原图大小，不会获得插件私有路径。设置页“图片资产”支持三种返回方式：

- `轻量预览`：默认方式，向 LLM 返回与画廊共用的 WebP 预览；可配置最大边长和质量。
- `仅返回资产信息`：不自动返回 `ImageContent`，速度最快；确实需要观察画面时再调用 `image_studio_view_asset`。
- `完整原图`：保持原行为，适合必须进行像素级检查的任务，但多模态请求可能明显变慢。

`image_studio_view_asset` 接受 1 至 8 个 `asset_id`，按输入顺序批量加载轻量预览或完整原图。任一资产不可用时不会返回部分图片，而会列出每个失败项的索引、资产 ID、具体原因和是否适合重试。`asset_id` 必须拥有当前会话的有效租约，不能跨会话读取。查看、发送或复制资产会续期软过期时间，但不能突破最长 7 天的硬期限。

AstrBot Core 自动生成的 `data/temp/tool_images` 路径只负责把临时视觉预览加入模型上下文，不是 Image Studio 资产引用。不得发送、复制、编辑、用作参考图或传给其他工具。插件内继续处理使用 `asset_id`；交给 Python、Shell 或其他插件前，先通过 `image_studio_send_output(destination=workspace)` 取得 workspace 路径。原图资产保存失败时，生图工具会明确报错，不会退回只能依赖临时缓存路径的结果。

`image_studio_send_output` 只操作当前会话，`destination=session` 可按顺序投递中途文字、Image Studio 图片资产、workspace 媒体或 HTTP(S) 媒体；`destination=workspace` 只把 Image Studio `asset_id` 复制到当前 local/sandbox workspace，并返回后续 Python、Shell 或其他工具可用的路径。它不接受目标会话或用户 ID。能力查询后，插件会从当前请求隐藏 AstrBot 原生 `send_message_to_user`；首次成功保存 Image Studio 原图资产后，还会隐藏可能存在的 `pc_send_current_media`。这些操作只影响当前 Agent 请求，下一轮消息会重新构建工具集，且未安装 Private Companion 时不会产生依赖或错误。

`image_studio_generate` 返回的是可继续处理的工作流资产，单次生图成功不代表整个用户任务已经完成。Agent 可以继续多次生图、改图、拼接或制作 GIF。`image_studio_send_output` 只负责投递产物或有意的中途通知，不会终止本轮 Agent；中途消息可以包含图片和文字，但已经发送的文字不应在后续重复。任务完成后应停止调用工具，直接输出一条非空的普通 assistant 文本回复；不能把最终正文塞进发送工具后以空响应结束。

## 历史与参考图

- 是否自动保留历史、最大记录数和最大图片容量均可在 WebUI 设置。数量按生成记录计数，一次生成多张图片仍计为一条。
- 手动导入和收藏的记录不计入自动历史的数量、容量限制，不参与超限自动清理；导入不受“保留生成历史”开关影响。
- 收藏作用于整次生成，包括全部成图、参数和仍保留的参考图。取消收藏后重新计入配额，并获得持久化的 24 小时自动清理保护；保护期不阻止手动删除。导入记录取消收藏后仍然豁免。
- 超限时按时间清理最早的符合条件记录；没有可清理记录时允许暂时超额。受限制占用按资产去重，排除导入/收藏共同持有的文件；总物理占用与历史配额分别显示。
- 任一有效配额达到 90% 时，画廊为全局清理队列中最前面的最多 10 条记录显示低饱和黄色提示。它表示候选顺序，不保证全部删除，也不延后清理。
- 开启“记录调用来源身份”后，指令和 LLM 工具记录平台、群聊/私聊、群 ID、群名称快照、用户 ID 和用户昵称；WebUI 生图没有聊天身份，不记录这些字段。名称仅是生成时快照，筛选和匹配仍使用 ID。
- 生成图和保留的参考图按内容 SHA-256 共享同一份文件；相同图片被多次使用不会重复占用历史空间。
- 结果图会作为画廊记录保存；参考图只显示在对应生成记录的详情中，不会单独成为画廊卡片。
- 参考图可以在详情中单独删除，删除后不会影响结果图和请求参数。
- 详情底部可删除指定成图：多图记录先显示默认全选的缩略图选择弹窗，确认后删除所选；单图记录直接二次确认。部分删除保留原请求和收藏，全部删除才移除整条记录。共享引用或有效 Agent 租约仍然存在时，物理文件继续保留。
- 即使历史参考图没有保留，仍可恢复已有参数并进入生图页面；界面会明确提示需要重新补充参考图。
- 画廊导出采用平铺 ZIP：每张图片与一个同名 JSON 成对保存，基础名称为 `YYYYMMDDHHMMSS_t2i|i2i_模型`；多图或重名时追加序号。
- 插件请求快照不保存 API 密钥和鉴权请求头；导入图片自带的原始元数据及工作流保持原样。ZIP 下载响应完成后会立即从插件数据目录删除。
- 插件启动时会执行一次存储维护，此后每小时检查数据库关系、租约、孤立原图/预览和暂存文件；设置页可以立即执行普通检查或重新计算全部原图 SHA-256 的深度检查。
- 原图缺失或校验异常时保留记录和元数据，标记文件不可用；可用原图的缩略图缺失时重新生成。

## 图片元数据与参数交换

支持 PNG、JPEG、WebP 等图片中的常见生成元数据：

- **NovelAI**：保留完整 Comment JSON，包括实际 seed、多角色参数和当前接口没有开放的字段。请求快照保留 NAI 第三方接口的画师串、原始提示词、中文尺寸等输入，避免混同于图片中的最终参数。
- **ComfyUI**：保留 `workflow` 界面工作流与 `prompt` API 执行图的原始 JSON 文本。沿输出节点解析已知节点的底模、有效 LoRA、正反向提示词及各采样阶段；动态或自定义节点无法确定时提示。复制与下载工作流直接在浏览器处理，不产生服务器下载缓存。
- **Stable Diffusion**：解析 A1111/Forge 的参数文本，包括 WebP/JPEG EXIF UserComment，同时保留原始文本与 JSON 化结果。
- **未知图片**：允许没有任何生成参数，支持手动填写来源与参数；不把文件尺寸、导入时间推断成原始生成尺寸或生成时间。

导入确认前，图片只在浏览器中预览与提取元数据，解析接口接收文本元数据；确认后逐张上传原图，后台重新解析并保存人工补充。每批最多 100 张，单图不超过 30 MB；失败项目保留重试，同一文件重复导入默认跳过。确认上传不是生图操作，也不自动关联到某个已配置的服务商。

详情中可逐字段复制，也可复制 Image Studio 参数、NAI 参数、NovelAI 原始元数据、ComfyUI 工作流或 SD 参数。原始元数据、人工补充、请求参数分别保留。请求尺寸与图片实际尺寸允许不同；元数据存在不代表接口能够精确复现。

生图页可读取剪贴板并回填；AstrBot 的沙盒页面或浏览器不允许读取时，提供手动粘贴。模型唯一匹配时自动选择，否则先选目标模型；仅填写该模型 schema 接受的字段，未支持、越界或大于网页安全整数范围的数值保留并提示，不静默钳位，也不自动提交生成。参数文本不携带参考图片，图生图仍需补充参考图。

本次只支持 ComfyUI 和 Stable Diffusion 图片归档及参数交换，不包含这两类工作流的执行器。模型参数 schema 继续控制生成输入，图片元数据使用独立解析器，不要求把全部工作流字段加入模型配置。

## 数据库版本

当前数据库结构为 `1-dev.1`。`PRAGMA user_version` 记录最近的正式基线（当前为 0），`schema_meta` 记录开发目标 1 和开发修订 1。配置文件版本、数据库版本、元数据解析器版本独立管理。

开发期间可以增加临时修订；发布前将当期修订合并成一次正式迁移。首次发布建立正式 v1 建表基线，以后保留已发布版本之间的升级路径。未知正式版本或开发修订会拒绝启动，不能仅改标记跳过迁移。结构与版本标记在同一事务提交，图片元数据回填在事务外执行并可重试。升级前应备份数据库及资产目录；先在备份副本验证，再切换部署。

## 数据位置

- AstrBot 插件配置：只保存 `enable_llm_tool`。
- Image Studio 配置：`data/plugin_data/astrbot_plugin_image_studio/studio_config.json`，包括 Provider 密钥、模型和历史策略。
- 历史数据库：`data/plugin_data/astrbot_plugin_image_studio/history.sqlite3`。
- 去重后的原图资源：`data/plugin_data/astrbot_plugin_image_studio/history/assets`。
- 画廊和 Agent 共用的内容寻址预览：`data/plugin_data/astrbot_plugin_image_studio/history/thumbnails`。
- sandbox 投递短期暂存：`data/plugin_data/astrbot_plugin_image_studio/delivery_staging`，发送后立即删除，每小时维护负责清理异常残留。
- 历史数据库中的来源身份字段：`context_type`、`platform_name`、`platform_id`、`group_id`、`group_name`、`user_id`、`user_name`。
- `generations` 保存请求、收藏和人工补充；`image_metadata` 按共享资产保存原始元数据、解析格式和结果。

## 开发验证

在仓库目录中使用 AstrBot 虚拟环境运行：

```bash
conda run -n astrbot python -m pytest -q tests
conda run -n astrbot ruff check .
conda run -n astrbot ruff format --check .
node --check pages/image-studio/app.js
node --check pages/image-studio/library.js
```

真实生图还需要在 WebUI 中配置可用端点、模型和凭据，并对目标服务商进行连接测试。

隔离 WebUI 验证可运行 `python tests/webui_harness.py --port 18765`，它创建临时数据库和合成图片，使用实际插件 API，但不会调用真实生图服务。安装 Playwright 后执行 `node tests/webui_browser.cjs`；也可用 `STUDIO_PLAYWRIGHT` 指定现有 Playwright 包路径。浏览器脚本包含删除操作，仅应对这个隔离服务运行。
