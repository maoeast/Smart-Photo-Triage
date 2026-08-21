"""Local-only, browser-based control panel for the safe SPT workflow."""

from __future__ import annotations

import ipaddress
import json
import secrets
import threading
import webbrowser
from dataclasses import asdict, is_dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from smart_photo_triage.ai import AnalysisOptions, FakeVisionProvider, analyze_workspace
from smart_photo_triage.executor import apply_plan, doctor_workspace, rollback_transaction
from smart_photo_triage.grouping import group_workspace
from smart_photo_triage.planner import PlannerOptions, approve_plan, build_plan, preflight_plan
from smart_photo_triage.preprocess import PreviewConfig, preprocess_workspace
from smart_photo_triage.review import create_review_server
from smart_photo_triage.scanner import scan_library, validate_scan_layout
from smart_photo_triage.workspace import Workspace, initialize_workspace

_MAX_REQUEST_BYTES = 16_384
_EXECUTE_CONFIRMATION = "EXECUTE"
_ROLLBACK_CONFIRMATION = "ROLLBACK"


def _result(value: object) -> object:
    if hasattr(value, "to_dict"):
        return value.to_dict()  # type: ignore[no-any-return]
    if is_dataclass(value):
        return asdict(value)
    return value


class GuiHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], workspace: Workspace) -> None:
        self.workspace = workspace
        self.csrf_token = secrets.token_urlsafe(32)
        self.operation_lock = threading.Lock()
        self.review_servers: list[tuple[object, threading.Thread]] = []
        super().__init__(address, GuiRequestHandler)

    def close_review_servers(self) -> None:
        for server, thread in self.review_servers:
            server.shutdown()  # type: ignore[attr-defined]
            server.server_close()  # type: ignore[attr-defined]
            thread.join(timeout=5)
        self.review_servers.clear()


class GuiRequestHandler(BaseHTTPRequestHandler):
    server: GuiHTTPServer

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def _valid_host(self) -> bool:
        return (
            self.headers.get("Host", "").casefold() == urlsplit(self._base_url()).netloc.casefold()
        )

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; script-src 'self'; "
            "base-uri 'none'; frame-ancestors 'none'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: object) -> None:
        self._send(
            status,
            json.dumps(payload, ensure_ascii=False, default=str).encode(),
            "application/json; charset=utf-8",
        )

    def do_GET(self) -> None:  # noqa: N802
        if not self._valid_host():
            self._json(HTTPStatus.BAD_REQUEST, {"error": "INVALID_HOST"})
            return
        target = urlsplit(self.path)
        if target.path == "/":
            self._send(HTTPStatus.OK, _HTML.encode(), "text/html; charset=utf-8")
        elif target.path == "/app.js":
            self._send(HTTPStatus.OK, _JS.encode(), "text/javascript; charset=utf-8")
        elif target.path == "/app.css":
            self._send(HTTPStatus.OK, _CSS.encode(), "text/css; charset=utf-8")
        elif target.path == "/api/bootstrap":
            self._json(
                HTTPStatus.OK,
                {
                    "csrf_token": self.server.csrf_token,
                    "workspace": str(self.server.workspace.root),
                    "execute_confirmation": _EXECUTE_CONFIRMATION,
                    "rollback_confirmation": _ROLLBACK_CONFIRMATION,
                },
            )
        elif target.path == "/api/doctor":
            try:
                self._json(HTTPStatus.OK, _result(doctor_workspace(self.server.workspace)))
            except (OSError, ValueError):
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "LOCAL_STATE_ERROR"})
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "NOT_FOUND"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._valid_host() or self.headers.get("Origin") != self._base_url():
            self._json(HTTPStatus.FORBIDDEN, {"error": "INVALID_ORIGIN"})
            return
        if not secrets.compare_digest(self.headers.get("X-SPT-CSRF", ""), self.server.csrf_token):
            self._json(HTTPStatus.FORBIDDEN, {"error": "INVALID_CSRF"})
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
            if not 2 <= length <= _MAX_REQUEST_BYTES:
                raise ValueError
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "INVALID_JSON"})
            return
        action = urlsplit(self.path).path.removeprefix("/api/")
        try:
            with self.server.operation_lock:
                result = self._run(action, payload)
        except (OSError, ValueError) as error:
            self._json(
                HTTPStatus.BAD_REQUEST, {"error": "OPERATION_REJECTED", "detail": str(error)}
            )
            return
        except Exception:  # noqa: BLE001
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "LOCAL_OPERATION_FAILED"})
            return
        self._json(HTTPStatus.OK, result)

    def _run(self, action: str, payload: dict[str, object]) -> object:
        workspace = self.server.workspace
        if action == "scan":
            source = Path(_text(payload, "source"))
            output = Path(_text(payload, "output"))
            validate_scan_layout(source, workspace.root, output)
            return _result(scan_library(workspace, source, output=output, hash_content=True))
        if action == "prepare":
            pre = preprocess_workspace(workspace, config=PreviewConfig())
            grouped = group_workspace(workspace)
            analysis = analyze_workspace(
                workspace, provider=FakeVisionProvider(), options=AnalysisOptions()
            )
            return {
                "preprocess": _result(pre),
                "group": _result(grouped),
                "analysis": _result(analysis),
            }
        if action == "review":
            review = create_review_server(workspace, port=0)
            thread = threading.Thread(target=review.serve_forever, daemon=True)
            thread.start()
            self.server.review_servers.append((review, thread))
            return {"url": f"http://127.0.0.1:{review.server_address[1]}/"}
        if action == "plan":
            plan = build_plan(
                workspace, PlannerOptions(output_root=Path(_text(payload, "output")), mode="COPY")
            )
            return {
                "plan_id": plan.plan_id,
                "entry_count": len(plan.entries),
                "approval_state": plan.approval_state,
                "payload_sha256": plan.payload_sha256,
            }
        if action == "rollback":
            if _text(payload, "confirmation") != _ROLLBACK_CONFIRMATION:
                raise ValueError("type ROLLBACK to permit rollback")
            return _result(rollback_transaction(workspace, _text(payload, "transaction_id")))
        plan_id = _text(payload, "plan_id")
        if action == "approve":
            return _result(approve_plan(workspace, plan_id))
        if action == "preflight":
            return _result(preflight_plan(workspace, plan_id))
        if action == "dry-run":
            report = preflight_plan(workspace, plan_id)
            if not report.ok:
                return {"preflight": _result(report), "applied": None}
            return {
                "preflight": _result(report),
                "applied": _result(apply_plan(workspace, report, dry_run=True)),
            }
        if action == "execute":
            if _text(payload, "confirmation") != _EXECUTE_CONFIRMATION:
                raise ValueError("type EXECUTE to permit file changes")
            report = preflight_plan(workspace, plan_id)
            if not report.ok:
                return {"preflight": _result(report), "applied": None}
            return {
                "preflight": _result(report),
                "applied": _result(apply_plan(workspace, report, dry_run=False)),
            }
        raise ValueError("unknown GUI action")


