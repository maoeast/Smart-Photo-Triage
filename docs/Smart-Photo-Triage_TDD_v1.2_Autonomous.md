# Smart-Photo-Triage 自主 TDD 与 Release Verification 规范

| 属性 | 内容 |
|---|---|
| 文档版本 | v1.2.0 |
| 配套 PRD | `Smart-Photo-Triage_PRD_v1.2_Goal.md` |
| 执行模式 | Codex Goal Mode，自主推进 |
| 用户中间审批 | 默认不需要 |
| 最终目标 | 自动完成实现、验证、修复和 Release Evidence，交用户最终验收 |

> 本文不是“告诉 Codex 每个函数怎么写”的施工清单，而是一组可验证的工程契约。Codex 可以自行选择实现路径，但必须用测试和可复现证据证明最终行为符合要求。

---

## 1. 自主 TDD 工作模型

每个内部阶段都必须执行：

```text
Understand requirement
-> add/adjust test
-> observe meaningful RED
-> minimal implementation
-> GREEN
-> refactor if useful
-> targeted regression
-> full regression
-> update implementation status
-> auto-continue
```

“先写 failing test”适用于新的可测试行为。

以下场景不要求人为制造 RED：

- 纯文档；
- CI 配置；
- 已有测试已经能够准确覆盖新修复；
- 仅重构且行为不变。

禁止通过语法错误、错误 import、无意义断言伪造 RED。

---

## 2. Goal Mode 自主规则

### 2.1 Auto-continue

每个阶段 Gate 通过后，Codex 自动进入下一阶段，不等待用户批准。

### 2.2 Self-repair

测试失败时：

1. 判断是产品缺陷、测试缺陷还是环境缺陷；
2. 保留正确需求；
3. 修复错误实现或错误测试；
4. 再运行 targeted tests；
5. 再运行 regression suite；
6. 继续。

不得仅为了“变绿”而削弱安全断言。

### 2.3 BLOCKER

只有 PRD 定义的 BLOCKER 可以暂停。

### 2.4 Durable progress

每个阶段完成后更新：

```text
docs/implementation-status.md
```

格式至少包含：

```text
Current phase
Gate result
Tests added
Known limitations
ADR changes
Next phase
Blockers
```

---

## 3. 测试层级

测试至少分为：

```text
Unit
Integration
E2E
Safety Regression
Fault Injection
Performance Smoke
External Optional
```

### 3.1 Unit

默认无网络、无真实媒体库、无真实用户目录。

### 3.2 Integration

可以调用：

- SQLite；
- 本地临时文件系统；
- ExifTool；
- FFmpeg；
- 本地 HTTP Review UI。

环境缺少外部二进制时应明确 skip，并由 CI/doctor 负责另一个层级的验证。

### 3.3 External Optional

真实视觉 API 测试标记为：

```text
external
```

默认 CI 不因缺少 API key 失败。

如果 Release 环境存在凭据，Release Evidence 应执行至少一个真实 Provider smoke test。

---

## 4. 测试夹具原则

自动测试不使用真实家庭照片作为仓库 fixture。

优先使用：

- Pillow 动态生成图片；
- 明确许可的小型 metadata 样例；
- 程序化生成短视频；
- dummy AAE/XMP/JSON；
- 临时目录；
- Fake Vision Provider。

必须覆盖以下 fixture 语义：

```text
same content two paths
same size different content
same timestamp different image
missing EXIF
filename datetime
unknown timezone
rotated EXIF orientation
HEIC image
Live Photo pair
sidecar set
short video
medium video
long video
corrupt image
corrupt video
burst positive
burst visual negative
false chain burst
Windows reserved filename
case-insensitive collision
Unicode filename
output nested in source
source nested in output
symlink/junction candidate
partial target
modified target after apply
stale plan source changed
concurrent apply lock
bundle partial failure
```

---

## 5. CI 基线

推荐 GitHub Actions 或等价 CI。

最低 matrix：

```text
Windows + supported Python
Linux + supported Python
```

Windows 是 P0 平台，不得只在 Linux CI 上宣布 Release Candidate。

CI 默认运行：

```text
lint
static checks if configured
unit
integration where dependencies available
safety regression
E2E fake-AI
coverage
```

