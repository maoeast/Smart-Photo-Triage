from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request

import pytest
from PIL import Image

import smart_photo_triage.database as database_module
from smart_photo_triage.ai import (
    AnalysisOptions,
    CloudDisabledError,
    FakeVisionProvider,
    GeminiVisionProvider,
    PermanentProviderError,
    SchemaValidationError,
    SplittableProviderError,
    TransientProviderError,
    VisionRequest,
    analyze_workspace,
    estimate_workspace_analysis,
    validate_analysis_result,
    validate_response_mapping,
)
from smart_photo_triage.cli import main
from smart_photo_triage.database import apply_migrations, connect_database, read_workspace_id
from smart_photo_triage.workspace import Workspace, initialize_workspace


def valid_result(item_id: int, **changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "item_id": item_id,
        "scene_category": "01_家庭生活",
        "disposition": "KEEP",
        "confidence": 0.92,
        "quality_score": 0.81,
        "tags": ["synthetic", "indoor"],
        "short_desc": "synthetic preview",
        "reason": "clear generated fixture",
    }
    payload.update(changes)
    return payload


def query(workspace: Workspace, sql: str, parameters: tuple[object, ...] = ()) -> list[tuple]:
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        return connection.execute(sql, parameters).fetchall()


def seed_ready_previews(
    workspace: Workspace,
    count: int,
    *,
    source_root: Path,
    include_sidecar: bool = False,
) -> tuple[int, ...]:
    source_root.mkdir(parents=True, exist_ok=True)
    media_ids: list[int] = []
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for index in range(1, count + 1):
            source = source_root / f"private-family-{index}.png"
            Image.new("RGB", (12, 8), (index * 30, 40, 90)).save(source)
            preview = workspace.root / "previews" / f"preview-{index}.webp"
            Image.new("RGB", (12, 8), (index * 30, 40, 90)).save(preview, format="WEBP")
            preview_bytes = preview.read_bytes()
            cursor = connection.execute(
                """
                INSERT INTO media_item(
                    original_path,path_key,source_root,source_root_key,parent_key,bundle_stem,
                    media_type,extension,size_bytes,mtime_ns,source_present,content_sha256,
                    captured_at,capture_source,capture_confidence,capture_timezone_status,
                    last_seen_at,last_seen_scan_id,preview_path,preview_version
                ) VALUES (?,?,?,?,?,?, 'IMAGE','.png',10,20,1,?,
                          '2024-01-01T00:00:00','SYNTHETIC','HIGH','UNKNOWN','now','scan',?,?)
                """,
                (
                    str(source.resolve()),
                    str(source.resolve()).casefold(),
                    str(source_root.resolve()),
                    str(source_root.resolve()).casefold(),
                    str(source_root.resolve()).casefold(),
                    source.stem,
                    f"original-content-sha-sentinel-{index}",
                    str(preview),
                    "preview-private-v1",
                ),
            )
            media_id = int(cursor.lastrowid)
            media_ids.append(media_id)
            connection.execute(
                """
                INSERT INTO media_preprocess(
                    media_id,source_fingerprint,preview_fingerprint,preview_path,
                    preview_version,preview_status,perceptual_hash,perceptual_hash_version,
                    quality_json,quality_score,preview_width,preview_height,updated_at,
                    preview_sha256
                ) VALUES (?,?,?,?,?,'READY','0000000000000000','dhash64-v1',?,?,?,?,?,?)
                """,
                (
                    media_id,
                    f"source-fingerprint-{index}",
                    f"preview-fingerprint-{index}",
                    str(preview),
                    "preview-private-v1",
                    json.dumps(
                        {
                            "sharpness": 0.5,
                            "exposure": 0.7,
                            "clipping": 0.0,
                            "resolution": 96,
                            "score": 0.6,
                            "advisory": "REVIEW",
                        }
                    ),
                    0.6,
                    12,
                    8,
                    "now",
                    hashlib.sha256(preview_bytes).hexdigest(),
                ),
            )
        if include_sidecar:
            sidecar = source_root / "private-family-1.AAE"
            sidecar.write_text("SIDE-CAR-RAW-PRIVATE-SENTINEL", encoding="utf-8")
            connection.execute(
                """
                INSERT INTO media_item(
                    original_path,path_key,source_root,source_root_key,parent_key,bundle_stem,
                    media_type,extension,size_bytes,mtime_ns,source_present,content_sha256,
                    capture_source,capture_confidence,capture_timezone_status,
                    last_seen_at,last_seen_scan_id
                ) VALUES (?,?,?,?,?,?,'SIDECAR','.aae',10,20,1,?,
                          'SYNTHETIC','HIGH','UNKNOWN','now','scan')
                """,
                (
                    str(sidecar.resolve()),
                    str(sidecar.resolve()).casefold(),
                    str(source_root.resolve()),
                    str(source_root.resolve()).casefold(),
                    str(source_root.resolve()).casefold(),
                    sidecar.stem,
                    "sidecar-content-sha-sentinel",
                ),
            )
        connection.commit()
    return tuple(media_ids)


