from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, suppress
from pathlib import Path
from threading import Barrier, Event
from uuid import uuid4

import pytest

import smart_photo_triage.cli as cli_module
import smart_photo_triage.database as database_module
import smart_photo_triage.scanner as scanner_module
from smart_photo_triage.cli import main
from smart_photo_triage.database import (
    MigrationError,
    apply_migrations,
    connect_database,
    read_workspace_id,
)
from smart_photo_triage.metadata import CaptureMetadata
from smart_photo_triage.scanner import ScanLayoutError, scan_library
from smart_photo_triage.workspace import Workspace, initialize_workspace


def make_sidecar(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")


def synthetic_metadata(_path: Path, _media_type: str) -> CaptureMetadata:
    return CaptureMetadata("2024-01-01T00:00:00", "SYNTHETIC", "HIGH", "UNKNOWN")


def query(workspace: Workspace, sql: str, parameters: tuple[object, ...] = ()) -> list[tuple]:
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        return connection.execute(sql, parameters).fetchall()


def test_directory_exclude_pattern_protects_all_historical_descendants(tmp_path: Path) -> None:
    source = tmp_path / "source"
    make_sidecar(source / "hidden" / "nested" / "old.json")
    make_sidecar(source / "keep.json")
    workspace = initialize_workspace(tmp_path / "workspace")
    scan_library(workspace, source, metadata_extractor=synthetic_metadata)

    result = scan_library(
        workspace,
        source,
        exclude_globs=("hidden",),
        metadata_extractor=synthetic_metadata,
    )

    assert result.missing_count == 0
    assert query(
        workspace,
        "SELECT source_present FROM media_item WHERE original_path LIKE '%old.json'",
    ) == [(1,)]


def test_file_disappearing_after_initial_stat_is_not_persisted_present(tmp_path: Path) -> None:
    source = tmp_path / "source"
    disappearing = source / "vanishes.json"
    make_sidecar(disappearing)
    workspace = initialize_workspace(tmp_path / "workspace")

    def delete_during_metadata(path: Path, _media_type: str) -> CaptureMetadata:
        path.unlink()
        raise OSError("synthetic post-stat disappearance")

    result = scan_library(workspace, source, metadata_extractor=delete_during_metadata)

    assert result.error_count == 1
    assert query(workspace, "SELECT source_present FROM media_item") in ([], [(0,)])


def test_validator_rejects_partial_unique_path_key_index(tmp_path: Path) -> None:
    with closing(connect_database(tmp_path / "partial-unique.sqlite3")) as connection:
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
            "CREATE UNIQUE INDEX media_item_path_partial_unique "
            "ON media_item(path_key) WHERE source_present = 1"
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


def test_same_source_active_run_is_rejected_without_second_scan_run(tmp_path: Path) -> None:
    source = tmp_path / "source"
    make_sidecar(source / "one.json")
    workspace = initialize_workspace(tmp_path / "workspace")
    item_started = Event()
    release_item = Event()

    def blocking_metadata(path: Path, media_type: str) -> CaptureMetadata:
        item_started.set()
        assert release_item.wait(timeout=5)
        return synthetic_metadata(path, media_type)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            scan_library,
            workspace,
            source,
            metadata_extractor=blocking_metadata,
        )
        assert item_started.wait(timeout=2)
        try:
            with pytest.raises(ScanLayoutError, match="active|already running"):
                scan_library(workspace, source, metadata_extractor=synthetic_metadata)
        finally:
            release_item.set()
        first.result(timeout=5)

    assert query(workspace, "SELECT COUNT(*) FROM scan_run") == [(1,)]


