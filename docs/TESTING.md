# 开发验证

在插件仓库根目录运行 `python scripts/verify.py`，默认依次检查 Python 格式与静态错误、JavaScript 语法、Git 空白错误、全部 Python 测试，最后构建并校验安装包。浏览器测试按需指定，不会默认运行全部历史场景。

验证脚本始终使用启动它的 Python 解释器。请先激活包含 AstrBot 依赖的虚拟环境；脚本不会自动安装依赖、启动真实生图请求或提交 Git。

## 准备环境

需要 Python 3.12+（同时满足所用 AstrBot 版本要求）、Git，以及 Node.js 20+。AstrBot 可以已安装在当前虚拟环境，也可以使用源码目录：

```bash
# 当前目录为插件仓库。
ASTRBOT_SOURCE="/path/to/AstrBot"

# 使用已激活的虚拟环境。
python -m pip install -r "$ASTRBOT_SOURCE/requirements.txt"
python -m pip install -r requirements.txt pytest ruff fastapi uvicorn httpx
```

调用验证脚本时使用 `--astrbot-root /path/to/AstrBot` 指定源码。默认查找插件同级的 `AstrBot` 目录；现有 `PYTHONPATH` 会保留。`ASTRBOT_ROOT` 是宿主的可写运行目录，不用于指定源码；验证脚本忽略继承值，替换为本次独立临时目录，防止宿主导入时迁移现有配置。单独运行 pytest 或宿主导入脚本时，也应在独立临时目录中执行并显式设置临时 `ASTRBOT_ROOT`。

浏览器依赖通过仓库内的锁文件安装：

```bash
npm ci
npx playwright install chromium webkit
```

Playwright 固定为 `1.62.0`，对应 Chromium revision `1234` 和通常使用的 WebKit revision `2336`（部分操作系统使用其官方覆盖版本）。Linux 缺少浏览器系统依赖时，可自行运行 `npx playwright install --with-deps chromium webkit`。该命令可能需要系统包管理权限，验证脚本不会替你执行。

`node_modules` 仅供开发测试，不进入插件安装包；插件运行不依赖 Node.js 或 Playwright。

## 常用命令

```bash
# 默认检查：静态检查 + 全部 Python 测试 + 构建安装包
python scripts/verify.py

# 只运行指定类别；多个标志可以组合
python scripts/verify.py --backend
python scripts/verify.py --static --package

# 列出浏览器测试场景
python scripts/verify.py --list-webui

# 各场景均启动独立测试服务和数据目录
python scripts/verify.py --webui comfy_workspace comfy_provider --browser chromium
python scripts/verify.py --webui comfy_gallery_run --browser webkit

# 触屏显示图、实际双击缩放、迟到响应和资源回收
python scripts/verify.py --webui mobile_display media_objects

# ComfyUI 显示快照批量匹配：上游变化、下游约束、导入与图组编辑
python scripts/verify.py --webui import_snapshot_matching

# 有证据的显示快照自动识别、手动内容保护、详情与编辑器来源
python scripts/verify.py --webui display_snapshots

# 手动文本节点绑定、预览失效、规则保存删除与手工字段保护
python scripts/verify.py --webui node_rules

# 图组标题内联编辑、取消、搜索和刷新持久化，以及卡片信息显示设置
python scripts/verify.py --webui gallery_titles gallery_card_info

# 可同时执行后端、选定的浏览器场景和打包
python scripts/verify.py --backend --webui comfy_workspace --browser chromium --package
```

`--webui` 接受去掉 `webui_` 和 `.cjs` 的场景名，也接受完整文件名。多次传入同一场景只执行一次。

场景发现递归扫描 `tests/webui/`，子目录中仍使用同样的短名称；重名场景会报错，避免悄悄漏跑。共享路径集中在 `tests/support/webui_paths.cjs`，新增场景应使用该辅助模块而不是假定固定目录深度。

`--list-webui` 中的浏览器标记：

