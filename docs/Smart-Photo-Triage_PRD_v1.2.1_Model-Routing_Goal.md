# Smart-Photo-Triage v1.2.1 多模态 Provider 与模型路由升级 PRD

| 属性 | 内容 |
|---|---|
| 文档版本 | v1.2.1 Goal-Driven Upgrade Baseline |
| 编制日期 | 2026-08-21 |
| 前置版本 | Smart-Photo-Triage v1.2.0 |
| 配套 TDD | `Smart-Photo-Triage_TDD_v1.2.1_Model-Routing_Goal.md` |
| 开发模式 | Codex Goal Mode，自主拆解、自主验证、自主修复，用户只做最终验收 |
| 核心升级目标 | 将 v1.2 的“可替换 Vision Provider”升级为可配置、多 Provider、可路由、可回退、可审计的模型无关多模态调用层 |
| 产品边界 | 本版本只升级 AI Provider 基础设施，不引入人物识别、事件聚类、地点图谱、CLIP/SigLIP 语义检索等 v2 能力 |

> 本文是 v1.2.0 的增量升级 PRD，不替代 v1.2.0 的产品、安全和文件事务基线。凡本文未明确修改的 v1.2.0 要求继续有效。

---

## 0. 版本定位与启动条件

### 0.1 为什么是 v1.2.1

v1.2.0 已经要求业务层依赖可替换的 Vision Provider，而不是把 Gemini 写死在核心业务逻辑中，同时缓存身份已经包含 provider、model、prompt version、schema version。

v1.2.1 不重做这一层，而是补齐四个长期维护能力：

1. **Provider Contract**：统一定义不同厂商和自定义端点的能力、配置和错误语义。
2. **Custom Endpoint**：允许用户通过配置接入 OpenAI-compatible 多模态服务，而不是每增加一个厂商就复制一套业务逻辑。
3. **Model Routing**：不同任务可以使用不同模型，普通任务优先低成本模型，困难任务和连拍比较可升级到更强模型。
4. **Fallback / Escalation / Audit**：调用失败或低置信度时可以按照明确规则有界回退，并完整记录实际使用的 Provider、模型、原因和成本估算。

### 0.2 启动条件

Codex 只有在以下条件满足后才开始 v1.2.1：

```text
v1.2.0 implementation exists
+ v1.2.0 non-external automated tests green
+ v1.2.0 has reached READY_FOR_FINAL_ACCEPTANCE or an equivalent accepted baseline
+ user explicitly starts the v1.2.1 Goal
```

v1.2.1 开发不得反向打断正在执行的 v1.2.0 Goal。

### 0.3 升级原则

```text
Backward compatible
+ additive migration
+ provider agnostic
+ explicit privacy boundary
+ bounded routing
+ auditable decisions
+ no silent cloud escalation
```

---

# 1. 用户问题

v1.2.0 的 Provider 抽象解决了“核心业务不直接绑定某一家模型”的问题，但实际使用仍可能面临：

- 用户希望在 Gemini、OpenAI、Claude、Qwen、豆包、GLM 等模型之间选择；
- 中国大陆、海外、企业内网的可用模型和网络条件不同；
- 同一用户可能同时拥有多个 API；
- 普通照片分类没有必要使用最昂贵模型；
- 连拍 Best Shot、复杂场景、低置信度结果可能需要更强模型；
- 某 Provider 临时 429、5xx、timeout 时，不希望整批任务停止；
- OpenAI-compatible 并不等于所有端点都支持相同的多图、JSON Schema 和视觉能力；
- 用户需要知道一次分析实际用了哪个模型、为什么升级、发生了多少次 fallback；
- 自定义 base URL 带来新的数据外发和安全边界；
- 不希望增加 Qwen、豆包、GLM 时分别复制厂商专用业务逻辑形成维护负担。

因此 v1.2.1 的目标不是“增加更多模型名称”，而是建立稳定的 **Provider Platform**。

---

# 2. 产品目标

v1.2.1 完成后，用户应能够：

