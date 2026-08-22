# Smart-Photo-Triage v1.2.1 自主 TDD 与模型路由 Release Verification 规范

| 属性 | 内容 |
|---|---|
| 文档版本 | v1.2.1 |
| 配套 PRD | `Smart-Photo-Triage_PRD_v1.2.1_Model-Routing_Goal.md` |
| 前置基线 | Smart-Photo-Triage v1.2.0 |
| 执行模式 | Codex Goal Mode，自主推进 |
| 用户中间审批 | 默认不需要 |
| Hard Stop | `READY_FOR_FINAL_ACCEPTANCE_1_2_1` |
| 核心原则 | 用自动测试证明 Provider 可插拔、路由有界、隐私不可绕过、v1.2 无回归 |

> 本文不是实现步骤脚本，而是一组可执行工程契约。Codex 可自行决定模块划分、依赖和内部 API，但必须用测试、迁移证据和 E2E 证明行为满足 PRD。

---

# 1. Goal Mode 规则

## 1.1 Auto-continue

每个内部 Phase Gate 通过后自动进入下一阶段，不等待用户批准。

## 1.2 Self-repair

测试失败时：

1. 判断需求缺陷、实现缺陷、测试缺陷或环境缺陷；
2. 保留正确需求；
3. 修复根因；
4. 重新执行最小相关测试；
5. 执行必要 regression；
6. Gate green 后继续。

不得为了“让测试绿”降低隐私、安全或 backward compatibility 要求。

## 1.3 RED

新增可测试行为应优先：

```text
requirement
-> failing test
-> minimal implementation
-> green
-> refactor
-> regression
```

纯文档、CI wiring、无行为变化的迁移说明无需人为制造 RED。

## 1.4 Hard Stop

到达：

```text
READY_FOR_FINAL_ACCEPTANCE_1_2_1
```

后停止。

不得自行开始人物、事件、地点、向量检索等后续版本。

---

# 2. 全局 P0 回归原则

v1.2.1 的第一条 Gate 不是新功能，而是：

```text
v1.2 full regression green
```

任何 Provider Router 实现都不得破坏：

- source read-only；
- no hard delete；
- no overwrite；
- stale-plan protection；
- bundle atomicity；
- copy verify；
- crash resume；
- rollback；
- HUMAN > AI > RULE；
- no-cloud default；
- AI request privacy；
- deterministic plan；
- operation journal。

只要上述 P0 回归失败，后续 Phase 不得继续。

---

# 3. 测试分层

建议：

```text
unit
contract
integration
security
migration
routing
E2E fake-provider
external smoke optional
```

External smoke 永远不能成为无凭据环境下阻塞核心开发的必要条件。

---

# 4. 测试 Fixtures

禁止使用用户真实照片、真实家庭视频或真实 API 凭据。

至少提供：

```text
synthetic single image
synthetic two-image burst
synthetic multi-image burst
invalid image payload
oversized preview metadata
fake low-confidence result
fake high-confidence result
fake schema-invalid result
fake 429
fake timeout
fake 500
fake auth error
fake billing error
fake privacy-blocked error
fake capability mismatch
loopback endpoint stub
LAN endpoint stub
remote endpoint stub
redirect-to-remote stub
OpenAI-compatible mock endpoint
Anthropic mock endpoint
Gemini/default provider mock
```

所有 remote mock 默认只能指向测试 HTTP server，不访问真实公网。

---

# Phase A. Baseline Lock and Upgrade Skeleton

## A1. v1.2 Baseline

### T-A-001 v1.2 full regression

升级前记录：

```text
pytest full result
coverage
lint/static checks
```

必须 green。

### T-A-002 existing config loads

v1.2 单 Provider 配置可以被当前代码加载。

### T-A-003 existing DB snapshot opens

使用测试 DB snapshot 验证升级代码可读取 v1.2 DB。

### T-A-004 no media rescan required

升级 migration 后，已有 media records 数量和 identity 不变。

## A2. Migration Skeleton

### T-A-005 migration idempotent

v1.2 -> v1.2.1 migration 执行两次不会重复创建或破坏状态。

### T-A-006 human decisions preserved

升级前后的 HUMAN decision 数量和内容一致。