def _text(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise ValueError(f"{key} is required")
    return value.strip()


def create_gui_server(
    workspace_root: Path, *, host: str = "127.0.0.1", port: int = 0
) -> GuiHTTPServer:
    address = ipaddress.ip_address(host)
    if not address.is_loopback or address.version != 4 or str(address) != "127.0.0.1":
        raise ValueError("GUI may bind only to 127.0.0.1")
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("GUI port is invalid")
    return GuiHTTPServer((host, port), initialize_workspace(workspace_root))


def serve_gui(workspace_root: Path, *, port: int = 0, open_browser: bool = True) -> None:
    server = create_gui_server(workspace_root, port=port)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Smart Photo Triage GUI: {url}")
    try:
        if open_browser:
            webbrowser.open(url, new=2)
        server.serve_forever()
    finally:
        server.close_review_servers()
        server.server_close()


_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>Smart Photo Triage</title>
<link rel="stylesheet" href="/app.css"></head><body><main><h1>Smart Photo Triage</h1>
<p>本地工作流。扫描和准备阶段只读，真实复制必须输入确认词。</p>
<label>工作区 <input id="workspace" readonly></label><label>照片源 <input id="source"></label>
<label>输出目录 <input id="output"></label><section><button data-action="scan">1. 扫描</button>
<button data-action="prepare">2. 准备</button><button data-action="review">3. 审核</button>
<button data-action="plan">4. 计划</button></section><label>计划 ID <input id="plan_id"></label>
<section><button data-action="approve">5. 批准</button><button data-action="preflight">6. 预检</button>
<button data-action="dry-run">7. 模拟执行</button></section><label>确认词 <input id="confirmation"></label>
<button class="danger" data-action="execute">8. 真正复制</button><button data-action="doctor">诊断</button>
<label>事务 ID <input id="transaction_id"></label><button data-action="rollback">安全回滚</button>
<pre id="result">正在连接本地控制台…</pre></main><script src="/app.js"></script></body></html>"""
_CSS = """body{font:16px system-ui;margin:0;background:#f6f7fb;color:#18212f}
main{max-width:900px;margin:32px auto;padding:24px;background:#fff;border-radius:12px}label{display:block;margin:14px 0}
input{display:block;width:100%;box-sizing:border-box;padding:9px;margin-top:5px}button{padding:10px;margin:5px}.danger{background:#a22;color:#fff}
pre{white-space:pre-wrap;background:#111;color:#d8f8d8;padding:16px;border-radius:8px;min-height:120px}"""
_JS = """let csrf;const byId=id=>document.getElementById(id),result=byId("result");
async function call(action){const p={source:byId("source").value,output:byId("output").value,plan_id:byId("plan_id").value,transaction_id:byId("transaction_id").value,confirmation:byId("confirmation").value};const r=await fetch("/api/"+action,{method:"POST",headers:{"Content-Type":"application/json","X-SPT-CSRF":csrf},body:JSON.stringify(p)});const d=await r.json();result.textContent=JSON.stringify(d,null,2);if(d.plan_id)byId("plan_id").value=d.plan_id;if(d.transaction_id)byId("transaction_id").value=d.transaction_id;if(d.url)window.open(d.url,"_blank","noopener");}
async function boot(){const d=await (await fetch("/api/bootstrap")).json();csrf=d.csrf_token;byId("workspace").value=d.workspace;result.textContent="准备就绪。";document.querySelectorAll("[data-action]").forEach(b=>b.onclick=()=>call(b.dataset.action));}boot().catch(e=>result.textContent=String(e));"""
