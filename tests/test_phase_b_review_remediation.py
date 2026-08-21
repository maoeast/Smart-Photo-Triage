from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

import smart_photo_triage.scanner as scanner_module
from smart_photo_triage.cli import main
from smart_photo_triage.database import MigrationError, apply_migrations, connect_database
from smart_photo_triage.metadata import CaptureMetadata
from smart_photo_triage.scanner import ScanLayoutError, scan_library
from smart_photo_triage.workspace import (
    Workspace,
    WorkspaceOwnershipError,
    initialize_workspace,
)


def make_sidecars(source: Path, count: int) -> list[Path]:
    source.mkdir(parents=True, exist_ok=True)
    paths = [source / f"item-{index:03d}.json" for index in range(count)]
    for path in paths:
        path.write_text("{}", encoding="utf-8")
    return paths


def db_rows(workspace: Workspace, sql: str, parameters: tuple[object, ...] = ()) -> list[tuple]:
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        return connection.execute(sql, parameters).fetchall()


def synthetic_metadata(_path: Path, _media_type: str) -> CaptureMetadata:
    return CaptureMetadata("2024-01-01T00:00:00", "SYNTHETIC", "HIGH", "UNKNOWN")


def source_presence(workspace: Workspace) -> dict[str, int]:
    return {
        Path(original_path).name: int(source_present)
        for original_path, source_present in db_rows(
            workspace,
            "SELECT original_path, source_present FROM media_item ORDER BY original_path",
        )
    }


def latest_run(workspace: Workspace) -> tuple:
    return db_rows(
        workspace,
        """
        SELECT id, status, indexed_count, missing_count, error_count, warning_count
        FROM scan_run ORDER BY rowid DESC LIMIT 1
        """,
    )[0]


def test_incomplete_walk_preserves_all_previous_presence_and_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    make_sidecars(source, 3)
    workspace = initialize_workspace(tmp_path / "workspace")
    scan_library(workspace, source, metadata_extractor=synthetic_metadata)
    original_walk = scanner_module._iter_supported_files

    def incomplete_walk(
        current_source: Path,
        excluded_roots: list[Path],
        exclude_globs: tuple[str, ...],
        on_error,  # type: ignore[no-untyped-def]
        on_skipped_prefix,  # type: ignore[no-untyped-def]
    ) -> Iterator[Path]:
        iterator = original_walk(
            current_source,
            excluded_roots,
            exclude_globs,
            on_error,
            on_skipped_prefix,
        )
        yield next(iterator)
        on_error(current_source / "unreadable", PermissionError("synthetic walk denial"))

    monkeypatch.setattr(scanner_module, "_iter_supported_files", incomplete_walk)

    result = scan_library(workspace, source, metadata_extractor=synthetic_metadata)

    assert result.missing_count == 0
    assert source_presence(workspace) == {
        "item-000.json": 1,
        "item-001.json": 1,
        "item-002.json": 1,
    }
    assert latest_run(workspace)[1] == "COMPLETE_WITH_WARNINGS"


