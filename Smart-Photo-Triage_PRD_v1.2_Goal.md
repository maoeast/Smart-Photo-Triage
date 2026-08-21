# Smart-Photo-Triage 本地智能相册整理与可视化系统 PRD

| 属性 | 内容 |
|---|---|
| 文档版本 | v1.2.0 Goal-Driven Baseline |
| 修订日期 | 2026-08-20 |
| 基线来源 | v1.1.0 工程安全基线，重构为 Codex Goal Mode 自主实施版本 |
| 产品形态 | 本地优先 CLI + 本地复核 Web UI |
| 主要平台 | Windows 10/11，兼容 Linux/WSL2 |
| 默认实现语言 | Python 3.11+ |
| 数据存储 | SQLite |
| 核心目标 | 对 100GB+ 混杂照片和视频完成索引、重复候选、连拍精选、AI 分类、人工复核、确定性整理、安全落盘与恢复 |
| 开发模式 | Codex Goal Mode 长任务自主实施，内部阶段化验证，用户仅做最终验收 |

> 本 PRD 定义产品目标、业务行为、数据安全边界、核心数据合同和最终验收条件。它不规定 Coding Agent 每一步应如何编码，也不内嵌单轮执行 Prompt。具体测试与自主开发门禁见《Smart-Photo-Triage_TDD_v1.2_Autonomous.md》。

---

## 1. 产品结论

Smart-Photo-Triage 是一个单机、本地优先的媒体整理工具，不是另一个 Immich、PhotoPrism 或 Google Photos。

系统重点解决五件事：

1. 在不破坏原始图库的情况下建立可靠索引；
2. 找出完全重复、近重复和连拍候选；
3. 用低成本视觉 AI 帮助分类、描述和连拍精选；
4. 用本地可视化界面让用户快速纠正 AI；
5. 将最终决定转换为可验证、可恢复、不可覆盖用户文件的整理事务。

首要优先级：

```text
数据安全
> 正确性
> 可恢复性
> 幂等性
> 可验证性
> 用户体验
> 性能
> 开发速度
```

---

## 2. Goal Mode 开发治理

### 2.1 开发策略

本项目采用：

```text
一个持久 Goal
+ Codex 自主拆解里程碑
+ 每个里程碑自动验证
+ Gate 通过后自动继续
+ 最终生成 Release Candidate
+ 用户只做最终验收
```

不采用：

```text
M0 完成 -> 等用户批准
M1 完成 -> 等用户批准
M2 完成 -> 等用户批准
...
```

也不采用：

```text
一次性生成全部代码 -> 最后补测试
```

对用户而言是一轮长任务。对工程内部而言仍然必须分阶段。

### 2.2 Codex 可自主决定的事项

在不违反本 PRD 的前提下，Codex 可以自主：

- 拆解工作包和实现顺序；
- 选择模块内部实现方式；
- 重构已有代码；
- 新增或删除内部模块；
- 选择标准库或轻量依赖；
- 调整数据访问方式；
- 调整测试组织结构；
- 调整性能实现；
- 修改内部 API；
- 添加开发脚本、CI 和诊断工具；
- 修复测试发现的问题；
- 在多个可行方案中选择代码更少、风险更低的一种。

只要满足最终行为、约束、数据合同和 TDD Gate，不要求逐字实现本 PRD 中的示例伪代码。

### 2.3 架构偏离规则

本 PRD 中标为“推荐”的技术选择不是绝对命令。

如果 Codex 判断存在更简单或更可靠的实现，可以自行调整，但必须同时满足：

1. 不降低数据安全；
2. 不增加明显部署复杂度；
3. 不引入不必要的大型基础设施；
4. 自动测试可以证明行为等价或更好；
5. 在 `docs/adr/` 写简短 ADR，记录原因、收益和风险。

### 2.4 必须停止并报告 BLOCKER 的情况

只有以下情况允许中断长任务等待用户：