1. 在不修改业务流水线的情况下选择不同 Vision Provider；
2. 通过配置增加 OpenAI-compatible 多模态端点；
3. 为普通单图分析和 Burst Review 配置不同模型；
4. 对低置信度结果配置一次受控的强模型升级；
5. 在临时网络或服务错误时按明确顺序 fallback；
6. 明确禁止某些错误触发 fallback；
7. 在任何 remote fallback 发生前受到 `allow_cloud` 约束；
8. 查看 Provider 能力、配置健康状态和路由结果；
9. 运行前看到请求量、远程 preview 数据量和可获得的成本估算；
10. 保持 v1.2.0 的统一 AI 输出 Schema、人工决策优先级和文件安全边界；
11. 使用旧的 v1.2.0 单 Provider 配置时仍可正常升级运行；
12. 在无任何云凭据时依然可以依靠 Fake Provider 完成全部自动测试。

---

# 3. 非目标

以下内容不进入 v1.2.1：

- 人脸检测；
- Face Embedding；
- 人物自动聚类和人物命名；
- Event Engine；
- GPS 地点聚类；
- reverse geocoding 产品化；
- CLIP/SigLIP 图片语义向量；
- 向量数据库；
- OCR 全库索引；
- 自然语言“Ask Photos”；
- 原生视频上传到厂商 API；
- 自动 hard delete；
- 自动根据在线模型排行榜选择 Provider；
- 自动抓取厂商实时价格；
- 训练或微调任何模型；
- 在程序内部托管大模型权重；
- 引入 LiteLLM、OpenRouter 或其他网关作为强制运行依赖；
- 将 v1.2.1 扩展为完整 AI Gateway 产品。

这些能力统一进入后续产品路线备忘录，不得以“顺手实现”为理由进入 v1.2.1。

---

# 4. 继承 v1.2.0 的不可违反约束

以下 v1.2.0 原则继续是 P0：

```text
source library default read-only
AI cannot delete / move / overwrite
allow_cloud defaults false
raw media cannot be uploaded
absolute source path cannot be uploaded
content hash cannot be uploaded
sidecar raw content cannot be uploaded
HUMAN > AI > RULE
REJECT_CANDIDATE is not DELETE
all real AI output must pass strong schema validation
```

v1.2.1 新增的 Router、Fallback、Custom Endpoint 不得形成绕过上述约束的第二条调用路径。

---

# 5. 目标架构

推荐逻辑结构：

```text
Analysis Pipeline
       |
       v
AI Task
       |
       v
Model Router
       |
       +-------------------------+
       |                         |
       v                         v
Provider Registry         Route Policy
       |                         |
       +-------------+-----------+
                     |
           +---------+---------+-------------------+
           |                   |                   |
           v                   v                   v
      Gemini Driver       OpenAI Driver      Anthropic Driver
                                                   |
                              +--------------------+
                              |
                              v
                    OpenAI-Compatible Driver
                              |
                 +------------+-------------+
                 |            |             |
                 v            v             v
               Qwen        Doubao         GLM / other
                 |
                 v
          custom compatible endpoint

All provider results
       |
       v
Unified Internal Schema
       |
       v
v1.2 AI result/cache/human review pipeline
```

说明：

- 上层业务只认识 `AI Task` 与统一输出 Schema；
- Router 不拥有文件权限；
- Provider Driver 只负责把统一请求转换为厂商请求，再把厂商响应转换为统一 Schema；
- 不为每一个 OpenAI-compatible 厂商复制核心逻辑；
- 厂商差异通过 `ProviderCapabilities`、请求适配和错误映射解决。

---

# 6. Provider Contract

## 6.1 Provider Registry

系统必须有一个 Provider Registry。

每个 Provider 实例至少有：

```text
provider_id
driver
model
display_name optional
endpoint/base_url optional
api_key_env optional
network_scope
capability_profile
request_limits
enabled
```

`provider_id` 是用户配置中的稳定逻辑名称，例如：

```text
qwen_fast
openai_strong
gemini_burst
claude_review
local_vlm
```

`provider_id` 不等于厂商名，也不等于模型名。

## 6.2 必须支持的 Driver 类型

v1.2.1 至少支持：

