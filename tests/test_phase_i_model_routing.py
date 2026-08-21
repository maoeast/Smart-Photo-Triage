"""v1.2.1 provider platform contracts, using synthetic requests only."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from urllib.error import HTTPError

import pytest

import smart_photo_triage.database as database_module
from smart_photo_triage.ai import VisionRequest, VisionRequestItem, analyze_workspace_routed
from smart_photo_triage.cli import main
from smart_photo_triage.database import apply_migrations, connect_database
from smart_photo_triage.model_routing import (
    Budget,
    ErrorClass,
    FakeDriver,
    ModelRouter,
    NetworkScope,
    ProviderCapabilities,
    ProviderConfig,
    ProviderFailure,
    ProviderRegistry,
    RoutePolicy,
    TaskType,
    classify_endpoint,
    validate_provider_network_access,
)
from smart_photo_triage.provider_drivers import build_driver
from smart_photo_triage.routing_storage import SQLiteProviderCache, persist_route_execution
from smart_photo_triage.workspace import initialize_workspace


def request() -> VisionRequest:
    return VisionRequest(
        items=(
            VisionRequestItem(
                item_id=1,
                media_type="IMAGE",
                preview_mime_type="image/png",
                preview_bytes=b"synthetic-preview",
                quality={"score": 0.8},
            ),
        ),
        prompt_version="synthetic-prompt-v1",
        schema_version="synthetic-schema-v1",
    )


def result(*, confidence: float = 0.95) -> list[dict[str, object]]:
    return [
        {
            "item_id": 1,
            "scene_category": "01_家庭生活",
            "disposition": "KEEP",
            "confidence": confidence,
            "quality_score": 0.8,
            "tags": ["synthetic"],
            "short_desc": "synthetic item",
            "reason": "synthetic contract result",
        }
    ]


def capabilities(*, multi_image: bool = True) -> ProviderCapabilities:
    return ProviderCapabilities(
        supports_image=True,
        supports_multi_image=multi_image,
        supports_structured_json=True,
        supports_json_schema=True,
        max_images_per_request=8,
        max_request_bytes=1024 * 1024,
        supported_image_mime_types=frozenset({"image/png"}),
        supports_system_prompt=True,
        capability_profile_version="synthetic-v1",
    )


def provider(
    provider_id: str,
    *,
    scope: NetworkScope = NetworkScope.LOOPBACK,
    enabled: bool = True,
    multi_image: bool = True,
) -> ProviderConfig:
    base_url = {
        NetworkScope.LOOPBACK: "http://127.0.0.1:9999/v1",
        NetworkScope.LAN: "http://192.168.1.10:9999/v1",
        NetworkScope.REMOTE: "https://api.example.test/v1",
    }[scope]
    return ProviderConfig(
        provider_id=provider_id,
        driver="fake",
        model=f"{provider_id}-model",
        base_url=base_url,
        network_scope=scope,
        capabilities=capabilities(multi_image=multi_image),
        enabled=enabled,
    )


def test_t_b_registry_rejects_duplicate_disabled_and_invalid_route_references() -> None:
    first = provider("fast")
    with pytest.raises(ValueError, match="duplicate"):
        ProviderRegistry((first, first))

    registry = ProviderRegistry((first, provider("disabled", enabled=False)))
    with pytest.raises(ValueError, match="disabled"):
        ModelRouter(registry, {TaskType.ITEM_ANALYSIS: RoutePolicy("disabled")})
    with pytest.raises(ValueError, match="unknown"):
        ModelRouter(registry, {TaskType.ITEM_ANALYSIS: RoutePolicy("missing")})
    with pytest.raises(ValueError, match="itself"):
        ModelRouter(registry, {TaskType.ITEM_ANALYSIS: RoutePolicy("fast", ("fast",))})


@pytest.mark.parametrize(
    ("url", "scope"),
    [
        ("http://127.0.0.1:1234/v1", NetworkScope.LOOPBACK),
        ("http://[::1]:1234/v1", NetworkScope.LOOPBACK),
        ("http://192.168.1.10:1234/v1", NetworkScope.LAN),
        ("https://api.example.test/v1", NetworkScope.REMOTE),
    ],
)
def test_t_d_endpoint_scope_classification_is_not_name_based(url: str, scope: NetworkScope) -> None:
    assert classify_endpoint(url) is scope


def test_t_d_network_policy_defaults_closed_and_rejects_userinfo_and_remote_http() -> None:
    remote = provider("remote", scope=NetworkScope.REMOTE)
    with pytest.raises(ProviderFailure, match="PRIVACY_BLOCKED"):
        validate_provider_network_access(remote, allow_cloud=False, allow_lan=False)
    with pytest.raises(ValueError, match="credentials"):
        ProviderConfig(
            provider_id="bad-url",
            driver="openai_compatible",
            model="model",
            base_url="https://user:password@example.test/v1",
            network_scope=NetworkScope.REMOTE,
            capabilities=capabilities(),
        )
    with pytest.raises(ValueError, match="HTTPS"):
        ProviderConfig(
            provider_id="bad-http",
            driver="openai_compatible",
            model="model",
            base_url="http://api.example.test/v1",
            network_scope=NetworkScope.REMOTE,
            capabilities=capabilities(),
        )


def test_t_e_local_failure_cannot_silently_use_remote_without_allow_cloud() -> None:
    local = provider("local")
    remote = provider("remote", scope=NetworkScope.REMOTE)
    registry = ProviderRegistry((local, remote))
    router = ModelRouter(
        registry,
        {TaskType.ITEM_ANALYSIS: RoutePolicy("local", ("remote",))},
        drivers={
            "local": FakeDriver(lambda _request: ProviderFailure(ErrorClass.NETWORK_ERROR)),
            "remote": FakeDriver(lambda _request: result()),
        },
    )

    execution = router.run(TaskType.ITEM_ANALYSIS, request(), allow_cloud=False)

    assert execution.result is None
    assert [attempt.provider_id for attempt in execution.attempts] == ["local", "remote"]
    assert execution.attempts[-1].error_class is ErrorClass.PRIVACY_BLOCKED


def test_t_e_fallback_escalation_cache_and_budget_are_bounded() -> None:
    fast = provider("fast")
    strong = provider("strong")
    registry = ProviderRegistry((fast, strong))
    router = ModelRouter(
        registry,
        {
            TaskType.ITEM_ANALYSIS: RoutePolicy(
                "fast", ("strong",), confidence_below=0.72, escalate_to="strong"
            )
        },
        drivers={
            "fast": FakeDriver(lambda _request: result(confidence=0.5)),
            "strong": FakeDriver(lambda _request: result(confidence=0.96)),
        },
        budget=Budget(max_requests=2),
    )

    first = router.run(TaskType.ITEM_ANALYSIS, request())
    second = router.run(TaskType.ITEM_ANALYSIS, request())

    assert first.effective_provider_id == "strong"
    assert first.escalated is True
    assert [attempt.provider_id for attempt in first.attempts] == ["fast", "strong"]
    assert all(attempt.status == "SUCCESS" for attempt in first.attempts)
    assert all(attempt.cache_hit for attempt in second.attempts)
    assert second.request_count == 0
    assert router.budget.requests_used == 2


def test_t_e_rate_limit_retries_once_then_falls_back_with_one_global_attempt_bound() -> None:
    calls: list[str] = []

    def limited(_request: VisionRequest) -> object:
        calls.append("fast")
        return ProviderFailure(ErrorClass.RATE_LIMIT)

    router = ModelRouter(
        ProviderRegistry((provider("fast"), provider("fallback"))),
        {TaskType.ITEM_ANALYSIS: RoutePolicy("fast", ("fallback",))},
        drivers={
            "fast": FakeDriver(limited),
            "fallback": FakeDriver(lambda _request: calls.append("fallback") or result()),
        },
        max_provider_attempts_per_task=3,
        max_retry_per_provider=1,
    )

    execution = router.run(TaskType.ITEM_ANALYSIS, request())

    assert execution.effective_provider_id == "fallback"
    assert [attempt.provider_id for attempt in execution.attempts] == ["fast", "fast", "fallback"]
    assert calls == ["fast", "fast", "fallback"]


def test_t_f_remote_byte_and_cost_budget_block_before_driver_or_fallback() -> None:
    remote = provider("remote", scope=NetworkScope.REMOTE)
    calls: list[str] = []
    router = ModelRouter(
        ProviderRegistry((remote,)),
        {TaskType.ITEM_ANALYSIS: RoutePolicy("remote")},
        drivers={"remote": FakeDriver(lambda _request: calls.append("called") or result())},
        budget=Budget(max_remote_preview_bytes=1),
    )

    execution = router.run(TaskType.ITEM_ANALYSIS, request(), allow_cloud=True)

    assert execution.result is None
    assert execution.attempts[0].error_class is ErrorClass.BUDGET_BLOCKED
    assert calls == []


def test_t_c_capability_mismatch_is_skipped_before_driver_and_auth_does_not_fallback() -> None:
    incompatible = provider("incompatible", multi_image=False)
    fallback = provider("fallback")
    calls: list[str] = []
    router = ModelRouter(
        ProviderRegistry((incompatible, fallback)),
        {TaskType.BURST_REVIEW: RoutePolicy("incompatible", ("fallback",))},
        drivers={
            "incompatible": FakeDriver(lambda _request: calls.append("bad") or result()),
            "fallback": FakeDriver(lambda _request: calls.append("fallback") or result()),
        },
    )
    burst = VisionRequest(
        items=(request().items[0], request().items[0]),
        prompt_version="synthetic-prompt-v1",
        schema_version="synthetic-schema-v1",
    )
    execution = router.run(TaskType.BURST_REVIEW, burst)
    assert calls == ["fallback"]
    assert execution.attempts[0].error_class is ErrorClass.CAPABILITY_MISMATCH

    auth_router = ModelRouter(
        ProviderRegistry((provider("first"), provider("second"))),
        {TaskType.ITEM_ANALYSIS: RoutePolicy("first", ("second",))},
        drivers={
            "first": FakeDriver(lambda _request: ProviderFailure(ErrorClass.AUTH_ERROR)),
            "second": FakeDriver(lambda _request: result()),
        },
    )
    auth = auth_router.run(TaskType.ITEM_ANALYSIS, request())
    assert len(auth.attempts) == 1
    assert auth.attempts[0].error_class is ErrorClass.AUTH_ERROR


def test_t_b_secret_is_environment_only_and_never_present_in_observability() -> None:
    os.environ["SPT_SYNTHETIC_KEY"] = "not-for-output"
    configured = ProviderConfig(
        provider_id="keyed",
        driver="fake",
        model="synthetic",
        base_url="http://127.0.0.1:9999/v1",
        api_key_env="SPT_SYNTHETIC_KEY",
        network_scope=NetworkScope.LOOPBACK,
        capabilities=capabilities(),
    )
    try:
        assert configured.resolve_api_key() == "not-for-output"
        assert "not-for-output" not in repr(configured.redacted_summary())
    finally:
        os.environ.pop("SPT_SYNTHETIC_KEY", None)


class _Response:
    def __init__(self, payload: object) -> None:
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _size: int = -1) -> bytes:
        return self.payload


@pytest.mark.parametrize("driver_name", ["gemini", "openai", "anthropic", "openai_compatible"])
def test_t_c_native_and_compatible_drivers_normalize_mock_structured_results(
    driver_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPT_DRIVER_TEST_KEY", "synthetic-secret")
    endpoint = {
        "gemini": "https://generativelanguage.googleapis.com/v1beta",
        "openai": "https://api.openai.com/v1",
        "anthropic": "https://api.anthropic.com/v1",
        "openai_compatible": "https://compatible.example.test/v1",
    }[driver_name]
    configured = ProviderConfig(
        provider_id=f"{driver_name}-test",
        driver=driver_name,
        model="synthetic-model",
        base_url=endpoint,
        api_key_env="SPT_DRIVER_TEST_KEY",
        network_scope=NetworkScope.REMOTE,
        capabilities=capabilities(),
    )
    seen: list[object] = []
    text = json.dumps(result())
    envelopes = {
        "gemini": {"candidates": [{"content": {"parts": [{"text": text}]}}]},
        "openai": {"choices": [{"message": {"content": text}}]},
        "openai_compatible": {"choices": [{"message": {"content": text}}]},
        "anthropic": {"content": [{"text": text}]},
    }

    def open_mock(http_request: object, timeout: float) -> _Response:
        assert timeout > 0
        seen.append(http_request)
        return _Response(envelopes[driver_name])

    driver = build_driver(configured, opener=open_mock)
    assert driver.analyze(request()) == result()
    serialized = repr(seen[0])
    assert "synthetic-secret" not in serialized


def test_t_a_and_f_additive_migration_preserves_v12_records_and_adds_route_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrations = database_module.MIGRATIONS
    assert migrations[-1].version >= 12
    database_path = tmp_path / "v12.sqlite3"
    workspace_id = "12345678123456781234567812345678"
    monkeypatch.setattr(database_module, "MIGRATIONS", migrations[:-1])
    with connect_database(database_path) as connection:
        assert apply_migrations(connection, workspace_id=workspace_id) == 11
        connection.execute(
            "INSERT INTO preprocess_run(id,config_fingerprint,preview_version,started_at,status) "
            "VALUES ('preserved-v12','cfg','v','now','COMPLETE')"
        )
        connection.commit()
        monkeypatch.setattr(database_module, "MIGRATIONS", migrations)
        assert apply_migrations(connection, workspace_id=workspace_id) == migrations[-1].version
        assert connection.execute("SELECT id FROM preprocess_run").fetchall() == [
            ("preserved-v12",)
        ]
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"provider_analysis_cache", "ai_route_run", "ai_route_attempt"} <= tables
        assert apply_migrations(connection, workspace_id=workspace_id) == migrations[-1].version


def test_t_f_sqlite_cache_is_provider_specific_and_audit_has_no_sensitive_payload(
    tmp_path: Path,
) -> None:
    with connect_database(tmp_path / "routing.sqlite3") as connection:
        apply_migrations(connection)
        registry = ProviderRegistry((provider("fast"), provider("strong")))
        cache = SQLiteProviderCache(connection)
        router = ModelRouter(
            registry,
            {
                TaskType.ITEM_ANALYSIS: RoutePolicy(
                    "fast", confidence_below=0.72, escalate_to="strong"
                )
            },
            drivers={
                "fast": FakeDriver(lambda _request: result(confidence=0.5)),
                "strong": FakeDriver(lambda _request: result(confidence=0.95)),
            },
            cache=cache,
        )
        execution = router.run(TaskType.ITEM_ANALYSIS, request())
        run_id = persist_route_execution(connection, execution)
        rerun = router.run(TaskType.ITEM_ANALYSIS, request())

        assert execution.effective_provider_id == "strong"
        assert all(attempt.cache_hit for attempt in rerun.attempts)
        assert connection.execute("SELECT COUNT(*) FROM provider_analysis_cache").fetchone() == (2,)
        assert connection.execute(
            "SELECT attempt_count,escalated FROM ai_route_run WHERE id=?", (run_id,)
        ).fetchone() == (2, 1)
        joined = " ".join(
            str(row[0])
            for row in connection.execute(
                "SELECT result_json || route_reason FROM provider_analysis_cache "
                "CROSS JOIN ai_route_attempt"
            )
        )
        assert "synthetic-preview" not in joined
        assert "SPT_" not in joined


def _seed_routed_preview(tmp_path: Path) -> tuple[object, int]:
    workspace = initialize_workspace(tmp_path / "workspace")
    preview = workspace.root / "previews" / "synthetic.png"
    preview.write_bytes(b"v1.2.1 synthetic preview only")
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = source_root / "source.png"
    source.write_bytes(b"never read by router")
    with sqlite3.connect(workspace.database_path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO media_item(
                original_path,path_key,source_root,source_root_key,parent_key,bundle_stem,
                media_type,extension,size_bytes,mtime_ns,source_present,content_sha256,
                capture_source,capture_confidence,capture_timezone_status,last_seen_at,last_seen_scan_id
            ) VALUES (
                ?,?,?,?,?,?, 'IMAGE','.png',1,1,1,'source-hash',
                'SYNTHETIC','HIGH','UNKNOWN','now','scan'
            )
            """,
            (
                str(source),
                str(source).casefold(),
                str(source_root),
                str(source_root).casefold(),
                str(source_root).casefold(),
                "source",
            ),
        )
        media_id = int(cursor.lastrowid)
        connection.execute(
            """
            INSERT INTO media_preprocess(
                media_id,source_fingerprint,preview_fingerprint,preview_path,preview_version,
                preview_status,quality_json,updated_at,preview_sha256
            ) VALUES (?, 'source-fp','preview-fp',?,'preview-v1','READY','{"score":0.8}','now',?)
            """,
            (media_id, str(preview), hashlib.sha256(preview.read_bytes()).hexdigest()),
        )
    return workspace, media_id