- PRD 存在无法通过合理工程判断消解的业务冲突；
- 需要真实用户凭据且没有替代 Fake/Mock；
- 需要向外部系统执行不可逆写操作；
- 需要对真实源图库执行 move、delete 或覆盖；
- 发现源数据可能已损坏且继续执行存在扩大损失的风险；
- 新方案需要引入与本地优先目标冲突的服务型基础设施；
- 发现许可证风险可能影响项目使用方式。

普通实现选择、依赖版本、小型重构、测试失败不构成 BLOCKER。Codex 应自行修复并继续。

---

## 3. 用户责任边界

### 3.1 开发阶段

用户不需要：

- 审批每个 milestone；
- 审批普通依赖；
- 选择每个内部算法；
- 阅读每次单元测试；
- 指导每个代码文件如何实现。

### 3.2 用户只负责最终验收

Codex 完成 Release Candidate 后，用户执行最终验收流程：

1. 查看 Release Evidence；
2. 对 500 到 1000 个真实媒体的 Pilot 副本运行；
3. 抽查分类、重复组和连拍结果；
4. 验证本地复核 UI；
5. 执行 copy 模式；
6. 验证 rollback；
7. 决定是否允许在完整真实图库上使用。

正式对真实源图库执行 `move` 永远属于用户运行时决定，不属于 Codex 开发自主权。

---

## 4. 用户问题

本地约有 100GB+、跨度多年的媒体文件，常见来源包括：

- 儿童成长和家庭生活；
- 家庭聚会；
- 旅行和风景；
- 工作文档、白板、产品照片；
- 手机截图、备忘和聊天截图；
- 高频连拍；
- 重复导出；
- HEIC + MOV Live Photo；
- AAE/XMP/JSON sidecar；
- 闭眼、严重虚焦、黑屏、误触短视频等低价值素材。

用户当前难以快速回答：

- 哪些文件完全重复？
- 哪些只是视觉相近？
- 哪些属于同一次连拍？
- 连拍中哪一张最值得保留？
- 哪些属于家庭、旅行、工作、截图？
- 哪些只是疑似废片？
- 哪些 AI 判断需要人工纠正？
- 如何在不覆盖、不误删原始文件的情况下批量整理？

---

## 5. MVP 成功结果

MVP 必须让用户完成以下闭环：

```text
Source Library
    ↓
Read-only Scan
    ↓
Metadata + Preview
    ↓
Exact Duplicate + Burst Candidates
    ↓
Optional Vision AI
    ↓
Local Review UI
    ↓
Immutable Plan
    ↓
Preflight / Dry-run
    ↓
Copy or Move + Verify
    ↓
Resume / Rollback
```

用户最终应可以：

1. 指定一个或多个源目录；
2. 中断并继续扫描；
3. 浏览扫描统计；
4. 查看精确重复组；
5. 查看连拍组和 Best Shot 建议；
6. 查看 AI 分类、标签、简短描述、置信度；
7. 修改分类和处置状态；
8. 生成确定性整理计划；
9. 查看预执行风险；
10. 以 copy 或 move 模式执行；
11. 查看每个文件操作状态；
12. 从崩溃中恢复；
13. 对事务执行安全 rollback。

---

## 6. MVP 非目标

以下内容不进入 MVP：

- 人脸身份识别；
- 人物自动命名；
- 云端照片库；
- 多用户协作；
- 手机 App；
- GPS 地图产品；
- 照片编辑器；
- RAW 调色；
- 自动 hard delete；
- 向量数据库；
- Elasticsearch/OpenSearch；
- Redis；
- Docker 服务集群；
- 独立微服务体系；
- 复杂相册故事生成。

---

## 7. 不可违反的安全不变量

以下是产品级硬约束。实现细节可以变化，这些约束不能变化。

### INV-01 源库默认只读

Scan、Preprocess、Group、AI、Review、Plan 阶段不得修改源媒体。