class RecordingProvider:
    def __init__(
        self,
        *,
        name: str = "recording",
        model: str = "recording-v1",
        is_cloud: bool = False,
        handler=None,  # type: ignore[no-untyped-def]
    ) -> None:
        self.name = name
        self.model = model
        self.is_cloud = is_cloud
        self.handler = handler or (
            lambda request: [valid_result(item.item_id) for item in request.items]
        )
        self.requests: list[VisionRequest] = []

    def analyze(self, request: VisionRequest) -> object:
        self.requests.append(request)
        return self.handler(request)


@pytest.mark.parametrize("confidence", [-0.001, 1.001])
def test_t_d_001_invalid_confidence_is_rejected(confidence: float) -> None:
    with pytest.raises(SchemaValidationError, match="confidence"):
        validate_analysis_result(valid_result(1, confidence=confidence))


def test_t_d_002_unknown_category_is_rejected() -> None:
    with pytest.raises(SchemaValidationError, match="category"):
        validate_analysis_result(valid_result(1, scene_category="99_UNKNOWN"))


def test_t_d_003_missing_item_id_is_rejected() -> None:
    payload = valid_result(1)
    del payload["item_id"]

    with pytest.raises(SchemaValidationError, match="item_id"):
        validate_analysis_result(payload)


def test_t_d_004_shuffled_provider_results_map_by_item_id() -> None:
    mapped = validate_response_mapping(
        [valid_result(3), valid_result(1), valid_result(2)],
        expected_item_ids=(1, 2, 3),
    )

    assert tuple(mapped) == (1, 2, 3)
    assert [mapped[item_id].item_id for item_id in (1, 2, 3)] == [1, 2, 3]


def test_t_d_005_low_confidence_reject_is_forced_to_review() -> None:
    result = validate_analysis_result(
        valid_result(1, disposition="REJECT_CANDIDATE", confidence=0.49),
        confidence_threshold=0.5,
    )

    assert result.disposition == "REVIEW"


def test_t_d_006_ai_schema_has_no_delete_or_file_action() -> None:
    with pytest.raises(SchemaValidationError, match="action|field"):
        validate_analysis_result(valid_result(1, action="DELETE"))

    result = validate_analysis_result(valid_result(1))
    assert not hasattr(result, "action")
    assert not hasattr(result, "source_path")
    assert not hasattr(result, "target_path")


def captured_request_text(tmp_path: Path) -> tuple[str, RecordingProvider, Workspace]:
    workspace = initialize_workspace(tmp_path / "workspace")
    source = tmp_path / "private-source"
    seed_ready_previews(workspace, 1, source_root=source, include_sidecar=True)
    provider = RecordingProvider()

    result = analyze_workspace(workspace, provider=provider)

    assert result.analyzed_count == 1
    assert len(provider.requests) == 1
    return (
        json.dumps(provider.requests[0].to_payload(), ensure_ascii=False, sort_keys=True),
        provider,
        workspace,
    )


def test_t_d_007_request_excludes_source_absolute_path(tmp_path: Path) -> None:
    request_text, _provider, _workspace = captured_request_text(tmp_path)

    assert str((tmp_path / "private-source").resolve()) not in request_text
    assert "private-family-1.png" not in request_text


def test_t_d_008_request_excludes_original_content_hash(tmp_path: Path) -> None:
    request_text, _provider, _workspace = captured_request_text(tmp_path)

    assert "original-content-sha-sentinel" not in request_text
    assert "content_sha256" not in request_text
    assert "preview_sha256" not in request_text


def test_t_d_009_request_excludes_sidecar_raw_content(tmp_path: Path) -> None:
    request_text, _provider, _workspace = captured_request_text(tmp_path)

    assert "SIDE-CAR-RAW-PRIVATE-SENTINEL" not in request_text
    assert ".AAE" not in request_text


