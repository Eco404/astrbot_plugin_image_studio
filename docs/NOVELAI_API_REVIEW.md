# NovelAI 官方接口核对

核对日期：2026-09-13。目标开发版本：`1.3.0-dev.1`。

已实现独立服务商类型 `novelai_official`，保留现有 `nai_direct` 第三方 GET 协议。本文保留文档核对依据；实现已完成离线验证，尚未使用真实 Token 调用生图、额度或账户接口。

## 当前实现状态

- `novelai.py` / `providers.py`：Bearer 鉴权，JSON 优先、ZIP 兼容的完整原图接收，响应项错误与可读图片分别保留，单底图图生图，订阅查询。
- `models.py` / `service.py`：四款内置模型的基础参数、按模式过滤参数、基础图生图默认开启且最多一张底图、同 Token 跨 Provider 共享串行限制。
- `storage.py` / `parameter_exchange.py`：逐图实际参数写入已有补充字段；按记录策略过滤，详情与复现跟随所选图片；官方与第三方同属 NovelAI 来源但各自映射参数。
- `image_metadata.py`：常规元数据与 alpha 隐写元数据合并，保留冲突来源；解析器 9，数据库仍为 v2。
- WebUI：官方服务商配置和预设、按模式显示重绘参数、Anlas 与 V5 使用额度分开显示。

当前 V5 默认 `params_version=4`，V4.5 默认 3，均保留手工 schema 扩展覆盖的校验入口；不默认注入 `use_new_shared_trial`。这两个决定仍需真实账号验证。没有开放流式预览、多角色、Vibe Transfer、角色参考或局部重绘。

参考源码快照：`caru-ini/novelai-sdk@72964b1`、`dafeiwu666/astrbot_plugin_ppnai@44c14c9`、`YayiMiko/astrbot_plugin_n5@3cc74dc`、`Aeka0/NAI-Utility-Tool@8f61bae` 及官方 `NovelAI/novelai-image-metadata@3428907`。采用协议事实独立实现，没有引入整套 SDK 或复制其高层工作流。

## 真实联调验收

以下项目尚未执行，不能以离线测试代替：

| 项目 | 核实内容 | 完成依据 |
| --- | --- | --- |
| 模型请求 | 四款模型 ID、V4.5/V5 的 params_version 和基本采样组合 | 记录脱敏成功请求及实际返回 |
| JSON 原图 | images 对象格式、seed/index、PNG/WebP 原图与元数据 | 原图可读取，逐图参数能复制和复现 |
| 订阅查询 | PAT 对图片域名 /user/subscription 的权限、Anlas 字段与 usage | 对照同一账户官网显示；不保留完整账户原始响应 |
| 试用与付费 | 不发送 shared_trial 的实际行为、V5 免费额度与 Anlas 变化 | 小量人工触发请求前后对照；不承诺本地条件判断等于免费 |
| 图生图 | 单底图编码、输出尺寸、strength/noise/extra_noise_seed | 四款内置模型均按官方 Image2Image 能力默认开启；仍需用真实账号验证具体模型和尺寸组合 |
| 批次与并发 | 目标模型在不同尺寸下的样本上限、同账号实际限制 | 初期串行/原生1张可用；更高设置单独核实 |

真实接口验证前不自动创建后台任务、读取既有真实配置密钥或发送测试生成请求。

## 官方资料