External AI 单独触发。

---

# Phase A. Engineering Baseline

## A1. 目标

建立最小、可持续的工程骨架，不实现照片业务。

## A2. 必须证明

### T-A-001 CLI help

```text
spt --help exits 0
```

### T-A-002 default config

最小配置可加载，且默认：

```text
allow_cloud = false
```

### T-A-003 workspace idempotent init

重复初始化同一 workspace 不失败、不破坏已有状态。

### T-A-004 DB migration idempotent

初始化两次得到相同可用 schema。

### T-A-005 clean install smoke

从干净虚拟环境按照 README 可以安装并运行 CLI help。

## A3. Gate

- tests green；
- lint green；
- package imports clean；
- Windows path smoke green；
- implementation status updated。

Gate 通过后自动进入 Phase B。

---

# Phase B. Scan, Identity, Metadata, Bundle

## B1. 文件身份

### T-B-001 same content, two paths

Given：

```text
/a/photo.jpg
/b/copy.jpg
```

内容相同。

Expected：

```text
media_item count = 2
content_sha256 equal when hashed
```

### T-B-002 same path rescanned

未变化文件二次扫描：

- 不新增媒体实例；
- last_seen 可更新；
- 业务状态不漂移。

### T-B-003 missing source

外部删除源文件后重新扫描：

- DB 历史不直接删除；
- 标记 source absent。

### T-B-004 symlink default

默认不跟随 symlink/junction 跳出 source scope。

### T-B-005 output exclusion

output/workspace 位于 source 下时，系统要么拒绝配置，要么自动安全排除，不能把自己输出再次摄入。

## B2. Metadata

### T-B-006 time precedence

验证时间来源优先级。

### T-B-007 unknown timezone

无 offset 的时间不得被错误转换成 UTC。

### T-B-008 filename fallback

只有匹配受支持时间模式时才使用 filename datetime，并记录低于 EXIF 的 source/confidence。

### T-B-009 corrupt metadata

metadata 解析失败只影响单项。

## B3. Bundle

### T-B-010 Live Photo

```text
IMG_0001.HEIC
IMG_0001.MOV
```

在规则满足时进入同一 bundle。

### T-B-011 Live Photo + AAE

AAE 跟随同一逻辑资产。

### T-B-012 ambiguous bundle

存在多个可能 companion 时不得静默随机绑定，必须 deterministic 且可以产生 warning。

### T-B-013 missing companion

缺失 MOV/sidecar 不导致主文件被删除或 scan abort。

## B4. Gate

- 全部 B P0 green；
- 重复扫描 deterministic；
- 10k synthetic metadata scan smoke 无明显内存线性爆炸；
- regression green。

自动进入 Phase C。

---

# Phase C. Preview, Duplicate, Burst, Quality

## C1. Preview

### T-C-001 orientation

EXIF orientation 后 preview 视觉方向正确。

### T-C-002 HEIC preview

可生成 HEIC preview。

### T-C-003 deterministic preview fingerprint

同一源和 preview version 生成相同 fingerprint。

### T-C-004 corrupt image

单个损坏图片进入明确 failed 状态，batch 继续。

### T-C-005 video contact sheet sample count

验证短、中、长视频的默认抽帧数。

### T-C-006 corrupt video

不终止其他视频。

### T-C-007 bounded memory

视频处理不将完整视频解码到内存。

## C2. Exact Duplicate

### T-C-008 exact duplicate positive

同 size + 同 SHA -> 同 duplicate group。

### T-C-009 same size different bytes

不得误判 duplicate。

### T-C-010 duplicate no delete

建立重复组不会产生任何 source 文件删除或移动。

## C3. Burst

### T-C-011 burst positive

时间接近且视觉相似的一组进入同一 burst。

### T-C-012 time close visual different

不得仅因时间接近合并。

### T-C-013 visual similar time far

明显不在 burst time window 的项目不得仅凭 hash 接近自动成为同一 burst。

### T-C-014 false chain

当：

```text
A near B
B near C
A not near C
```

算法不得无条件链式扩张。

### T-C-015 deterministic grouping

相同输入顺序变化后，规范化 group result 一致。