### INV-02 不自动 hard delete

系统不提供基于 AI 结论自动永久删除原始媒体的路径。

### INV-03 AI 没有文件执行权

AI 只输出建议，不能直接 copy、move、rename、delete 或 overwrite。

### INV-04 内容身份和文件实例分离

两个路径即使内容完全相同，也必须保留两个媒体实例记录。

```text
media identity != content hash
```

### INV-05 No Overwrite

任何情况下都不得静默覆盖 hash 不同的目标文件。

### INV-06 Apply 前重新验证

Plan 生成后到 Apply 之间源文件可能发生变化。

Apply 必须校验计划所依据的文件身份和内容。如果源发生实质变化，必须拒绝执行该 entry 并报告 stale plan。

### INV-07 成功必须可证明

Copy/跨卷 Move 只有在目标内容校验成功后才能进入成功状态。

### INV-08 Bundle 不可被静默拆散

Live Photo 和明确关联 sidecar 必须作为逻辑 bundle 规划和执行。

### INV-09 Rollback 不误删用户后改文件

如果目标文件在整理完成后被用户修改，Rollback 不得自动删除它。

### INV-10 幂等

相同输入、相同配置、相同版本下，重复执行不得产生新的副本、漂移目标名或重复 AI 调用。

### INV-11 真实图库 destructive action 需要用户运行时确认

Codex 开发阶段只能对测试夹具、临时目录和明确 Pilot 副本执行破坏性测试。

---

## 8. 媒体和伴随文件范围

### 8.1 MVP 图片

- JPG/JPEG
- PNG
- WEBP
- HEIC/HEIF

### 8.2 MVP 视频

- MP4
- MOV
- M4V

### 8.3 MVP sidecar

- AAE
- XMP
- JSON sidecar

### 8.4 Post-MVP

- CR2/CR3/NEF/ARW/DNG 等 RAW；
- GIF 动图特殊处理；
- Google Photos Takeout 修复；
- Motion Photo 特殊容器。

---

## 9. 扫描与索引

### 9.1 扫描原则

扫描必须：

- 递归；
- 流式迭代；
- 可排除 glob；
- 默认不跟随 symlink/junction；
- 自动排除 workspace 和 output；
- 防止 output 位于 source 内造成二次扫描；
- 单文件错误不终止整个扫描。

### 9.2 媒体实例

至少记录：

```text
id
original_path
media_type
extension
size_bytes
mtime_ns
source_present
last_seen_at
content_sha256 optional
```

`content_sha256` 不是主键。

### 9.3 增量扫描

可以使用：

```text
normalized path + size + mtime_ns
```

作为快速未变化判断。

完整 SHA-256 允许延迟，至少在以下场景必须计算：

- 精确重复确认；
- 进入最终 Plan 的文件；
- copy/move 内容验证；
- 用户要求 full hash。

### 9.4 元数据时间

必须区分时间来源和可信度。

推荐优先级：

```text
EXIF DateTimeOriginal
> EXIF CreateDate/SubSec
> QuickTime creation metadata
> 可信文件名时间
> filesystem mtime
```

至少保存：

```text
captured_at
capture_source
capture_confidence
capture_timezone_status
```

不得将 filesystem mtime 伪装成高置信度拍摄时间。

---

## 10. Bundle

系统至少支持：

```text
SINGLE
LIVE_PHOTO
SIDECAR_SET
```

典型：

```text
IMG_1001.HEIC
IMG_1001.MOV
IMG_1001.AAE
```

Bundle 规则必须可审计。如果系统不确定关联关系，宁可产生 warning，也不要自动删除或强绑定错误文件。

Rename、Plan、Copy、Move、Rollback 都必须保留 bundle 关联。

---

## 11. Preview

### 11.1 图片 Preview

要求：

- 正确处理 EXIF Orientation；
- 默认最长边约 1024px，可配置；
- 输出 WebP 或等价低成本格式；
- 不修改原图；
- Preview 生成失败只影响单项；
- Preview 算法有 version。

