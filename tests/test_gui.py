from __future__ import annotations

import json
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from PIL import Image

from smart_photo_triage.gui import create_gui_server


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
        executed = _post(base, "execute", csrf, common)["applied"]
        assert executed["state"] == "DONE"
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
