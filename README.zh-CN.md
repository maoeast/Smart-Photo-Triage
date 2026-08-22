# Smart-Photo-Triage

[English](README.md) | [简体中文](README.zh-CN.md)

Smart-Photo-Triage 是一个本地优先的照片与视频整理工具。它先建立可审计的媒体索引和预览，再让您人工审核、生成不可变整理计划，并在明确批准后执行 COPY 或 MOVE。它不会把原始照片库交给自动化程序直接修改。

当前版本为 `1.2.1`，提供本地浏览器 GUI 和 CLI。基础运行时只依赖 Python 3.11+ 与 Pillow。扫描、预处理、分组和离线演示模型均不需要网络。

## 它能做什么

- 只读递归扫描照片和视频，持续记录媒体、拍摄时间与来源状态。
- 生成工作区内的受控预览图、本地质量指标、精确重复和连拍候选。
- 在本地审核页面中保存人工决定，优先级永远是 `人工 > AI > 规则`。
- 可选接入 Gemini、OpenAI、Anthropic、DeepSeek 及 OpenAI-compatible 视觉服务。
- 为单张分析和连拍复核分别配置模型、按顺序备用模型和一次低置信度升级。
- 生成不可变的 COPY/MOVE 整理计划，支持批准、预检、模拟执行、事务日志、诊断、恢复和安全回滚。

## Windows 桌面版. 普通用户推荐

下载并运行 `Smart-Photo-Triage-Setup-1.2.1.exe` 后，从开始菜单或桌面快捷方式打开 **Smart Photo Triage** 即可。无需 PowerShell、Python 或理解端口。程序默认安装到 `%LOCALAPPDATA%\Programs\Smart Photo Triage`，可从 Windows“已安装的应用”卸载，卸载不会删除您的工作区、照片源或输出目录。

桌面版使用 Microsoft Edge WebView2，并在窗口内管理仅本机可访问的随机端口。关闭窗口会停止本次本地服务。如果缺少 WebView2 Runtime，会显示中文提示和官方下载链接。当前安装包未做代码签名，Windows SmartScreen 可能显示“未知发布者”警告。

桌面版的“选择工作区”“选择照片源”“选择输出目录”会打开 Windows 原生文件夹对话框，取消不会改变已填写路径。API Key 使用当前 Windows 用户和本机绑定的 DPAPI 加密保存到工作区 `.spt-gui-secrets.json`，不以明文写入 `config.toml`、SQLite、日志或审计记录。

## CLI 与浏览器模式. 高级用户和自动化

保留的 `spt gui` 适合 PowerShell、自动化及 Codex/Claude Code 等 Agent。它默认使用固定 `127.0.0.1:8765`，可用 `--port 0` 自动选择端口。浏览器模式不提供原生目录对话框，请手动填写完整路径。

## 从源码运行 GUI

在 PowerShell 中安装：

```powershell
Set-Location F:\Projects\Smart-Photo-Triage
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install .
```