def test_t_b_gui_cloud_toggle_preserves_advanced_provider_configuration(tmp_path: Path) -> None:
    from smart_photo_triage.config import AppConfig, load_config, save_config

    path = tmp_path / "config.toml"
    path.write_text(
        """
[ai]
allow_cloud = false
allow_lan = false

[ai.providers.local]
driver = "openai_compatible"
model = "synthetic-local"
base_url = "http://127.0.0.1:9999/v1"
network_scope = "loopback"
""".lstrip(),
        encoding="utf-8",
    )
    save_config(path, AppConfig(allow_cloud=True))
    reloaded = load_config(path)

    assert reloaded.allow_cloud is True
    assert reloaded.providers[0].model == "synthetic-local"
    assert 'base_url = "http://127.0.0.1:9999/v1"' in path.read_text(encoding="utf-8")


def test_t_b_full_v121_config_parses_custom_capabilities_routes_and_limits(tmp_path: Path) -> None:
    from smart_photo_triage.config import load_config

    path = tmp_path / "config.toml"
    path.write_text(
        """
[ai]
allow_cloud = true
allow_lan = true

[ai.providers.fast]
driver = "openai_compatible"
model = "fast-model"
base_url = "https://compatible.example.test/v1"
api_key_env = "SPT_FAST_KEY"
network_scope = "remote"
estimated_cost_per_request = 0.02

[ai.providers.fast.capabilities]
supports_image = true
supports_multi_image = false
supports_structured_json = true
supports_json_schema = false
max_images_per_request = 1
max_request_bytes = 1024
supported_image_mime_types = ["image/png"]
supports_system_prompt = true
supports_streaming = false
capability_profile_version = "operator-v1"

[ai.providers.strong]
driver = "anthropic"
model = "strong-model"
network_scope = "remote"

[ai.routes.item_analysis]
primary = "fast"
fallbacks = ["strong"]

[ai.routes.item_analysis.escalation]
confidence_below = 0.72
to = "strong"
max_escalations = 1

[ai.routes.burst_review]
primary = "strong"

[ai.limits]
max_provider_attempts_per_task = 3
max_requests_per_run = 10
max_remote_preview_mb_per_run = 2
max_estimated_cost_per_run = 0.5
""".lstrip(),
        encoding="utf-8",
    )
    config = load_config(path)

    assert config.allow_cloud and config.allow_lan
    assert config.providers[0].capabilities.capability_profile_version == "operator-v1"
    assert config.route_map()[TaskType.ITEM_ANALYSIS].escalate_to == "strong"
    assert config.budget is not None and config.budget.max_remote_preview_bytes == 2 * 1024 * 1024


