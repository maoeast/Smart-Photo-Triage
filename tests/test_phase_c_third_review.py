from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

import pytest
from PIL import Image

import smart_photo_triage.preprocess as preprocess_module
from smart_photo_triage.database import MIGRATIONS, apply_migrations
from smart_photo_triage.preprocess import preprocess_workspace
from smart_photo_triage.scanner import scan_library
from smart_photo_triage.workspace import initialize_workspace


def make_png(path: Path, color: str = "navy") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (16, 16), color).save(path)


def test_real_original_v5_upgrades_additively_to_latest_and_preserves_phase_c_data(
    tmp_path: Path,
) -> None:
    database = tmp_path / "old-v5.sqlite3"
    workspace_id = "12345678123456781234567812345678"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for migration in MIGRATIONS[:5]:
            for statement in migration.statements:
                connection.execute(statement)
            if migration.version == 1:
                connection.execute(
                    "INSERT INTO workspace_metadata(key, value) VALUES ('workspace_id', ?)",
                    (workspace_id,),
                )
            connection.execute(
                "INSERT INTO schema_migration(version, name) VALUES (?, ?)",
                (migration.version, migration.name),
            )
            connection.execute(f"PRAGMA user_version = {migration.version}")

        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        burst_columns = {row[1] for row in connection.execute("PRAGMA table_info('burst_group')")}
        assert "grouping_run" not in tables
        assert "comparison_cap" not in burst_columns

        connection.execute(
            """
            INSERT INTO media_item(
                original_path, path_key, source_root, source_root_key, parent_key,
                bundle_stem, media_type, extension, size_bytes, mtime_ns, source_present,
                captured_at, capture_source, capture_confidence, capture_timezone_status,
                last_seen_at, last_seen_scan_id
            ) VALUES ('photo.png','photo.png','root','root','root','photo','IMAGE','.png',
                      10,20,1,'2024-01-01T00:00:00','MTIME','LOW','UNKNOWN','now','scan')
            """
        )
        media_id = int(connection.execute("SELECT id FROM media_item").fetchone()[0])
        connection.execute(
            """
            INSERT INTO media_preprocess(
                media_id, source_fingerprint, preview_fingerprint, preview_path,
                preview_version, preview_status, updated_at
            ) VALUES (?, 'source', 'preview', 'preview.webp', 'v1', 'READY', 'now')
            """,
            (media_id,),
        )
        connection.execute(
            """
            INSERT INTO burst_group(
                id, algorithm_version, representative_media_id,
                time_window_seconds, distance_threshold, created_at
            ) VALUES ('burst', 'old-v5', ?, 3.0, 8, 'now')
            """,
            (media_id,),
        )
        connection.execute(
            """
            INSERT INTO burst_member(
                group_id, media_id, distance, quality_score, is_representative, is_best_shot
            ) VALUES ('burst', ?, 0, 0.5, 1, 1)
            """,
            (media_id,),
        )
        connection.commit()

        assert apply_migrations(connection, workspace_id=workspace_id) == MIGRATIONS[-1].version
        assert connection.execute("PRAGMA user_version").fetchone() == (MIGRATIONS[-1].version,)
        assert connection.execute("SELECT value FROM workspace_metadata").fetchone() == (
            workspace_id,
        )
        assert connection.execute("SELECT path_key FROM media_item").fetchone() == ("photo.png",)
        assert connection.execute("SELECT preview_status FROM media_preprocess").fetchone() == (
            "READY",
        )
        assert connection.execute("SELECT id, comparison_cap FROM burst_group").fetchone() == (
            "burst",
            32,
        )
        assert connection.execute("SELECT media_id FROM burst_member").fetchone() == (media_id,)


@pytest.mark.skipif(os.name == "nt", reason="POSIX openat contract")
def test_posix_ancestor_swap_after_root_open_cannot_escape(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    nested = source / "nested"
    path = nested / "photo.png"
    make_png(path)
    outside = tmp_path / "outside"
    make_png(outside / "photo.png", "red")
    workspace = initialize_workspace(tmp_path / "workspace")
    scan_library(workspace, source)
    real_open = os.open
    swapped = False

    def racing_open(file, flags, mode=0o777, *, dir_fd=None):  # type: ignore[no-untyped-def]
        nonlocal swapped
        should_swap = (Path(file) == path) or (file == "nested" and dir_fd is not None)
        if should_swap and not swapped:
            swapped = True
            nested.rename(source / "nested-original")
            nested.symlink_to(outside, target_is_directory=True)
        return real_open(file, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(preprocess_module.os, "open", racing_open)
    result = preprocess_workspace(workspace)

    assert swapped
    assert result.failed_count == 1
    assert not list((workspace.root / "previews").rglob("*.webp"))


@pytest.mark.skipif(os.name != "nt", reason="Windows destination junction contract")
def test_windows_preview_destination_junction_swap_cannot_write_outside(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source"
    make_png(source / "photo.png")
    workspace = initialize_workspace(tmp_path / "workspace")
    scan_library(workspace, source)
    outside = tmp_path / "outside"
    outside.mkdir()
    swapped = False

    def hostile_swap(parent: Path) -> None:
        nonlocal swapped
        swapped = True
        parent.rename(parent.with_name(parent.name + "-original"))
        linked = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(parent), str(outside)],
            check=False,
            capture_output=True,
            text=True,
        )
        if linked.returncode != 0:
            raise OSError(linked.stderr or linked.stdout)

    monkeypatch.setattr(preprocess_module, "_SECURE_PUBLISH_TEST_HOOK", hostile_swap)
    result = preprocess_workspace(workspace)

    assert swapped
    assert result.failed_count == 1
    assert not any(
        path.name.endswith(".webp") and len(path.stem) == 64 for path in outside.iterdir()
    )


def test_keyboard_interrupt_always_removes_candidate(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    make_png(source / "photo.png")
    workspace = initialize_workspace(tmp_path / "workspace")
    scan_library(workspace, source)

    def interrupt(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise KeyboardInterrupt

    monkeypatch.setattr(preprocess_module, "_stable_source_identity", interrupt)
    with pytest.raises(KeyboardInterrupt):
        preprocess_workspace(workspace)

    assert not list(workspace.root.rglob("candidate-*"))


def test_preprocess_run_cleanup_removes_only_proven_dead_owner(tmp_path: Path, monkeypatch) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    runs = workspace.root / "state" / "preprocess-runs"
    dead = runs / "dead-run"
    active = runs / "active-run"
    for directory, pid in ((dead, 111), (active, 222)):
        directory.mkdir(parents=True)
        (directory / "owner.json").write_text(
            json.dumps({"pid": pid, "token": f"token-{pid}"}), encoding="utf-8"
        )
        (directory / "full-source-copy.bin").write_bytes(b"private media bytes")

    monkeypatch.setattr(
        preprocess_module,
        "_process_is_alive",
        lambda pid, _token: pid == 222,
        raising=False,
    )
    preprocess_workspace(workspace)

    assert not dead.exists()
    assert active.is_dir()
    assert (active / "full-source-copy.bin").read_bytes() == b"private media bytes"
