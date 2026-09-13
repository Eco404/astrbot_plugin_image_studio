# 开发与发布维护

面向维护者。使用说明见 [README](../README.md)，正式版本变化见 [CHANGELOG](../CHANGELOG.md)。

当前正式版本为插件 `1.2.0`，基于正式插件 `1.1.0` 和数据库 v2；本次发布没有数据库结构变化，继续使用正式 `user_version=2`，无需新的迁移标记。

## 1.2.0 正式基线

本次正式插件版本为 `1.2.0`，沿用图库数据库正式版本 **v2**。本次发布不改变数据库结构，不增加迁移步骤；开发期间的资产访问、临时保留和 WebUI 改动均在现有 v2 结构上完成。

| 标识 | 当前值 | 含义 |
| --- | --- | --- |
| 插件版本 | `1.2.0` | `metadata.yaml` 与 `main.py` 注册版本相同 |
| 数据库正式版本 | `2` | SQLite `PRAGMA user_version`，由 `database_schema.py` 管理 |
| 配置格式版本 | `2` | `studio_config.json` 的 `schema_version`，与数据库版本独立 |

## 1.1.0 正式基线（历史）

1.1.0 发布时的正式插件版本为 `1.1.0`，图库数据库版本为 **v2**。开发期的外部来源修订已收拢为一次 `v1 → v2` 发布迁移；新库直接创建最终结构，正式库不保留 `schema_meta` 开发标记。

| 标识 | 当前值 | 含义 |
| --- | --- | --- |
| 插件版本 | `1.1.0` | `metadata.yaml` 与 `main.py` 注册版本相同 |
| 数据库正式版本 | `2` | SQLite `PRAGMA user_version`，由 `database_schema.py` 管理 |
| 配置格式版本 | `2` | `studio_config.json` 的 `schema_version`，与数据库版本独立 |
| 图片解析器版本 | `image_metadata.PARSER_VERSION` | 控制派生元数据回填，与数据库结构版本独立 |

数据库结构和发布迁移集中在 `database_schema.py`。已发布的 v1 定义保持不变，v2 增加最终版本的 `external_sources`、`external_records` 及相关索引，不再先建早期开发表再逐列升级。

`storage.GenerationStore.initialize()` 调用 `ensure_release_schema()`。尺寸、生图来源和图片元数据的派生修复，以及租约和孤立文件维护，继续在结构迁移事务外执行，失败后可重试。

## 升级与开发库转换

| 当前数据库 | 1.1.0 的处理 |
| --- | --- |
| 空库 | 直接创建正式 v2，不备份空库 |
| 正式 v1（1.0.0） | 核验、备份，在一个事务中执行 `v1 → v2` |
| 正式 v2 | 只校验结构，不重复备份和迁移 |
| `user_version=1`、`schema_meta(2,2)` | 核验最终 `2-dev.2` 布局、备份，移除开发标记并转为 v2 |
| `user_version=0`、`schema_meta(1,3)` | 保留历史兼容：核验最终 `1-dev.3` 布局、备份，在一个事务中转换并升级为 v2 |
| `2-dev.1`、更早或未知开发修订、未来版本、结构异常 | 拒绝自动升级，不清空业务数据 |

表中的 `schema_meta(目标,修订)` 省略了固定主键 `id=1`。开发标记必须与实际的列、默认值、外键及索引结构一致，不能只改版本号。

插件版本与数据库版本不是一回事：`1.1.0-dev.1` 使用的正式 v1 数据库可以直接升级；较早 `1.1.0-dev.2` 代码产生的 `2-dev.1` 数据库，需要先由末版开发代码（如 `91d05dd`）升级到 `2-dev.2`。对于 `1-dev.1`、`1-dev.2` 或更早的无版本开发数据，可先在完整备份副本上使用末版 1.0 开发代码 `f68ecb3` 升级到 `1-dev.3`。无法识别时保留原始副本，不手动伪造版本标记。

迁移前通过 SQLite Backup API 创建一致性备份：

```text
data/plugin_data/astrbot_plugin_image_studio/backups/
  history-pre-v2-<UTC时间>-<唯一后缀>.sqlite3
```

备份失败不继续升级。迁移在 `BEGIN IMMEDIATE` 事务内再次核验数据库状态，成功后才写入正式版本号；中途失败整体回滚，备份保留。数据库备份不参与图片配额或临时文件清理。

自动备份只包含数据库，**不包含配置和图片文件**。升级前应停止 AstrBot，备份完整插件数据目录；不要只复制运行中的主 `.sqlite3` 文件而忽略 WAL 数据。

回退应同时恢复升级前代码与完整数据副本。不要让旧版插件打开已升级的 v2 库，也不要把自动备份覆盖到正在运行的数据库上；单独恢复数据库前必须核对原图、参考图和 WAL/SHM 状态。

## 外部图库维护

外部来源与本地记录共用分页、搜索和连续浏览。外部原图留在来源目录，缩略图由 Image Studio 管理；只有自有原图才进入本地资产清理流程。

