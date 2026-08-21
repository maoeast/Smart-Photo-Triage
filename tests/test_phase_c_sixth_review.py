from __future__ import annotations

import hashlib
import os
import sqlite3
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


def test_populated_original_v6_upgrades_to_latest_preserving_preview(tmp_path: Path) -> None:
    database = tmp_path / "old-v6.sqlite3"
    workspace_id = "12345678123456781234567812345678"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for migration in MIGRATIONS[:6]:
            for statement in migration.statements:
                connection.execute(statement)
            if migration.version == 1:
                connection.execute(
                    "INSERT INTO workspace_metadata(key,value) VALUES ('workspace_id',?)",
                    (workspace_id,),
                )
            connection.execute(
                "INSERT INTO schema_migration(version,name) VALUES (?,?)",
                (migration.version, migration.name),
            )
            connection.execute(f"PRAGMA user_version={migration.version}")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(media_preprocess)")}
        assert "preview_sha256" not in columns
        connection.execute(
            """
            INSERT INTO media_item(
              original_path,path_key,source_root,source_root_key,parent_key,bundle_stem,
              media_type,extension,size_bytes,mtime_ns,source_present,capture_source,
              capture_confidence,capture_timezone_status,last_seen_at,last_seen_scan_id
            ) VALUES ('p','p','r','r','r','p','IMAGE','.png',1,2,1,'MTIME','LOW','UNKNOWN','n','s')
            """
        )
        media_id = int(connection.execute("SELECT id FROM media_item").fetchone()[0])
        connection.execute(
            """
            INSERT INTO media_preprocess(
              media_id,source_fingerprint,preview_fingerprint,preview_path,
              preview_version,preview_status,updated_at
            ) VALUES (?,'s','f','p.webp','v','READY','now')
            """,
            (media_id,),
        )
        connection.commit()

        assert apply_migrations(connection, workspace_id=workspace_id) == MIGRATIONS[-1].version
        assert connection.execute(
            "SELECT preview_status,preview_sha256 FROM media_preprocess"
        ).fetchone() == (
            "READY",
            "",
        )
        assert connection.execute("SELECT value FROM workspace_metadata").fetchone() == (
            workspace_id,
        )
        assert connection.execute("SELECT deferred_count FROM preprocess_run").fetchall() == []


def test_valid_same_dimension_artifact_replacement_misses_cache(tmp_path: Path) -> None:
    source = tmp_path / "source"
    make_png(source / "photo.png")
    workspace = initialize_workspace(tmp_path / "workspace")
    scan_library(workspace, source)
    assert preprocess_workspace(workspace).processed_count == 1
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        preview_path, old_sha = connection.execute(
            "SELECT preview_path,preview_sha256 FROM media_preprocess"
        ).fetchone()
    preview = Path(preview_path)
    Image.new("RGB", (16, 16), "red").save(preview, format="WEBP")
    replacement_sha = hashlib.sha256(preview.read_bytes()).hexdigest()
    assert replacement_sha != old_sha

    result = preprocess_workspace(workspace)
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        new_sha = connection.execute("SELECT preview_sha256 FROM media_preprocess").fetchone()[0]

    assert result.cache_hit_count == 0
    assert result.processed_count == 1
    assert new_sha == hashlib.sha256(preview.read_bytes()).hexdigest()
    assert new_sha != replacement_sha


@pytest.mark.skipif(os.name != "nt", reason="Windows leaf reparse contract")
def test_windows_preview_leaf_reparse_is_not_read_as_cache(tmp_path: Path) -> None:
    source = tmp_path / "source"
    make_png(source / "photo.png")
    workspace = initialize_workspace(tmp_path / "workspace")
    scan_library(workspace, source)
    assert preprocess_workspace(workspace).processed_count == 1
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        preview = Path(
            connection.execute("SELECT preview_path FROM media_preprocess").fetchone()[0]
        )
    outside = tmp_path / "outside.webp"
    Image.new("RGB", (16, 16), "red").save(outside, format="WEBP")
    preview.unlink()
    try:
        os.symlink(outside, preview)
    except OSError as error:
        pytest.skip(f"file symlink unavailable: {error}")

    result = preprocess_workspace(workspace)

    assert result.cache_hit_count == 0
    assert result.processed_count == 1
    assert outside.is_file()
    assert not preview.is_symlink()


def test_candidate_unlink_failure_still_closes_item_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    make_png(source / "photo.png")
    workspace = initialize_workspace(tmp_path / "workspace")
    scan_library(workspace, source)
    original_unlink = Path.unlink

    def failing_unlink(path: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if path.name.startswith("candidate-"):
            raise OSError("injected candidate cleanup failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    result = preprocess_workspace(workspace)
    assert result.processed_count == 1
    workspace.root.rename(tmp_path / "workspace-renamed")


@pytest.mark.skipif(os.name == "nt", reason="POSIX dir-fd identity contract")
def test_posix_delete_rejects_directory_identity_swap_between_stat_and_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "parent"
    victim = parent / "victim"
    victim.mkdir(parents=True)
    (victim / "old.txt").write_text("old", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    real_open = os.open
    swapped = False

    def racing_open(file, flags, mode=0o777, *, dir_fd=None):  # type: ignore[no-untyped-def]
        nonlocal swapped
        if file == "victim" and dir_fd is not None and not swapped:
            swapped = True
            victim.rename(parent / "victim-old")
            outside.rename(victim)
        return real_open(file, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(preprocess_module.os, "open", racing_open)
    with pytest.raises(preprocess_module.PreviewError, match="identity|changed"):
        preprocess_module._nofollow_remove_tree(victim)
    assert (victim / "sentinel.txt").read_text(encoding="utf-8") == "keep"