### T-A-007 plan/journal untouched

升级迁移不得修改已有 plan 和 operation journal 的语义数据。

## A Gate

- v1.2 full regression green；
- migration tests green；
- no destructive DB migration；
- auto-continue Phase B。

---

# Phase B. Provider Registry and Config Contract

## B1. Provider Registry

### T-B-001 unique provider id

重复 `provider_id` 必须配置失败。

### T-B-002 missing driver

Provider 无 driver 时失败。

### T-B-003 missing model

需要 model 的 Driver 未配置 model 时失败。

### T-B-004 secret by env only

配置中的 `api_key_env` 可以解析环境变量。

真实 secret 不应写回配置快照、DB 或日志。

### T-B-005 disabled provider

disabled Provider 不参与 route。

## B2. Backward Config

### T-B-006 old provider config normalization

旧 v1.2 配置被归一为：

```text
default provider
+ default ITEM_ANALYSIS route
+ default BURST_REVIEW route
```

具体内部结构可变，但行为等价。

### T-B-007 old allow_cloud semantics preserved

旧配置 `allow_cloud=false` 升级后仍为 false。

## B3. Invalid Reference

### T-B-008 route missing provider

Route 指向不存在 Provider 时 config validation 失败。

### T-B-009 fallback duplicate

同一 Provider 重复出现在 fallback chain 时拒绝或确定性去重，不能形成循环。

### T-B-010 self fallback

Provider 不能 fallback 到自己形成无限 loop。

## B Gate

- registry/config contract green；
- backward config green；
- auto-continue Phase C。

---

# Phase C. Provider Contract and Capabilities

## C1. Unified Contract

对以下 Driver 使用相同 contract test suite：

```text
fake
gemini/default
openai
anthropic
openai_compatible
```

### T-C-001 valid unified item result

每个 Driver adapter 的有效 mock response 都能归一到 v1.2 ItemReview Schema。

### T-C-002 valid unified burst result

每个支持 multi-image 的 Driver 都能归一到 BurstReview。

### T-C-003 invalid confidence

越界 confidence 被拒绝。

### T-C-004 invalid category

未知 category 不能静默进入业务状态。

### T-C-005 missing item id

无法关联 item 时失败该项。

### T-C-006 shuffled mapping

返回顺序变化仍按 item/group id 关联。

## C2. Capabilities

### T-C-007 item requires image

不支持 image 的 Provider 在 preflight 被过滤，不发送请求。

### T-C-008 burst requires multi-image

不支持 multi-image 的 Provider 不得执行 Burst Review。

### T-C-009 max images

Burst 图片数超过 Provider capability 时必须：

- 选择其他满足能力的 route candidate；
- 或明确失败/拆分，拆分必须不改变 Best Shot 语义。

Codex 不得为了通过测试随意截断而丢图。

### T-C-010 mime support

不支持的 preview mime 在请求前转换或拒绝，不能发送已知非法 payload。

### T-C-011 structured output capability

Provider 不支持 JSON Schema 时，可以使用受控 JSON object/text + 本地强校验方案。

业务层收到的最终对象必须同样通过 Pydantic/等价 Schema。

## C3. Probe

### T-C-012 probe uses synthetic media only

`spt ai probe` 不读取用户媒体库。

### T-C-013 probe no persistent business result

Probe 不写入正式 media AI result。

### T-C-014 probe capability version

Probe 生成/更新 capability profile 时要有 profile version/time。

## C Gate

- all provider contract mocks green；
- capability preflight green；
- no real network required；
- auto-continue Phase D。

---

# Phase D. Network Scope and Privacy Security

这是 v1.2.1 最重要的 P0 Phase。

## D1. Cloud Default

### T-D-001 allow_cloud false

remote Provider 配置存在但 `allow_cloud=false` 时，零远程请求。

### T-D-002 allow_lan false

LAN Provider 在 `allow_lan=false` 时零请求。

### T-D-003 loopback allowed

loopback Provider 可在不打开 allow_cloud 的条件下使用。

## D2. Endpoint Validation

### T-D-004 remote requires https

remote HTTP endpoint 默认拒绝。

### T-D-005 URL credentials rejected

`https://user:password@example.com` 一类 URL userinfo 被拒绝。