- 扫描器位于 `external_gallery.py`，通过适配器枚举文件和读取附属参数。新增来源时扩展适配器，不调用来源插件的初始化或迁移逻辑。
- 默认每 5 分钟增量扫描。读取原图或预览发现丢失、变化时立即补扫；手动、定时及异常恢复共用同一调度，同一来源已有任务时复用任务。
- 只有完整枚举成功后才能清除已消失的索引。目录不可读、读取失败、取消和停用都不能当作空目录处理。
- 来源配置以稳定实例 ID 为键，记录类型、名称、路径、递归选项和权限；旧 `nai.enabled` 配置保留原 ID 转换。移除来源使用专用索引清除流程，不调用原图删除方法。
- 收藏、删除、下载／导出及参考图权限由后端核验，批量操作先检查整批权限。正常浏览原图不受下载按钮权限影响。
- 停用保留索引与收藏并回收不再使用的外部缩略图，再启用时重新核实文件。每小时存储维护另外检查 SQLite、租约及孤立文件。

外部时间依次选择 NAI 文件名时间戳、图片元数据创建时间、btime、mtime，自定义目录跳过文件名。排序时间写入 `generations.created_at`；只有 NAI 文件名或图片元数据时间用于 `generated_at`，btime/mtime 仅作排序依据。文件变化或时间规则版本更新时重新解析，不把普通扫描时间当作生成时间。

## 后续开发版本

1. 从 `1.2.0` 和数据库正式 v2 基线继续开发，不修改已发布结构的版本含义。
2. 普通 UI、指令或 Provider 修改可继续使用数据库 v2。只有结构变化时才启用下一个数据库目标版本。
3. 若下一次结构目标为 v3，开发库保留 `user_version=2`，另以 `schema_meta(target_version=3, dev_revision=1,2,...)` 标识 `3-dev.1` 等修订；结构和开发标记在同一事务内提交。
4. 插件开发版本使用 `1.2.1-dev.1` 等名称，与数据库版本独立。开发测试使用临时目录或独立数据副本，不与正式部署共用数据目录。
5. 正式版只接纳明确支持的正式基线和最终开发布局，拒绝其他未发布标记；不能通过“缺列就补”绕过结构校验。
6. 下次发布前将当期开发修订压缩为一次正式迁移，同时保留最终开发库到正式库的受控转换入口。
7. 已发布迁移不得删除。未来 v3 需保留 `v1 → v2 → v3` 的升级路径，并验证最终结构与直接创建 v3 一致。

## 测试与构建

使用 Python 3.12+ 的 AstrBot 环境。包含 AstrBot 导入的测试在临时工作目录执行，避免宿主初始化把运行文件写入仓库。以下示例从插件根目录开始，假定 AstrBot 源码位于同级 `AstrBot` 目录，环境名为 `astrbot`：

```bash
studio_repo_dir="$(pwd)"
studio_check_dir="$(mktemp -d)"
cd "$studio_check_dir"
PYTHONPATH="$studio_repo_dir/..:$studio_repo_dir/../AstrBot" conda run -n astrbot python -m pytest -q "$studio_repo_dir/tests"
conda run -n astrbot ruff check --select F,E9 "$studio_repo_dir"
conda run -n astrbot ruff format --check "$studio_repo_dir"
node --check "$studio_repo_dir/pages/image-studio/app.js"
node --check "$studio_repo_dir/pages/image-studio/library.js"
node --check "$studio_repo_dir/pages/image-studio/appearance.js"
node --check "$studio_repo_dir/pages/image-studio/viewer-backdrop.js"
git -C "$studio_repo_dir" diff --check
conda run -n astrbot python "$studio_repo_dir/scripts/build_plugin_package.py"
```

构建默认输出 `dist/astrbot_plugin_image_studio-v1.2.0.zip`，实际文件名跟随元数据版本；支持 `--root` 和 `--output`。构建读取当前工作区，不要求先提交，不执行数据库转换。

安装包只包含运行模块、WebUI、使用说明和指定的演示截图，不包含真实数据、日志、开发数据库、测试或维护文档。新增运行模块或 README 图片时，同步更新构建白名单和测试；第三方静态资源的许可证随包保留。

浏览器测试只连接 `tests/webui_harness.py` 创建的隔离服务，通过 `STUDIO_TEST_URL` 和 `STUDIO_PLAYWRIGHT` 指定地址与 Playwright。不要把含导入、删除或配置保存操作的测试对准真实部署。对手机合成、触摸或高刷的结论，应区分浏览器模拟与真实设备验证。

每次数据库发布至少验证：

- 空库初始化、重复启动，以及新库与升级库结构一致。
- 上一正式版完整数据升级，图片、收藏、引用、租约和导入关系保留。
- 受支持的最终开发库转换，外部来源、权限、时间字段和资产引用保留。
- 备份可读取和恢复；备份失败不写库，中途失败回滚结构和版本。
- 未知版本、开发标记与实际结构不符、备份后状态变化时拒绝迁移。
- 元数据回填失败可重试，不影响已提交的结构版本。
- 最终安装包的版本、简介、必需资源和第三方许可证一致。

## 发布检查

- 对齐 `metadata.yaml`、`main.py`、CHANGELOG 及自有前端资源缓存版本，保留第三方库本身的版本号。
- 验证最低 AstrBot 版本所需 API、插件导入、工具注册和页面 API。
- 发布前检查真实服务商的生图、图生图与产物投递；隔离测试不能替代上游接口验证。
- 检查截图、安装包和日志不含密钥或会话身份信息；第三方许可证不等于本插件许可证。
- 构建、提交、标签、推送和 GitHub Release 分别执行，按本次任务授权范围操作。
