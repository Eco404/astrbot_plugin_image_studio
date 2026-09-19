# 目录与职责

后端按职责归入 `backend/`，插件入口委托页面接口、指令编排和能力查询组装。数据库、配置格式、外部 API 地址和工具名称保持原有约定。内部 Python 导入路径已迁移，不保留根目录兼容副本。

```text
main.py                         AstrBot 注册、生命周期、入口及事件适配
backend/
  config.py                     配置读取、规范化、持久化
  models.py                     请求/结果结构、模型配置与参数策略
  api/
    routes.py                   WebUI 路由目录
    comfyui.py                  工作流导入、检查、任务查询与提交接口
    settings.py                 配置读取、保存、回滚及工作流配置写入
    generation.py               生图、参考图上传、服务商发现与额度
    gallery.py                  图库浏览、收藏、删除、导出与外部状态
    imports.py                  导入预检、上传票据、提交与编辑
    preferences.py              浏览器主题与图库偏好 Cookie
  commands/
    parser.py                   无宿主依赖的指令参数解析
    handler.py                  指令编排，显式注入事件图片解析等依赖
  tools/
    capability_catalog.py       服务商/模型发现、批量选择
    capabilities.py             完整能力载荷组装
  generation/service.py         生图校验、批次、并发、结果登记与复现
  generation/concurrency.py     服务商、模型与官方账号共享限流
  generation/comfyui_runtime.py  ComfyUI 与生成服务、图库、可恢复任务的协调
  comfyui/
    catalog.py                  节点事实、解析/执行指纹及语义目录组装
    rules/node_adapters.json    控件布局、种子契约与来源依据
    rules/prompt_analysis.json  提示词流向、条件处理与成图节点语义
  providers/
    executor.py                 服务商调度和长期客户端实例
    errors.py / http.py         共用错误、HTTP 校验及图片响应读取
    discovery.py                模型发现与能力提取
    openai_images.py / gemini.py OpenAI / Gemini 图片协议
    nai_direct.py / custom_json.py NAI 第三方及自定义请求协议
    comfyui/
      client.py                 ComfyUI HTTP/WebSocket 协议
      workflows.py              执行图规范化、参数绑定、执行计划
      job_store.py              任务/修订事务、引用、归档及清理决策
      job_manager.py            异步任务生命周期、恢复与关闭
      job_files.py              文件校验、共享图片写入及旧缓存操作
      job_types.py              任务状态、期限和快照校验
      storage.py                当前任务正文编码与还原
      imports.py                图片/JSON 导入结果适配
      ui_sync.py                仅同步明确写入的界面控件，兼容位置与具名值
      global_seed.py            已知全局种子提交钩子的映射与回写核验
      output_metadata.py        运行结果回写核验及 PNG 工作流元数据修正
    novelai/
      client.py                 官方生成/额度请求、Vibe 缓存与编码锁
      protocol.py               官方接口请求/响应处理
      catalog.py                模型能力目录
      inputs.py                 高级输入处理
      inpaint.py                蒙版与重绘合成
  gallery/
    store.py                    统一异步入口、修改锁、仓储组装与缓存修订
    context.py                  共用路径、数据库连接策略及外部异常回调
    imports.py                  导入、编辑、合并和批次收据事务
    queries.py                  图库查询、详情与媒体读取
    records.py                  生成记录、收藏、删除及导出事务
    assets.py                   资产、引用和临时保护
    external_records.py         外部图库索引与权限校验
    metadata_records.py         元数据缓存及回填
    storage.py                  当前图库正文编码与轻量投影
    maintenance.py              配额、清理和健康维护
    projection.py               参数投影和检索字段转换
    errors.py / constants.py    存储错误类型和共享规则
    external.py                 外部图库扫描及来源适配
    timestamps.py               外部图片的时间判定
  metadata/
    parser.py                   元数据解析的公共入口
    common.py / readers.py      共用解码、时间识别和图片字段读取
    novelai.py                  NovelAI 字段及隐写数据
    stable_diffusion.py         SD 参数文本
    comfyui/
      parser.py                 工作流遍历与生成参数提取
      graph.py                  从界面图读取已知控件与连线
      candidates.py / evidence.py 提示词候选、显示快照及证据核验
      user_rules.py             手动文本流声明、约束与规则指纹
  parameters/exchange.py        参数复制、导入与复现草稿映射
  media/                        图片格式/编码/缩略图/文件名及文件辅助函数
  database/schema.py            数据库正式/开发版本和迁移
  database/payloads.py          共享压缩正文、owner 引用与垃圾回收
  database/maintenance.py       完整磁盘统计、备份轮换与受控空间回收
  database/migrations/          固定版本正文转换及可续跑的图片布局迁移
    images_layout.py           旧 history 图片目录转为 images，协调文件与路径事务
  ui/                           浏览器主题、图库偏好的传输格式
pages/image-studio/
  app.js                       页面启动、生图与图库/详情协调
  components/                  通用展示、平衡布局算法与弹窗生命周期
  settings/controller.js       配置草稿、参数行为编辑、设置布局及保存流程
  settings/external-sources.js 外部来源配置
  gallery/controller.js       图库操作和详情参数展示
  gallery/imports.js           导入队列、图组编辑和快照缓存
  gallery/metadata*.js         浏览器图片元数据读取和参数展示
  comfyui/controls.js / .css   工作流配置与编辑
  comfyui/node-rules.js / .css 手动文本节点绑定、预览与规则管理
  novelai/controls.js / .css   官方参考图、角色与重绘输入
  vendor/                      随包附许可证的第三方静态资源
tests/
  backend/
    comfyui/                   工作流解析、执行与任务恢复
    gallery/                   图库、导入与外部来源
    providers/                 Provider 协议与高级输入
    storage/                   数据库、正文与存储维护
    config/                    配置、参数行为与默认值
    integration/               指令、工具及跨模块生成流程
    tooling/                   安装包与验证入口
  webui/                       浏览器场景
  support/                     隔离服务和数据库升级样例
  fixtures/                    可重复生成的最小图片元数据样例
scripts/                       验证入口和发布包构建
```