def test_stat_scope_error_suppresses_missing_for_the_whole_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    paths = make_sidecars(source, 2)
    workspace = initialize_workspace(tmp_path / "workspace")
    scan_library(workspace, source, metadata_extractor=synthetic_metadata)
    original_stat = Path.stat
    failing_key = str(paths[1].absolute())

    def selective_stat(path: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if str(path.absolute()) == failing_key:
            raise PermissionError("synthetic stat denial")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", selective_stat)

    result = scan_library(workspace, source, metadata_extractor=synthetic_metadata)

    assert result.missing_count == 0
    assert set(source_presence(workspace).values()) == {1}
    assert latest_run(workspace)[1] == "COMPLETE_WITH_WARNINGS"


def test_new_output_and_glob_exclusions_preserve_historical_presence(tmp_path: Path) -> None:
    source = tmp_path / "source"
    protected = source / "protected-output"
    hidden = source / "hidden"
    make_sidecars(protected, 1)
    make_sidecars(hidden, 1)
    make_sidecars(source, 1)
    workspace = initialize_workspace(tmp_path / "workspace")
    scan_library(workspace, source, metadata_extractor=synthetic_metadata)

    result = scan_library(
        workspace,
        source,
        output=protected,
        exclude_globs=("hidden/**",),
        metadata_extractor=synthetic_metadata,
    )

    assert result.missing_count == 0
    assert set(source_presence(workspace).values()) == {1}


def test_disappearance_after_initial_stat_is_an_item_error_not_run_rollback(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    disappearing, stable = make_sidecars(source, 2)
    workspace = initialize_workspace(tmp_path / "workspace")

    def disappearing_metadata(path: Path, _media_type: str) -> CaptureMetadata:
        if path == disappearing:
            path.unlink()
            raise OSError("synthetic disappearance after stat")
        return synthetic_metadata(path, _media_type)

    result = scan_library(workspace, source, metadata_extractor=disappearing_metadata)

    assert result.indexed_count == 2
    assert result.error_count == 1
    assert stable.exists()
    assert db_rows(workspace, "SELECT COUNT(*) FROM media_item") == [(2,)]
    assert latest_run(workspace)[1] == "COMPLETE_WITH_WARNINGS"


def test_v2_validator_rejects_media_path_key_without_unique_constraint(
    tmp_path: Path,
) -> None:
    with closing(connect_database(tmp_path / "missing-unique.sqlite3")) as connection:
        apply_migrations(connection)
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("ALTER TABLE media_item RENAME TO media_item_old")
        connection.execute("DROP TABLE media_item_old")
        connection.execute(
            """
            CREATE TABLE media_item (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                original_path TEXT NOT NULL,
                path_key TEXT NOT NULL,
                source_root TEXT NOT NULL,
                source_root_key TEXT NOT NULL,
                parent_key TEXT NOT NULL,
                bundle_stem TEXT NOT NULL,
                media_type TEXT NOT NULL,
                extension TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                source_present INTEGER NOT NULL DEFAULT 1,
                content_sha256 TEXT,
                captured_at TEXT,
                capture_source TEXT NOT NULL,
                capture_confidence TEXT NOT NULL,
                capture_timezone_status TEXT NOT NULL,
                width INTEGER,
                height INTEGER,
                duration_seconds REAL,
                preview_path TEXT,
                preview_version TEXT,
                last_seen_at TEXT NOT NULL,
                last_seen_scan_id TEXT NOT NULL,
                metadata_error TEXT
            )
            """
        )
        connection.execute(
            "CREATE INDEX media_item_source_seen_idx "
            "ON media_item(source_root_key, source_present, last_seen_scan_id)"
        )
        connection.execute(
            "CREATE INDEX media_item_bundle_idx "
            "ON media_item(source_root_key, source_present, parent_key, bundle_stem, path_key)"
        )
        connection.commit()

        with pytest.raises(MigrationError, match="path_key.*unique|unique.*path_key"):
            apply_migrations(connection)


def test_v2_validator_rejects_missing_scan_warning_foreign_keys(tmp_path: Path) -> None:
    with closing(connect_database(tmp_path / "missing-fk.sqlite3")) as connection:
        apply_migrations(connection)
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("ALTER TABLE scan_warning RENAME TO scan_warning_old")
        connection.execute("DROP TABLE scan_warning_old")
        connection.execute(
            """
            CREATE TABLE scan_warning (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id TEXT NOT NULL,
                media_id INTEGER,
                path_key TEXT,
                code TEXT NOT NULL,
                message TEXT NOT NULL
            )
            """
        )
        connection.execute("CREATE INDEX scan_warning_run_idx ON scan_warning(scan_id, code)")
        connection.commit()

        with pytest.raises(MigrationError, match="foreign key"):
            apply_migrations(connection)


def test_v2_validator_rejects_missing_performance_index(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "missing-index-workspace")
    with closing(connect_database(workspace.database_path)) as connection:
        connection.execute("DROP INDEX media_item_source_seen_idx")
        connection.commit()

    with pytest.raises(WorkspaceOwnershipError, match="index.*media_item_source_seen_idx"):
        initialize_workspace(workspace.root)


def test_scan_run_is_visible_as_running_before_item_work(tmp_path: Path) -> None:
    source = tmp_path / "source"
    make_sidecars(source, 1)
    workspace = initialize_workspace(tmp_path / "workspace")
    observed_statuses: list[str | None] = []

    def inspect_running(path: Path, media_type: str) -> CaptureMetadata:
        status = db_rows(workspace, "SELECT status FROM scan_run ORDER BY rowid DESC LIMIT 1")
        observed_statuses.append(str(status[0][0]) if status else None)
        return synthetic_metadata(path, media_type)

    scan_library(workspace, source, metadata_extractor=inspect_running)

    assert observed_statuses == ["RUNNING"]


def test_keyboard_interrupt_persists_batch_and_resume_skips_completed_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    make_sidecars(source, 5)
    workspace = initialize_workspace(tmp_path / "workspace")
    monkeypatch.setattr(scanner_module, "_SCAN_BATCH_SIZE", 1, raising=False)
    original_sha256 = scanner_module._sha256
    hash_calls = 0

    def count_hash(path: Path) -> str:
        nonlocal hash_calls
        hash_calls += 1
        return original_sha256(path)

    monkeypatch.setattr(scanner_module, "_sha256", count_hash)
    first_calls = 0

    def interrupt_third(path: Path, media_type: str) -> CaptureMetadata:
        nonlocal first_calls
        first_calls += 1
        if first_calls == 3:
            raise KeyboardInterrupt("synthetic stop")
        return synthetic_metadata(path, media_type)

    with pytest.raises(KeyboardInterrupt, match="synthetic stop"):
        scan_library(
            workspace,
            source,
            hash_content=True,
            metadata_extractor=interrupt_third,
        )

    interrupted = latest_run(workspace)
    assert interrupted[1] == "INTERRUPTED"
    assert interrupted[2] == 2
    assert hash_calls == 3
    assert db_rows(workspace, "SELECT COUNT(*) FROM media_item") == [(2,)]

    resume_calls = 0
    hash_calls = 0

    def count_resume(path: Path, media_type: str) -> CaptureMetadata:
        nonlocal resume_calls
        resume_calls += 1
        return synthetic_metadata(path, media_type)

    resumed = scan_library(
        workspace,
        source,
        hash_content=True,
        metadata_extractor=count_resume,
    )

    assert resumed.indexed_count == 5
    assert resume_calls == 3
    assert hash_calls == 3
    assert db_rows(
        workspace,
        "SELECT resume_of FROM scan_run WHERE id = ?",
        (resumed.scan_id,),
    ) == [(interrupted[0],)]


def test_unexpected_run_failure_is_recorded_and_committed_items_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    make_sidecars(source, 3)
    workspace = initialize_workspace(tmp_path / "workspace")
    monkeypatch.setattr(scanner_module, "_SCAN_BATCH_SIZE", 1, raising=False)
    original_rebuild = scanner_module._rebuild_bundles

    def fail_rebuild(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("synthetic terminal failure")

    monkeypatch.setattr(scanner_module, "_rebuild_bundles", fail_rebuild)
    with pytest.raises(RuntimeError, match="synthetic terminal failure"):
        scan_library(workspace, source, metadata_extractor=synthetic_metadata)

    failed = latest_run(workspace)
    assert failed[1] == "FAILED"
    assert failed[2] == 3
    assert db_rows(workspace, "SELECT COUNT(*) FROM media_item") == [(3,)]

    monkeypatch.setattr(scanner_module, "_rebuild_bundles", original_rebuild)
    resume_calls = 0

    def count_resume(path: Path, media_type: str) -> CaptureMetadata:
        nonlocal resume_calls
        resume_calls += 1
        return synthetic_metadata(path, media_type)

    result = scan_library(workspace, source, metadata_extractor=count_resume)

    assert result.indexed_count == 3
    assert resume_calls == 0


def test_unchanged_item_with_metadata_error_is_retried_and_cleared(tmp_path: Path) -> None:
    source = tmp_path / "source"
    make_sidecars(source, 1)
    workspace = initialize_workspace(tmp_path / "workspace")

    def transient_failure(path: Path, _media_type: str) -> CaptureMetadata:
        return CaptureMetadata(
            captured_at="2024-01-01T00:00:00",
            capture_source="FILESYSTEM_MTIME",
            capture_confidence="LOW",
            capture_timezone_status="UTC",
            error=f"transient metadata failure for {path.name}",
        )

    scan_library(workspace, source, metadata_extractor=transient_failure)
    assert db_rows(workspace, "SELECT metadata_error FROM media_item") == [
        ("transient metadata failure for item-000.json",)
    ]
    retries = 0

    def successful_retry(path: Path, media_type: str) -> CaptureMetadata:
        nonlocal retries
        retries += 1
        return synthetic_metadata(path, media_type)

    result = scan_library(workspace, source, metadata_extractor=successful_retry)

    assert result.unchanged_count == 1
    assert retries == 1
    assert db_rows(workspace, "SELECT metadata_error, capture_source FROM media_item") == [
        (None, "SYNTHETIC")
    ]


def test_cli_validates_source_before_creating_workspace_and_returns_stable_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace_path = tmp_path / "must-not-exist"

    exit_code = main(["scan", str(tmp_path / "missing-source"), "--workspace", str(workspace_path)])
    captured = capsys.readouterr()

    assert exit_code != 0
    assert not workspace_path.exists()
    assert "Scan failed:" in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("first_scope", ["parent", "child"])
def test_overlapping_source_roots_are_rejected_in_both_directions(
    tmp_path: Path, first_scope: str
) -> None:
    parent = tmp_path / "library"
    child = parent / "nested"
    make_sidecars(child, 1)
    workspace = initialize_workspace(tmp_path / f"workspace-{first_scope}")
    first = parent if first_scope == "parent" else child
    second = child if first_scope == "parent" else parent
    scan_library(workspace, first, metadata_extractor=synthetic_metadata)

    with pytest.raises(ScanLayoutError, match="overlap"):
        scan_library(workspace, second, metadata_extractor=synthetic_metadata)


def test_missing_marking_is_scoped_to_the_rescanned_disjoint_root(tmp_path: Path) -> None:
    source_a = tmp_path / "source-a"
    source_b = tmp_path / "source-b"
    item_a = make_sidecars(source_a, 1)[0]
    make_sidecars(source_b, 1)
    workspace = initialize_workspace(tmp_path / "workspace")
    scan_library(workspace, source_a, metadata_extractor=synthetic_metadata)
    scan_library(workspace, source_b, metadata_extractor=synthetic_metadata)
    item_a.unlink()

    scan_library(workspace, source_b, metadata_extractor=synthetic_metadata)

    assert source_presence(workspace) == {"item-000.json": 1}
    assert db_rows(
        workspace,
        "SELECT source_present FROM media_item WHERE source_root = ?",
        (str(source_a.resolve()),),
    ) == [(1,)]
