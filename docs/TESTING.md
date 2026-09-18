# 开发验证

在插件仓库根目录运行 `python scripts/verify.py`，默认依次检查 Python 格式与静态错误、JavaScript 语法、Git 空白错误、全部 Python 测试，最后构建并校验安装包。浏览器测试按需指定，不会默认运行全部历史场景。

验证脚本始终使用启动它的 Python 解释器。请先激活包含 AstrBot 依赖的虚拟环境；脚本不会自动安装依赖、启动真实生图请求或提交 Git。

## 准备环境

需要 Python 3.12+（同时满足所用 AstrBot 版本要求）、Git，以及 Node.js 20+。AstrBot 可以已安装在当前虚拟环境，也可以使用源码目录：

```bash
# 当前目录为插件仓库，AstrBot 源码为同级目录时无需设置此变量。
export ASTRBOT_ROOT="/path/to/AstrBot"

# 使用已激活的虚拟环境。
python -m pip install -r "$ASTRBOT_ROOT/requirements.txt"
python -m pip install -r requirements.txt pytest ruff fastapi uvicorn httpx
```

也可以在调用验证脚本时使用 `--astrbot-root /path/to/AstrBot`。默认查找插件同级的 `AstrBot` 目录；现有 `PYTHONPATH` 会保留。

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

# 可同时执行后端、选定的浏览器场景和打包
python scripts/verify.py --backend --webui comfy_workspace --browser chromium --package
```

`--webui` 接受去掉 `webui_` 和 `.cjs` 的场景名，也接受完整文件名。多次传入同一场景只执行一次。

`--list-webui` 中的浏览器标记：

| 标记 | 行为 |
| --- | --- |
| `selectable` | 支持 `--browser chromium` 或 `--browser webkit`；未指定则保留该脚本原有浏览器矩阵 |
| `chromium` | 既有脚本固定使用 Chromium |
| `native-matrix` | 既有脚本内含固定的多浏览器矩阵，请省略 `--browser`；脚本会拒绝无法兑现的浏览器覆盖 |
| `node` | 无需浏览器的 JavaScript 测试，例如哈希逻辑 |

浏览器覆盖通过现有脚本的 `STUDIO_BROWSER`、`STUDIO_BROWSERS` 或 `STUDIO_ENGINES` 传递，不会把 Chromium 运行伪装成 WebKit。窗口尺寸与明暗主题仍由每个场景自行定义。部分较早场景可能需要本地样例图片或包含已经过时的界面断言；它们不会因此被算作通过，失败时应修正场景或检查产品行为。

## 隔离、超时和结果

每个浏览器场景都启动自己的 `tests/support/webui_harness.py`，使用系统临时目录和自动分配的 `127.0.0.1` 端口；不复用运行中的部署服务。调用方已有的 `STUDIO_TEST_URL` 会被忽略。测试服务使用假图片提供方，场景中的远程接口采用固定结果或拦截，不需要真实 API Key。

验证结束、检查失败、超时或按 `Ctrl+C` 都会终止本次启动的测试服务及子进程，并清理测试服务的数据目录。截图、测试生成的样例及服务日志保存在 `dist/verification/` 的独立子目录中，可用 `--artifacts-dir` 指定其他位置；这些文件不会进入安装包。

每项检查或浏览器场景默认最多 600 秒，服务准备最多 60 秒。较慢环境可以显式调整：

```bash
python scripts/verify.py --webui comfy_provider --browser webkit --timeout 900 --startup-timeout 90
```

静态检查只覆盖 Git 识别到的已跟踪及未忽略源码，跳过第三方 `vendor` 文件，执行 `ruff check --select F,E9`、`ruff format --check`、`node --check`，以及暂存/未暂存改动的 `git diff --check`。不会自动格式化或修复文件。

如只需单项 Python 回归，仍可以直接运行：

```bash
studio_repo="$PWD"
studio_test_workdir="$(mktemp -d)"
(
  cd "$studio_test_workdir"
  PYTHONPATH="$(dirname "$studio_repo"):${ASTRBOT_ROOT:-$(dirname "$studio_repo")/AstrBot}" \
    python -m pytest -q "$studio_repo/tests/backend/test_capability_search_tool.py"
)
```

退出码 `0` 表示所选检查全部通过；`1` 表示依赖、检查或超时失败；`130` 表示用户中断。报告验证结果时，应注明所选场景和浏览器，不能将模拟提供方的通过等同于真实服务商生图验证。
