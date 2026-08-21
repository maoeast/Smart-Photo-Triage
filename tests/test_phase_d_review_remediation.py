from __future__ import annotations

import hashlib
import json
import sqlite3
import tracemalloc
from contextlib import closing, contextmanager
from pathlib import Path

import pytest
from PIL import Image

import smart_photo_triage.ai as ai_module
from smart_photo_triage.ai import (
    AnalysisOptions,
    CloudDisabledError,
    GeminiVisionProvider,
    PermanentProviderError,
    SplittableProviderError,
    TransientProviderError,
    VisionRequest,
    analyze_workspace,
    estimate_workspace_analysis,
)
from smart_photo_triage.cli import main
from smart_photo_triage.workspace import Workspace, initialize_workspace


def response(item_id: int, *, confidence: float = 0.5) -> dict[str, object]:
    return {
        "item_id": item_id,
        "scene_category": "05_其他",
        "disposition": "REJECT_CANDIDATE",
        "confidence": confidence,
        "quality_score": 0.5,
        "tags": ["synthetic"],
        "short_desc": "synthetic",
        "reason": "synthetic review fixture",
    }


class Provider:
    def __init__(
        self,
        handler=None,  # type: ignore[no-untyped-def]
        *,
        is_cloud: bool = False,
    ) -> None:
        self.name = "review-provider"
        self.model = "review-model"
        self.is_cloud = is_cloud
        self.handler = handler or (
            lambda request: [response(item.item_id) for item in request.items]
        )
        self.requests: list[VisionRequest] = []

    def analyze(self, request: VisionRequest) -> object:
        self.requests.append(request)
        return self.handler(request)


def seed(workspace: Workspace, count: int, source_root: Path) -> tuple[int, ...]:
    source_root.mkdir(parents=True, exist_ok=True)
    media_ids: list[int] = []
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        for index in range(1, count + 1):
            source = source_root / f"private-{index}.png"
            Image.new("RGB", (8, 8), (index * 20, 40, 90)).save(source)
            preview = workspace.root / "previews" / f"review-{index}.webp"
            Image.new("RGB", (8, 8), (index * 20, 40, 90)).save(preview, format="WEBP")
            preview_sha = hashlib.sha256(preview.read_bytes()).hexdigest()
            cursor = connection.execute(
                """
                INSERT INTO media_item(
                    original_path,path_key,source_root,source_root_key,parent_key,bundle_stem,
                    media_type,extension,size_bytes,mtime_ns,source_present,content_sha256,
                    capture_source,capture_confidence,capture_timezone_status,
                    last_seen_at,last_seen_scan_id,preview_path,preview_version
                ) VALUES (?,?,?,?,?,?,'IMAGE','.png',10,20,1,?,
                          'SYNTHETIC','HIGH','UNKNOWN','now','scan',?,'preview-v1')
                """,
                (
                    str(source.resolve()),
                    str(source.resolve()).casefold(),
                    str(source_root.resolve()),
                    str(source_root.resolve()).casefold(),
                    str(source_root.resolve()).casefold(),
                    source.stem,
                    f"original-private-sha-{index}",
                    str(preview),
                ),
            )
            media_id = int(cursor.lastrowid)
            media_ids.append(media_id)
            connection.execute(
                """
                INSERT INTO media_preprocess(
                    media_id,source_fingerprint,preview_fingerprint,preview_path,
                    preview_version,preview_status,quality_json,quality_score,
                    preview_width,preview_height,updated_at,preview_sha256
                ) VALUES (?,?,?,?,?,'READY',?,0.6,8,8,'now',?)
                """,
                (
                    media_id,
                    f"source-{index}",
                    f"preview-{index}",
                    str(preview),
                    "preview-v1",
                    json.dumps({"score": 0.6, "sharpness": 0.5}),
                    preview_sha,
                ),
            )
        connection.commit()
    return tuple(media_ids)


def rows(workspace: Workspace, sql: str) -> list[tuple]:
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        return connection.execute(sql).fetchall()