### 11.2 视频 Preview

生成 contact sheet，而不是解码整个视频到内存。

默认建议：

| 视频时长 | 样本帧 |
|---|---:|
| <=10 秒 | 3 |
| 10 到 60 秒 | 6 |
| >60 秒 | 9 |

采样应避开极端首尾位置。

---

## 12. 精确重复和连拍

### 12.1 Exact Duplicate

推荐候选流程：

```text
same size
-> SHA-256
-> same SHA-256
```

系统只创建重复候选组，不自动删除。

### 12.2 Burst Candidate

MVP 默认采用轻量策略：

```text
capture time window + pHash/dHash + deterministic grouping
```

要求：

- 不做全库 O(n²) 图像比较；
- 阈值配置化；
- 阈值有 algorithm_version；
- 避免 A≈B、B≈C 导致无限链式漂移；
- 同一输入结果确定性一致。

Codex 可以选择 representative、medoid 或其他轻量方法，只要通过 TDD 的 false-chain 和 deterministic tests。

### 12.3 Best Shot

本地指标可包含：

- sharpness；
- exposure；
- clipping；
- resolution；
- perceptual distance。

闭眼、表情、姿态、主体完整度等高级判断交给视觉 AI 或人工复核。

本地单一指标不得直接判定永久删除。

---

## 13. AI 分析

### 13.1 Provider 抽象

业务层必须依赖可替换 Vision Provider，而不是直接依赖某一家 API。

MVP 至少需要：

- 一个真实 Vision Provider；
- 一个 Fake Provider 用于全部自动测试。

Gemini 可以作为默认真实 Provider，但不得在核心业务代码写死具体模型版本。

### 13.2 云调用必须显式启用

默认：

```text
allow_cloud = false
```

用户显式允许后才可发送视觉输入。

### 13.3 禁止发送

- 原始媒体文件；
- 完整 EXIF dump；
- 本地绝对路径；
- SQLite 数据库；
- content hash；
- sidecar 原文。

允许发送：

- 受控尺寸的图片 Preview；
- 视频 contact sheet；
- 必要的匿名化本地质量指标。

### 13.4 单项 AI 结果

至少包含：

```text
item_id
scene_category
disposition
confidence
quality_score
tags
short_desc
reason
```

场景分类初始集合：

```text
01_家庭生活
02_旅行风光
03_工作与文档
04_截图与备忘
05_其他
```

处置状态：

```text
KEEP
REVIEW
REJECT_CANDIDATE
```

AI 不返回 DELETE。

### 13.5 低置信度保护

默认：

```text
confidence < threshold
=> final disposition cannot be REJECT_CANDIDATE
=> REVIEW
```

阈值可配置。

### 13.6 结构化输出

真实 Provider 输出必须进行强 Schema 校验。

非法 category、缺失 ID、越界 confidence 等必须视为无效结果，而不是带病写入业务状态。

### 13.7 缓存

缓存必须至少区分：

```text
input fingerprint
preview version
provider
model
prompt version
schema version
```

Preview 的内容 hash 可以作为 AI 输入 fingerprint 的核心，不要求为了每次 AI 调用先重新读取整个原始大文件。

### 13.8 成本可见

系统应提供 AI 运行前估算：

- 待分析 item 数；
- 预计上传 preview 总量；
- cache hit 数；
- 预计 API 请求批次数。

如果 Provider 可稳定估算费用，可以展示估算值，但不要伪装成精确账单。

---

## 14. 本地复核 UI

### 14.1 定位

Review UI 是本地结果复核器，不是完整照片管理平台。

### 14.2 运行方式

推荐默认体验：

```bash
spt review
```

启动仅绑定：

```text
127.0.0.1
```

的本地 Web UI，并自动打开浏览器。

要求：