### T-D-006 loopback classification

`127.0.0.1`、`::1` 正确识别。

### T-D-007 fake hostname not loopback

仅名称看似 local 但解析为 remote 的 endpoint 不能被简单字符串规则误判为 loopback。

实现可选择更安全的 URL policy，但测试必须覆盖绕过风险。

## D3. Redirect

### T-D-008 localhost redirect remote blocked

```text
127.0.0.1
-> redirect
-> https://remote.example
```

在 `allow_cloud=false` 时不得跟随。

### T-D-009 remote redirect scope revalidated

如果允许 redirect，每一级重新验证 scope。

推荐实现可以直接默认关闭 redirect。

## D4. Request Privacy

对所有 Driver 运行同一 privacy contract：

### T-D-010 no raw media

只允许 v1.2 Preview/contact sheet。

### T-D-011 no absolute path

### T-D-012 no content hash

### T-D-013 no sidecar raw

### T-D-014 no SQLite payload

### T-D-015 no secret in request metadata outside auth

### T-D-016 log redaction

日志和 exception 不含：

```text
API key
Authorization header
base64 image
full request body
```

## D5. Fallback Privacy

### T-D-017 local to remote blocked without consent

local primary 失败后，即使 route 配置 remote fallback，只要 `allow_cloud=false` 就不能升级。

### T-D-018 remote fallback allowed with explicit consent

route 明确 + `allow_cloud=true` 后才可进入 remote fallback。

### T-D-019 privacy blocked never bypassed

`PRIVACY_BLOCKED` 错误不得通过换 Provider 绕过。

## D Gate

- privacy P0 all green；
- no-cloud regression green；
- redirect tests green；
- auto-continue Phase E。

---

# Phase E. Router, Fallback, Escalation

## E1. Deterministic Task Routing

### T-E-001 item route primary

ITEM_ANALYSIS 使用配置 primary。

### T-E-002 burst route primary

BURST_REVIEW 可使用不同 primary。

### T-E-003 deterministic order

相同 config/capabilities 下 fallback candidate order 稳定。

### T-E-004 disabled skipped

disabled provider 被跳过。

### T-E-005 capability mismatch skipped before network

能力不足不消耗远程 request。

## E2. Error Fallback

### T-E-006 429 fallback

primary 429 达到 bounded retry 后进入配置 fallback。

### T-E-007 timeout fallback

### T-E-008 5xx fallback

### T-E-009 network error fallback

### T-E-010 schema invalid bounded

Schema invalid 可以进行有限 retry/repair 或 fallback，但总 attempts 不超过上限。

## E3. Permanent Error

### T-E-011 auth no silent fallback

默认 AUTH_ERROR 不触发另一家 Provider。

### T-E-012 billing no silent fallback

### T-E-013 config no fallback

### T-E-014 privacy no fallback

这些错误必须明确可见。

## E4. Bounded Attempts

### T-E-015 max attempts

任何 task attempts 不超过配置上限。

### T-E-016 fallback cycle impossible

即使恶意/错误配置也不会无限循环。

### T-E-017 retry plus fallback total bounded

Provider 内重试和跨 Provider fallback 必须有全局 task 上限。

## E5. Confidence Escalation

### T-E-018 high confidence no escalation

### T-E-019 low confidence escalates once

### T-E-020 escalated result schema valid

### T-E-021 escalation result selection

明确记录 primary result 与 escalated result，effective AI result 的选择规则可预测。

### T-E-022 human decision survives escalation

已有 HUMAN decision 不因强模型结果改变。

### T-E-023 escalation cache hit

强模型已有 cache 时不重复调用。

## E Gate

- router/fallback/escalation green；
- bounded property proven；
- HUMAN precedence regression green；
- auto-continue Phase F。

---

# Phase F. Cache, Audit, Budget

## F1. Provider Cache Identity

### T-F-001 provider differs

provider_id 变化不能错误 cache hit。

### T-F-002 model differs

### T-F-003 endpoint identity differs

### T-F-004 capability profile version

如果能力配置会改变请求结构，profile version 必须进入适当 fingerprint。

### T-F-005 prompt/schema/preview regression

继续保持 v1.2 cache invalidation tests。

