"""Local-only, browser-based control panel for the safe SPT workflow."""

from __future__ import annotations

import ipaddress
import json
import secrets
import threading
import webbrowser
from dataclasses import asdict, is_dataclass, replace
from errno import EADDRINUSE
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from smart_photo_triage.ai import (
    AnalysisOptions,
    FakeVisionProvider,
    analyze_workspace_routed,
)
from smart_photo_triage.config import (
    AppConfig,
    load_config,
    normalized_ai_config,
    save_ai_config,
    save_config,
)
from smart_photo_triage.copy_jobs import CopyJob
from smart_photo_triage.executor import apply_plan, doctor_workspace, rollback_transaction
from smart_photo_triage.grouping import group_workspace
from smart_photo_triage.gui_assets import CSS, HTML, JS, SETTINGS_HTML
from smart_photo_triage.gui_progress_assets import COPY_PROGRESS_JS
from smart_photo_triage.gui_secrets import (
    GuiSecretError,
    load_provider_secret,
    remove_provider_secret,
    save_provider_secret,
)
from smart_photo_triage.model_routing import (
    ModelRouter,
    ProviderConfig,
    ProviderRegistry,
    RoutePolicy,
    TaskType,
    builtin_capabilities,
    classify_endpoint,
)
from smart_photo_triage.planner import PlannerOptions, approve_plan, build_plan, preflight_plan
from smart_photo_triage.preprocess import PreviewConfig, preprocess_workspace
from smart_photo_triage.provider_drivers import build_driver
from smart_photo_triage.review import create_review_server
from smart_photo_triage.scanner import scan_library, validate_scan_layout
from smart_photo_triage.workspace import Workspace, initialize_workspace

_MAX_REQUEST_BYTES = 16_384
_CLOUD_CONFIRMATION = "ALLOW_CLOUD"
_LAN_CONFIRMATION = "ALLOW_LAN"
_EXECUTE_CONFIRMATION = "EXECUTE"
_ROLLBACK_CONFIRMATION = "ROLLBACK"
_GUI_VERSION = "1.2.1"
_DEFAULT_PROVIDER_URLS = {
    "fake": "http://127.0.0.1:9999/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
}


def _result(value: object) -> object:
    if hasattr(value, "to_dict"):
        return value.to_dict()  # type: ignore[no-any-return]
    if is_dataclass(value):
        return asdict(value)
    return value


def _provider_has_saved_secret(workspace: Workspace, provider_id: str) -> bool:
    try:
        return load_provider_secret(workspace, provider_id) is not None
    except GuiSecretError:
        return False


def _ai_summary(workspace: Workspace) -> dict[str, object]:
    config = normalized_ai_config(load_config(workspace.config_path))
    routes = config.route_map()
    return {
        "version": _GUI_VERSION,
        "allow_cloud": config.allow_cloud,
        "allow_lan": config.allow_lan,
        "providers": [
            {
                **provider.redacted_summary(),
                "api_key_configured": (
                    provider.resolve_api_key() is not None
                    or _provider_has_saved_secret(workspace, provider.provider_id)
                ),
                "display_name": provider.display_name,
                "base_url": provider.base_url,
                "api_key_env": provider.api_key_env,
            }
            for provider in config.providers
        ],
        "routes": {
            task_type.value: {
                "primary": route.primary,
                "fallbacks": list(route.fallbacks),
                "confidence_below": route.confidence_below,
                "escalate_to": route.escalate_to,
            }
            for task_type, route in routes.items()
        },
    }


def _router_from_workspace_config(workspace: Workspace) -> tuple[AppConfig, ModelRouter]:
    config = normalized_ai_config(load_config(workspace.config_path))
    registry = ProviderRegistry(config.providers)
    drivers: dict[str, object] = {}
    for provider in registry.providers:
        if provider.driver == "fake":
            drivers[provider.provider_id] = FakeVisionProvider(model=provider.model)
            continue
        saved_api_key = load_provider_secret(workspace, provider.provider_id)
        if saved_api_key is not None or provider.resolve_api_key() is not None:
            drivers[provider.provider_id] = build_driver(provider, api_key=saved_api_key)
    return config, ModelRouter(
        registry,
        config.route_map(),
        drivers=drivers,
        budget=config.budget,
        max_provider_attempts_per_task=config.max_provider_attempts_per_task,
    )