- 不依赖 CDN；
- 无互联网也能使用；
- 不向局域网暴露；
- 可以直接持久化人工 decision 到 SQLite；
- 服务停止后数据不丢失。

Codex 可选择标准库 HTTP Server 或轻量框架，最终以代码简单度和测试可靠性为准。

### 14.3 静态导出

可额外提供：

```bash
spt dashboard export
```

生成只读静态快照，便于备份和审阅。

静态导出不是人工 decision 的唯一写入路径。

### 14.4 UI 必须功能

- 年/月筛选；
- category 筛选；
- KEEP/REVIEW/REJECT_CANDIDATE 筛选；
- filename/tags/short_desc 搜索；
- exact duplicate group；
- burst group；
- Best Shot 标识；
- Lightbox；
- AI reason；
- confidence；
- 本地质量指标；
- 修改 category；
- 修改 disposition；
- 标记保留；
- 标记淘汰候选；
- 显示人工覆盖 AI 的状态。

### 14.5 大库 UI

不得一次性创建全库 DOM。

必须分页或虚拟化。

MVP 分页即可，默认每页 100 左右，可配置。

---

## 15. Human Decision 优先级

最终 decision 来源可以是：

```text
RULE
AI
HUMAN
```

优先级：

```text
HUMAN > AI > RULE
```

人工选择必须始终覆盖 AI 结果。

系统不得因为重新运行 AI 而静默覆盖人工 decision。

---

## 16. 整理 Plan

### 16.1 Plan 必须先于文件变更

正式修改文件前必须生成不可变 Plan。

Plan 至少包含：

```text
plan_id
schema_version
created_at
config fingerprint
source root fingerprint
entries
```

每个 entry 至少包含：

```text
media_id
bundle_id optional
source_path
target_path
action
expected_size
expected_sha256
decision_source
```

### 16.2 Plan 确定性

在相同 DB 状态和配置下重复生成，目标路径和操作结果应保持一致。

### 16.3 命名

推荐：

```text
YYYYMMDD_HHMMSS_{short_desc}_{short_id}.{ext}
```

`short_id` 用于避免：

- 同一秒多张；
- 相同描述；
- 大小写冲突；
- 并发生成目标路径时的不稳定 `(1)`、`(2)`。

### 16.4 文件名安全

必须处理：

- Windows 非法字符；
- 控制字符；
- 尾随空格和点；
- Windows 保留设备名；
- 目标路径过长风险；
- 大小写不敏感文件系统碰撞；
- Unicode 规范化碰撞风险。

### 16.5 无可靠时间

不得伪造高置信度日期。

进入类似：

```text
_时间待确认/
```

的明确路径。

### 16.6 淘汰候选

最终仍为 `REJECT_CANDIDATE` 的项目只能移动到可恢复的待审区域，例如：

```text
_待审废片/
```

不是 permanent delete。

---

## 17. Apply Preflight

任何正式 copy/move 前必须执行 preflight。

至少检查：

- 源文件是否仍存在；
- 源 size 是否匹配；
- 源 SHA-256 是否匹配 Plan；
- 目标根目录是否可写；
- workspace 是否可写；
- output 是否错误位于 source 内；
- source 是否错误位于 output 内；
- 目标路径冲突；
- bundle 完整性 warning；
- 可用空间估算；
- 未完成旧事务；
- 同一 workspace 是否有另一个 apply 正在运行。

存在安全冲突时，不得部分蒙混通过。

---

## 18. 文件执行器

### 18.1 默认 Dry Run

默认操作必须是 dry-run 或明确无副作用预览。

### 18.2 Copy

安全语义：

```text
source
-> temp target
-> flush/fsync where applicable
-> SHA-256 verify
-> atomic finalize
-> journal DONE
```

### 18.3 同文件系统 Move

可以使用安全 rename，但必须：

- no-overwrite；
- journal 先记录 PREPARED；
- 完成后验证目标存在；
- 保持 bundle 状态一致。

### 18.4 跨文件系统 Move