## F2. Route and Result Separation

### T-F-006 route priority change reuses provider result

仅调整 route 顺序时，已存在且仍有效的 provider-level result 可以复用。

### T-F-007 route execution new audit

新的 route run 仍生成 route decision 记录。

## F3. Audit

### T-F-008 attempt trace

每一次 attempt 记录 provider/model/status/reason/attempt index。

### T-F-009 cache trace

cache hit 明确可见。

### T-F-010 escalation trace

### T-F-011 fallback trace

### T-F-012 remote bytes estimate

远程 Preview 数据量有可解释估计。

### T-F-013 no sensitive audit data

audit 不保存禁止字段。

## F4. Budget Guard

### T-F-014 max requests

设置 request ceiling 后 Router 不得超过。

### T-F-015 remote MB ceiling

### T-F-016 cost ceiling if pricing configured

### T-F-017 fallback respects budget

fallback 不能绕过 budget。

### T-F-018 no pricing configured

无可靠价格时不得伪造精确成本。

## F Gate

- cache green；
- audit green；
- budget guard green；
- auto-continue Phase G。

---

# Phase G. CLI, Review UI, Docs

## G1. CLI

### T-G-001 providers redacts secret

### T-G-002 doctor config errors

### T-G-003 doctor no network default

### T-G-004 probe synthetic only

### T-G-005 route explain deterministic

### T-G-006 estimate cache aware

## G2. Review UI

### T-G-007 shows effective provider/model

### T-G-008 shows fallback/escalated state

### T-G-009 provider metadata read-only

v1.2.1 UI 不需要做完整 Provider 配置管理。

### T-G-010 HUMAN editing unchanged

原人工复核行为无回归。

## G3. Documentation

至少更新：

```text
README
configuration reference
provider guide
privacy guide
upgrade guide v1.2 -> v1.2.1
docs/implementation-status.md
docs/adr/ if needed
```

必须包含：

- OpenAI-compatible 并不保证 vision/schema 能力；
- `allow_cloud` / `allow_lan`；
- secret 配置；
- routing 示例；
- fallback/error 行为；
- external smoke 的限制。

## G Gate

- CLI tests green；
- UI regression green；
- docs current；
- auto-continue Phase H。

---

# Phase H. Full Upgrade E2E and Release Evidence

## H1. Fake Multi-Provider E2E

建立 synthetic dataset，完整运行：

```text
scan
-> preprocess
-> group
-> AI router
-> review
-> plan
-> dry-run
```

AI 部分至少模拟：

```text
cheap primary success
low confidence -> strong escalation
429 -> fallback
permanent auth error
local primary -> remote blocked
cache rerun
```

### T-H-001 full E2E first run

### T-H-002 second run no duplicate remote side effect

Fake Provider invocation count 证明 cache 和幂等。

### T-H-003 route audit complete

### T-H-004 HUMAN survives rerun

### T-H-005 v1.2 file pipeline unchanged

Planner/Executor/Recovery/Rollback 全套回归继续 green。

## H2. Cross-platform

至少：

```text
Windows CI
Linux CI
```

Provider mock tests 不依赖真实厂商网络。

## H3. External Smoke

如果存在相应凭据，可以分别执行：

```text
Gemini/default
OpenAI
Anthropic
one OpenAI-compatible provider
```

每个仅使用：

```text
generated single image
generated burst
```

无凭据：

```text
NOT RUN - no credentials
```

不得因此把核心版本判定为失败。

## H4. Release Evidence

生成：

```text
docs/release-evidence-v1.2.1.md
```

至少包含：

### Baseline

- v1.2 regression result；
- upgrade migration result。

### Provider Contract

- drivers tested；
- capability profiles；
- mock contract matrix。

### Router

- task route tests；
- fallback；
- escalation；
- bounded attempts。

### Privacy

- allow_cloud；
- allow_lan；
- redirect；
- no sensitive payload；
- local-to-remote guard。

### Cache / Budget

- cache reuse/invalidation；
- audit；
- request/MB budget。

### E2E

- first run；
- second run；
- invocation counts；
- no duplicate side effects。

### External Status

每个 Provider：

```text
VERIFIED
NOT RUN - no credentials
FAILED - reason
```

