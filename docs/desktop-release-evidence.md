# Windows 桌面分发 Release Evidence

版本：`1.2.1`。本文记录桌面分发的证据边界，不能替代真实发布环境验收。

## 本机已验证

2026-08-22，Windows 10 22H2，本机 Python 3.13.2：

- `scripts/build_windows.ps1 -Python .\\.venv\\Scripts\\python.exe` 使用 PyInstaller 6.22.2 成功生成 `release\\dist\\Smart Photo Triage\\Smart Photo Triage.exe`（`onedir`）。
- 实际启动该 EXE 后，确认进程存活、Microsoft Edge WebView2 151.0.4129.93 已加载，且应用仅监听 `127.0.0.1:39618`。结束该 smoke 进程后该端口不再处于 `Listen` 状态。
- 最新的 `onedir` EXE 已再次启动并返回主页面 HTTP `200`，随机端口 `127.0.0.1:44647` 未对 LAN 监听。终止该 smoke 实例后端口不再处于 `Listen` 状态。
- 本机 NSIS 3.11 已成功生成 `release\\installer\\Smart-Photo-Triage-Setup-1.2.1-candidate.exe`（15,790,958 bytes，SHA-256 `280DCA5A69A36393BA6A0DD33748FD2A0959E60A61087168FF49DBDC76234920`）。构建脚本为每次新建的安装包使用时间戳文件名，避免覆盖或锁定既有产物。
- `tests/test_gui.py`：12 项通过。覆盖合成图片的 COPY. 二次确认. 回滚. Fake Provider. 保存加密 Provider Key. 桌面目录选择. 取消目录选择不改值. 桌面关闭后服务退出. WebView2 缺失提示. 浏览器模式不暴露目录选择 API。
- `ruff check src tests`：通过。

自动化验证只使用 Pillow 生成的合成图片与 Fake Provider，绝不读取真实照片、视频或 API Key。

## 尚待发布环境验证

- 使用干净的 Windows 10/11 用户账户实际安装生成的 NSIS 安装包。
- 验证 WebView2 缺失时的中文安装提示，并在安装 Evergreen WebView2 Runtime 后重新启动。
- 验证开始菜单、可选桌面快捷方式、Windows 已安装应用卸载入口与 `%LOCALAPPDATA%\\Programs\\Smart Photo Triage` 安装路径。
- 验证 SmartScreen 提示。当前构建未进行代码签名，Windows 可能显示未知发布者或 SmartScreen 警告。
- 尚未在干净账户执行安装和卸载的端到端验证。当前安装器使用 NSIS，脚本为 `installer/SmartPhotoTriage.nsi`。

## 安全不变量

- 桌面 HTTP 后端只绑定 `127.0.0.1`，桌面随机端口不对 LAN 或远端公开。
- 原始照片不会被移动、删除或改写。真实复制仍须批准、预检与第二次确认。
- Router 不拥有文件修改权限，默认不允许任何云端或 LAN 外发。