def test_cache_identity_covers_threshold_quality_media_type_and_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    (media_id,) = seed(workspace, 1, tmp_path / "private-source")
    provider = Provider()

    first = analyze_workspace(
        workspace,
        provider=provider,
        options=AnalysisOptions(confidence_threshold=0.4),
    )
    second = analyze_workspace(
        workspace,
        provider=provider,
        options=AnalysisOptions(confidence_threshold=0.65),
    )
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        connection.execute(
            "UPDATE media_preprocess SET quality_json=? WHERE media_id=?",
            (json.dumps({"score": 0.7, "sharpness": 0.9}), media_id),
        )
        connection.commit()
    quality_changed = analyze_workspace(workspace, provider=provider)
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        connection.execute(
            "UPDATE media_preprocess SET quality_json=? WHERE media_id=?",
            ('{"sharpness":0.9,"score":0.7}', media_id),
        )
        connection.commit()
    reordered_quality = analyze_workspace(workspace, provider=provider)
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        connection.execute("UPDATE media_item SET media_type='VIDEO' WHERE id=?", (media_id,))
        connection.commit()
    media_type_changed = analyze_workspace(workspace, provider=provider)
    monkeypatch.setattr(ai_module, "ANALYSIS_POLICY_VERSION", "analysis-policy-review-v2")
    policy_changed = analyze_workspace(workspace, provider=provider)

    assert first.analyzed_count == 1
    assert second.analyzed_count == 1
    assert quality_changed.analyzed_count == 1
    assert reordered_quality.cache_hit_count == 1
    assert media_type_changed.analyzed_count == 1
    assert policy_changed.analyzed_count == 1
    assert len(provider.requests) == 5
    analyses = rows(
        workspace,
        "SELECT input_fingerprint,disposition FROM ai_analysis ORDER BY id",
    )
    assert len({item[0] for item in analyses}) == 5
    assert analyses[0][1] == "REJECT_CANDIDATE"
    assert analyses[1][1] == "REVIEW"
    preview_sha = rows(workspace, "SELECT preview_sha256 FROM media_preprocess")[0][0]
    assert all(item[0] != preview_sha for item in analyses)