def _optional_text(payload: dict[str, object], key: str, *, maximum: int = 4096) -> str | None:
    value = payload.get(key, "")
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f"{key} is invalid")
    return value.strip() or None


def _provider_from_payload(payload: dict[str, object]) -> ProviderConfig:
    driver = _choice(
        payload,
        "driver",
        {"fake", "gemini", "openai", "anthropic", "openai_compatible"},
        default="fake",
    )
    base_url = _optional_text(payload, "base_url", maximum=1024) or _DEFAULT_PROVIDER_URLS.get(
        driver
    )
    if base_url is None:
        raise ValueError("OpenAI-compatible providers require a base URL")
    return ProviderConfig(
        provider_id=_required_text(payload, "provider_id", maximum=64),
        driver=driver,
        model=_required_text(payload, "model", maximum=256),
        base_url=base_url,
        network_scope=classify_endpoint(base_url),
        capabilities=builtin_capabilities(driver),
        display_name=_optional_text(payload, "display_name", maximum=128),
        api_key_env=_optional_text(payload, "api_key_env", maximum=128),
    )


def _csv_provider_ids(payload: dict[str, object], key: str) -> tuple[str, ...]:
    raw = _optional_text(payload, key, maximum=1024)
    if raw is None:
        return ()
    values = tuple(item.strip() for item in raw.split(",") if item.strip())
    if len(values) != len(set(values)):
        raise ValueError(f"{key} contains duplicate provider IDs")
    return values


def _route_from_payload(payload: dict[str, object], prefix: str) -> RoutePolicy:
    primary = _required_text(payload, f"{prefix}_primary", maximum=64)
    fallbacks = _csv_provider_ids(payload, f"{prefix}_fallbacks")
    threshold_text = _optional_text(payload, f"{prefix}_confidence_below", maximum=32)
    escalation = _optional_text(payload, f"{prefix}_escalate_to", maximum=64)
    if threshold_text is None and escalation is None:
        return RoutePolicy(primary, fallbacks)
    if threshold_text is None or escalation is None:
        raise ValueError(f"{prefix} escalation needs a provider and confidence threshold")
    try:
        threshold = float(threshold_text)
    except ValueError as error:
        raise ValueError(f"{prefix} confidence threshold is invalid") from error
    return RoutePolicy(primary, fallbacks, threshold, escalation, 1)


class GuiHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], workspace: Workspace) -> None:
        self.workspace = workspace
        self.csrf_token = secrets.token_urlsafe(32)
        self.operation_lock = threading.Lock()
        self.review_servers: list[tuple[object, threading.Thread]] = []
        self.copy_job: CopyJob | None = None
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
            self._send(HTTPStatus.OK, HTML.encode(), "text/html; charset=utf-8")
        elif target.path == "/settings":
            self._send(HTTPStatus.OK, SETTINGS_HTML.encode(), "text/html; charset=utf-8")
        elif target.path == "/app.js":
            self._send(HTTPStatus.OK, JS.encode(), "text/javascript; charset=utf-8")
        elif target.path == "/copy-progress.js":
            self._send(HTTPStatus.OK, COPY_PROGRESS_JS.encode(), "text/javascript; charset=utf-8")
        elif target.path == "/app.css":
            self._send(HTTPStatus.OK, CSS.encode(), "text/css; charset=utf-8")
        elif target.path == "/favicon.ico":
            self._send(HTTPStatus.NO_CONTENT, b"", "image/x-icon")
        elif target.path == "/api/bootstrap":
            ai = _ai_summary(self.server.workspace)
            self._json(
                HTTPStatus.OK,
                {
                    "csrf_token": self.server.csrf_token,
                    "workspace": str(self.server.workspace.root),
                    "cloud_confirmation": _CLOUD_CONFIRMATION,
                    "lan_confirmation": _LAN_CONFIRMATION,
                    "execute_confirmation": _EXECUTE_CONFIRMATION,
                    "rollback_confirmation": _ROLLBACK_CONFIRMATION,
                    **ai,
                },
            )
        elif target.path == "/api/doctor":
            try:
                self._json(HTTPStatus.OK, _result(doctor_workspace(self.server.workspace)))
            except (OSError, ValueError):
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "LOCAL_STATE_ERROR"})
        elif target.path == "/api/copy-job":
            job = self.server.copy_job
            self._json(HTTPStatus.OK, {"job": job.snapshot() if job is not None else None})
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
            config, router = _router_from_workspace_config(workspace)
            analysis = analyze_workspace_routed(
                workspace,
                router=router,
                options=AnalysisOptions(),
                allow_cloud=config.allow_cloud,
                allow_lan=config.allow_lan,
            )
            return {
                "preprocess": _result(pre),
                "group": _result(grouped),
                "analysis": _result(analysis),
            }
        if action == "configure-cloud":
            enabled = _bool(payload, "allow_cloud")
            if (
                enabled
                and _required_text(payload, "confirmation", maximum=64) != _CLOUD_CONFIRMATION
            ):
                raise ValueError("type ALLOW_CLOUD to enable cloud analysis")
            save_config(workspace.config_path, AppConfig(allow_cloud=enabled))
            return {"allow_cloud": enabled, "api_key_stored": False}
        if action == "save-provider":
            config = normalized_ai_config(load_config(workspace.config_path))
            provider = _provider_from_payload(payload)
            providers = tuple(
                provider if item.provider_id == provider.provider_id else item
                for item in config.providers
            )
            if not any(item.provider_id == provider.provider_id for item in config.providers):
                providers = (*providers, provider)
            save_ai_config(workspace.config_path, replace(config, providers=providers))
            api_key = _optional_text(payload, "api_key", maximum=4096)
            if api_key is not None:
                save_provider_secret(workspace, provider.provider_id, api_key)
            return {
                "saved_provider_id": provider.provider_id,
                "api_key_saved": api_key is not None,
                "ai": _ai_summary(workspace),
            }
        if action == "forget-provider-key":
            provider_id = _required_text(payload, "provider_id", maximum=64)
            return {
                "removed": remove_provider_secret(workspace, provider_id),
                "ai": _ai_summary(workspace),
            }
        if action == "save-routes":
            config = normalized_ai_config(load_config(workspace.config_path))
            routes = (
                (TaskType.ITEM_ANALYSIS, _route_from_payload(payload, "item")),
                (TaskType.BURST_REVIEW, _route_from_payload(payload, "burst")),
            )
            save_ai_config(workspace.config_path, replace(config, routes=routes))
            return {"ai": _ai_summary(workspace)}
        if action == "configure-privacy":
            config = normalized_ai_config(load_config(workspace.config_path))
            allow_cloud = _bool(payload, "allow_cloud")
            allow_lan = _bool(payload, "allow_lan")
            if (
                allow_cloud
                and not config.allow_cloud
                and _required_text(payload, "cloud_confirmation", maximum=64) != _CLOUD_CONFIRMATION
            ):
                raise ValueError("type ALLOW_CLOUD to enable remote providers")
            if (
                allow_lan
                and not config.allow_lan
                and _required_text(payload, "lan_confirmation", maximum=64) != _LAN_CONFIRMATION
            ):
                raise ValueError("type ALLOW_LAN to enable LAN providers")
            save_ai_config(
                workspace.config_path,
                replace(config, allow_cloud=allow_cloud, allow_lan=allow_lan),
            )
            return {"ai": _ai_summary(workspace), "api_key_stored": False}
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
            active_job = self.server.copy_job
            if active_job is not None and active_job.snapshot()["state"] in {"QUEUED", "RUNNING"}:
                raise ValueError("a copy job is already running")
            job = CopyJob(workspace, report)
            self.server.copy_job = job
            job.start()
            return {"preflight": _result(report), "copy_job": job.snapshot()}
        raise ValueError("unknown GUI action")


def _text(payload: dict[str, object], key: str) -> str:
    return _required_text(payload, key, maximum=4096)


def _required_text(payload: dict[str, object], key: str, *, maximum: int) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{key} is required")
    return value.strip()


def _secret(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise ValueError("a valid API key is required")
    return value.strip()


def _choice(payload: dict[str, object], key: str, choices: set[str], *, default: str) -> str:
    value = payload.get(key, default)
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"{key} is invalid")
    return value