必须等价于：

```text
PREPARED
-> copy temp
-> verify target hash
-> finalize target
-> COPIED_VERIFIED
-> remove source
-> DONE
```

禁止先删除源文件再验证目标。

### 18.5 Workspace Lock

Apply/Resume/Rollback 等文件变更流程必须使用 workspace 级排他锁，避免两个进程同时执行同一事务。

---

## 19. Operation Journal

Operation Journal 是恢复依据，不使用动态生成的 `rollback.py`。

至少记录：

```text
transaction_id
media_id
bundle_id optional
operation
source_path
target_path
source_sha256
target_sha256
state
error
created_at
updated_at
```

建议状态：

```text
PREPARED
COPIED_VERIFIED
DONE
FAILED
ROLLBACK_PENDING
ROLLED_BACK
ROLLBACK_FAILED
```

状态设计可以调整，但必须覆盖 crash resume 和 safe rollback。

---

## 20. Resume 与 Doctor

系统应提供：

```bash
spt doctor
```

或等价入口。

必须能识别：

- 半完成 `.partial`；
- PREPARED；
- COPIED_VERIFIED；
- source missing；
- target hash mismatch；
- stale lock；
- plan stale；
- bundle partial completion。

系统应给出安全可执行的 resume/rollback 状态，而不是要求用户手动猜测。

---

## 21. Rollback

用户可对一个 transaction 执行 rollback。

Rollback 关键保护：

```text
current target hash == journal target hash
```

才允许自动删除或逆向移动目标。

如果用户后来修改过 target，必须停止自动删除，并报告 manual inspection required。

Bundle rollback 必须保持关联文件一致性。

---

## 22. SQLite 数据合同

具体字段可在实现时小幅调整，但以下语义实体必须存在。

### 22.1 media_item

负责文件实例，不以内容 hash 为主键。

至少包含：

```text
id
original_path
media_type
extension
size_bytes
mtime_ns
source_present
content_sha256 nullable
captured_at nullable
capture_source
capture_confidence
capture_timezone_status
width/height/duration optional
preview_path
preview_version
last_seen_at
```

### 22.2 asset_bundle / bundle_member

描述 SINGLE、LIVE_PHOTO、SIDECAR_SET 等逻辑资产。

### 22.3 duplicate_group / duplicate_member

描述 exact duplicate content candidates。

### 22.4 burst_group / burst_member

至少记录 algorithm version 和 member distance/score。

### 22.5 ai_analysis

必须可以根据输入 fingerprint 和 AI 版本配置唯一缓存。

### 22.6 review_decision

持久化最终人工选择，并记录 decision source。

### 22.7 operation_journal

作为 Apply/Resume/Rollback 的事实来源。

### 22.8 schema migration

所有 schema 变更必须有版本化 migration，并支持重复初始化而不破坏已有数据。

---

## 23. CLI 用户体验

最终命令名字可以由 Codex轻微调整，但必须覆盖以下能力：

```bash
spt init
spt scan <source>
spt preprocess
spt group
spt analyze
spt review
spt plan build
spt plan inspect
spt apply <plan> --dry-run
spt apply <plan> --mode copy
spt apply <plan> --mode move
spt doctor
spt resume <transaction>
spt rollback <transaction>
spt stats
```

可以提供一键：

```bash
spt run <source>
```

但它必须组合同一套阶段函数，不能复制一套旁路业务逻辑。

---

## 24. 推荐工程技术边界

### 24.1 推荐默认

- Python 3.11+
- SQLite
- pathlib
- logging
- subprocess 调 ExifTool/FFmpeg
- Pillow
- HEIC 兼容库
- pHash/dHash 轻量库
- Pydantic 或等价强 Schema
- Typer/argparse 等 CLI
- 一个真实 Vision Provider
- Fake Vision Provider

### 24.2 本地 Review UI

可以采用：