| 标记 | 行为 |
| --- | --- |
| `selectable` | 支持 `--browser chromium` 或 `--browser webkit`；未指定则保留该脚本原有浏览器矩阵 |
| `chromium` | 既有脚本固定使用 Chromium |
| `native-matrix` | 既有脚本内含固定的多浏览器矩阵，请省略 `--browser`；脚本会拒绝无法兑现的浏览器覆盖 |
| `node` | 无需浏览器的 JavaScript 测试，例如哈希逻辑 |

浏览器覆盖通过现有脚本的 `STUDIO_BROWSER`、`STUDIO_BROWSERS` 或 `STUDIO_ENGINES` 传递，不会把 Chromium 运行伪装成 WebKit。窗口尺寸与明暗主题仍由每个场景自行定义。较早场景的界面断言需要随产品行为维护，失败不能直接视为通过。

图片元数据回归使用 `tests/fixtures/image_metadata.py` 生成最小合成样例，经 `tests/support/webui_fixtures.cjs` 提供给浏览器；NovelAI、ComfyUI、A1111 与 WebP 导入分支不再因本地 `data/image/` 缺少真实图片而跳过。私人图库样本仅用于另行授权的额外验证，不进入仓库或安装包。后端复用 ComfyUI 运行环境和图库图片生成器分别位于 `tests/support/comfy_runtime.py`、`gallery_images.py`，无需从其他测试文件导入这些公共夹具。

## 隔离、超时和结果

对话工具回归覆盖工具定义中的模型选择与调用前提、同轮能力复用、模式参数/中文字段/数值约束、ComfyUI 持久任务与会话鉴权、任务恢复和显式幂等、结果来源说明、资产登记重试及复制部分成功。人格提示词和其他插件上下文不应被追加或改写。ComfyUI 使用假客户端暂停和释放执行、注入错误，并检查实际提交次数；这不代表真实服务商生图或真实 LLM 选型已经验证。

1.4.2 的投递入口回归应检查：请求准备钩子仅复制工具集合，保留所有名称、不注入系统提示词；仅本轮首次成功取得已登记生成资产后，且本请求具备 `image_studio_send_output` 时，隐藏本请求的 `send_message_to_user` 和 `pc_send_current_media`。普通模式和 `skills_like` 模式的可见与原始执行集合应一致受限，其他工具及全局工具定义保持原样。需覆盖通过任务查询取得结果、部分成功、资产登记失败后重取、后续失败不解除，以及下条消息恢复正常列表。能力查询、搜索、查看旧图、失败、pending 和缺少专用发送工具时均不得触发。发送说明应限定在本工具中；session 成功按有无文字给出避免重复的指引，workspace 和不确定投递不应声称已发送。这些检查验证状态边界与返回内容，不证明模型永不重复发送，也不构成按内容去重。

工具附图包装的回归入口为 `tests/backend/integration/test_agent_image_context.py`。使用隔离的宿主 runner 和假提供方，检查生成结果与已有图片查看的来源说明、同正文的重复调用、多张图片、混合其他工具及不支持图片输入的模型。须验证修改发生在下一次请求和压缩前，原消息及图片对象、`user` 角色和图片数据保持不变，旧历史与其他工具不受影响。结构不符、图片路径或调用编号不符、截断结果、取消及异常应保持原内容；还需检查重复安装、卸载、其他插件外层包装和不支持的宿主版本或签名。

查看 LLM trace 时，应检查工具执行后下一次模型请求的消息列表（Provider 接口参数为 `contexts`）。AstrBot 的工具日志及中间 `tool_call_result` 事件在包装修正前产生，仍显示旧说明不等于模型收到旧说明。自动化回归使用模拟结果，不调用真实 LLM、生图或发送服务；实机证据单独记录如下。

### 1.4.2 实机验证与限制