def test_t_d_010_cloud_disabled_default_blocks_before_network(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    seed_ready_previews(workspace, 1, source_root=tmp_path / "source")
    provider = RecordingProvider(is_cloud=True)

    with pytest.raises(CloudDisabledError, match="allow_cloud"):
        analyze_workspace(workspace, provider=provider)

    assert provider.requests == []


def test_t_d_011_identical_cache_key_skips_second_provider_call(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    seed_ready_previews(workspace, 1, source_root=tmp_path / "source")
    provider = RecordingProvider()

    first = analyze_workspace(workspace, provider=provider)
    second = analyze_workspace(workspace, provider=provider)

    assert first.analyzed_count == 1
    assert second.cache_hit_count == 1
    assert second.analyzed_count == 0
    assert len(provider.requests) == 1


def test_t_d_012_prompt_version_invalidates_cache(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    seed_ready_previews(workspace, 1, source_root=tmp_path / "source")
    provider = RecordingProvider()
    analyze_workspace(
        workspace, provider=provider, options=AnalysisOptions(prompt_version="prompt-v1")
    )

    result = analyze_workspace(
        workspace, provider=provider, options=AnalysisOptions(prompt_version="prompt-v2")
    )

    assert result.analyzed_count == 1
    assert len(provider.requests) == 2


def test_t_d_013_schema_version_invalidates_cache(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    seed_ready_previews(workspace, 1, source_root=tmp_path / "source")
    provider = RecordingProvider()
    analyze_workspace(
        workspace, provider=provider, options=AnalysisOptions(schema_version="schema-v1")
    )

    result = analyze_workspace(
        workspace, provider=provider, options=AnalysisOptions(schema_version="schema-v2")
    )

    assert result.analyzed_count == 1
    assert len(provider.requests) == 2


@pytest.mark.parametrize(
    ("second_name", "second_model"),
    [("other-provider", "model-v1"), ("recording", "model-v2")],
)
def test_t_d_014_provider_or_model_invalidates_cache(
    tmp_path: Path, second_name: str, second_model: str
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    seed_ready_previews(workspace, 1, source_root=tmp_path / "source")
    first_provider = RecordingProvider(name="recording", model="model-v1")
    second_provider = RecordingProvider(name=second_name, model=second_model)
    analyze_workspace(workspace, provider=first_provider)

    result = analyze_workspace(workspace, provider=second_provider)

    assert result.analyzed_count == 1
    assert len(second_provider.requests) == 1


def test_t_d_015_preview_version_or_fingerprint_invalidates_cache(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    (media_id,) = seed_ready_previews(workspace, 1, source_root=tmp_path / "source")
    provider = RecordingProvider()
    analyze_workspace(workspace, provider=provider)
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        connection.execute(
            """
            UPDATE media_preprocess
            SET preview_version='preview-private-v2', preview_fingerprint='preview-fingerprint-v2'
            WHERE media_id=?
            """,
            (media_id,),
        )
        connection.commit()

    result = analyze_workspace(workspace, provider=provider)

    assert result.analyzed_count == 1
    assert len(provider.requests) == 2


def test_t_d_016_transient_failures_retry_only_to_bound(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    seed_ready_previews(workspace, 1, source_root=tmp_path / "source")
    attempts = 0

    def transient_then_success(request: VisionRequest) -> object:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise TransientProviderError("synthetic 429")
        return [valid_result(item.item_id) for item in request.items]

    provider = RecordingProvider(handler=transient_then_success)

    result = analyze_workspace(
        workspace,
        provider=provider,
        options=AnalysisOptions(max_retries=2),
        retry_sleep=lambda _seconds: None,
    )

    assert result.analyzed_count == 1
    assert attempts == 3


def test_t_d_017_permanent_failure_is_not_retried(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    seed_ready_previews(workspace, 1, source_root=tmp_path / "source")

    def permanent(_request: VisionRequest) -> object:
        raise PermanentProviderError("synthetic invalid key")

    provider = RecordingProvider(handler=permanent)

    with pytest.raises(PermanentProviderError):
        analyze_workspace(
            workspace,
            provider=provider,
            options=AnalysisOptions(max_retries=8),
            retry_sleep=lambda _seconds: None,
        )

    assert len(provider.requests) == 1


def test_t_d_018_bad_item_is_split_and_isolated_from_batch(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    item_ids = seed_ready_previews(workspace, 3, source_root=tmp_path / "source")
    bad_id = item_ids[1]

    def fail_batch_with_bad_item(request: VisionRequest) -> object:
        if bad_id in {item.item_id for item in request.items}:
            raise SplittableProviderError(f"synthetic bad item {bad_id}")
        return [valid_result(item.item_id) for item in reversed(request.items)]

    provider = RecordingProvider(handler=fail_batch_with_bad_item)

    result = analyze_workspace(
        workspace,
        provider=provider,
        options=AnalysisOptions(batch_size=3, max_retries=0),
    )

    assert result.analyzed_count == 2
    assert result.failed_count == 1
    assert result.failures[0][0] == bad_id
    assert {row[0] for row in query(workspace, "SELECT media_id FROM ai_analysis")} == {
        item_ids[0],
        item_ids[2],
    }


def test_fake_provider_runs_complete_offline_workspace_flow(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    item_ids = seed_ready_previews(workspace, 2, source_root=tmp_path / "source")
    provider = FakeVisionProvider(record_requests=True)

    first = analyze_workspace(workspace, provider=provider)
    second = analyze_workspace(workspace, provider=provider)

    assert (first.analyzed_count, first.failed_count) == (2, 0)
    assert (second.cache_hit_count, second.analyzed_count) == (2, 0)
    assert {row[0] for row in query(workspace, "SELECT media_id FROM ai_analysis")} == set(item_ids)
    assert len(provider.requests) == 1


def test_estimate_reports_pending_upload_cache_and_batches(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    seed_ready_previews(workspace, 3, source_root=tmp_path / "source")
    provider = RecordingProvider()
    options = AnalysisOptions(batch_size=2)

    before = estimate_workspace_analysis(workspace, provider=provider, options=options)
    analyze_workspace(workspace, provider=provider, options=options)
    after = estimate_workspace_analysis(workspace, provider=provider, options=options)

    assert before.item_count == 3
    assert before.pending_count == 3
    assert before.cache_hit_count == 0
    assert before.upload_bytes > 0
    assert before.request_batch_count == 2
    assert after.pending_count == 0
    assert after.cache_hit_count == 3
    assert after.upload_bytes == 0
    assert after.request_batch_count == 0


class FakeHTTPResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self) -> FakeHTTPResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self.payload if size < 0 else self.payload[:size]


def test_gemini_adapter_uses_injected_model_and_parses_structured_json() -> None:
    observed: list[Request] = []
    response_payload = json.dumps(
        {
            "candidates": [
                {
                    "content": {
                        "parts": [{"text": json.dumps([valid_result(7)])}],
                    }
                }
            ]
        }
    ).encode()

    def fake_open(request: Request, timeout: float) -> FakeHTTPResponse:
        assert timeout > 0
        observed.append(request)
        return FakeHTTPResponse(response_payload)

    provider = GeminiVisionProvider(
        model="chosen-model-not-core-default",
        api_key="synthetic-test-key",
        opener=fake_open,
    )
    request = VisionRequest(items=(), prompt_version="prompt-v1", schema_version="schema-v1")

    payload = provider.analyze(request)

    assert payload == [valid_result(7)]
    assert len(observed) == 1
    assert "chosen-model-not-core-default" in observed[0].full_url


@pytest.mark.parametrize(
    ("failure", "expected_error"),
    [
        (HTTPError("https://example.invalid", 429, "rate", None, None), TransientProviderError),
        (HTTPError("https://example.invalid", 503, "server", None, None), TransientProviderError),
        (TimeoutError("synthetic timeout"), TransientProviderError),
        (HTTPError("https://example.invalid", 401, "auth", None, None), PermanentProviderError),
    ],
)
def test_gemini_adapter_classifies_transient_and_permanent_failures(
    failure: Exception, expected_error: type[Exception]
) -> None:
    def fail_open(_request: Request, timeout: float) -> FakeHTTPResponse:
        assert timeout > 0
        raise failure

    provider = GeminiVisionProvider(
        model="synthetic-model",
        api_key="synthetic-test-key",
        opener=fail_open,
    )

    with pytest.raises(expected_error):
        provider.analyze(
            VisionRequest(items=(), prompt_version="prompt-v1", schema_version="schema-v1")
        )


def test_phase_d_v8_database_upgrades_additively_to_v9(tmp_path: Path, monkeypatch) -> None:
    all_migrations = database_module.MIGRATIONS
    assert len(all_migrations) >= 9
    database_path = tmp_path / "v8.sqlite3"
    workspace_id = "12345678123456781234567812345678"
    monkeypatch.setattr(database_module, "MIGRATIONS", all_migrations[:8])
    with closing(connect_database(database_path)) as connection:
        assert apply_migrations(connection, workspace_id=workspace_id) == 8
        connection.execute(
            "INSERT INTO preprocess_run(id,config_fingerprint,preview_version,started_at,status) "
            "VALUES ('preserved','cfg','v','now','COMPLETE')"
        )
        connection.commit()

        monkeypatch.setattr(database_module, "MIGRATIONS", all_migrations)
        assert apply_migrations(connection, workspace_id=workspace_id) == all_migrations[-1].version
        assert read_workspace_id(connection) == workspace_id
        assert connection.execute(
            "SELECT id,status FROM preprocess_run WHERE id='preserved'"
        ).fetchone() == ("preserved", "COMPLETE")
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"ai_run", "ai_analysis"} <= tables


def test_phase_d_cli_analyze_fake_minimal_path(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    workspace = initialize_workspace(tmp_path / "workspace")
    seed_ready_previews(workspace, 1, source_root=tmp_path / "source")

    exit_code = main(["analyze", "--workspace", str(workspace.root), "--provider", "fake"])

    assert exit_code == 0
    assert "Analysis complete" in capsys.readouterr().out
    assert query(workspace, "SELECT COUNT(*) FROM ai_analysis") == [(1,)]