- Vanilla HTML/CSS/JS；
- 或非常轻量的本地 Web 实现。

必须满足：

- 无 CDN；
- 无外网依赖；
- 绑定 127.0.0.1；
- 简单可测试。

### 24.3 默认不采用

除非 Codex 写 ADR 并能证明明显必要，否则不采用：

- PyTorch；
- TensorFlow；
- ONNX Runtime；
- Postgres；
- Redis；
- Qdrant；
- Elasticsearch；
- Docker Compose 服务集群；
- 微服务拆分；
- 重型前端工程栈。

---

## 25. 日志与隐私

日志可以包含：

- media_id；
- 相对路径或脱敏路径；
- batch id；
- operation state；
- error code；
- timing；
- cache hit。

日志不得包含：

- API key；
- 图片 base64；
- 完整 AI 请求体；
- 不必要的完整 EXIF；
- 用户敏感媒体内容描述的大段复制。

---

## 26. 性能要求

不使用与硬件强耦合的绝对秒数作为主要 CI Gate。

结构性要求：

- 扫描流式；
- 缩略图逐项或有界并发；
- 视频不完整解码到内存；
- AI batch 有上限；
- SQLite 使用批事务；
- 已成功 AI fingerprint 不重复请求；
- Burst 只在时间候选窗口内比较；
- Dashboard 只渲染当前页或虚拟视窗；
- 完整 SHA-256 支持延迟计算。

Release Evidence 应输出：

```text
files/sec
preview items/sec
videos/min
peak RSS
AI cache hit rate
estimated uploaded preview MB
exact duplicate group count
burst group count
failed item count
```

---

## 27. 错误策略

默认：

```text
single item failure != whole run failure
```

但以下情况属于全局安全错误：

- DB corruption；
- workspace 不可写；
- output root 不可写；
- source/output 重叠风险；
- operation journal 出现无法解释的不一致；
- apply preflight 发现计划内容身份已经变化；
- workspace 已被另一个变更进程锁定。

---

## 28. 开发过程的持久状态

Goal Mode 运行可能持续较长时间。Codex 必须把关键状态写入仓库，而不是只存在对话里。

至少维护：

```text
docs/implementation-status.md
docs/adr/
docs/release-evidence.md
```

`implementation-status.md` 至少记录：

- 当前阶段；
- 已通过 Gate；
- 未解决问题；
- 下一步；
- BLOCKER。

Codex 中断后应可根据仓库状态继续，而不是要求用户重新解释整个项目。

---

## 29. Release Candidate Definition of Done

只有同时满足以下条件，Codex 才能宣布“Ready for Final Acceptance”。

### 29.1 功能 DoD

- [ ] 可扫描大型本地目录；
- [ ] 可增量续扫；
- [ ] metadata 来源和可信度可追踪；
- [ ] JPG/PNG/WEBP/HEIC preview；
- [ ] MP4/MOV/M4V contact sheet；
- [ ] Live Photo/sidecar bundle；
- [ ] exact duplicate group；
- [ ] burst group；
- [ ] Best Shot 建议；
- [ ] 可插拔 AI Provider；
- [ ] AI structured output；
- [ ] AI cache；
- [ ] local review UI；
- [ ] HUMAN 覆盖 AI；
- [ ] deterministic plan；
- [ ] preflight；
- [ ] dry-run；
- [ ] copy；
- [ ] move；
- [ ] verify；
- [ ] journal；
- [ ] resume；
- [ ] rollback；
- [ ] doctor。

### 29.2 安全 DoD

- [ ] 开发期间未对真实源图库执行 destructive action；
- [ ] source 默认只读；
- [ ] AI 不拥有 delete/move 权限；
- [ ] no-overwrite；
- [ ] stale plan 可检测；
- [ ] copy hash verify；
- [ ] cross-volume move 安全；
- [ ] crash resume；
- [ ] bundle partial failure 不会被标 DONE；
- [ ] rollback 不误删修改后的 target；
- [ ] concurrent apply 被阻止；
- [ ] rerun 幂等。