```text
gemini
openai
anthropic
openai_compatible
fake
```

要求：

- `gemini` 可以复用 v1.2 已有真实 Provider，但要纳入统一 Registry；
- `openai` 使用原生 OpenAI API 适配；
- `anthropic` 使用原生 Anthropic Messages/Vision 适配；
- `openai_compatible` 用于 Qwen、豆包、GLM 以及用户自定义兼容端点；
- `fake` 用于所有自动测试和确定性 E2E。

如果 Codex 发现当前 v1.2 已有实现方式更简洁，可以通过 ADR 调整类名和模块结构，但必须保留行为等价性。

## 6.3 OpenAI-compatible 的边界

不得假设“OpenAI-compatible”意味着：

- 一定支持图片；
- 一定支持多图；
- 一定支持 JSON Schema；
- 一定支持完全相同的 message content；
- 一定支持完全相同的参数；
- 一定支持相同的错误码；
- 一定支持相同的最大图片数。

因此 Generic Driver 必须依赖能力合同，而不是通过 Provider 名称猜测。

---

# 7. ProviderCapabilities

每个 Provider 必须暴露经过验证或配置声明的能力对象。

最低字段：

```text
supports_image
supports_multi_image
supports_structured_json
supports_json_schema
max_images_per_request optional
max_request_bytes optional
supported_image_mime_types
supports_system_prompt
supports_streaming optional
capability_profile_version
```

本项目 v1.2.1 不依赖厂商原生 video input，因此无需把 `supports_video` 作为路由必需能力。视频继续沿用 v1.2：

```text
video
-> local FFmpeg
-> contact sheet
-> image-capable Provider
```

## 7.1 能力来源

### Built-in Driver

内建 Driver 可以提供保守默认能力。

### OpenAI-compatible Custom Endpoint

不得盲目假设能力。

可以通过以下方式之一确定：

1. 用户明确声明 capability；
2. `spt ai probe <provider_id>` 用 synthetic image 做兼容性探测；
3. 使用经过版本化的内建 preset。

Probe 只证明“当前兼容测试通过”，不证明供应商安全性和长期兼容性。

## 7.2 能力不足

任务开始前 Router 必须进行 capability preflight。

例如：

```text
burst needs multi-image
provider supports_multi_image = false
=> do not send request
=> skip provider or report CAPABILITY_MISMATCH
```

不能等到远程调用之后才发现明显能力不匹配。

---

# 8. Unified AI Task Contract

v1.2.1 路由层只处理明确任务类型。

MVP 路由任务至少：

```text
ITEM_ANALYSIS
BURST_REVIEW
```

不得把文件移动、删除、重命名、Plan 或 Rollback 变成 AI task。

## 8.1 ITEM_ANALYSIS 输入

继续使用 v1.2 的受控 Preview 和匿名化本地质量指标。

## 8.2 BURST_REVIEW 输入

继续使用同一 BurstGroup 的受控 Preview 集。

Router 必须确保选中的 Provider 支持足够的 multi-image 能力。

## 8.3 输出

所有 Provider 最终必须归一为 v1.2 已有的内部 Schema。

至少保留：

```text
item_id / group_id
scene_category
disposition
confidence
quality_score
tags
short_desc
reason
```

任何 Provider 特有字段不得直接渗透到业务数据库核心状态。

如确有必要保留，可写入独立 diagnostic metadata，但不能改变业务决策语义。

---

# 9. 配置合同

推荐配置示例：

```yaml
ai:
  enabled: true
  allow_cloud: false
  allow_lan: false

  providers:
    qwen_fast:
      driver: openai_compatible
      base_url: "https://example-compatible-endpoint/v1"
      model: "vision-fast"
      api_key_env: "SPT_QWEN_API_KEY"
      network_scope: remote

    openai_strong:
      driver: openai
      model: "configured-at-deploy-time"
      api_key_env: "OPENAI_API_KEY"
      network_scope: remote

    local_vlm:
      driver: openai_compatible
      base_url: "http://127.0.0.1:1234/v1"
      model: "local-vision-model"
      network_scope: loopback

  routes:
    item_analysis:
      primary: qwen_fast
      fallbacks:
        - openai_strong
      escalation:
        confidence_below: 0.72
        to: openai_strong
        max_escalations: 1

    burst_review:
      primary: openai_strong
      fallbacks: []

  limits:
    max_provider_attempts_per_task: 3
    max_remote_preview_mb_per_run: null
    max_requests_per_run: null
    max_estimated_cost_per_run: null
```

