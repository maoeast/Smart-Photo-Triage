from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

from PIL import Image

import smart_photo_triage.cli as cli_module
import smart_photo_triage.preprocess as preprocess_module
from smart_photo_triage.cli import main
from smart_photo_triage.preprocess import (
    FFmpegVideoBackend,
    PreprocessResult,
    preprocess_workspace,
)
from smart_photo_triage.scanner import scan_library
from smart_photo_triage.workspace import initialize_workspace


def make_png(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (16, 16), "navy").save(path)


def fingerprint_for(path: Path, root: Path) -> str:
    identity = preprocess_module._stable_source_identity(path, root)
    source_signature = preprocess_module._source_fingerprint("IMAGE", identity.content_sha256)
    return preprocess_module.preview_fingerprint(source_signature)


def test_default_lock_wait_covers_maximum_ffmpeg_operation_budget() -> None:
    backend = FFmpegVideoBackend(timeout_seconds=2.0)
    assert preprocess_module._default_publication_lock_wait_timeout(backend) >= 25.0


def test_explicit_busy_budget_is_deferred_not_failed_or_ready(tmp_path: Path) -> None:
    source = tmp_path / "source"
    media = source / "photo.png"
    make_png(media)
    workspace = initialize_workspace(tmp_path / "workspace")
    scan_library(workspace, source)
    fingerprint = fingerprint_for(media, source)
    lock = workspace.root / "state" / "publication-locks" / f"{fingerprint}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(
        json.dumps({"pid": preprocess_module.os.getpid(), "token": "live"}),
        encoding="utf-8",
    )

    result = preprocess_workspace(workspace, lock_wait_timeout=0.05)

    assert result.failed_count == 0
    assert result.deferred_count == 1
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM media_preprocess").fetchone() == (0,)
        assert connection.execute(
            "SELECT status FROM preprocess_run WHERE id=?", (result.run_id,)
        ).fetchone() == ("COMPLETE_WITH_DEFERRED",)


def test_healthy_writer_outlasting_old_budget_waits_then_cache_hits(tmp_path: Path) -> None:
    source = tmp_path / "source"
    media = source / "photo.png"
    make_png(media)
    workspace = initialize_workspace(tmp_path / "workspace")
    scan_library(workspace, source)
    verified = threading.Event()
    release = threading.Event()
    results = []

    def hook(_path: Path) -> None:
        if not verified.is_set():
            verified.set()
            assert release.wait(1)

    def writer() -> None:
        results.append(preprocess_workspace(workspace, lock_wait_timeout=1.0))

    preprocess_module._POST_VERIFY_TEST_HOOK = hook
    try:
        first = threading.Thread(target=writer)
        first.start()
        assert verified.wait(1)
        second = threading.Thread(target=writer)
        second.start()
        time.sleep(0.1)
        assert second.is_alive()
        release.set()
        first.join(2)
        second.join(2)
    finally:
        preprocess_module._POST_VERIFY_TEST_HOOK = None
        release.set()

    assert sorted(
        (result.processed_count, result.cache_hit_count, result.deferred_count)
        for result in results
    ) == [(0, 1, 0), (1, 0, 0)]


def test_contender_waits_for_new_lock_owner_metadata_publication(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source"
    media = source / "photo.png"
    make_png(media)
    workspace = initialize_workspace(tmp_path / "workspace")
    scan_library(workspace, source)
    lock_created = threading.Event()
    publish_owner = threading.Event()
    original_write_new = preprocess_module._secure_write_new
    first_lock = True
    gate = threading.Lock()
    results = []

    def paused_write_new(binding, name: str, payload: bytes) -> None:  # type: ignore[no-untyped-def]
        nonlocal first_lock
        with gate:
            pause = first_lock and name.endswith(".lock")
            if pause:
                first_lock = False
        if not pause:
            original_write_new(binding, name, payload)
            return
        flags = (
            preprocess_module.os.O_WRONLY
            | preprocess_module.os.O_CREAT
            | preprocess_module.os.O_EXCL
            | getattr(preprocess_module.os, "O_BINARY", 0)
            | getattr(preprocess_module.os, "O_NOFOLLOW", 0)
        )
        descriptor = (
            preprocess_module.os.open(name, flags, 0o600, dir_fd=binding.directory_fd)
            if binding.directory_fd is not None
            else preprocess_module.os.open(binding.access_path / name, flags, 0o600)
        )
        try:
            lock_created.set()
            assert publish_owner.wait(1)
            with preprocess_module.os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(payload)
        finally:
            if descriptor >= 0:
                preprocess_module.os.close(descriptor)

    def writer() -> None:
        results.append(preprocess_workspace(workspace, lock_wait_timeout=1.0))

    monkeypatch.setattr(preprocess_module, "_secure_write_new", paused_write_new)
    first = threading.Thread(target=writer)
    first.start()
    assert lock_created.wait(1)
    second = threading.Thread(target=writer)
    second.start()
    second.join(0.05)
    assert second.is_alive()
    publish_owner.set()
    first.join(2)
    second.join(2)

    assert sorted(
        (result.processed_count, result.cache_hit_count, result.deferred_count)
        for result in results
    ) == [(0, 1, 0), (1, 0, 0)]


def test_cli_displays_deferred_count(tmp_path: Path, monkeypatch, capsys) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    monkeypatch.setattr(
        cli_module,
        "preprocess_workspace",
        lambda *_args, **_kwargs: PreprocessResult("run", 0, 0, 0, 2),
    )

    assert main(["preprocess", "--workspace", str(workspace.root)]) == 0
    assert "failed=0 deferred=2" in capsys.readouterr().out
