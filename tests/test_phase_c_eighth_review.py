from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

from PIL import Image

import smart_photo_triage.preprocess as preprocess_module
from smart_photo_triage.preprocess import preprocess_workspace
from smart_photo_triage.scanner import scan_library
from smart_photo_triage.workspace import initialize_workspace


def make_png(path: Path, color: str = "navy") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (16, 16), color).save(path)


def test_two_media_with_one_fingerprint_generate_once_and_share_ready_artifact(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    make_png(source / "a.png")
    make_png(source / "b.png")
    workspace = initialize_workspace(tmp_path / "workspace")
    scan_library(workspace, source)
    calls = 0

    def counted_opener(path: Path):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return Image.open(path)

    result = preprocess_workspace(workspace, image_opener=counted_opener)

    assert calls == 1
    assert result.processed_count == 1
    assert result.cache_hit_count == 1
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        rows = connection.execute(
            "SELECT preview_status,preview_path,preview_sha256 "
            "FROM media_preprocess ORDER BY media_id"
        ).fetchall()
    assert len(rows) == 2
    assert {row[0] for row in rows} == {"READY"}
    assert len({row[1] for row in rows}) == 1
    assert len({row[2] for row in rows}) == 1


def test_second_writer_cannot_enter_between_postverify_and_ready_commit(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    make_png(source / "photo.png")
    workspace = initialize_workspace(tmp_path / "workspace")
    scan_library(workspace, source)
    verified = threading.Event()
    release = threading.Event()
    calls_lock = threading.Lock()
    opener_calls = 0
    hook_calls = 0
    results = []
    errors: list[BaseException] = []

    def counted_opener(path: Path):  # type: ignore[no-untyped-def]
        nonlocal opener_calls
        with calls_lock:
            opener_calls += 1
        return Image.open(path)

    def postverify_hook(_path: Path) -> None:
        nonlocal hook_calls
        with calls_lock:
            hook_calls += 1
            current = hook_calls
        if current == 1:
            verified.set()
            assert release.wait(5)

    def run_writer() -> None:
        try:
            results.append(preprocess_workspace(workspace, image_opener=counted_opener))
        except BaseException as error:
            errors.append(error)

    preprocess_module._POST_VERIFY_TEST_HOOK = postverify_hook
    try:
        first = threading.Thread(target=run_writer)
        first.start()
        assert verified.wait(5)
        second = threading.Thread(target=run_writer)
        second.start()
        time.sleep(0.2)
        with calls_lock:
            assert opener_calls == 1
            assert hook_calls == 1
        assert second.is_alive()
        release.set()
        first.join(5)
        second.join(5)
    finally:
        preprocess_module._POST_VERIFY_TEST_HOOK = None
        release.set()

    assert not errors
    assert len(results) == 2
    assert sorted((result.processed_count, result.cache_hit_count) for result in results) == [
        (0, 1),
        (1, 0),
    ]
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        assert connection.execute("SELECT preview_status FROM media_preprocess").fetchone() == (
            "READY",
        )


def lock_path_for(source_file: Path, source_root: Path, workspace_root: Path) -> Path:
    identity = preprocess_module._stable_source_identity(source_file, source_root)
    source_signature = preprocess_module._source_fingerprint("IMAGE", identity.content_sha256)
    fingerprint = preprocess_module.preview_fingerprint(source_signature)
    lock_root = workspace_root / "state" / "publication-locks"
    lock_root.mkdir(parents=True, exist_ok=True)
    return lock_root / f"{fingerprint}.lock"


def test_proven_dead_publication_owner_is_reclaimed(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    source_file = source / "photo.png"
    make_png(source_file)
    workspace = initialize_workspace(tmp_path / "workspace")
    scan_library(workspace, source)
    lock = lock_path_for(source_file, source, workspace.root)
    lock.write_text(json.dumps({"pid": 987654321, "token": "dead"}), encoding="utf-8")
    real_alive = preprocess_module._process_is_alive
    monkeypatch.setattr(
        preprocess_module,
        "_process_is_alive",
        lambda pid, token: False if pid == 987654321 else real_alive(pid, token),
    )

    result = preprocess_workspace(workspace)

    assert result.processed_count == 1
    assert not lock.exists()


def test_unknown_publication_owner_is_preserved_and_returns_bounded_busy(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source"
    source_file = source / "photo.png"
    make_png(source_file)
    workspace = initialize_workspace(tmp_path / "workspace")
    scan_library(workspace, source)
    lock = lock_path_for(source_file, source, workspace.root)
    lock.write_text("not-owner-json", encoding="utf-8")
    monkeypatch.setattr(preprocess_module, "_PUBLICATION_LOCK_POLL_SECONDS", 0.005)

    result = preprocess_workspace(workspace, lock_wait_timeout=0.05)

    assert result.failed_count == 0
    assert result.deferred_count == 1
    assert lock.read_text(encoding="utf-8") == "not-owner-json"
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM media_preprocess").fetchone() == (0,)


def test_postverify_external_replacement_is_failed_and_not_deleted(tmp_path: Path) -> None:
    source = tmp_path / "source"
    make_png(source / "photo.png")
    workspace = initialize_workspace(tmp_path / "workspace")
    scan_library(workspace, source)
    replacement_sha = ""

    def replace_after_verify(path: Path) -> None:
        nonlocal replacement_sha
        replacement = path.with_suffix(".replacement")
        Image.new("RGB", (16, 16), "red").save(replacement, format="WEBP")
        replacement_sha = preprocess_module._file_sha256(replacement)
        os.replace(replacement, path)

    preprocess_module._POST_VERIFY_TEST_HOOK = replace_after_verify
    try:
        result = preprocess_workspace(workspace)
    finally:
        preprocess_module._POST_VERIFY_TEST_HOOK = None

    assert result.failed_count == 1
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        status, path, digest = connection.execute(
            "SELECT preview_status,preview_path,preview_sha256 FROM media_preprocess"
        ).fetchone()
        fingerprint = connection.execute(
            "SELECT preview_fingerprint FROM media_preprocess"
        ).fetchone()[0]
    canonical = workspace.root / "previews" / fingerprint[:2] / f"{fingerprint}.webp"
    assert status == "FAILED"
    assert path is None
    assert digest == ""
    assert preprocess_module._file_sha256(canonical) == replacement_sha