字段名称可由 Codex 在实现中合理调整，但以下语义必须存在：

- Provider 实例；
- Driver；
- model；
- endpoint；
- secret environment variable；
- network scope；
- per-task route；
- primary；
- ordered fallback；
- optional confidence escalation；
- bounded attempt；
- optional budget limits。

---

# 10. Network Scope 与隐私安全

自定义 base URL 引入新的 P0 风险。

## 10.1 Network Scope

至少区分：

```text
loopback
lan
remote
```

### loopback

只允许明确 loopback 地址，例如 `127.0.0.1`、`::1`。

### lan

默认关闭。只有用户显式 `allow_lan=true` 才能使用。

### remote

只有 `allow_cloud=true` 才能发送 Preview。

## 10.2 禁止静默升级到远程

如果当前 primary 是本地模型：

```text
local primary
-> fails
-> remote fallback
```

只有同时满足：

```text
remote fallback is explicitly configured
AND allow_cloud = true
```

才允许发生。

Router 绝不能因为“提高成功率”自动把本地任务升级到云端。

## 10.3 HTTPS

remote endpoint 默认必须是 HTTPS。

HTTP 仅允许 loopback，或用户对受控 LAN 明确开启的不安全开发模式。

## 10.4 Redirect

自定义 Provider 请求不得未经重新校验就跟随 redirect。

推荐默认关闭 redirect。

如果实现允许 redirect，则每一级目标都必须重新经过 network scope 和 allow_cloud/allow_lan 校验。

禁止：

```text
localhost endpoint
-> HTTP redirect
-> remote internet endpoint
```

绕过云外发授权。

## 10.5 Secrets

API Key：

- 只通过环境变量、系统安全存储或等价秘密机制读取；
- 不写入项目配置文件示例中的真实值；
- 不写入 SQLite；
- 不写日志；
- 不出现在 exception repr；
- 不写入 release evidence。

---

# 11. Model Routing

## 11.1 路由原则

Router 的工作不是“自动猜哪个模型最好”，而是执行用户定义的确定性策略。

最低支持：

```text
task type route
+ provider capability filter
+ ordered primary/fallback
+ confidence escalation
+ bounded attempts
```

## 11.2 推荐用法

### 普通单图

```text
cheap / balanced model
```

### Burst Review

```text
strong multi-image model
```

### Low Confidence

```text
primary result confidence < configured threshold
-> escalate once to stronger provider
```

这允许：

```text
大量普通照片走低成本模型
少量困难照片升级强模型
```

而不需要所有媒体都调用旗舰模型。

## 11.3 路由必须确定

在相同：

```text
task
config
capabilities
provider health class
```

条件下，候选 Provider 顺序必须确定。

瞬时网络可用性可以改变最终命中的 Provider，但 Router 不能随机选取模型。

## 11.4 Escalation

低置信度升级必须：

- 配置开启；
- 有明确 threshold；
- 最多执行配置次数；
- 默认一次；
- 记录升级前后的结果；
- 不形成无限递归；
- HUMAN 决策已经存在时，不因重新路由覆盖 HUMAN。

---

# 12. Fallback 与错误分类

Provider 错误必须映射到统一错误类型。

至少：

```text
RATE_LIMIT
TIMEOUT
SERVER_ERROR
NETWORK_ERROR
SCHEMA_INVALID
CAPABILITY_MISMATCH
AUTH_ERROR
BILLING_ERROR
CONFIG_ERROR
PRIVACY_BLOCKED
CONTENT_REJECTED
UNKNOWN_PROVIDER_ERROR
```

## 12.1 默认可 fallback

建议默认仅对以下错误允许按 route fallback：