### T-C-016 complexity guard

大量媒体时不得退化为全库成对视觉比较。

## C4. Quality

### T-C-017 local score non-destructive

无论质量分多低，本地指标本身都不能产生 delete/move action。

## C5. Gate

- C P0 green；
- preview cache/resume green；
- grouping deterministic；
- 1k generated media smoke report；
- regression green。

自动进入 Phase D。

---

# Phase D. Vision AI, Structured Output, Cache

## D1. Fake Provider first

所有核心逻辑必须在 Fake Provider 下完整可测。

## D2. Schema tests

### T-D-001 invalid confidence

```text
confidence < 0 or > 1
```

必须拒绝。

### T-D-002 invalid category

未知 category 必须拒绝或映射到明确 fallback，不能静默写入未知业务状态。

### T-D-003 missing item id

结果不可关联时必须失败该项。

### T-D-004 shuffled response mapping

Provider 返回顺序变化时仍按 item_id 正确映射。

### T-D-005 low confidence reject

```text
REJECT_CANDIDATE + low confidence
=> REVIEW
```

### T-D-006 AI cannot delete

AI 结果无法直接创建文件删除操作。

## D3. Privacy tests

### T-D-007 request excludes source absolute path

AI request 不包含真实绝对路径。

### T-D-008 request excludes content hash

不发送 content SHA。

### T-D-009 request excludes sidecar raw content

不发送 AAE/XMP/JSON 原文。

### T-D-010 cloud disabled by default

未显式允许时，真实 Provider 不发起网络调用。

## D4. Cache tests

### T-D-011 cache hit

完全相同 fingerprint 不重复调用 Provider。

### T-D-012 prompt invalidation

prompt version 变化必须重新分析。

### T-D-013 schema invalidation

schema version 变化必须重新分析。

### T-D-014 model/provider invalidation

provider/model 变化必须重新分析。

### T-D-015 preview invalidation

preview version/fingerprint 变化必须重新分析。

## D5. Retry tests

### T-D-016 retry transient

429/5xx/timeout 可有限重试。

### T-D-017 no retry permanent

invalid key/schema programming error 等永久错误不得无限重试。

### T-D-018 split batch

batch 某项持续失败时，系统能够缩小或隔离失败，避免永久拖死整个库。

## D6. External smoke

如果有 API key：

- 1 张公开/生成测试图；
- 1 个 synthetic burst；
- 验证结构化响应和隐私 request builder。

没有 API key 时记录：

```text
EXTERNAL_NOT_RUN
```

但不阻止 Fake-AI 主流程开发。

## D7. Gate

- D P0 green；
- no-network default proven；
- cache invalidation green；
- retry bounded；
- regression green。

自动进入 Phase E。

---

# Phase E. Review UI and Human Decisions

## E1. Local server safety

### T-E-001 loopback only

Review 服务默认只能绑定 `127.0.0.1` 或等价 loopback。

### T-E-002 no CDN

HTML/CSS/JS 不依赖外部 CDN。

### T-E-003 offline load

断网环境仍可加载 UI 和已有 thumbnail。

## E2. Decision behavior

### T-E-004 human overrides AI

AI：

```text
REJECT_CANDIDATE
```

Human：

```text
KEEP
```

最终 effective decision 必须 KEEP。

### T-E-005 AI rerun does not erase human

重新分析后 HUMAN decision 仍保持。

### T-E-006 category edit persists

页面刷新后人工 category 仍存在。

### T-E-007 disposition edit persists

页面刷新后人工 disposition 仍存在。

### T-E-008 group views

Exact duplicate 和 burst group 可以稳定展示成员。

### T-E-009 pagination

1000+ mock items 时只渲染当前页或虚拟区，而不是创建全量卡片。

### T-E-010 static export optional

如果实现 dashboard export，它必须只读、安全、无 CDN。

## E3. Browser verification

优先使用可自动化的浏览器测试验证至少：

```text
open list
filter category
open lightbox
change decision
reload
verify persistence
```

如果当前环境无法自动浏览器测试，至少提供 JS/unit integration tests，并在 Release Evidence 中明确人工 UI smoke 状态。

## E4. Gate

