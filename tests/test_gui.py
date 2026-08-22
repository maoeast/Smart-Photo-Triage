from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest
from PIL import Image

from smart_photo_triage.cli import build_parser
from smart_photo_triage.desktop import DesktopBridge, DesktopLaunchError, launch_desktop
from smart_photo_triage.gui import create_gui_server
from smart_photo_triage.gui_assets import HTML, JS, SETTINGS_HTML
from smart_photo_triage.gui_secrets import load_provider_secret


def _json(url: str) -> dict[str, object]:
    with urlopen(url, timeout=5) as response:
        return json.loads(response.read())


def _post(base: str, action: str, csrf: str, payload: dict[str, object]) -> dict[str, object]:
    request = Request(
        f"{base}/api/{action}",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Origin": base,
            "X-SPT-CSRF": csrf,
        },
        method="POST",
    )
    with urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def test_local_gui_runs_safe_synthetic_copy_lifecycle(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    Image.new("RGB", (18, 12), "red").save(source / "IMG_20250101_000001.png")
    server = create_gui_server(tmp_path / "workspace")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        base = f"http://{host}:{port}"
        bootstrap = _json(f"{base}/api/bootstrap")
        csrf = str(bootstrap["csrf_token"])
        common = {"source": str(source), "output": str(output), "plan_id": "", "confirmation": ""}
        assert _post(base, "scan", csrf, common)["indexed_count"] == 1
        assert "preprocess" in _post(base, "prepare", csrf, common)
        plan = _post(base, "plan", csrf, common)
        plan_id = str(plan["plan_id"])
        common["plan_id"] = plan_id
        assert _post(base, "approve", csrf, common)["state"] == "APPROVED"
        assert _post(base, "preflight", csrf, common)["ok"] is True
        assert _post(base, "dry-run", csrf, common)["applied"]["state"] == "DRY_RUN"
        try:
            _post(base, "execute", csrf, common)
        except HTTPError as error:
            assert error.code == 400
        else:
            raise AssertionError("GUI executed without the explicit confirmation")
        assert not output.exists()
        common["confirmation"] = "EXECUTE"
        queued = _post(base, "execute", csrf, common)["copy_job"]
        assert queued["state"] in {"QUEUED", "RUNNING"}
        deadline = time.monotonic() + 10
        job = _json(f"{base}/api/copy-job")["job"]
        while job["state"] in {"QUEUED", "RUNNING"} and time.monotonic() < deadline:
            time.sleep(0.02)
            job = _json(f"{base}/api/copy-job")["job"]
        assert job["state"] == "DONE"
        assert job["completed_files"] == 1
        assert job["copied_bytes"] == job["total_bytes"]
        assert job["bytes_per_second"] > 0
        executed = {"transaction_id": job["transaction_id"], "state": job["state"]}
        assert any(output.rglob("*.png"))
        common["confirmation"] = "ROLLBACK"
        common["transaction_id"] = str(executed["transaction_id"])
        assert _post(base, "rollback", csrf, common)["state"] == "ROLLED_BACK"
        assert not any(output.rglob("*.png"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_gui_cloud_authorization_requires_explicit_confirmation_and_never_stores_key(
    tmp_path: Path,
) -> None:
    server = create_gui_server(tmp_path / "workspace")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        base = f"http://{host}:{port}"
        bootstrap = _json(f"{base}/api/bootstrap")
        csrf = str(bootstrap["csrf_token"])
        assert bootstrap["allow_cloud"] is False
        try:
            _post(base, "configure-cloud", csrf, {"allow_cloud": True, "confirmation": "wrong"})
        except HTTPError as error:
            assert error.code == 400
        else:
            raise AssertionError("GUI enabled cloud analysis without explicit confirmation")
        result = _post(
            base,
            "configure-cloud",
            csrf,
            {"allow_cloud": True, "confirmation": "ALLOW_CLOUD", "api_key": "not-stored"},
        )
        assert result == {"allow_cloud": True, "api_key_stored": False}
        assert _json(f"{base}/api/bootstrap")["allow_cloud"] is True
        config = (tmp_path / "workspace" / "config.toml").read_text(encoding="utf-8")
        assert config == "allow_cloud = true\n"
        assert "not-stored" not in config
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_gui_exposes_v121_provider_registry_and_saves_user_encrypted_provider_key(
    tmp_path: Path,
) -> None:
    server = create_gui_server(tmp_path / "workspace")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        base = f"http://{host}:{port}"
        bootstrap = _json(f"{base}/api/bootstrap")
        csrf = str(bootstrap["csrf_token"])
        assert bootstrap["version"] == "1.2.1"
        assert bootstrap["providers"][0]["driver"] == "fake"
        result = _post(
            base,
            "save-provider",
            csrf,
            {
                "provider_id": "qwen_compatible",
                "display_name": "Qwen compatible",
                "driver": "openai_compatible",
                "model": "qwen-vl-plus",
                "base_url": "https://compatible.example/v1",
                "api_key_env": "SPT_QWEN_API_KEY",
                "api_key": "API_KEY_VALUE",
            },
        )
        providers = result["ai"]["providers"]
        qwen = next(
            provider for provider in providers if provider["provider_id"] == "qwen_compatible"
        )
        assert qwen["api_key_configured"] is True
        assert result["api_key_saved"] is True
        config = (tmp_path / "workspace" / "config.toml").read_text(encoding="utf-8")
        assert 'driver = "openai_compatible"' in config
        assert 'api_key_env = "SPT_QWEN_API_KEY"' in config
        assert "qwen-vl-plus" in config
        assert "API_KEY_VALUE" not in config
        secret_file = tmp_path / "workspace" / ".spt-gui-secrets.json"
        assert "API_KEY_VALUE" not in secret_file.read_text(encoding="utf-8")
        assert load_provider_secret(server.workspace, "qwen_compatible") == "API_KEY_VALUE"
        routes = _post(
            base,
            "save-routes",
            csrf,
            {
                "item_primary": "qwen_compatible",
                "item_fallbacks": "default_fake",
                "item_confidence_below": "0.72",
                "item_escalate_to": "default_fake",
                "burst_primary": "qwen_compatible",
                "burst_fallbacks": "",
                "burst_confidence_below": "",
                "burst_escalate_to": "",
            },
        )
        assert routes["ai"]["routes"]["item_analysis"]["primary"] == "qwen_compatible"
        assert (
            _json(f"{base}/api/bootstrap")["routes"]["burst_review"]["primary"] == "qwen_compatible"
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _FakeWindow:
    def __init__(self, selected: object) -> None:
        self.selected = selected
        self.calls: list[tuple[object, str]] = []

    def create_file_dialog(self, dialog_type: object, *, directory: str) -> object:
        self.calls.append((dialog_type, directory))
        return self.selected


class _FakeWebview:
    FOLDER_DIALOG = "folder"

    def __init__(self, selected: object = None) -> None:
        self.window = _FakeWindow(selected)
        self.started_with: str | None = None
        self.created: tuple[str, str, dict[str, object]] | None = None

    def create_window(self, title: str, url: str, **kwargs: object) -> _FakeWindow:
        self.created = (title, url, kwargs)
        return self.window

    def start(self, *, gui: str) -> None:
        self.started_with = gui


class _MissingWebview(_FakeWebview):
    def start(self, *, gui: str) -> None:
        raise RuntimeError("Edge WebView2 is unavailable")


def test_desktop_directory_picker_changes_active_workspace_and_preserves_cancel(
    tmp_path: Path,
) -> None:
    selected = tmp_path / "chosen-workspace"
    selected.mkdir()
    server = create_gui_server(tmp_path / "workspace")
    webview = _FakeWebview([str(selected)])
    bridge = DesktopBridge(server, webview)
    bridge.attach_window(webview.window)
    try:
        result = bridge.choose_directory("workspace")
        assert result["selected"] == str(selected)
        assert result["workspace"] == str(selected)
        assert (selected / ".spt-workspace").exists()
        webview.window.selected = None
        cancelled = bridge.choose_directory("source")
        assert cancelled["selected"] is None
        assert cancelled["workspace"] == str(selected)
    finally:
        server.server_close()


def test_desktop_launch_uses_edge_webview_and_stops_local_server(tmp_path: Path) -> None:
    webview = _FakeWebview()

    launch_desktop(tmp_path / "workspace", webview_module=webview)

    assert webview.started_with == "edgechromium"
    assert webview.created is not None
    title, url, options = webview.created
    assert title == "Smart Photo Triage"
    assert url.startswith("http://127.0.0.1:")
    assert options["js_api"].__class__ is DesktopBridge
    try:
        urlopen(url, timeout=1)
    except URLError:
        pass
    else:
        raise AssertionError("desktop close left its local GUI server running")


def test_desktop_missing_webview2_has_an_actionable_chinese_error(tmp_path: Path) -> None:
    with pytest.raises(DesktopLaunchError, match="WebView2 Runtime"):
        launch_desktop(tmp_path / "workspace", webview_module=_MissingWebview())


def test_gui_uses_a_second_click_confirmation_instead_of_a_typed_copy_token() -> None:
    assert 'id="confirmation"' not in HTML
    assert "window.confirm" in JS
    assert 'body.confirmation="EXECUTE"' in JS
    assert 'body.confirmation="ROLLBACK"' in JS


def test_gui_serves_model_settings_on_a_separate_page(tmp_path: Path) -> None:
    server = create_gui_server(tmp_path / "workspace")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        base = f"http://{host}:{port}"
        with urlopen(f"{base}/", timeout=5) as response:
            home = response.read().decode()
        with urlopen(f"{base}/settings", timeout=5) as response:
            settings = response.read().decode()
        assert 'href="/settings"' in home
        assert 'id="provider_id"' not in home
        assert 'id="provider_id"' in settings
        assert 'href="/"' in settings
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_gui_deepseek_preset_reuses_the_openai_compatible_driver() -> None:
    assert 'value="deepseek"' in SETTINGS_HTML
    assert "https://api.deepseek.com" in JS
    assert 'apiKeyEnv:"DEEPSEEK_API_KEY"' in JS
    assert 'selectedService==="deepseek"?"openai_compatible":selectedService' in JS


def test_gui_validates_provider_draft_before_sending_a_request() -> None:
    assert "function providerDraftProblem(body)" in JS
    assert "请填写模型名称。请复制您在服务商控制台实际可用的模型 ID。" in JS
    assert 'if(action==="save-provider"){const problem=providerDraftProblem(body)' in JS
    assert 'byId(problem[1])?.focus()' in JS


def test_browser_gui_keeps_directory_paths_manual_and_exposes_no_picker_api() -> None:
    assert 'window.pywebview?.api' in JS
    assert '浏览器模式：请在左侧手动输入完整路径' in JS
    assert 'api("choose-directory"' not in JS


def test_gui_cli_uses_a_stable_default_port() -> None:
    args = build_parser().parse_args(["gui"])

    assert args.port == 8765