Linux：

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install .
```

工作区初始化可安全重复执行。它只创建配置、SQLite 数据库和状态目录，不会扫描或修改照片：

```powershell
.\.venv\Scripts\spt.exe init --workspace "D:\SPT-Workspace"
```

启动本地 GUI：

```powershell
.\.venv\Scripts\spt.exe gui --workspace "D:\SPT-Workspace"
```

浏览器会打开 [http://127.0.0.1:8765/](http://127.0.0.1:8765/)。该地址只允许本机访问，不会开放局域网服务。浏览器模式请手动输入三个目录路径。

若 `8765` 已被占用，请使用另一个端口：

```powershell
.\.venv\Scripts\spt.exe gui --workspace "D:\SPT-Workspace" --port 8766
```

如要让系统自动挑选可用端口，使用 `--port 0`。关闭 GUI 时，在启动窗口按 `Ctrl+C`。

## GUI 推荐流程

1. 首页点击“选择照片源”和“选择输出目录”。工作区、照片源、输出目录必须相互独立。
2. 点击“扫描”，再点击“准备与分析”。这两个阶段不修改照片源。
3. 打开“人工审核”，确认或修改 AI 建议。
4. 返回首页生成复制计划，然后依次执行“批准”“预检”“模拟执行”。
5. 确认无误后点击“真实复制”，并在浏览器二次确认中选择确定。
6. 若需要撤销一次已完成的复制，填写复制记录编号后使用“安全回滚”。

默认 GUI 只生成 COPY 计划。真实复制只会创建输出副本，不会移动或删除原始照片。

## 配置 AI 分析服务

日常整理无需反复设置模型。首次配置或切换服务时，打开顶部的“模型与隐私”页面：

1. 填写配置编号、选择模型服务并填写服务商实际提供的模型 ID。
2. Gemini、OpenAI、Anthropic 和 DeepSeek 会自动填入官方服务地址。其他兼容服务需要自己填写地址。
3. 在“API 密钥”密码框粘贴真实 Key，点击“保存分析服务”。
4. 在“单张照片分析”和“连拍照片复核”中选择要使用的服务，并保存模型使用方式。
5. 需要联网模型时，展开“允许连接模型服务”，按页面提示明确开启互联网或局域网权限。

GUI 会将 API Key 以当前 Windows 用户绑定的加密形式保存，因此下次启动可以直接使用。Key 不会以明文写入 `config.toml`、SQLite、日志或路由审计记录。将工作区复制到另一台电脑，或换用另一个 Windows 用户后，需要重新填写 Key。

云端模型只会收到工作区内的受控预览图与匿名质量指标，不会收到原始媒体文件、源路径、原始内容 hash、sidecar 原文或完整工作区数据库。联网默认关闭，且本地服务失败不会在未授权时静默上传到远程服务。

更多 Provider、路由、预算、隐私边界和 v1.2 升级说明见 [模型路由说明](docs/model-routing-v1.2.1.md)。

## CLI 快速参考

初始化工作区：

```powershell
spt init --workspace ".\.spt"
```

扫描与预处理：

```powershell
spt scan "D:\Pilot Photo Copy" --workspace ".\.spt"
spt preprocess --workspace ".\.spt"
spt group --workspace ".\.spt"
```

使用默认离线演示模型分析：

```powershell
spt analyze --workspace ".\.spt" --provider fake
```

查看 v1.2.1 模型路由状态，不发出网络请求：

```powershell
spt ai providers --workspace "D:\SPT-Workspace"
spt ai doctor --workspace "D:\SPT-Workspace"
spt ai estimate --workspace "D:\SPT-Workspace"
```

生成和执行计划：

```powershell
spt plan build --workspace ".\.spt" --output ".\organized" --mode copy
spt plan inspect "plan-..." --workspace ".\.spt"
spt plan approve "plan-..." --workspace ".\.spt"
spt plan preflight "plan-..." --workspace ".\.spt"
spt apply "plan-..." --workspace ".\.spt"
spt apply "plan-..." --workspace ".\.spt" --execute
```

`spt apply` 默认是模拟执行。只有附加 `--execute` 才可能创建文件。CLI 直接使用 Gemini 时仍需要在调用进程提供环境变量；普通用户建议使用 GUI 保存 API Key。

## 安全与数据边界

- 默认禁用云端与局域网模型连接。
- 扫描、预处理、分组和审核不会移动、删除或改写照片源。
- 所有真实文件操作都必须经过不可变计划、批准和预检。
- Router 没有任何扫描、复制、移动、删除或回滚文件的权限。
- AI 结果不会覆盖人工决定。
- 回滚只处理可由事务日志与 hash 验证归属的输出副本。

建议先选择少量可复制的非敏感测试媒体完成一轮流程，再用于正式照片库。自动化测试只使用合成数据集，不读取您的真实照片。

## 文档导航

- [本地 GUI 中文操作手册](docs/USER_MANUAL_zh-CN.md)：安装、启动、停止、按钮说明、模型设置、回滚与排障。
- [模型路由说明](docs/model-routing-v1.2.1.md)：Provider Registry、网络边界、fallback、缓存、预算和升级行为。
- [v1.2.1 发布证据](docs/release-evidence-v1.2.1.md)
- [v1.2 发布证据](docs/release-evidence.md)
- [实现状态](docs/implementation-status.md)

## 开发与验证

安装开发依赖后运行：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests
```

项目的 Release Candidate 证据覆盖合成 E2E、迁移、路由、fallback、缓存、预算、恢复和回滚验证。请不要将真实 API Key、真实照片或家庭数据提交到仓库。