```text
RATE_LIMIT
TIMEOUT
SERVER_ERROR
NETWORK_ERROR
SCHEMA_INVALID after bounded repair/retry
```

`CAPABILITY_MISMATCH` 应在 preflight 中跳过该 Provider。

## 12.2 默认不得 fallback

以下错误默认不应被“换一家模型”悄悄掩盖：

```text
AUTH_ERROR
BILLING_ERROR
CONFIG_ERROR
PRIVACY_BLOCKED
```

## 12.3 有界

必须有：

```text
max_provider_attempts_per_task
max_retry_per_provider
max_escalations
```

任何任务不得进入无限 Provider 循环。

---

# 13. Cache Contract

v1.2 已要求 cache identity 至少包含：

```text
input fingerprint
preview version
provider
model
prompt version
schema version
```

v1.2.1 扩展为至少考虑：

```text
provider_id
driver
model
endpoint identity
capability profile version
prompt version
schema version
preview fingerprint
```

## 13.1 不同 Provider 的结果不能错误复用

例如：

```text
qwen_fast result
```

不能作为：

```text
openai_strong result
```

的 cache hit。

## 13.2 Escalation 缓存

如果 primary 已有有效 cache result：

- 可先读取 primary cache；
- 如果该结果触发当前 route 的 escalation 条件，则可以继续查找 strong provider cache；
- strong provider 结果存在时不重复远程请求。

## 13.3 Router 规则变化

纯粹改变 route priority 不要求废弃底层 Provider 结果。

推荐将：

```text
provider result cache
```

和：

```text
route execution record
```

分离。

这样用户改变路由后可以复用已经存在的模型分析结果。

---

# 14. Audit Trail

每一个 AI task 至少要能回答：

```text
这个任务为什么调用这个模型？
调用了几次？
发生过 fallback 吗？
是否发生 confidence escalation？
哪个结果成为 effective AI result？
是否命中 cache？
是否向 remote endpoint 发送了 preview？
发送了多少 preview bytes？
```

建议记录：

```text
ai_run
ai_task
ai_attempt
route_decision
```

具体表结构由 Codex 决定。

每个 attempt 至少记录：

```text
task_id
provider_id
driver
model
started_at
finished_at
status
error_class optional
cache_hit
remote_bytes_estimated
route_reason
attempt_index
```

不得记录：

- API key；
- 完整 base64 图片；
- 完整请求体；
- 本地绝对源路径；
- content hash；
- sidecar 原文。

---

# 15. 成本与预算可见性

## 15.1 原则

Provider 价格经常变化。

v1.2.1 不硬编码厂商实时价格作为业务真理。

## 15.2 可选价格元数据

允许用户或 preset 配置：

```text
estimated_cost_rule
```

如不可可靠估算，只展示：

- 请求数；
- remote preview MB；
- Provider/model 分布；
- cache hit；
- escalation 次数。

## 15.3 Budget Guard

可选配置：

```text
max_requests_per_run
max_remote_preview_mb_per_run
max_estimated_cost_per_run
```

如果用户设置 budget limit：

- 预计超过上限时不得静默继续；
- 必须在 run 前或达到阈值时进入明确 blocked/paused 状态；
- 不得通过 fallback 绕过预算。

预算限制只控制 AI 调用，不影响本地扫描、Preview、重复检测等本地能力。

---

# 16. CLI 与 Review UI

v1.2.1 至少增加以下可观察能力。

## 16.1 CLI

命令名称可调整，但行为至少包括：

```text
spt ai providers
spt ai doctor
spt ai probe <provider_id>
spt ai route explain <task-type>
spt ai estimate
```

### providers

显示：

- provider_id；
- driver；
- model；
- network_scope；
- enabled；
- capability summary。

不得显示完整 secret。

### doctor

验证：

- 配置合法；
- env key 是否存在；
- endpoint scope；
- route reference；
- capability requirement；
- fallback graph 无循环；
- budget 配置。

默认不执行远程真实调用，除非显式请求。

### probe

只允许使用 synthetic / generated test image。

不得使用用户真实媒体做 Provider Probe。