- local-only security green；
- human precedence green；
- persistence green；
- pagination green；
- regression green。

自动进入 Phase F。

---

# Phase F. Planner and Preflight

## F1. Determinism

### T-F-001 plan deterministic

同 DB/config 生成两次，canonical plan 内容一致。

### T-F-002 ordering independent

DB 查询返回顺序变化不应导致目标路径漂移。

## F2. Filename safety

### T-F-003 Windows reserved name

处理：

```text
CON PRN AUX NUL COM1.. LPT1..
```

### T-F-004 illegal chars

处理 Windows 非法字符和控制字符。

### T-F-005 trailing dot/space

不得生成 Windows 不安全结尾。

### T-F-006 same second same description

两个媒体不会产生相同目标。

### T-F-007 case insensitive collision

`Photo.jpg` 和 `photo.jpg` 在 Windows 目标语义下不能冲突覆盖。

### T-F-008 Unicode normalization collision

不同 Unicode 表示导致规范化同名时仍可生成唯一稳定目标。

## F3. Time safety

### T-F-009 no reliable captured_at

进入 `_时间待确认` 或等价明确状态。

## F4. Bundle

### T-F-010 bundle path consistency

Live Photo HEIC/MOV/AAE 位于同一逻辑目录并保持关联命名。

## F5. Stale plan

### T-F-011 source content changed after plan

Plan 后源文件内容改变，preflight 必须拒绝该 entry。

### T-F-012 source missing

不得继续假装可执行。

## F6. Path/config safety

### T-F-013 output inside source

危险 source/output 布局必须被拒绝或明确安全处理。

### T-F-014 source inside output

同上。

### T-F-015 unwritable output

Preflight 阻止正式 apply。

### T-F-016 existing incomplete transaction

系统不能忽略旧的不一致事务直接启动新的危险 apply。

## F7. Gate

- Planner tests green；
- stale plan detection green；
- Windows path suite green；
- preflight safety green；
- regression green。

自动进入 Phase G。

---

# Phase G. Executor, Recovery, Rollback

这是全项目最高风险阶段。

## G1. Dry Run

### T-G-001 dry-run zero mutation

执行前后 source/target 文件系统内容一致。

## G2. No Overwrite

### T-G-002 target different hash

不得覆盖。

### T-G-003 target same hash

可识别 ALREADY_PRESENT 或等价状态，不生成第二个副本。

## G3. Copy

### T-G-004 copy hash verify

只有目标 SHA 正确才 DONE。

### T-G-005 partial target cleanup/resume

存在上次 `.partial` 时按 journal 安全处理。

### T-G-006 fsync/finalization order

通过可测试 abstraction/fault injection 证明 finalize 不早于验证所需步骤。

## G4. Move

### T-G-007 same filesystem move no overwrite

目标已存在不同内容时拒绝。

### T-G-008 cross filesystem sequence

必须验证 target 后才允许删除 source。

## G5. Crash fault injection

在以下边界注入故障：

```text
after PREPARED
after partial copy
after target verify
before source delete
after source delete
before journal DONE
```

每个故障必须有可解释恢复状态。

### T-G-009 crash after PREPARED

源保持可用，可 resume。

### T-G-010 crash after verified copy before delete

恢复时不得重复复制，也不得丢源。

### T-G-011 crash after source delete before DONE

doctor/resume 能根据 target hash 和 journal 收敛到正确状态。

## G6. Bundle atomic semantics

### T-G-012 bundle partial failure

HEIC 成功而 MOV 失败时，bundle 不得整体 DONE。

系统必须能 resume 或 rollback 到可解释状态。

## G7. Concurrency

### T-G-013 second apply blocked

两个 apply 不能同时持有同一 workspace 变更锁。

### T-G-014 stale lock recovery

进程异常退出留下锁时，doctor 可以安全识别，不得永久锁死。

## G8. Idempotency

### T-G-015 same plan twice

重复执行同一 plan 不产生：

```text
(1)
(2)
duplicate copy
renaming drift
```

## G9. Rollback

### T-G-016 normal copy rollback

按产品定义恢复到合理状态，且不误删不相关文件。

### T-G-017 normal move rollback

原路径恢复，内容 hash 不变。

### T-G-018 modified target