@pytest.mark.parametrize(
    "body",
    [
        "[ai.providers.bad]\ndriver = 'openai_compatible'\n"
        "model = 'x'\nnetwork_scope = 'loopback'\n",
        "[ai]\nallow_cloud = 1\n",
        "[ai]\n[ai.routes.item_analysis]\nprimary = 'missing'\n",
    ],
)
def test_t_b_invalid_v121_config_fails_closed(tmp_path: Path, body: str) -> None:
    from smart_photo_triage.config import ConfigError, load_config

    path = tmp_path / "invalid.toml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


@pytest.mark.parametrize(
    ("status", "error_class"),
    [
        (401, ErrorClass.AUTH_ERROR),
        (402, ErrorClass.BILLING_ERROR),
        (429, ErrorClass.RATE_LIMIT),
        (503, ErrorClass.SERVER_ERROR),
    ],
)
def test_t_c_http_driver_error_classes_are_sanitized(
    status: int, error_class: ErrorClass, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPT_DRIVER_TEST_KEY", "synthetic-secret")
    configured = ProviderConfig(
        provider_id="openai-test",
        driver="openai",
        model="synthetic-model",
        base_url="https://api.openai.com/v1",
        api_key_env="SPT_DRIVER_TEST_KEY",
        network_scope=NetworkScope.REMOTE,
        capabilities=capabilities(),
    )

    def fail(_request: object, timeout: float) -> object:
        assert timeout > 0
        raise HTTPError("https://example.test", status, "synthetic-secret", None, None)

    with pytest.raises(ProviderFailure) as captured:
        build_driver(configured, opener=fail).analyze(request())
    assert captured.value.error_class is error_class
    assert "synthetic-secret" not in str(captured.value)


def test_t_g_ai_cli_is_observable_redacted_and_performs_no_network(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    assert main(["ai", "providers", "--workspace", str(workspace.root)]) == 0
    providers = capsys.readouterr().out
    assert "default_fake" in providers
    assert "api_key_configured" in providers
    assert "synthetic-secret" not in providers
    assert (
        main(["ai", "route", "explain", "item_analysis", "--workspace", str(workspace.root)]) == 0
    )
    assert '"primary": "default_fake"' in capsys.readouterr().out
    assert main(["ai", "doctor", "--workspace", str(workspace.root)]) == 0
    assert '"network_requests": 0' in capsys.readouterr().out
    assert main(["ai", "probe", "default_fake", "--workspace", str(workspace.root)]) == 0
    assert '"synthetic_only": true' in capsys.readouterr().out


def test_t_h_ai_cli_run_executes_offline_route_and_second_run_uses_provider_cache(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace, _media_id = _seed_routed_preview(tmp_path)

    assert main(["ai", "run", "--workspace", str(workspace.root)]) == 0
    assert "analyzed=1" in capsys.readouterr().out
    assert main(["ai", "run", "--workspace", str(workspace.root)]) == 0
    assert "cache_hits=1" in capsys.readouterr().out


def test_t_h_routed_workspace_e2e_cache_audit_and_human_decision_survive_rerun(
    tmp_path: Path,
) -> None:
    workspace, media_id = _seed_routed_preview(tmp_path)
    driver = FakeDriver(lambda _request: result(confidence=0.5))
    router = ModelRouter(
        ProviderRegistry((provider("fast"), provider("strong"))),
        {TaskType.ITEM_ANALYSIS: RoutePolicy("fast", confidence_below=0.72, escalate_to="strong")},
        drivers={"fast": driver, "strong": FakeDriver(lambda _request: result(confidence=0.95))},
    )

    first = analyze_workspace_routed(workspace, router=router)
    with sqlite3.connect(workspace.database_path) as connection:
        connection.execute(
            """
            INSERT INTO review_decision(
                media_id,scene_category,disposition,decision_source,revision,created_at,updated_at
            ) VALUES (?, '05_其他','REVIEW','HUMAN',1,'now','now')
            """,
            (media_id,),
        )
    second = analyze_workspace_routed(workspace, router=router)

    assert (first.analyzed_count, first.request_count, first.failed_count) == (1, 2, 0)
    assert (second.request_count, second.cache_hit_count, second.failed_count) == (0, 2, 0)
    with sqlite3.connect(workspace.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM ai_route_run").fetchone() == (2,)
        assert connection.execute("SELECT COUNT(*) FROM ai_route_attempt").fetchone() == (4,)
        assert connection.execute(
            "SELECT decision_source,scene_category,disposition "
            "FROM review_decision WHERE media_id=?",
            (media_id,),
        ).fetchone() == ("HUMAN", "05_其他", "REVIEW")