### route explain

给出某任务的：

```text
primary
capability filters
fallback order
escalation rule
privacy gate
```

### estimate

按当前 pending AI tasks 给出：

- 预计 tasks；
- route 分布；
- cache hits；
- remote preview MB；
- 可用时的成本估算。

## 16.2 Review UI

v1.2.1 不要求重做 UI。

建议在已有详情中增加只读信息：

```text
effective provider
effective model
route reason
cache hit
escalated yes/no
attempt count
```

用户仍然只编辑 HUMAN decision，不在此版本把 UI 变成完整 Provider 管理后台。

---

# 17. Backward Compatibility

## 17.1 v1.2 旧配置

如果 v1.2 只有：

```text
provider
model
allow_cloud
```

升级后必须：

- 自动映射为一个默认 Provider 实例和默认 route；
- 或提供清晰、无损的一次性 config migration；
- 不能要求用户重新扫描媒体库；
- 不能丢失已有 AI cache；
- 不能丢失 HUMAN decisions；
- 不能改变已有 file plan/journal 语义。

## 17.2 DB Migration

必须 additive 或安全迁移。

禁止为了 Provider Router：

- 清空数据库；
- 重建媒体索引；
- 删除旧 AI result；
- 修改文件实例主键；
- 修改 operation journal 的核心安全语义。

---

# 18. Provider Presets

可以提供经过版本化的配置 preset，降低用户接入成本。

例如：

```text
Gemini
OpenAI
Anthropic
Qwen via OpenAI-compatible
Doubao via OpenAI-compatible
GLM via OpenAI-compatible
Local OpenAI-compatible endpoint
```

Preset 只能提供：

- driver 类型；
- base URL 模板；
- capability 保守值；
- 文档说明。

不得硬编码：

- 用户 API key；
- 不可验证的长期价格；
- 永久有效的具体模型 ID；
- 对厂商服务稳定性的承诺。

模型名应配置化。

---

# 19. Goal Mode 开发治理

v1.2.1 继续采用：

```text
one persistent Goal
+ autonomous milestone decomposition
+ TDD
+ deterministic gates
+ self-repair
+ final release evidence
```

用户不审批每个阶段。

Codex 在 Gate 通过后自动继续。

普通事项不构成 BLOCKER：

- 模块拆分；
- 内部类名；
- 小型依赖；
- 测试组织；
- 迁移实现细节；
- HTTP client 选择；
- Provider adapter 代码结构。

真正 BLOCKER：

- 需要破坏 v1.2 P0 安全不变量；
- 需要清空或不可逆迁移现有数据库；
- 需要用真实用户媒体完成云 Provider 测试；
- 需要用户真实 API Key 且 Fake/Mock 无法继续；
- PRD 与 v1.2 已验收行为存在无法兼容的 P0 冲突；
- 新依赖存在无法接受的许可证风险。

无真实 API Key 不属于 BLOCKER。External smoke 可以记录 `NOT RUN - no credentials`。

---

# 20. Definition of Done

只有同时满足以下条件，v1.2.1 才可报告：

```text
READY_FOR_FINAL_ACCEPTANCE_1_2_1
```

## 20.1 Provider DoD

- [ ] Provider Registry 可用；
- [ ] Gemini 或 v1.2 默认真实 Provider 已纳入 Registry；
- [ ] OpenAI Driver 可用；
- [ ] Anthropic Driver 可用；
- [ ] OpenAI-compatible Driver 可用；
- [ ] Fake Driver 可完整测试；
- [ ] ProviderCapabilities 可验证；
- [ ] Custom Endpoint 可配置；
- [ ] Secret 不写入仓库/DB/日志。

## 20.2 Routing DoD

- [ ] ITEM_ANALYSIS 可独立配置 route；
- [ ] BURST_REVIEW 可独立配置 route；
- [ ] capability preflight 可用；
- [ ] ordered fallback 可用；
- [ ] low-confidence escalation 可用；
- [ ] fallback/escalation 有界；
- [ ] route explain 可用；
- [ ] router 不拥有文件权限；
- [ ] HUMAN decision 不被 route rerun 覆盖。

