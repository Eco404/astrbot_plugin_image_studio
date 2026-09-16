# 目录与职责

第一轮整理将已有后端模块归入 `backend/`，拆出 ComfyUI Page API 和能力查询载荷组装。数据库、配置格式、外部 API 地址和工具名称保持原有约定。内部 Python 导入路径已迁移，不保留根目录兼容副本。

```text
main.py                         AstrBot 注册、生命周期、入口及事件适配
backend/
  config.py                     配置读取、规范化、持久化
  models.py                     请求/结果结构、模型配置与参数策略
  api/
    routes.py                   WebUI 路由目录
    comfyui.py                  工作流导入、检查、任务查询与提交接口
  tools/
    capability_catalog.py       服务商/模型发现、批量选择
    capabilities.py             完整能力载荷组装
  generation/service.py         生图校验、批次、并发、结果登记与复现
  providers/
    executor.py                 通用服务商调度及 HTTP 适配
    comfyui/
      client.py                 ComfyUI HTTP/WebSocket 协议
      workflows.py              执行图规范化、参数绑定、执行计划
      jobs.py                   任务/修订持久化与任务管理
      runtime.py                生图服务与可恢复任务的协调
      imports.py                图片/JSON 导入结果适配
    novelai/
      protocol.py               官方接口请求/响应处理
      catalog.py                模型能力目录
      inputs.py                 高级输入处理
      inpaint.py                蒙版与重绘合成
  gallery/
    store.py                    图库、资产、导入和清理的统一存储入口
    external.py                 外部图库扫描及来源适配
    timestamps.py               外部图片的时间判定
  metadata/
    parser.py                   图片元数据解析
    exchange.py                 参数复制、导入与复现映射
  database/schema.py            数据库正式/开发版本和迁移
  ui/                           浏览器主题、图库偏好的传输格式
pages/image-studio/             现有原生 JavaScript/CSS 页面与第三方静态资源
tests/                         Python 回归、浏览器场景和隔离 harness
scripts/                       验证入口和发布包构建
```

## 入口与依赖

`main.py` 保留 AstrBot 的插件类、装饰器和注册签名。能力工具调用 `query_capabilities(settings, ...)` 获取载荷，再由入口维护本次事件的能力查询状态。搜索摘要不会触发完整能力授权。纯载荷模块不导入插件入口或 AstrBot 事件。

`ComfyAPI` 通过构造参数接收配置读取函数、存储、服务/任务运行器获取函数和结果序列化函数。每次请求读取当前设置，避免保存配置后仍使用旧快照；不通过继承或全局状态访问插件实例。修改服务商配置的事务仍由入口管理，后续可以单独提取设置控制器。

`routes.py` 仅集中登记现有路径、方法、处理函数和描述。处理函数继续经过插件入口的薄包装；不改变宿主提供的认证、上下文绑定或返回格式。

`backend/` 子包的 `__init__.py` 保持轻量。内部调用使用明确的相对导入；测试引用相应的新模块，避免根目录转发层和模块别名掩盖循环依赖。

## 持久化边界

代码目录移动不移动数据。`StarTools.get_data_dir` 仍是运行数据目录入口，原配置文件、图库路径、SQLite 文件、任务修订和资产编号均沿用既有位置与格式。

本轮保留 `GenerationStore` 内已有事务、锁、文件回收和取消保护；ComfyUI 任务仍使用同一图库数据库和原任务状态机。结构迁移仍由 `database/schema.py` 集中处理，不因代码目录变化增加数据库版本。

安装包显式要求入口和关键后端文件，并只从 `backend/` 收集 Python 运行文件。打包测试检查遗漏、符号链接、临时数据排除，并在隔离目录从解压包导入全部模块，防止测试意外依赖源码工作区。开发用 npm 依赖、测试、日志和缓存不进入插件包。

## 后续拆分顺序

第一轮未拆散存储事务，也未移动前端和测试目录。后续分别处理：

1. 将入口其余图库、导入、设置 API 和指令解析按职责提取。
2. 明确 `app.js` 和 `library.js` 中生图、图库、灯箱、导入、设置的状态所有者，再提取组件。
3. 为生成服务与 ComfyUI 运行器提供显式共享的并发控制能力，消除私有字段赋值。
4. 将图片编码、格式识别等无状态函数从存储模块中提取，之后按事务边界拆分查询、导入、资产和维护。
5. 当测试共享入口稳定后，再按业务或运行环境调整测试文件位置。

每一步分别验证工具注册、接口、持久化及对应浏览器场景。避免在同一批结构整理中更改生成行为、配置格式或数据库结构。