## 入口与依赖

`main.py` 保留 AstrBot 的插件类、装饰器和注册签名。能力工具调用 `query_capabilities(settings, ...)` 获取载荷，再由入口维护本次事件的能力查询状态。搜索摘要不会触发完整能力授权。纯载荷模块不导入插件入口或 AstrBot 事件。

各 API 控制器通过构造参数接收配置读取函数、存储、服务获取函数等明确依赖。每次请求读取当前设置，避免保存配置后仍使用旧快照；不通过继承或全局状态访问插件实例。`SettingsAPI` 使用原有设置锁完成持久化与回滚，成功后调用入口的配置应用函数更新运行服务。`ImportsAPI` 管理待上传项目与批次锁，`GalleryAPI` 管理导出下载票据，插件维护与关闭流程显式调用它们。

`routes.py` 仅集中登记现有路径、方法、处理函数和描述。处理函数继续经过插件入口的薄包装；不改变宿主提供的认证、上下文绑定或返回格式。

`backend/` 子包的 `__init__.py` 保持轻量。内部调用使用明确的相对导入；测试引用相应的新模块，避免根目录转发层和模块别名掩盖循环依赖。

`backend/comfyui/` 的声明不依赖 Provider 执行或图库解析器。两份内置规则集中在该目录，显示节点的输入与缓存字段只在 `node_adapters.json` 声明，解析语义引用它。读取元数据使用明确的读取字段，写回界面使用经过验证的布局和约束，用户文本规则不获得控件写入或种子执行权限。解析指纹只覆盖实际读取的规则，种子执行约束与源码说明等无关变动不会使解析缓存失效。

Provider 调度通过显式适配函数和长期客户端组合；共用 HTTP/图片读取与专属协议分开。NovelAI Vibe 缓存、失败缓存和并发锁随客户端存活，不在每次请求重建。ComfyUI 文件组件不打开数据库、也不决定资源是否过期；任务仓储持有事务和引用决策，异步任务管理器只负责任务生命周期。

图库通过通用删除后回调通知生成协调层核对已发布结果，回调在图库提交及释放锁后执行。任务仓储自行判断恢复依赖和共享文件引用；回调失败由统一维护重试，图库不依赖 ComfyUI Provider 实现。

## 页面状态

前端保留原生 JavaScript 模块工厂与既有加载顺序。通用弹窗拥有自己的打开/关闭与退出动画状态；导入模块拥有上传队列、编辑草稿和受容量限制的快照缓存；设置模块拥有配置草稿、已保存基线和保存/重读队列。它们通过明确回调访问导航或当前选中图片，不持有整个应用状态。