## 20.3 Privacy DoD

- [ ] allow_cloud 默认 false；
- [ ] allow_lan 默认 false；
- [ ] loopback/lan/remote 正确区分；
- [ ] remote fallback 不可绕过 allow_cloud；
- [ ] redirect 不可绕过 network scope；
- [ ] remote 默认 HTTPS；
- [ ] 原始媒体从不上传；
- [ ] absolute path/hash/sidecar raw content 不进入请求；
- [ ] logs 不泄漏 secret/request body。

## 20.4 Compatibility DoD

- [ ] v1.2 全部自动回归测试继续通过；
- [ ] v1.2 单 Provider 配置可升级；
- [ ] 旧 AI cache 可保留；
- [ ] HUMAN decision 保留；
- [ ] 媒体库无需重扫；
- [ ] Plan/Apply/Journal 行为无回归。

## 20.5 Evidence DoD

- [ ] provider contract tests green；
- [ ] router tests green；
- [ ] privacy/security tests green；
- [ ] cache/migration tests green；
- [ ] fake-provider full E2E 连续两次无重复远程副作用；
- [ ] Windows CI green；
- [ ] Linux CI green 或明确平台限制；
- [ ] `docs/release-evidence-v1.2.1.md` 已生成；
- [ ] External provider smoke 状态逐一标明 VERIFIED / NOT RUN。

---

# 21. 最终验收

用户最终验收不要求一次拥有所有 Provider 的真实 API Key。

最小 Pilot：

1. 使用现有 v1.2 已验证 Provider 跑一组 synthetic/公开测试媒体；
2. 使用 Fake Provider 验证 routing/fallback/escalation；
3. 至少配置一个 OpenAI-compatible endpoint 做兼容性 smoke，如用户已有凭据；
4. 检查 `spt ai route explain`；
5. 检查 AI run audit；
6. 验证关闭 `allow_cloud` 后所有 remote route 均被阻止；
7. 验证 local primary 不会静默升级到 remote fallback；
8. 验证人工 Decision 不因模型切换而改变。

真实 100GB 源图库的 move/delete 仍不属于 v1.2.1 开发或验收的自动权限范围。

---

# 22. Codex Goal 语义

启动时无需再复制整个 PRD。

推荐短 Goal：

```text
基于已完成的 Smart-Photo-Triage v1.2.0，实现并验证
Smart-Photo-Triage_PRD_v1.2.1_Model-Routing_Goal.md
与 Smart-Photo-Triage_TDD_v1.2.1_Model-Routing_Goal.md。

采用 Goal-driven + TDD 模式自主持续推进。
内部阶段 Gate 通过后自动继续，不等待我逐阶段批准。
保持 v1.2 全部安全不变量和回归测试。
不得使用我的真实媒体或凭据做自动云测试。

只有真正 BLOCKER 才询问我。
达到全部 v1.2.1 Definition of Done 后生成 release evidence，
并报告 READY_FOR_FINAL_ACCEPTANCE_1_2_1。
```

---

# 23. 研究与技术依据备忘

截至 2026-08 的官方文档验证显示：

- Alibaba Cloud Model Studio 对 Qwen/Qwen-VL 提供 OpenAI-compatible 接口，迁移核心参数为 API Key、base URL、model；
- Anthropic Claude API 原生支持单图和多图视觉输入；
- OpenAI-compatible 生态可以显著降低接入不同厂商的重复代码，但能力差异仍必须通过 ProviderCapabilities 显式治理。

因此 v1.2.1 的工程重点应是“统一能力合同与安全路由”，而不是维护不断增长的厂商 if/else 列表。

参考：
- Alibaba Cloud Model Studio, OpenAI-compatible: https://www.alibabacloud.com/help/en/model-studio/compatibility-of-openai-with-dashscope
- Alibaba Cloud Model Studio, OpenAI-compatible Vision: https://www.alibabacloud.com/help/en/model-studio/qwen-vl-compatible-with-openai
- Anthropic Vision: https://docs.anthropic.com/zh-CN/docs/build-with-claude/vision