def test_cloud_disabled_estimate_reads_no_preview_and_calls_no_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    seed(workspace, 1, tmp_path / "private-source")
    provider = Provider(is_cloud=True)
    preview_reads = 0

    def read_spy(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        nonlocal preview_reads
        preview_reads += 1
        raise AssertionError("cloud gate must run before preview read")

    monkeypatch.setattr(ai_module, "_controlled_preview_bytes", read_spy)

    with pytest.raises(CloudDisabledError, match="allow_cloud"):
        estimate_workspace_analysis(workspace, provider=provider)
    assert preview_reads == 0
    assert provider.requests == []


def test_cli_cloud_disabled_estimate_only_reads_no_preview_or_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    seed(workspace, 1, tmp_path / "private-source")
    preview_reads = 0

    def read_spy(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        nonlocal preview_reads
        preview_reads += 1
        raise AssertionError("CLI cloud gate must run before preview read")

    monkeypatch.setattr(ai_module, "_controlled_preview_bytes", read_spy)
    monkeypatch.setenv("SPT_GEMINI_API_KEY", "synthetic-key-not-a-real-credential")

    exit_code = main(
        [
            "analyze",
            "--workspace",
            str(workspace.root),
            "--provider",
            "gemini",
            "--model",
            "synthetic-model",
            "--estimate-only",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "allow_cloud" in captured.err
    assert preview_reads == 0


def test_global_permanent_invalid_key_stops_eight_item_batch_after_one_call(
    tmp_path: Path,
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    seed(workspace, 8, tmp_path / "private-source")

    def invalid_key(_request: VisionRequest) -> object:
        raise PermanentProviderError("invalid API key should be global")

    provider = Provider(invalid_key)

    with pytest.raises(PermanentProviderError, match="GLOBAL_PERMANENT_PROVIDER_ERROR"):
        analyze_workspace(workspace, provider=provider, options=AnalysisOptions(batch_size=8))

    assert len(provider.requests) == 1
    assert rows(workspace, "SELECT status,request_count FROM ai_run") == [("FAILED", 1)]


def test_declared_splittable_item_error_isolates_one_item(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    media_ids = seed(workspace, 8, tmp_path / "private-source")
    bad_id = media_ids[4]

    def item_error(request: VisionRequest) -> object:
        if bad_id in {item.item_id for item in request.items}:
            raise SplittableProviderError("provider declared item-local")
        return [response(item.item_id, confidence=0.9) for item in request.items]

    provider = Provider(item_error)
    result = analyze_workspace(
        workspace,
        provider=provider,
        options=AnalysisOptions(batch_size=8, max_retries=0),
    )

    assert (result.analyzed_count, result.failed_count) == (7, 1)
    assert result.failures[0][0] == bad_id
    assert len(provider.requests) <= 2 * len(media_ids) - 1


def test_transient_exhaustion_has_one_batch_retry_budget_without_split(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    seed(workspace, 8, tmp_path / "private-source")

    def unavailable(_request: VisionRequest) -> object:
        raise TransientProviderError("synthetic 503")

    provider = Provider(unavailable)
    result = analyze_workspace(
        workspace,
        provider=provider,
        options=AnalysisOptions(batch_size=8, max_retries=2),
        retry_sleep=lambda _seconds: None,
    )

    assert result.failed_count == 8
    assert len(provider.requests) == 3
    assert result.request_count == 3


@pytest.mark.parametrize(
    "options",
    [
        {"batch_size": 101},
        {"max_retries": 11},
    ],
)
def test_analysis_options_have_hard_upper_bounds(options: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="batch|retries|maximum"):
        AnalysisOptions(**options)


def test_gemini_timeout_has_finite_upper_bound() -> None:
    with pytest.raises(ValueError, match="timeout|maximum"):
        GeminiVisionProvider(
            model="synthetic",
            api_key="synthetic",
            timeout_seconds=301.0,
        )


def test_global_and_item_errors_are_redacted_from_public_results(
    tmp_path: Path,
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    seed(workspace, 1, tmp_path / "private-source")
    secret = "synthetic-secret-api-key"
    private_path = str((tmp_path / "private-source").resolve())

    def global_error(_request: VisionRequest) -> object:
        raise PermanentProviderError(f"invalid {secret} at {private_path}")

    with pytest.raises(PermanentProviderError) as captured:
        analyze_workspace(workspace, provider=Provider(global_error))
    assert secret not in str(captured.value)
    assert private_path not in str(captured.value)

    def item_error(_request: VisionRequest) -> object:
        raise SplittableProviderError(f"bad {secret} at {private_path}")

    result = analyze_workspace(
        workspace,
        provider=Provider(item_error),
        options=AnalysisOptions(max_retries=0),
    )
    serialized = repr(result.failures)
    assert secret not in serialized
    assert private_path not in serialized
    assert "media_id" not in serialized


def test_preview_read_error_does_not_expose_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    seed(workspace, 1, tmp_path / "private-source")
    private_path = str((workspace.root / "previews" / "review-1.webp").resolve())

    @contextmanager
    def fail_open(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise OSError(f"cannot read {private_path}")
        yield

    monkeypatch.setattr(ai_module, "_open_source_nofollow", fail_open)
    result = analyze_workspace(workspace, provider=Provider())

    assert result.failed_count == 1
    assert private_path not in repr(result.failures)


@pytest.mark.parametrize("allow_cloud", ["true", 1, object()])
def test_cloud_authorization_requires_literal_true_before_any_preview_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_cloud: object
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    seed(workspace, 1, tmp_path / "private-source")
    provider = Provider(is_cloud=True)
    preview_reads = 0

    def read_spy(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        nonlocal preview_reads
        preview_reads += 1
        raise AssertionError("non-boolean cloud authorization reached preview input")

    monkeypatch.setattr(ai_module, "_controlled_preview_bytes", read_spy)

    with pytest.raises(CloudDisabledError, match="allow_cloud"):
        analyze_workspace(workspace, provider=provider, allow_cloud=allow_cloud)  # type: ignore[arg-type]
    assert preview_reads == 0
    assert provider.requests == []


def test_estimate_reads_no_preview_bytes_and_analyze_loads_only_one_batch_ahead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    media_ids = seed(workspace, 24, tmp_path / "private-source")
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        for media_id in media_ids:
            preview_path = Path(
                connection.execute(
                    "SELECT preview_path FROM media_preprocess WHERE media_id=?", (media_id,)
                ).fetchone()[0]
            )
            preview_path.write_bytes(bytes([media_id % 251]) * 1024 * 1024)
            digest = hashlib.sha256(preview_path.read_bytes()).hexdigest()
            connection.execute(
                "UPDATE media_preprocess SET preview_sha256=? WHERE media_id=?",
                (digest, media_id),
            )
        connection.commit()

    original_read = ai_module._controlled_preview_bytes
    preview_reads = 0
    reads_at_provider_calls: list[int] = []

    def read_spy(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal preview_reads
        preview_reads += 1
        return original_read(*args, **kwargs)

    def handler(request: VisionRequest) -> object:
        reads_at_provider_calls.append(preview_reads)
        return [response(item.item_id, confidence=0.9) for item in request.items]

    monkeypatch.setattr(ai_module, "_controlled_preview_bytes", read_spy)
    provider = Provider(handler)
    estimate = estimate_workspace_analysis(
        workspace, provider=provider, options=AnalysisOptions(batch_size=8)
    )
    assert estimate.upload_bytes == 24 * 1024 * 1024
    assert preview_reads == 0

    result = analyze_workspace(workspace, provider=provider, options=AnalysisOptions(batch_size=8))

    assert result.analyzed_count == 24
    assert reads_at_provider_calls == [8, 16, 24]


@pytest.mark.parametrize("mutation", ["changed", "missing"])
def test_cache_hit_revalidates_current_preview_artifact_before_reuse(
    tmp_path: Path, mutation: str
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    (media_id,) = seed(workspace, 1, tmp_path / "private-source")
    provider = Provider()
    first = analyze_workspace(workspace, provider=provider)
    preview_path = Path(
        rows(
            workspace,
            f"SELECT preview_path FROM media_preprocess WHERE media_id={media_id}",
        )[0][0]
    )
    if mutation == "changed":
        preview_path.write_bytes(b"changed-after-cache")
    else:
        preview_path.unlink()

    second = analyze_workspace(workspace, provider=provider)

    assert first.analyzed_count == 1
    assert second.cache_hit_count == 0
    assert second.failed_count == 1
    assert len(provider.requests) == 1


def test_gemini_response_read_is_explicitly_bounded() -> None:
    read_sizes: list[int] = []

    class OversizedResponse:
        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, size: int) -> bytes:
            read_sizes.append(size)
            return b"x" * size

    provider = GeminiVisionProvider(
        model="synthetic",
        api_key="synthetic",
        opener=lambda *_args, **_kwargs: OversizedResponse(),
    )

    with pytest.raises(PermanentProviderError, match="bounded size"):
        provider.analyze(VisionRequest(items=(), prompt_version="p", schema_version="s"))
    assert read_sizes == [ai_module._MAX_RESPONSE_BYTES + 1]


@pytest.mark.parametrize(
    "options",
    [
        {"batch_size": 2.5},
        {"max_retries": 1.5},
    ],
)
def test_analysis_integer_controls_reject_floats(options: dict[str, float]) -> None:
    with pytest.raises(ValueError, match="integer"):
        AnalysisOptions(**options)  # type: ignore[arg-type]


@pytest.mark.parametrize("timeout", [True, "30"])
def test_gemini_timeout_rejects_bool_and_non_numeric(timeout: object) -> None:
    with pytest.raises(ValueError, match="timeout"):
        GeminiVisionProvider(
            model="synthetic",
            api_key="synthetic",
            timeout_seconds=timeout,  # type: ignore[arg-type]
        )


def test_global_failure_after_success_preserves_results_and_audit_conservation(
    tmp_path: Path,
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    media_ids = seed(workspace, 4, tmp_path / "private-source")
    calls = 0

    def handler(request: VisionRequest) -> object:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PermanentProviderError("invalid synthetic secret")
        return [response(item.item_id, confidence=0.9) for item in request.items]

    provider = Provider(handler)
    with pytest.raises(PermanentProviderError, match="GLOBAL_PERMANENT_PROVIDER_ERROR"):
        analyze_workspace(
            workspace,
            provider=provider,
            options=AnalysisOptions(batch_size=2),
        )

    assert {row[0] for row in rows(workspace, "SELECT media_id FROM ai_analysis")} == set(
        media_ids[:2]
    )
    assert rows(
        workspace,
        "SELECT status,item_count,pending_count,cache_hit_count,analyzed_count,"
        "failed_count,request_count FROM ai_run",
    ) == [("FAILED", 4, 4, 0, 2, 2, 2)]


def seed_shared_descriptor_rows(workspace: Workspace, count: int) -> None:
    preview = workspace.root / "previews" / "shared.webp"
    preview.write_bytes(b"bounded-shared-preview")
    preview_sha = hashlib.sha256(preview.read_bytes()).hexdigest()
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        for index in range(1, count + 1):
            cursor = connection.execute(
                """
                INSERT INTO media_item(
                    original_path,path_key,source_root,source_root_key,parent_key,bundle_stem,
                    media_type,extension,size_bytes,mtime_ns,source_present,
                    capture_source,capture_confidence,capture_timezone_status,
                    last_seen_at,last_seen_scan_id,preview_path,preview_version
                ) VALUES (?,?,?,?,?,?,'IMAGE','.png',10,20,1,
                          'SYNTHETIC','HIGH','UNKNOWN','now','scan',?,'preview-v1')
                """,
                (
                    f"synthetic-{index}.png",
                    f"synthetic-{index}.png",
                    "synthetic",
                    "synthetic",
                    "synthetic",
                    f"synthetic-{index}",
                    str(preview),
                ),
            )
            media_id = int(cursor.lastrowid)
            connection.execute(
                """
                INSERT INTO media_preprocess(
                    media_id,source_fingerprint,preview_fingerprint,preview_path,
                    preview_version,preview_status,quality_json,quality_score,
                    preview_width,preview_height,updated_at,preview_sha256
                ) VALUES (?,?,?,?,?,'READY','{}',0.5,8,8,'now',?)
                """,
                (
                    media_id,
                    f"source-{index}",
                    f"preview-{index}",
                    str(preview),
                    "preview-v1",
                    preview_sha,
                ),
            )
        connection.commit()


def test_descriptor_pipeline_memory_is_independent_of_total_pending_count(
    tmp_path: Path,
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    seed_shared_descriptor_rows(workspace, 10_000)

    def stop_after_first_batch(_request: VisionRequest) -> object:
        raise PermanentProviderError("stop after bounded batch")

    tracemalloc.start()
    try:
        with pytest.raises(PermanentProviderError):
            analyze_workspace(
                workspace,
                provider=Provider(stop_after_first_batch),
                options=AnalysisOptions(batch_size=8),
            )
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 6 * 1024 * 1024


def test_fake_provider_default_retains_no_request_or_preview_bytes(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    seed(workspace, 3, tmp_path / "private-source")
    provider = ai_module.FakeVisionProvider()

    analyze_workspace(workspace, provider=provider, options=AnalysisOptions(batch_size=1))

    assert provider.requests == []


def test_split_tree_shares_one_retry_budget_and_has_linear_request_bound(
    tmp_path: Path,
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    seed_shared_descriptor_rows(workspace, 100)
    seen_batches: set[tuple[int, ...]] = set()

    def adversarial(request: VisionRequest) -> object:
        ids = tuple(item.item_id for item in request.items)
        if ids not in seen_batches:
            seen_batches.add(ids)
            raise TransientProviderError("one transient per split node")
        raise SplittableProviderError("split every batch")

    result = analyze_workspace(
        workspace,
        provider=Provider(adversarial),
        options=AnalysisOptions(batch_size=100, max_retries=10),
        retry_sleep=lambda _seconds: None,
    )

    assert result.request_count <= 209
    assert result.failed_count == 100


def test_public_item_error_codes_are_fixed_enums_without_exception_names(
    tmp_path: Path,
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    seed(workspace, 1, tmp_path / "private-source")

    class LEAKED_SECRET(SplittableProviderError):
        pass

    def leak(_request: VisionRequest) -> object:
        raise LEAKED_SECRET("private provider message")

    result = analyze_workspace(
        workspace,
        provider=Provider(leak),
        options=AnalysisOptions(max_retries=0),
    )

    assert result.failures == ((1, "ITEM_SPLITTABLE_PROVIDER_ERROR"),)
    assert "LEAKED_SECRET" not in repr(result)


def test_execution_pass_preview_disappearance_is_audited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    (media_id,) = seed(workspace, 1, tmp_path / "private-source")
    preview = Path(rows(workspace, "SELECT preview_path FROM media_preprocess")[0][0])
    original = ai_module._iter_descriptors
    calls = 0

    def race(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        iterator, stats = original(*args, **kwargs)
        if calls == 1:

            def delete_after_summary():  # type: ignore[no-untyped-def]
                yield from iterator
                preview.unlink()

            return delete_after_summary(), stats
        return iterator, stats

    monkeypatch.setattr(ai_module, "_iter_descriptors", race)
    result = analyze_workspace(workspace, provider=Provider())

    assert result.analyzed_count == 0
    assert result.cache_hit_count == 0
    assert result.failures == ((media_id, "PREVIEW_READ_ERROR"),)
    assert rows(
        workspace,
        "SELECT status,item_count,cache_hit_count,analyzed_count,failed_count FROM ai_run",
    ) == [("COMPLETE_WITH_FAILURES", 1, 0, 0, 1)]


def test_batch_database_failure_rolls_back_counts_and_uses_stable_code(
    tmp_path: Path,
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    media_ids = seed(workspace, 2, tmp_path / "private-source")
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        connection.execute(
            f"""
            CREATE TRIGGER fail_second_ai_insert
            BEFORE INSERT ON ai_analysis WHEN NEW.media_id={media_ids[1]}
            BEGIN SELECT RAISE(ABORT, 'LEAKED_DB_SECRET'); END
            """
        )
        connection.commit()

    with pytest.raises(ai_module.AnalysisError, match="DB_WRITE_ERROR"):
        analyze_workspace(
            workspace,
            provider=Provider(),
            options=AnalysisOptions(batch_size=2),
        )

    assert rows(workspace, "SELECT COUNT(*) FROM ai_analysis") == [(0,)]
    assert rows(
        workspace,
        "SELECT status,analyzed_count,failed_count,request_count FROM ai_run",
    ) == [("FAILED", 0, 2, 1)]


def test_quality_is_bounded_numeric_allowlist_and_fake_history_is_redacted(
    tmp_path: Path,
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    (media_id,) = seed(workspace, 1, tmp_path / "private-source")
    huge = "X" * (4 * 1024 * 1024)
    quality = {
        "score": 0.6,
        "sharpness": 0.5,
        "advisory": huge,
        "exposure": "0.5",
        "clipping": {"nested": True},
        "resolution": -1,
    }
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        connection.execute(
            "UPDATE media_preprocess SET quality_json=? WHERE media_id=?",
            (json.dumps(quality), media_id),
        )
        connection.commit()
    provider = ai_module.FakeVisionProvider(record_requests=True, history_limit=2)

    analyze_workspace(workspace, provider=provider)

    assert provider.requests[0].items[0].quality == {}
    assert provider.requests[0].items[0].preview_bytes == b""
    assert huge not in repr(provider.requests)


def test_failed_audit_uses_execution_stats_after_cache_race_and_db_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    media_ids = seed(workspace, 2, tmp_path / "private-source")
    provider = Provider()
    analyze_workspace(workspace, provider=provider)
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        connection.execute("DELETE FROM ai_analysis WHERE media_id=?", (media_ids[1],))
        connection.execute(
            f"""CREATE TRIGGER fail_item2_write BEFORE INSERT ON ai_analysis
            WHEN NEW.media_id={media_ids[1]}
            BEGIN SELECT RAISE(ABORT, 'LEAKED_DB_SECRET'); END"""
        )
        connection.commit()
    preview1 = Path(
        rows(
            workspace,
            f"SELECT preview_path FROM media_preprocess WHERE media_id={media_ids[0]}",
        )[0][0]
    )
    original = ai_module._iter_descriptors
    calls = 0

    def race(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        iterator, stats = original(*args, **kwargs)
        if calls == 1:

            def mutate_after_summary():  # type: ignore[no-untyped-def]
                yield from iterator
                preview1.write_bytes(b"changed-after-summary")

            return mutate_after_summary(), stats
        return iterator, stats

    monkeypatch.setattr(ai_module, "_iter_descriptors", race)
    with pytest.raises(ai_module.AnalysisError, match="DB_WRITE_ERROR"):
        analyze_workspace(workspace, provider=provider)

    audit = rows(
        workspace,
        "SELECT status,item_count,pending_count,cache_hit_count,analyzed_count,failed_count "
        "FROM ai_run ORDER BY started_at DESC LIMIT 1",
    )[0]
    assert audit[0] == "FAILED"
    assert audit[1] == audit[3] + audit[4] + audit[5] == 2
    assert audit[2:] == (1, 0, 0, 2)


@pytest.mark.parametrize("failure_site", ["update", "commit"])
def test_terminal_failed_audit_write_fault_returns_only_stable_db_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_site: str
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    seed(workspace, 1, tmp_path / "private-source")
    if failure_site == "update":
        with closing(sqlite3.connect(workspace.database_path)) as connection:
            connection.execute(
                """CREATE TRIGGER fail_terminal_update BEFORE UPDATE ON ai_run
                WHEN NEW.status='FAILED'
                BEGIN SELECT RAISE(ABORT, 'LEAKED_DB_SECRET'); END"""
            )
            connection.commit()
    else:
        real_connect = ai_module.connect_database

        class CommitFault:
            def __init__(self, connection: sqlite3.Connection) -> None:
                self.connection = connection

            def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
                return getattr(self.connection, name)

            def commit(self) -> None:
                status = self.connection.execute(
                    "SELECT status FROM ai_run ORDER BY started_at DESC LIMIT 1"
                ).fetchone()
                if status == ("FAILED",):
                    raise sqlite3.IntegrityError("LEAKED_DB_SECRET")
                self.connection.commit()

        monkeypatch.setattr(
            ai_module,
            "connect_database",
            lambda path, read_only=False: CommitFault(real_connect(path, read_only=read_only)),
        )

    def permanent(_request: VisionRequest) -> object:
        raise PermanentProviderError("provider secret")

    with pytest.raises(ai_module.AnalysisError, match="^DB_WRITE_ERROR$") as captured:
        analyze_workspace(workspace, provider=Provider(permanent))
    assert "LEAKED" not in str(captured.value)
    assert rows(workspace, "SELECT status FROM ai_run")[-1] == ("RUNNING",)


@pytest.mark.parametrize(
    "failure_site",
    ["complete_update", "complete_commit", "initial_insert", "initial_commit"],
)
def test_all_run_audit_sqlite_faults_use_fixed_db_write_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_site: str
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    seed(workspace, 1, tmp_path / "private-source")
    if failure_site.endswith("update"):
        with closing(sqlite3.connect(workspace.database_path)) as connection:
            connection.execute(
                """CREATE TRIGGER fail_complete_update BEFORE UPDATE ON ai_run
                WHEN NEW.status='COMPLETE'
                BEGIN SELECT RAISE(ABORT, 'LEAKED_DB_SECRET'); END"""
            )
            connection.commit()
    elif failure_site.endswith("insert"):
        with closing(sqlite3.connect(workspace.database_path)) as connection:
            connection.execute(
                """CREATE TRIGGER fail_initial_insert BEFORE INSERT ON ai_run
                BEGIN SELECT RAISE(ABORT, 'LEAKED_DB_SECRET'); END"""
            )
            connection.commit()
    else:
        real_connect = ai_module.connect_database

        class AuditCommitFault:
            def __init__(self, connection: sqlite3.Connection) -> None:
                self.connection = connection
                self.failed = False

            def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
                return getattr(self.connection, name)

            def commit(self) -> None:
                status = self.connection.execute(
                    "SELECT status FROM ai_run ORDER BY started_at DESC LIMIT 1"
                ).fetchone()
                target = "COMPLETE" if failure_site == "complete_commit" else "RUNNING"
                if not self.failed and status == (target,):
                    self.failed = True
                    raise sqlite3.IntegrityError("LEAKED_DB_SECRET")
                self.connection.commit()

        monkeypatch.setattr(
            ai_module,
            "connect_database",
            lambda path, read_only=False: AuditCommitFault(real_connect(path, read_only=read_only)),
        )

    with pytest.raises(ai_module.AnalysisError, match="^DB_WRITE_ERROR$") as captured:
        analyze_workspace(workspace, provider=Provider())
    assert "LEAKED" not in str(captured.value)
    audits = rows(
        workspace,
        "SELECT status,item_count,cache_hit_count,analyzed_count,failed_count FROM ai_run",
    )
    if failure_site.startswith("initial"):
        assert audits == []
    else:
        assert audits == [("FAILED", 1, 0, 1, 0)]


@pytest.mark.parametrize(
    "failure_site",
    ["summary_select", "cache_lookup", "estimate_select", "rollback", "connect", "close"],
)
def test_all_ai_database_boundaries_normalize_sqlite_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_site: str
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    seed(workspace, 1, tmp_path / "private-source")
    real_connect = ai_module.connect_database

    if failure_site == "connect":

        def fail_connect(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise sqlite3.OperationalError("LEAKED_DB_SECRET")

        monkeypatch.setattr(ai_module, "connect_database", fail_connect)
    else:

        class BoundaryFault:
            def __init__(self, connection: sqlite3.Connection) -> None:
                self.connection = connection

            def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
                return getattr(self.connection, name)

            def execute(self, sql: str, parameters=()):  # type: ignore[no-untyped-def]
                normalized = " ".join(sql.split())
                if failure_site in {"summary_select", "estimate_select"} and normalized.startswith(
                    "SELECT m.id,m.media_type"
                ):
                    raise sqlite3.OperationalError("LEAKED_DB_SECRET")
                if failure_site == "cache_lookup" and normalized.startswith(
                    "SELECT 1 FROM ai_analysis"
                ):
                    raise sqlite3.OperationalError("LEAKED_DB_SECRET")
                if failure_site == "rollback" and normalized.startswith("INSERT INTO ai_analysis"):
                    raise sqlite3.IntegrityError("LEAKED_DB_SECRET")
                return self.connection.execute(sql, parameters)

            def rollback(self) -> None:
                if failure_site == "rollback":
                    raise sqlite3.OperationalError("LEAKED_ROLLBACK_SECRET")
                self.connection.rollback()

            def close(self) -> None:
                self.connection.close()
                if failure_site == "close":
                    raise sqlite3.OperationalError("LEAKED_CLOSE_SECRET")

        monkeypatch.setattr(
            ai_module,
            "connect_database",
            lambda path, read_only=False: BoundaryFault(real_connect(path, read_only=read_only)),
        )

    operation = (
        (lambda: estimate_workspace_analysis(workspace, provider=Provider()))
        if failure_site == "estimate_select"
        else (lambda: analyze_workspace(workspace, provider=Provider()))
    )
    with pytest.raises(ai_module.AnalysisError, match="^DB_WRITE_ERROR$") as captured:
        operation()
    assert "LEAKED" not in str(captured.value)