本轮仍加载 29 个脚本和 12 份样式。参数行为编辑及设置布局由设置模块负责；详情与设置共享平衡布局算法，各自管理观察器与调度。图片缓存和手势交接的状态归属保持不变。

自有静态资源使用 `1.4.0-dev.1-import-spacing` 缓存标识，避免升级后旧脚本与新目录混用；插件版本仍为 `1.4.0-dev.1`。

图库列表、详情导航和图片缓存仍由 `app.js` 协调，既有手势、模糊背景与媒体复用组件沿用原边界。此处保留紧密相关的状态以避免跨组件转交期间重置缓存或丢失手势。后续可依据实际新增功能继续提取，不以文件行数作为唯一拆分目标。

1.3.1 的画廊使用 `gallery/list?light=1` 获取不含图片字节的列表，立即展示卡片占位，再通过既有单图接口加载预览；未指定 `light` 的调用保持兼容。列表请求使用修订号防止迟到响应覆盖当前页，图片加载使用独立页面会话和最多四个并发请求，优先处理可见及邻近卡片。预览复用详情页的图片身份、缩略图版本缓存及请求去重；同页刷新保留 DOM，过期页面只停止待执行图片任务，已发出的 bridge 请求不再更新旧卡片。

1.3.2 的触屏主图增加 `gallery/image/<id>?detail=display&max_edge=...`。显示预算根据屏幕适应尺寸和 DPR 选择 768／1024／1536／2048 桶；返回的 `width/height` 保持原图逻辑尺寸，`display_width/display_height` 表示实际显示图尺寸。`backend/media/display.py` 在线程中生成去除元数据、保留透明度的 WebP，并发最多 2 个，编码缓存限制 16 MiB／48 项；资源状态在缓存使用前和返回前重新检查。原有 preview、original、下载及参考图接口保持契约，不新增持久缓存文件或数据库迁移。

手机和平板的详情、灯箱复用同一显示图缓存，放大时才请求原图，缩回时恢复显示图。灯箱闲时队列每轮只启动一个阶段，详情的主图升级避开活动手势。`media-objects.js` 将高清原图的 base64 在线程中转为 Blob URL；不支持 Worker 时按块处理并让出主线程，URL 由灯箱会话持有，远离当前图片或关闭时释放。显示用 URL 不进入原图数据缓存，也不作为下载或参考图内容。双击倍率按 fit 计算，解码缓存同时限制 8 项和估算 32 MiB RGBA 像素占用；这些预算各自独立，不代表浏览器总内存上限。

导航反馈独立于图片处理队列。详情页分别记录请求目标与主图已绘制的图片身份，换图时及时替换前景，只有已显示同一图片的清晰度升级才等待闲时；不能因命中缓存保留旧图直到滑动层撤除。浏览器尚未完成主图解码时，已落地的滑动层继续覆盖旧前景，后续触摸仍可立即接管；冷图可交接至明确的占位图。灯箱图片条跟随 PhotoSwipe 已确认的 `potentialIndex`，在主图弹簧动画结束前即可更新组别、选中项并按帧居中，下载目标与权限使用同一索引；滑动取消后跟随组件恢复。图片条保留同一玻璃容器，未缓存缩略图仍通过受限闲时队列读取。

## 持久化边界

`StarTools.get_data_dir` 仍是运行数据目录入口，配置、数据库文件 `history.sqlite3`、任务修订和资产编号沿用原约定。本地原图与参考图统一放在 `images/assets`，预览缓存放在 `images/thumbnails`；旧 `history/assets`、`history/thumbnails` 由独立的 `database/migrations/images_layout.py` 在首次初始化时迁移。外部图库原图保留来源路径。

图片布局迁移先预检冲突和符号链接，校验后通过链接或复制准备目标文件，再用单一事务更新资产、预览和 ComfyUI 已知图库路径，提交后清除已核验旧副本及空目录。迁移日志支持中断续跑，未知旧目录内容保留；日常统计兼容新旧布局。这一文件与路径转换不增加数据库修订；当前数据库为 4-dev.2，插件版本为 `1.4.0-dev.1`。

