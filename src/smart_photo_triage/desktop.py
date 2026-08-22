"""Windows desktop launcher for the local Smart Photo Triage GUI.

The HTTP application remains loopback-only.  Pywebview owns the native window and
the folder-selection bridge, so browser visitors cannot trigger operating-system
dialogs or receive local directory information.
"""

from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path
from typing import Protocol

from smart_photo_triage.gui import GuiHTTPServer, _ai_summary, create_gui_server
from smart_photo_triage.workspace import initialize_workspace

_PICKABLE_FIELDS = {"workspace", "source", "output"}
_WEBVIEW2_HELP = "https://developer.microsoft.com/microsoft-edge/webview2/"


class DesktopLaunchError(RuntimeError):
    """The desktop shell could not be made available to the user."""


class _Window(Protocol):
    def create_file_dialog(self, dialog_type: object, *, directory: str) -> object: ...


class _Webview(Protocol):
    FOLDER_DIALOG: object

    def create_window(self, title: str, url: str, **kwargs: object) -> _Window: ...

    def start(self, *, gui: str) -> None: ...


class DesktopBridge:
    """The only desktop-only capability exposed to the bundled web page."""

    def __init__(self, server: GuiHTTPServer, webview: _Webview) -> None:
        self._server = server
        self._webview = webview
        self._window: _Window | None = None

    def attach_window(self, window: _Window) -> None:
        self._window = window

    def choose_directory(self, field: str) -> dict[str, object]:
        if field not in _PICKABLE_FIELDS:
            raise ValueError("目录字段无效")
        if self._window is None:
            raise RuntimeError("桌面窗口尚未准备完成")
        with self._server.operation_lock:
            selected = self._window.create_file_dialog(
                self._webview.FOLDER_DIALOG, directory=str(self._server.workspace.root)
            )
            selected_path = _first_path(selected)
            if selected_path is not None and field == "workspace":
                self._server.workspace = initialize_workspace(selected_path)
            workspace = self._server.workspace
            return {
                "field": field,
                "selected": str(selected_path) if selected_path is not None else None,
                "workspace": str(workspace.root),
                "ai": _ai_summary(workspace),
            }


def _first_path(value: object) -> Path | None:
    if value is None or value == "":
        return None
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if not isinstance(value, str) or not value:
        return None
    return Path(value)


def _load_webview() -> _Webview:
    try:
        import webview
    except ImportError as error:
        raise DesktopLaunchError(
            "桌面组件未安装。请使用发布包重新安装 Smart Photo Triage。"
        ) from error
    return webview  # type: ignore[return-value]


def launch_desktop(workspace_root: Path, *, webview_module: _Webview | None = None) -> None:
    """Run one local GUI server until the native window closes."""
    webview = webview_module or _load_webview()
    server = create_gui_server(workspace_root, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    bridge = DesktopBridge(server, webview)
    try:
        window = webview.create_window(
            "Smart Photo Triage",
            url,
            js_api=bridge,
            width=1280,
            height=900,
            min_size=(960, 680),
        )
        bridge.attach_window(window)
        webview.start(gui="edgechromium")
    except Exception as error:  # noqa: BLE001
        raise DesktopLaunchError(
            "桌面版无法启动。请安装 Microsoft Edge WebView2 Runtime 后重试："
            f"{_WEBVIEW2_HELP}"
        ) from error
    finally:
        server.shutdown()
        server.close_review_servers()
        server.server_close()
        thread.join(timeout=5)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="smart-photo-triage-desktop")
    parser.add_argument(
        "--workspace", type=Path, default=Path(".spt"), help="工作区路径，默认 .spt"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        launch_desktop(args.workspace)
    except DesktopLaunchError as error:
        message = f"Smart Photo Triage 桌面版启动失败：{error}"
        print(message, file=sys.stderr)
        if sys.platform == "win32":
            try:
                import ctypes

                ctypes.windll.user32.MessageBoxW(None, message, "Smart Photo Triage", 0x10)
            except Exception:  # noqa: BLE001
                pass
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