def _bool(payload: dict[str, object], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be true or false")
    return value


def create_gui_server(
    workspace_root: Path, *, host: str = "127.0.0.1", port: int = 0
) -> GuiHTTPServer:
    address = ipaddress.ip_address(host)
    if not address.is_loopback or address.version != 4 or str(address) != "127.0.0.1":
        raise ValueError("GUI may bind only to 127.0.0.1")
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("GUI port is invalid")
    return GuiHTTPServer((host, port), initialize_workspace(workspace_root))


def serve_gui(workspace_root: Path, *, port: int = 8765, open_browser: bool = True) -> None:
    try:
        server = create_gui_server(workspace_root, port=port)
    except OSError as error:
        if error.errno in {EADDRINUSE, 10048} or getattr(error, "winerror", None) == 10048:
            raise ValueError(
                f"GUI port {port} is already in use. Close the existing GUI or use --port."
            ) from error
        raise
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
<label>输出目录 <input id="output"></label><fieldset><legend>分析模型</legend>
<label>提供方 <select id="provider"><option value="fake">离线演示模型（不上传）</option><option value="gemini">Google Gemini（上传受控预览）</option></select></label>
<label>Gemini 模型 <input id="model" placeholder="例如：你在 Gemini 控制台启用的模型 ID"></label>
<label>Gemini API Key <input id="api_key" type="password" autocomplete="off" placeholder="仅用于本次准备，不保存"></label>
<label class="check"><input id="allow_cloud" type="checkbox"> 我理解 Gemini 会接收受控预览图和必要元数据</label>
<label>启用云端确认词 <input id="cloud_confirmation" placeholder="启用时输入 ALLOW_CLOUD"></label>
<button data-action="configure-cloud">保存云端授权</button><p class="hint">API Key 不会写入工作区或配置文件。关闭勾选并保存可禁用云端。</p></fieldset>
<section><button data-action="scan">1. 扫描</button><button data-action="prepare">2. 准备</button>
<button data-action="review">3. 审核</button><button data-action="plan">4. 计划</button></section><label>计划 ID <input id="plan_id"></label>
<section><button data-action="approve">5. 批准</button><button data-action="preflight">6. 预检</button>
<button data-action="dry-run">7. 模拟执行</button></section><label>确认词 <input id="confirmation"></label>
<button class="danger" data-action="execute">8. 真正复制</button><button data-action="doctor">诊断</button>
<label>事务 ID <input id="transaction_id"></label><button data-action="rollback">安全回滚</button>
<pre id="result">正在连接本地控制台…</pre></main><script src="/app.js"></script></body></html>"""
_CSS = """body{font:16px system-ui;margin:0;background:#f6f7fb;color:#18212f}
main{max-width:900px;margin:32px auto;padding:24px;background:#fff;border-radius:12px}label{display:block;margin:14px 0}
input,select{display:block;width:100%;box-sizing:border-box;padding:9px;margin-top:5px}button{padding:10px;margin:5px}.danger{background:#a22;color:#fff}
fieldset{margin:20px 0;border:1px solid #bdc7d8;border-radius:8px}.check input{display:inline;width:auto;margin-right:8px}.hint{font-size:.9em;color:#4a5568}
pre{white-space:pre-wrap;background:#111;color:#d8f8d8;padding:16px;border-radius:8px;min-height:120px}"""
_JS = """let csrf;const byId=id=>document.getElementById(id),result=byId("result");
async function call(action){const p={source:byId("source").value,output:byId("output").value,plan_id:byId("plan_id").value,transaction_id:byId("transaction_id").value,confirmation:byId("confirmation").value,provider:byId("provider").value,model:byId("model").value,api_key:byId("api_key").value,allow_cloud:byId("allow_cloud").checked};if(action==="configure-cloud")p.confirmation=byId("cloud_confirmation").value;const r=await fetch("/api/"+action,{method:"POST",headers:{"Content-Type":"application/json","X-SPT-CSRF":csrf},body:JSON.stringify(p)});const d=await r.json();result.textContent=JSON.stringify(d,null,2);if(d.plan_id)byId("plan_id").value=d.plan_id;if(d.transaction_id)byId("transaction_id").value=d.transaction_id;if(d.url)window.open(d.url,"_blank","noopener");}
async function boot(){const d=await (await fetch("/api/bootstrap")).json();csrf=d.csrf_token;byId("workspace").value=d.workspace;byId("allow_cloud").checked=Boolean(d.allow_cloud);result.textContent="准备就绪。";document.querySelectorAll("[data-action]").forEach(b=>b.onclick=()=>call(b.dataset.action));}boot().catch(e=>result.textContent=String(e));"""