def test_parent_child_registration_race_allows_only_one_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "library"
    child = parent / "nested"
    make_sidecar(child / "one.json")
    workspace = initialize_workspace(tmp_path / "workspace")
    before_check = Barrier(2)
    after_check = Barrier(2)
    original_reject = scanner_module._reject_overlapping_source

    def synchronized_reject(
        connection: sqlite3.Connection, source_root: Path, source_root_key: str
    ) -> None:
        with suppress(Exception):
            before_check.wait(timeout=0.3)
        original_reject(connection, source_root, source_root_key)
        with suppress(Exception):
            after_check.wait(timeout=0.3)

    monkeypatch.setattr(scanner_module, "_reject_overlapping_source", synchronized_reject)
    outcomes: list[str] = []

    def run(scope: Path) -> None:
        try:
            scan_library(workspace, scope, metadata_extractor=synthetic_metadata)
            outcomes.append("success")
        except ScanLayoutError:
            outcomes.append("overlap")

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(run, scope) for scope in (parent, child)]
        for future in futures:
            future.result(timeout=5)

    assert sorted(outcomes) == ["overlap", "success"]
    assert query(workspace, "SELECT COUNT(*) FROM scan_run") == [(1,)]


def test_real_old_v2_database_upgrades_to_current_and_preserves_identity_and_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "old-v2.sqlite3"
    workspace_id = uuid4().hex
    all_migrations = database_module.MIGRATIONS
    monkeypatch.setattr(database_module, "MIGRATIONS", all_migrations[:2])
    with closing(connect_database(database_path)) as connection:
        assert apply_migrations(connection, workspace_id=workspace_id) == 2
        scan_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(scan_run)")}
        assert "config_fingerprint" not in scan_columns
        assert "resume_of" not in scan_columns
        connection.execute(
            """
            INSERT INTO scan_run(id, source_root, source_root_key, started_at, status)
            VALUES ('old-run', 'C:/source', 'c:/source', '2024-01-01T00:00:00', 'COMPLETE')
            """
        )
        connection.execute(
            """
            INSERT INTO media_item(
                original_path, path_key, source_root, source_root_key, parent_key,
                bundle_stem, media_type, extension, size_bytes, mtime_ns,
                source_present, captured_at, capture_source, capture_confidence,
                capture_timezone_status, last_seen_at, last_seen_scan_id
            ) VALUES (
                'C:/source/old.json', 'c:/source/old.json', 'C:/source', 'c:/source',
                'c:/source', 'old', 'SIDECAR', '.json', 2, 1, 1,
                '2024-01-01T00:00:00', 'SYNTHETIC', 'HIGH', 'UNKNOWN',
                '2024-01-01T00:00:00', 'old-run'
            )
            """
        )
        connection.commit()

        monkeypatch.setattr(database_module, "MIGRATIONS", all_migrations)
        assert apply_migrations(connection, workspace_id=workspace_id) == all_migrations[-1].version
        assert read_workspace_id(connection) == workspace_id
        assert connection.execute(
            "SELECT original_path, source_present FROM media_item"
        ).fetchall() == [("C:/source/old.json", 1)]
        assert connection.execute(
            """
            SELECT config_fingerprint, resume_of, owner_pid, owner_token, terminal_reason
            FROM scan_run WHERE id = 'old-run'
            """
        ).fetchone() == ("", None, None, None, None)


def test_cli_catches_sqlite_operational_error_but_not_keyboard_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "source"
    make_sidecar(source / "one.json")

    def database_failure(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise sqlite3.OperationalError("synthetic database failure")

    monkeypatch.setattr(cli_module, "scan_library", database_failure)
    exit_code = main(["scan", str(source), "--workspace", str(tmp_path / "db-error-workspace")])
    captured = capsys.readouterr()
    assert exit_code == 2
    assert "synthetic database failure" in captured.err
    assert "Traceback" not in captured.err

    def interrupted(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise KeyboardInterrupt("synthetic user interrupt")

    monkeypatch.setattr(cli_module, "scan_library", interrupted)
    with pytest.raises(KeyboardInterrupt, match="synthetic user interrupt"):
        main(["scan", str(source), "--workspace", str(tmp_path / "interrupt-workspace")])
