# v1.2.1 多 Provider 与模型路由

v1.2.1 保持本地优先。没有明确授权时，任何 remote 或 LAN Provider 都不会收到 preview。
Provider Router 只接收已经由既有 Preview 边界构建的 `VisionRequest`，没有扫描、计划、移动、删除、覆盖或回滚文件的能力。

## 配置

工作区 `config.toml` 仍兼容旧格式：

```toml
allow_cloud = false
```

以下是 v1.2.1 配置示例。API Key 只能写环境变量名，禁止写入 TOML、SQLite、日志或证据文件。

```toml
[ai]
allow_cloud = false
allow_lan = false

[ai.providers.local_vlm]
driver = "openai_compatible"
model = "your-local-vision-model"
base_url = "http://127.0.0.1:1234/v1"
network_scope = "loopback"

[ai.providers.qwen_fast]
driver = "openai_compatible"
model = "configured-by-operator"
base_url = "https://your-compatible-endpoint.example/v1"
api_key_env = "SPT_QWEN_API_KEY"
network_scope = "remote"

[ai.providers.openai_strong]
driver = "openai"
model = "configured-by-operator"
api_key_env = "OPENAI_API_KEY"
network_scope = "remote"

[ai.routes.item_analysis]
primary = "qwen_fast"
fallbacks = ["openai_strong"]

[ai.routes.item_analysis.escalation]
confidence_below = 0.72
to = "openai_strong"
max_escalations = 1

[ai.routes.burst_review]
primary = "openai_strong"

[ai.limits]
max_provider_attempts_per_task = 3
max_requests_per_run = 100
max_remote_preview_mb_per_run = 200
```

支持的 driver 为 `fake`、`gemini`、`openai`、`anthropic` 和 `openai_compatible`。Qwen、豆包、GLM 和其他兼容端点复用同一个 `openai_compatible` 驱动，不复制供应商业务逻辑。

“OpenAI-compatible”不保证视觉输入、多图或 JSON Schema 能力。必须显式声明或使用保守内建 capability profile。Router 会在网络请求前检查图片数、MIME、字节上限和结构化输出能力。

## 隐私与网络边界

- `loopback` 仅接受字面 `127.0.0.1` 或 `::1`。名称中含 local/localhost 不会被当作 loopback。
- `lan` 需 `allow_lan = true`。
- `remote` 必须 HTTPS，且需 `allow_cloud = true`。
- URL 中的用户名或密码被拒绝。
- HTTP redirect 默认禁用，不能从本地端点跳转到公网。
- local Provider 失败时，只有 route 明确列出 remote fallback 且 `allow_cloud = true` 才可能尝试 remote。
- Provider 只看到受控 preview、匿名质量指标和 item ID。原始媒体、绝对源路径、内容 hash、sidecar 原文、完整请求体和密钥不会进入请求审计。

## 路由行为

`ITEM_ANALYSIS` 与 `BURST_REVIEW` 各自具有 primary、ordered fallback 和可选的一次低置信度 escalation。只有 `RATE_LIMIT`、`TIMEOUT`、`SERVER_ERROR`、`NETWORK_ERROR`、受限的 `SCHEMA_INVALID` 和预检 `CAPABILITY_MISMATCH` 可继续 fallback。认证、计费、配置、隐私和预算阻断不会静默换供应商。

Provider cache key 包含 provider ID、driver、model、endpoint identity、capability profile version、prompt/schema version 和 preview 指纹。单纯变更 route 顺序可复用相同 Provider 的结果，但绝不跨 Provider/model/endpoint 误命中。

每一次 route run 与 attempt 会记录 provider/model、状态、错误类、cache hit、route reason、尝试序号和 remote preview 字节估计。审计不保存敏感字段。

## CLI 可观察性

以下命令默认不执行网络请求：

```powershell
spt ai providers --workspace "D:\SPT-Workspace"
spt ai doctor --workspace "D:\SPT-Workspace"
spt ai route explain item_analysis --workspace "D:\SPT-Workspace"
spt ai estimate --workspace "D:\SPT-Workspace"
spt ai probe local_vlm --workspace "D:\SPT-Workspace"
```

`providers` 仅显示 `api_key_configured: true/false`，从不输出密钥。当前 `probe` 是离线的声明 profile 校验，仅使用 synthetic metadata，不读取用户媒体，也不写入业务 AI 结果。真实外部 smoke 需要操作员单独提供凭据，release evidence 会标注其状态。

## 从 v1.2 升级

首次打开已有工作区会运行加性 SQLite migration v12，创建 Provider cache 与 route audit 表。不会重扫媒体、清空旧 `ai_analysis`、修改 `review_decision`、改写 immutable plan 或 operation journal。旧根级 `allow_cloud` 配置仍有效，且默认保持关闭。GUI 切换 cloud 授权会保留已有 `[ai.providers.*]` 配置。