| 资料 | 用途 |
| --- | --- |
| [图片 API Swagger](https://image.novelai.net/docs/index.html)、[原始 Swagger JSON](https://image.novelai.net/docs/doc.json) | 请求、响应、图生图、角色结构、流式枚举及账户字段；本次协议核对的主要来源 |
| [Primary API Swagger](https://api.novelai.net/docs/) | 主账户服务及鉴权说明；部分 schema 比图片 API 文档旧 |
| [账户设置](https://docs.novelai.net/en/text/usersettings/account) | Persistent API Token 的获取和替换 |
| [图片数量](https://docs.novelai.net/en/image#number-of-images) | 原生批次与消耗说明 |
| [模型说明](https://docs.novelai.net/en/image/models) | 当前 V5、V4.5 等模型的用户功能 |
| [订阅与 Anlas](https://docs.novelai.net/en/subscription)、[Opus Usage Limits](https://docs.novelai.net/en/faq#opus-usage-limits) | 免费生成条件、Anlas 与 V5 使用额度的区别 |
| [Image2Image](https://docs.novelai.net/en/image/controltools)、[Steps & Prompt Guidance](https://docs.novelai.net/en/image/stepsguidance)、[Seed](https://docs.novelai.net/en/image/seed) | 参数含义与复现限制 |

这些是官方域名下的文档。`NAI2API/server/providers.js` 仅作为第三方实现参考，不把它的默认值、重试、计费和账号池策略当成官方规范。

## 鉴权

用户在 NovelAI 的 User Settings → Account → Get Persistent API Token 中复制完整 Token。官方说明关闭弹窗后不能再次查看该 Token；重新生成会使旧 Token 失效。

图片 Swagger 的 `/user/create-persistent-token` 明确给出：

```http
Authorization: Bearer pst-<token>
```

插件接收用户复制的完整 Token，构造 Bearer 请求头，不重复拼接 `pst-`，也不接收账号密码来自动登录。创建 Persistent Token 本身需要 JWT，不能用已有 Persistent Token 再创建一个。

## 优先接入普通 JSON 生图

官方图片服务地址为 `https://image.novelai.net`，普通生图入口：

```http
POST /ai/generate-image
Authorization: Bearer <完整 Persistent API Token>
Content-Type: application/json
Accept: application/json
```

Swagger 的 `image.ImageGenerationRequest` 包含 `action`、`input`、`model`、`parameters` 和可选用途未详述的 `url`。NAI2API 使用 `action: "generate"` 进行文生图；该动作值来自参考实现，官方 schema 只把 `action` 声明为字符串，没有枚举值。

主要字段在 `parameters` 内：

| 字段 | 官方声明 / 对插件的含义 |
| --- | --- |
| `width`、`height` | 整数尺寸；需按模型校验，不能直接发送第三方的“竖图”字符串 |
| `n_samples` | 整数图片数；对应一次请求的图片数量 |
| `seed` | 整数种子；真实返回种子应逐图记录 |
| `steps` | 数字；API schema 没有给出完整范围 |
| `scale` | 数字；Prompt Guidance |
| `cfg_rescale` | 数字；Guidance Rescale，不能与 `scale` 混用 |
| `sampler`、`noise_schedule` | 字符串；官方 schema 未枚举每个模型的合法组合 |
| `negative_prompt` | 反向提示词 |
| `v4_prompt`、`v4_negative_prompt` | 嵌套的 `caption.base_caption`、`caption.char_captions`，以及 `use_coords`、`use_order`、`legacy_uc` |
| `image_format` | 官方枚举为 `png`、`webp`；与 HTTP 响应包装格式分开 |

普通接口声明可返回 ZIP 或 JSON。发送 `Accept: application/json` 时，成功响应文档为 HTTP **201**，结构是：

```json
{
  "images": [
    {"image": "<Base64 图片字节>", "index": 0, "seed": 123456}
  ]
}
```

因此第一阶段可直接用 JSON，逐项解码 `images[].image`、保留 `index` 与 `seed`，无需先引入 ZIP 或 MessagePack 依赖。解析时应核验每项，避免过滤掉失败项后图片编号发生错位。成功处理不能只接受 HTTP 200。原始图片字节直接进入现有资产与元数据流程，不经重编码。

NAI2API 的普通 JSON 分支使用 `payload.image || payload.data || payload.images?.[0]`，会把官方结构的第一项对象当作图片字符串。通过替换 `fetch` 返回上述结构的离线模拟，已确认它不能正确解码该响应；这部分应按官方文档重新实现。

`x-correlation-id` 是文档列出的可选请求头：6 个字母或数字，方便报告上游错误。NAI2API 附带的 Origin、Referer、User-Agent、`x-initiated-at` 没有被该 endpoint 声明为必填头。

## 流式接口

`POST /ai/generate-image-stream` 使用相同请求结构。接口摘要描述 SSE，`image.StreamingType` 则明确同时列出 `msgpack` 和 `sse`，通过 `parameters.stream` 选择。

官方说明中间、最终和错误三种事件，但 Swagger 没有完整描述 MessagePack 的二进制分帧以及事件全部字段。NAI2API 的“4 字节大端长度 + MessagePack”解析方式可参考，仍需真实脱敏响应验证。不能因为 endpoint 摘要写 SSE 就认定 MessagePack 未公开支持。

第一阶段可不展示采样进度；后续实现流式时，只把明确的最终图片当作生成成功。NAI2API 的 `finalImage || lastImage` 会在仅收到中间图并随后报错时返回中间图，应避免沿用。

## 批次、并发与费用

官方用户文档说明批次数量随分辨率变化，例如 Small 最多 6 张、Normal/Large 最多 4 张；这不是所有模型、尺寸、操作都通用的 API 上限。Swagger 的 `n_samples` 没有补充条件范围。

官方原文：**“generating more than one image at once will always cost Anlas.”** 这也适用于 Opus 用户。所以更大的原生批次不一定更适合默认启用。

官方订阅说明的常规免费条件包括单张、无 base image、最多 28 步及相应尺寸范围；页首脚注使用 “up to 1024x1024 pixels”。订阅正文有 “at least Normal Sized” 的不一致措辞，不能用它实现“大图也免费”的判断。

V5 的 Opus 免费生成另受可恢复的 Usage Limit 限制；FAQ 明确其他模型不受这项新限制，付费 Anlas 生成也不受这项限制。不能把 V5 写成无条件无限免费，也不能把该百分比换算成固定剩余张数。

在本次核对的官方文档中，没有找到固定的账号并发请求数。NAI2API 的 `n_samples=1` 与账号并发 1 是参考实现的策略，分别不等于官方原生批次上限和已确认的官方并发上限。

接入建议：初期采用 `n_samples=1`、同账号请求串行的保守调度，仍允许用户请求多张，由插件拆分完成。应明确这是插件初始策略；后续开放原生批次时提示其消耗差异。同账号跨模型共享并发限制，同一个 Token 配置为多个 Provider 时也不能绕过。

## 额度与订阅查询

当前图片服务 Swagger 公开：

- `GET /user/data`：聚合账户、订阅、keystore、设置等信息。
- `GET /user/subscription`：返回订阅状态，数据范围更小，可优先核验是否能满足额度展示。

`user.SubscriptionResponse` 包含 `active`、`expiresAt`、`tier`、`trainingStepsLeft` 与 `usage`。`trainingStepsLeft` 下有 `fixedTrainingStepsLeft`、`purchasedTrainingSteps`。NAI2API 把两者相加显示为点数；当前 schema 没有完整解释它们与 Anlas 的对应关系，旧 Primary schema 仍沿用 module training steps 的描述，实际账户余额应在接入时对照官网核验。

`usage` 的官方定义是：

| 字段 | 官方说明 |
| --- | --- |
| `percent` | `[0-100+]`，可能大于 100 |
| `isNegative` | 为 true 时不可用 |
| `timeUntilNextPercent` | 单位秒；从当前数值增加 1% 所需时间，例如 1.4% → 2.4%；超过上限暂停恢复时为 0 |

不能把 `timeUntilNextPercent` 当作完全恢复所需时间。余额、订阅状态与免费使用额度应分别展示，不把原始 `/user/data` 全部内容返回给 LLM。

Primary Swagger 前言建议第三方通常不要使用 Primary API 的 `/ai/` 以外路由；同时当前图片 API 文档公开上述账户路径。应保留这个区别，接入前确认 Persistent Token 在目标地址上的账户查询权限，查询失败不阻止生图。无需实现账号登录、Token 创建或订阅修改操作。

## 模型与高级图片能力

官方模型页已经介绍 V5 Full/Curated、V4.5 Full/Curated 等模型，但 ImageGenerationRequest 中的 `model` 只是字符串，没有提供完整 API ID 枚举。`/oa/v1/models` 声明返回的是 OpenAI 兼容接口的文本模型结构，不能据此自动填充图片模型列表。首批官方模型 ID 需要结合已知参考实现与一次实际请求验证。

官方用户文档明确支持 Image2Image，API 参数也列出 `image`、`strength`、`noise`、`extra_noise_seed`、`mask`、`img2img`、角色参考与 Vibe Transfer 数组。当前可确认的普通参考图能力是：V4.5 Full、V4.5 Curated、V5 Full、V5 Curated 均支持单张底图 Image2Image；插件因此在创建这四个内置模型时默认开启图生图并将参考图上限预填为 1。Vibe Transfer 和角色参考使用独立的 API 参数与预处理流程，不能等同于普通参考图，当前未接入。action 值、编码细节、数组配对和兼容组合没有全部在 schema 中解释。NAI2API 当前只发文生图，不能作为这些功能的完整范例。

NAI2API 写死的 `params_version=3`、`uncond_scale`、`cfg_sched_eligibility`、`use_new_shared_trial` 等应分别核验：字段存在不代表固定值正确，未出现在当前 schema 的字段也不能擅自认定必填。特别是启用试用或免费额度的开关，不应在没有确认语义的情况下默默注入。

官方 Seed 文档提醒复现还受分辨率、原生图片数量、参考图及采样器影响；保存 seed 并不保证像素级一致。沿用插件现有“不回填本次数量和并发配置”的规则时，也应说明这一复现边界。

## 接入顺序与验证边界

1. 新增 `novelai_official` 类型、Bearer 鉴权、模型专用参数构造及普通 JSON 生图响应。
2. 接入既有调度、图库、资产工具；完善逐图真实 seed 及实际参数记录。官方和第三方同属 `novelai` 生图来源，但参数映射与服务商选择独立。
3. 核验订阅查询权限与余额语义，接入可降级的额度显示。
4. 使用脱敏的实际响应核验模型 ID、参数组合、批次、失败状态和最终图片元数据；之后再扩展流式和高级参考图功能。

官方生成接口说明所有生成请求应由人的操作发起，禁止制造过量负载的自动生成。插件应以用户发起的任务为入口，不增加无人触发的后台循环生图。

本次证据包括：在线读取官方 Swagger 和用户文档；离线模拟官方 JSON 响应，核对 NAI2API 的解码不兼容。尚未证明任何真实账户、模型、免费额度或图生图组合可用。当前图片 schema 已可指导基础接入，详细限值和未公开的固定参数仍需后续实测。