用户修改 target 后，rollback 拒绝自动删除。

### T-G-019 rollback rerun

重复 rollback 幂等或明确报告 already rolled back。

## G10. Permission and disk errors

### T-G-020 permission denied

单项错误进入可恢复 failed，不把不完整文件标 DONE。

### T-G-021 insufficient capacity preflight

如果空间检查可用，应在 copy 之前尽早阻断明显不足。

## G11. Gate

G Gate 必须全部通过才能进入 Release E2E。

安全核心不得用 skip 代替 pass。

自动进入 Phase H。

---

# Phase H. E2E, Performance, Release Candidate

## H1. Deterministic synthetic E2E library

至少构建：

```text
20+ images
2 exact duplicates
1 same-size different-content pair
1 burst with >=4 items
1 false-chain burst case
1 HEIC/MOV bundle
1 sidecar
2 videos
1 corrupt image
1 corrupt video
1 missing EXIF item
1 Windows filename edge case
```

使用 Fake AI。

## H2. Full flow

必须自动运行：

```text
init
scan
preprocess
group
analyze fake
review decision injection
plan
preflight
dry-run
apply copy
verify
doctor
rollback
```

### T-H-001 source unchanged through analysis

Scan 到 Plan 阶段 source byte-for-byte 不变。

### T-H-002 expected outputs

Apply copy 后目标数量和 hash 正确。

### T-H-003 human decision honored

Human override 反映到 Plan 和 target。

### T-H-004 bundle preserved

Bundle 目标关系正确。

### T-H-005 failures isolated

损坏媒体不影响其他项完成。

## H3. Two-pass idempotency

完整流程第二次执行：

- 不产生新媒体实例重复；
- 不产生重复 AI 调用；
- 不产生重复 target；
- 不产生目标名漂移；
- Plan canonical result稳定。

## H4. 1k media smoke

生成或准备至少 1000 项安全测试媒体/metadata。

收集：

```text
scan throughput
preview throughput
peak RSS
SQLite size
cache hit behavior
group counts
failed items
```

不设不合理的跨硬件固定秒数 Gate，但必须证明架构没有明显 O(n²) 全库行为和无界内存增长。

## H5. Optional real Provider smoke

如果环境配置真实 AI key：

- 执行少量公开/合成图片；
- 不使用用户家庭真实照片作为自动 smoke；
- 验证 Schema、cache、retry、privacy builder。

## H6. Clean install

在干净环境：

```text
install
spt doctor
spt --help
synthetic e2e
```

必须可复现。

---

## 6. Coverage Gate

总体建议：

```text
line coverage >= 85%
```

安全核心：

```text
executor >= 95%
planner/preflight >= 90%
journal/recovery critical branches strongly covered
```

Coverage 不是唯一质量指标。

以下测试属于 P0，缺少任何一项不得宣布 Release Candidate：

```text
same content two paths
source read-only before apply
no-overwrite
stale-plan detection
copy hash verify
cross-volume delete-after-verify
crash resume at multiple boundaries
bundle partial failure
concurrent apply lock
rerun idempotent
rollback modified target
human overrides AI
AI cannot delete
cloud disabled by default
cache version invalidation
false-chain burst
deterministic plan
Windows filename collision
```

---

## 7. Safety Test Coding Rule

为了可靠模拟崩溃，不应依赖真正杀死随机进程来获得覆盖。

推荐让 Executor 的危险边界可以注入测试 fault，例如：

```text
AFTER_PREPARED
AFTER_TEMP_COPY
AFTER_VERIFY
AFTER_FINALIZE
AFTER_SOURCE_DELETE
BEFORE_DONE
```

具体实现方式由 Codex 自行决定。

要求：

- 不污染正常业务接口；
- 不在生产默认启用；
- 可以确定性重放故障；
- 每个 journal state 都能被测试覆盖。

---

## 8. Test Quality Rules

测试必须：

- 验证可观察行为，不锁死无关内部实现；
- 路径测试显式考虑 Windows；
- 使用临时目录；
- 不能访问真实用户 Photo 根目录；
- 不要求网络才可跑核心测试；
- 对时间使用可控 clock 或宽松稳定断言；
- 对随机 ID 使用注入或规范化比较；
- 对 Plan 使用 canonical serialization；
- 对 AI 使用 Fake Provider；
- 对 fault 使用确定性注入。