`GenerationStore` 保留异步调用、原修改锁、缓存修订和取消保护。同步事务整体移入对应仓储，仓储共用 `GalleryContext`，不创建独立修改锁，也不通过代理访问整个 store。跨仓储调用以显式服务回调传入，数据库连接仍由原事务发起者持有。ComfyUI 任务继续使用同一图库数据库和原任务状态机，结构迁移由 `backend/database/schema.py` 集中处理。

1.3.x 的数据库正式基线为 v3，已发布迁移保持不变。当前 1.4.0 存储开发使用 4-dev.2：任务、图库和元数据仓储通过 `database/payloads.py` 共享不可变正文，各自持有引用；`gallery/storage.py` 维护轻量投影与按需展开，`providers/comfyui/storage.py` 管理任务编码。开发结构和正文迁移在同一备份保护的事务内提交；细节见 [开发与发布维护](DEVELOPMENT.md)及[存储生命周期](STORAGE_LIFECYCLE.md)。

`database/migrations/v4_storage.py` 固定旧数据到 4-dev.1 的转换契约，不调用日常业务仓储、当前解析器或可变投影代码。版本转换中相似的编码代码属于有意冻结的历史契约，不能随当前业务重构一起改写。后续 4-dev.2 由 `schema.py` 添加图组标题列，已有 4-dev.1 不重复转换正文。

图组标题保存为 `generations.title`，与模型名称、提示词及解析投影独立；`POST gallery/title` 经过图库修改锁只更新该列，列表与搜索直接读取。前端 `gallery/controller.js` 管理单个内联编辑器，确认后更新当前卡片，取消不提交；空标题仍回退到原来的模型／来源显示。

`GenerationConcurrency` 由主生成服务持有并随当前设置调整；ComfyUI 执行视图通过构造参数借用同一组件。历史任务和临时工作流不会用快照重新覆盖当前并发上限，异常和取消仍释放已占用的所有层级。NovelAI 官方相同账号继续共用串行限制。

元数据中的 ComfyUI 图分析用于展示和导入参数识别，执行图校验与绑定仍在 `providers/comfyui/workflows.py`。两者保留独立语义，不能把展示用的近似图直接视为可执行工作流。

解析器 10 的显示快照匹配从其关联输出端口向下游追踪，只纳入通向所选保存节点的连线与节点类型，允许该输出上游的提示词来源变化。前端继续校验观察节点与快照来源，遇到歧义不自动选择。普通文本候选和保存输出的匹配仍使用整个保存分支指纹；元数据版本回填更新匹配信息并保留人工覆盖，不改变数据库结构或执行工作流。

解析器 11 将显示快照证据识别放在条件摘要之前。`metadata/comfyui/evidence.py` 按采样阶段与正反向分别汇总文本，只补全已知编码器直接读取的未解析输入；观察节点、文本输入、已知条件链及采样器到所选保存节点的 API/UI 连线需一致且启用。只接纳唯一有效工作流回写，不将候选注入通用静态求值器；未知转换、API 备用值和冲突快照保持手动。直接采用的状态为 `snapshot`，组合或不同阶段保留 `summary`／`partial`，`prompt_sources`／`negative_prompt_sources` 保存使用位置与观察来源，`freshness` 仍为 `unverified`。清零条件不贡献文本，也不标记自动采用；同一编码器在不同链路中的证据独立核验。

解析器 12 将节点字段、控件顺序、已知条件组合和观察节点声明集中到内置 JSON，图遍历、证据核验、冲突与深度限制继续在代码中执行。用户绑定只支持有限的字面值、原样传递、明确拼接和显示回写；直接读取的结果为 `declared`，`prompt_sources` 保留规则身份及用户声明来源。显示快照可以经过已声明的有限文本关系抵达编码器，但未经绑定的节点仍中断自动识别。

安装包显式要求入口和关键后端文件，`backend/` 默认只收集 Python 运行文件，内置规则 JSON 为单独白名单文件；用户规则留在数据目录。打包测试检查遗漏、符号链接、临时数据排除，并在隔离目录从解压包导入全部模块，防止测试意外依赖源码工作区。开发用 npm 依赖、测试、日志和缓存不进入插件包。

## 维护约定

按模块职责新增实现，宿主注册签名集中在 `main.py`；无状态功能不要反向依赖存储入口。保留事务和异步生命周期边界，再考虑进一步拆小。通过统一验证入口检查工具注册、接口、持久化及对应浏览器场景，避免在结构整理中混入生成行为、配置格式或数据库版本变更。