### Known Limitations

只写真实限制。

## H Gate

只有全部 non-external P0 和 regression green 时允许：

```text
READY_FOR_FINAL_ACCEPTANCE_1_2_1
```

否则：

```text
NOT_READY
```

并继续自主修复可解决问题。

---

# 5. P0 Safety Matrix

Release Evidence 必须逐项列出测试 ID 和结果。

| P0 | 必须证明 |
|---|---|
| P0-01 | v1.2 全部源文件安全不变量无回归 |
| P0-02 | remote Provider 在 allow_cloud=false 时零请求 |
| P0-03 | LAN Provider 在 allow_lan=false 时零请求 |
| P0-04 | local failure 不会静默升级 remote |
| P0-05 | redirect 不能绕过 network scope |
| P0-06 | raw media 永不进入 Provider |
| P0-07 | absolute path/hash/sidecar raw 永不进入 Provider |
| P0-08 | secret/request body 不进入日志和 audit |
| P0-09 | Router attempts 有界 |
| P0-10 | PRIVACY_BLOCKED 不可 fallback 绕过 |
| P0-11 | HUMAN decision 不被 reroute/escalation 覆盖 |
| P0-12 | v1.2 DB/config 可安全升级 |
| P0-13 | 旧 AI cache 不被错误丢失或跨模型误复用 |
| P0-14 | budget limit 不可被 fallback 绕过 |

---

# 6. Coverage Gate

最低建议：

```text
overall line coverage >= v1.2 baseline and >= 85%
router/provider/config/security new modules >= 90%
privacy and routing P0 branches must have explicit tests
```

不能为了覆盖率使用无意义断言。

复杂 HTTP adapter 的厂商 SDK 内部不计入项目 coverage，但 adapter 分支逻辑必须覆盖。

---

# 7. 性能与负载测试

不以网络真实延迟作为 CI Gate。

使用 Fake Provider 测试：

```text
10k pending items route planning
provider selection throughput
audit insert batching
cache lookup
memory growth
```

目标是证明 Router 不会把原本可流式执行的 v1.2 AI pipeline 变成一次性全库内存结构。

Release Evidence 记录：

```text
tasks/sec for routing
peak RSS
cache hit rate
provider attempt counts
audit row count
```

---

# 8. BLOCKER 定义

只有以下情况停止询问用户：

- v1.2 已验收 P0 行为与 v1.2.1 需求无法同时满足；
- 必须不可逆重写现有 DB；
- 必须使用真实用户媒体才能继续；
- 必须拥有某真实 API Key 且 mock/fake 无法覆盖开发；
- 引入的新 SDK/模型依赖存在高风险许可证冲突；
- 发现自定义 endpoint 设计会无法避免隐私边界绕过。

普通 SDK 差异、接口错误、测试失败、厂商 mock 构造不属于 BLOCKER。

---

# 9. Final Goal

Codex 的持续 Goal：

```text
Deliver Smart-Photo-Triage v1.2.1 as a backward-compatible model-agnostic
multi-provider upgrade on top of the verified v1.2 baseline.

Implement and verify Provider Registry, ProviderCapabilities, native/default
adapters, a generic OpenAI-compatible adapter, per-task deterministic routing,
bounded fallback, low-confidence escalation, network-scope privacy gates,
cache correctness, auditability, optional budget guards, CLI observability,
safe migration, and complete v1.2 regression.

Continue autonomously through all internal phases. Do not ask for routine
implementation decisions. Do not use real user media or credentials for
automated tests.

The work is complete only when all non-external P0 tests and v1.2 regressions
are green, the multi-provider synthetic E2E succeeds twice without duplicate
side effects, release evidence is current, and the final verdict is:

READY_FOR_FINAL_ACCEPTANCE_1_2_1
```

---

# 10. 最终原则

本 TDD 的目标仍然是：

```text
PRD invariants
+ executable contract tests
+ security tests
+ migration tests
+ CI
+ E2E
+ release evidence
```

管理 Codex，而不是由用户逐阶段管理 Codex。

用户最终只需要判断：

```text
v1.2.1 是否在不牺牲 v1.2 安全性的前提下，
真正实现了可配置、多 Provider、可路由、可审计的多模态能力。
```