禁止：

- 为了 coverage 写无意义断言；
- 把 P0 安全测试全部 mock 到没有真实文件 IO；
- 用 `time.sleep()` 长等待模拟正确性；
- 测试失败后删除测试规避问题；
- 通过修改预期值适配错误实现。

---

## 9. Dependency Verification

Codex 可自主选择轻量依赖，但必须在 Release 前检查：

- dependency tree；
- 许可证；
- 未使用依赖；
- 是否存在明显更简单标准库方案；
- 是否引入网络服务；
- 是否引入大型 native runtime。

重大偏离写 ADR。

---

## 10. Static and Quality Checks

至少配置：

```text
formatter/linter
pytest
coverage
```

类型检查可以采用 mypy、pyright 或等价方案，是否作为 hard gate 由 Codex 根据代码结构决定。

安全关键路径的类型必须清晰，不允许大量 `Any` 掩盖路径、journal state 和 AI schema 错误。

---

## 11. Release Evidence

Codex 完成全部阶段后必须生成：

```text
docs/release-evidence.md
```

内容至少包括：

### 11.1 Build Summary

- 实现版本；
- Python 版本；
- 平台；
- 主要依赖；
- 外部工具。

### 11.2 Test Summary

- total tests；
- passed；
- skipped；
- external not run；
- coverage；
- Windows CI；
- Linux CI。

### 11.3 P0 Safety Matrix

逐项：

```text
Test ID
Risk
Result
Evidence command
```

### 11.4 E2E

- 第一次 E2E；
- 第二次 E2E；
- idempotency；
- rollback；
- crash recovery。

### 11.5 Performance Smoke

- 1k dataset；
- throughput；
- peak RSS；
- 缓存行为；
- 失败项数量。

### 11.6 Known Limitations

只写真实限制。

### 11.7 External AI Status

明确：

```text
VERIFIED
```

或：

```text
NOT RUN - no credentials
```

### 11.8 Final Verdict

只能是：

```text
READY_FOR_FINAL_ACCEPTANCE
```

或：

```text
NOT_READY
```

---

## 12. Release Candidate Hard Stop

Codex 可以自主从 Phase A 连续工作到 Phase H。

但到达：

```text
READY_FOR_FINAL_ACCEPTANCE
```

后必须停止。

不得自行：

- 对真实用户 100GB 图库执行 move；
- 删除用户媒体；
- 将用户真实家庭图片上传到云端做“最终验收”；
- 把 Release Candidate 自动当成用户已经验收。

此处开始由用户执行 PRD 第 30 节最终验收。

---

## 13. Goal Mode 启动时建议的验收判据

Codex 的 Goal 不应写成：

```text
Implement the PRD.
```

应写成可验证目标：

```text
Deliver a Smart-Photo-Triage release candidate that satisfies the PRD and this TDD specification.

Continue autonomously through all internal phases, and do not stop for ordinary implementation decisions. The user will only perform final acceptance.

The work is complete only when all non-external automated tests pass, every P0 safety invariant is covered and green, Windows and Linux CI pass, coverage gates pass, the synthetic full E2E pipeline succeeds twice without duplicate side effects, crash fault-injection and rollback suites pass, a 1000-item smoke benchmark has been recorded, documentation is current, and docs/release-evidence.md reports READY_FOR_FINAL_ACCEPTANCE.

Do not perform destructive operations on real user media during development. If a genuine BLOCKER under the PRD occurs, stop and report the exact blocker, impact, and recommended resolution. Otherwise make reasonable engineering decisions, document material deviations as ADRs, self-repair failures, and keep moving toward the verified goal.
```

---

## 14. 最终原则

本 TDD 的目标不是让用户管理 Codex，而是让自动验证管理 Codex。

工程控制面从：

```text
user approves every step
```

转变为：

```text
PRD invariants
+ executable tests
+ CI
+ deterministic gates
+ release evidence
```

用户最终只判断一件事：

```text
这个 Release Candidate 在真实 Pilot 上是否满足实际使用需求。
```
