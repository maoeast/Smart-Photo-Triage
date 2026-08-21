from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import pytest

import smart_photo_triage.database as database_module
import smart_photo_triage.scanner as scanner_module
from smart_photo_triage.database import apply_migrations, connect_database, read_workspace_id
from smart_photo_triage.metadata import CaptureMetadata
from smart_photo_triage.scanner import ScanLayoutError, scan_library
from smart_photo_triage.workspace import Workspace, initialize_workspace


def synthetic_metadata(_path: Path, _media_type: str) -> CaptureMetadata:
    return CaptureMetadata("2024-01-01T00:00:00", "SYNTHETIC", "HIGH", "UNKNOWN")


def query(workspace: Workspace, sql: str, parameters: tuple[object, ...] = ()) -> list[tuple]:
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        return connection.execute(sql, parameters).fetchall()


@pytest.mark.skipif(os.name != "nt", reason="Windows process-handle semantics")
def test_windows_process_alive_checks_exit_code_while_popen_handle_remains_open() -> None:
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert scanner_module._process_is_alive(process.pid, "synthetic-owner") is True
        process.terminate()
        process.wait(timeout=5)
        assert scanner_module._process_is_alive(process.pid, "synthetic-owner") is False
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def insert_active_run(
    workspace: Workspace,
    source: Path,
    *,
    scan_id: str,
    owner_pid: int,
    discovered_count: int = 0,
    indexed_count: int = 0,
) -> None:
    layout = scanner_module.validate_scan_layout(source, workspace.root)
    fingerprint = scanner_module._scan_config_fingerprint(layout, (), False)
    with closing(connect_database(workspace.database_path)) as connection:
        connection.execute(
            """
            INSERT INTO scan_run(
                id, source_root, source_root_key, config_fingerprint, started_at, status,
                discovered_count, indexed_count, owner_pid, owner_token
            ) VALUES (?, ?, ?, ?, '2024-01-01T00:00:00+00:00', 'RUNNING', ?, ?, ?, ?)
            """,
            (
                scan_id,
                str(layout.source_root),
                scanner_module._path_key(layout.source_root),
                fingerprint,
                discovered_count,
                indexed_count,
                owner_pid,
                f"owner-{scan_id}",
            ),
        )
        connection.commit()


def test_live_active_owner_is_rejected_without_polluting_scan_run(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    workspace = initialize_workspace(tmp_path / "workspace")
    insert_active_run(workspace, source, scan_id="live-run", owner_pid=12345)
    checked: list[tuple[int, str | None]] = []

    def process_alive(pid: int, token: str | None) -> bool:
        checked.append((pid, token))
        return True

    with pytest.raises(ScanLayoutError, match="active|already running"):
        scan_library(
            workspace,
            source,
            metadata_extractor=synthetic_metadata,
            process_alive=process_alive,
        )

    assert checked == [(12345, "owner-live-run")]
    assert query(workspace, "SELECT id, status FROM scan_run") == [("live-run", "RUNNING")]


def test_dead_active_owner_is_atomically_interrupted_and_resumed(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "one.json").write_text("{}", encoding="utf-8")
    workspace = initialize_workspace(tmp_path / "workspace")
    insert_active_run(
        workspace,
        source,
        scan_id="dead-run",
        owner_pid=987654321,
        discovered_count=9,
        indexed_count=7,
    )

    result = scan_library(
        workspace,
        source,
        metadata_extractor=synthetic_metadata,
        process_alive=lambda pid, token: False,
    )

    assert query(
        workspace,
        """
        SELECT status, discovered_count, indexed_count, error_count, terminal_reason
        FROM scan_run WHERE id = 'dead-run'
        """,
    ) == [("INTERRUPTED", 9, 7, 1, "OWNER_PROCESS_NOT_FOUND")]
    assert query(
        workspace,
        "SELECT resume_of, status FROM scan_run WHERE id = ?",
        (result.scan_id,),
    ) == [("dead-run", "COMPLETE")]


def test_real_v3_database_upgrades_to_v4_without_identity_or_data_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "old-v3.sqlite3"
    workspace_id = uuid4().hex
    all_migrations = database_module.MIGRATIONS
    assert len(all_migrations) >= 4
    monkeypatch.setattr(database_module, "MIGRATIONS", all_migrations[:3])

    with closing(connect_database(database_path)) as connection:
        assert apply_migrations(connection, workspace_id=workspace_id) == 3
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(scan_run)")}
        assert "owner_pid" not in columns
        assert "owner_token" not in columns
        assert "terminal_reason" not in columns
        connection.execute(
            """
            INSERT INTO scan_run(
                id, source_root, source_root_key, config_fingerprint, started_at, status,
                discovered_count, indexed_count
            ) VALUES (
                'v3-run', 'C:/source', 'c:/source', 'fingerprint',
                '2024-01-01T00:00:00+00:00', 'INTERRUPTED', 9, 7
            )
            """
        )
        connection.commit()

        monkeypatch.setattr(database_module, "MIGRATIONS", all_migrations[:4])
        assert apply_migrations(connection, workspace_id=workspace_id) == 4
        assert read_workspace_id(connection) == workspace_id
        assert connection.execute(
            """
            SELECT id, discovered_count, indexed_count, owner_pid, owner_token, terminal_reason
            FROM scan_run
            """
        ).fetchall() == [("v3-run", 9, 7, None, None, None)]