### 29.3 测试 DoD

以 TDD 文档定义为准，至少：

- [ ] P0 Safety tests 全绿；
- [ ] Unit/Integration/E2E 全绿；
- [ ] Windows CI 全绿；
- [ ] Linux CI 全绿或明确说明平台限制；
- [ ] 整体 line coverage >= 85%；
- [ ] executor/planner 核心安全路径覆盖率达到 TDD Gate；
- [ ] E2E 完整流程连续执行两次无重复副作用；
- [ ] fault injection 场景全绿。

### 29.4 工程 DoD

- [ ] README 可从干净环境安装；
- [ ] `spt doctor` 可验证依赖；
- [ ] 不需要用户理解内部 Python 模块才能运行；
- [ ] 没有 API key 写入仓库；
- [ ] 没有 CDN；
- [ ] 没有未说明的高风险许可证依赖；
- [ ] ADR 与实际实现一致；
- [ ] Release Evidence 已生成。

---

## 30. 最终用户验收

用户只在 Release Candidate 完成后介入。

### A. 环境验收

```bash
spt doctor
```

必须清晰显示：

- Python/runtime；
- SQLite；
- ExifTool；
- FFmpeg；
- AI Provider 是否配置；
- workspace；
- output。

### B. Pilot

使用 500 到 1000 个真实媒体的独立副本。

运行：

```text
scan
preprocess
group
optional AI
review
plan
dry-run
copy
verify
rollback
```

### C. 人工抽查

建议至少抽查：

- 30 个普通照片分类；
- 20 个 burst group；
- 10 个 duplicate group；
- 5 个视频；
- 5 个 Live Photo/sidecar bundle；
- 20 个 REJECT_CANDIDATE。

### D. 最终决策

如果 Pilot 满足实际使用需求，则用户可以允许：

```text
全库 scan/analyze/review
```

是否最终执行 `move` 由用户自行决定。

优先建议第一次正式整理仍使用 `copy`。

---

## 31. Codex Goal Contract

本项目推荐只给 Codex 一个强 Goal，不需要逐个 milestone 重新提示。

目标语义如下：

```text
Build Smart-Photo-Triage into a release candidate that satisfies this PRD and the autonomous TDD specification.

The product must safely index and organize a 100GB+ local mixed photo/video library without modifying source files during analysis, detect exact duplicates and burst candidates, optionally use a pluggable vision AI on privacy-reduced previews, provide a fully local review UI, generate deterministic immutable organization plans, and execute copy/move operations with no-overwrite, content verification, crash-resumable journaling, idempotency, bundle safety, and safe rollback.

Work autonomously through the internal milestones. Write failing tests before implementation for new behavior, repair failures, refactor when useful, and continue automatically when each internal gate passes.

You may make reasonable implementation and dependency decisions without asking the user, provided they remain inside the PRD's safety and complexity boundaries. Record material architecture deviations as ADRs.

Do not perform destructive operations on any real user source library during development. Stop only for a genuine BLOCKER defined by the PRD.

The work is ready for final user acceptance only when the complete non-external test suite, P0 safety suite, platform CI, coverage gates, deterministic two-pass E2E flow, fault-injection recovery tests, documentation checks, and release evidence requirements all pass.
```

---

## 32. 参考设计来源

本项目可参考但不复制以下项目的设计思路：

- Immich
- PhotoPrism
- julyx10/lap
- photo-cli/photo-cli
- sgaunet/moraine
- idealo/imagededup
- local-ai-culling
- local-photo-sorter

重点吸收：

- 本地优先；
- asset/bundle 思维；
- hash verify；
- no-overwrite；
- sidecar 跟随；
- KEEP/REVIEW/REJECT 工作流；
- 可恢复状态机；
- 轻量 hash 去重。

除完成许可证审查外，不直接复制第三方项目实现代码。