2026-09-20 在 AstrBot 4.27.4、`full` 普通工具模式下，使用真实私聊的配置、人格、历史及其他插件上下文完成一次文生图和 QQ 投递测试。测试请求经事件队列注入，使用真实 LLM 与默认生图服务；QQ 投递通过平台适配器的私聊主动发送接口完成，不覆盖 QQ 入站解析及被动回复的消息编号机制。

逐次采集实际 Provider 请求确认：

- 生成前、未查询能力而被拒绝后、完成能力查询后，两个冲突发送工具均保持可用。
- 唯一一次真实生成成功后，可用工具从 24 项减至 22 项，移除 `send_message_to_user` 与 `pc_send_current_media`。
- 下一次模型请求中的图片旁带有“由你调用生图工具生成”的说明，当前结果中的宿主原生发送建议已移除。
- 模型使用 `image_studio_send_output` 和 `asset_id` 发送原图，随后单独输出最终正文；图片与文字各投递一次，平台调用均正常返回，未出现重复发送。

本次只生成一张图片。模型最初跳过能力查询，由校验拦下并补查，没有额外调用生图服务。测试后恢复临时采集包装并移除辅助插件及其接口；私人会话、原始 trace 与测试辅助代码不进入仓库或发布包。自动化验证共 2496 项通过，静态检查与安装包构建通过。

上述实机样本不能保证模型以后始终正确理解图片来源，也不等同于客户端送达或已读回执。`skills_like` 两阶段模式已通过独立工具集合的隔离回归，但其他插件在请求钩子之后再次浅复制该集合时，宿主的原始执行副本可能未被过滤，旧工具调用仍可能执行；此组合尚未修复，不属于本次实机验证范围。旧会话中的提示词不自动清理。

每个浏览器场景都启动自己的 `tests/support/webui_harness.py`，使用系统临时目录和自动分配的 `127.0.0.1` 端口；不复用运行中的部署服务。调用方已有的 `STUDIO_TEST_URL` 会被忽略。测试服务使用假图片提供方，场景中的远程接口采用固定结果或拦截，不需要真实 API Key。

验证结束、检查失败、超时或按 `Ctrl+C` 都会终止本次启动的测试服务及子进程，并清理测试服务的数据目录。截图、测试生成的样例及服务日志保存在 `dist/verification/` 的独立子目录中，可用 `--artifacts-dir` 指定其他位置；这些文件不会进入安装包。

每项检查或浏览器场景默认最多 600 秒，服务准备最多 60 秒。较慢环境可以显式调整：

```bash
python scripts/verify.py --webui comfy_provider --browser webkit --timeout 900 --startup-timeout 90
```

静态检查只覆盖 Git 识别到的已跟踪及未忽略源码，跳过第三方 `vendor` 文件，执行 `ruff check --select F,E9`、`ruff format --check`、`node --check`，以及暂存/未暂存改动的 `git diff --check`。不会自动格式化或修复文件。

如只需单项 Python 回归，仍可以直接运行：

后端测试按 `comfyui/`、`gallery/`、`providers/`、`storage/`、`config/`、`integration/`、`tooling/` 分组；测试文件名和参数化用例保持一致。Python 共享路径由 `tests/support/paths.py` 提供，移动测试不改变插件根目录定位。

```bash
studio_repo="$PWD"
studio_source_root="${ASTRBOT_SOURCE:-$(dirname "$studio_repo")/AstrBot}"
studio_test_workdir="$(mktemp -d)"
(
  cd "$studio_test_workdir"
  ASTRBOT_ROOT="$studio_test_workdir/astrbot-runtime" \
    PYTHONPATH="$(dirname "$studio_repo"):$studio_source_root${PYTHONPATH:+:$PYTHONPATH}" \
    python -m pytest -q "$studio_repo/tests/backend/integration/test_capability_search_tool.py"
)
```

退出码 `0` 表示所选检查全部通过；`1` 表示依赖、检查或超时失败；`130` 表示用户中断。报告验证结果时，应注明所选场景和浏览器，不能将模拟提供方的通过等同于真实服务商生图验证。
