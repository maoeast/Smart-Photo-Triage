from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import get_type_hints

import pytest
from PIL import Image

import smart_photo_triage.cli as cli_module
import smart_photo_triage.preprocess as preprocess_module
from smart_photo_triage.cli import main
from smart_photo_triage.database import MIGRATIONS, apply_migrations
from smart_photo_triage.preprocess import PreprocessResult, preprocess_workspace
from smart_photo_triage.scanner import scan_library
from smart_photo_triage.workspace import initialize_workspace


def make_png(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (16, 16), "navy").save(path)


def fingerprint_for(path: Path, root: Path) -> str:
    identity = preprocess_module._stable_source_identity(path, root)
    signature = preprocess_module._source_fingerprint("IMAGE", identity.content_sha256)
    return preprocess_module.preview_fingerprint(signature)


def test_preview_fingerprint_return_annotation_matches_runtime() -> None:
    result = preprocess_module.preview_fingerprint("source-fingerprint")

    assert isinstance(result, str)
    assert get_type_hints(preprocess_module.preview_fingerprint)["return"] is str


def test_published_artifact_return_annotation_matches_runtime(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    destination = workspace_root / "previews" / "artifact.webp"
    make_png(destination)
    with Image.open(destination) as image:
        width, height = image.size

    result = preprocess_module._published_artifact_sha256(
        destination,
        workspace_root,
        expected_width=width,
        expected_height=height,
    )

    assert isinstance(result, preprocess_module._ArtifactVerification)
    assert (
        get_type_hints(preprocess_module._published_artifact_sha256)["return"]
        is preprocess_module._ArtifactVerification
    )


def test_default_known_live_owner_has_no_derived_deadline(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    media = source / "photo.png"
    make_png(media)
    workspace = initialize_workspace(tmp_path / "workspace")
    scan_library(workspace, source)
    verified = threading.Event()
    release = threading.Event()
    results = []

    monkeypatch.setattr(
        preprocess_module,
        "_default_publication_lock_wait_timeout",
        lambda _backend: 0.02,
    )

    def hook(_path: Path) -> None:
        if not verified.is_set():
            verified.set()
            assert release.wait(1)

    def writer() -> None:
        results.append(preprocess_workspace(workspace))

    preprocess_module._POST_VERIFY_TEST_HOOK = hook
    try:
        first = threading.Thread(target=writer)
        first.start()
        assert verified.wait(1)
        second = threading.Thread(target=writer)
        second.start()
        time.sleep(0.08)
        assert second.is_alive()
        release.set()
        first.join(2)
        second.join(2)
    finally:
        preprocess_module._POST_VERIFY_TEST_HOOK = None
        release.set()

    assert sorted(
        (item.processed_count, item.cache_hit_count, item.deferred_count) for item in results
    ) == [(0, 1, 0), (1, 0, 0)]


def test_default_unknown_owner_defers_immediately_without_reclaim(tmp_path: Path, caplog) -> None:
    source = tmp_path / "source"
    media = source / "photo.png"
    make_png(media)
    workspace = initialize_workspace(tmp_path / "workspace")
    scan_library(workspace, source)
    lock = workspace.root / "state" / "publication-locks" / f"{fingerprint_for(media, source)}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("unknown-owner", encoding="utf-8")

    started = time.monotonic()
    result = preprocess_workspace(workspace)

    assert time.monotonic() - started < 0.5
    assert result.deferred_count == 1
    assert result.failed_count == 0
    assert lock.read_text(encoding="utf-8") == "unknown-owner"
    assert "owner metadata is unknown or malformed" in caplog.text


def test_populated_v7_upgrades_to_v8_with_deferred_count(tmp_path: Path) -> None:
    database = tmp_path / "v7.sqlite3"
    workspace_id = "12345678123456781234567812345678"
    with closing(sqlite3.connect(database)) as connection:
        for migration in MIGRATIONS[:7]:
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
        connection.execute(
            """
            INSERT INTO preprocess_run(
              id,config_fingerprint,preview_version,started_at,status,
              processed_count,cache_hit_count,failed_count
            ) VALUES ('run','cfg','v','now','COMPLETE',3,4,5)
            """
        )
        connection.commit()

        assert apply_migrations(connection, workspace_id=workspace_id) == MIGRATIONS[-1].version
        assert connection.execute(
            "SELECT processed_count,cache_hit_count,failed_count,deferred_count FROM preprocess_run"
        ).fetchone() == (3, 4, 5, 0)


def test_explicit_timeout_persists_exact_deferred_count_for_two_media(tmp_path: Path) -> None:
    source = tmp_path / "source"
    make_png(source / "a.png")
    make_png(source / "b.png")
    workspace = initialize_workspace(tmp_path / "workspace")
    scan_library(workspace, source)
    lock = (
        workspace.root
        / "state"
        / "publication-locks"
        / f"{fingerprint_for(source / 'a.png', source)}.lock"
    )
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(
        json.dumps({"pid": preprocess_module.os.getpid(), "token": "live"}),
        encoding="utf-8",
    )

    result = preprocess_workspace(workspace, lock_wait_timeout=0.02)

    assert result.deferred_count == 2
    assert result.failed_count == 0
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        assert connection.execute(
            "SELECT status,deferred_count FROM preprocess_run WHERE id=?", (result.run_id,)
        ).fetchone() == ("COMPLETE_WITH_DEFERRED", 2)


def test_failure_and_defer_persist_combined_terminal_counts(tmp_path: Path) -> None:
    source = tmp_path / "source"
    media = source / "healthy.png"
    make_png(media)
    (source / "corrupt.png").write_bytes(b"not-an-image")
    workspace = initialize_workspace(tmp_path / "workspace")
    scan_library(workspace, source)
    lock = workspace.root / "state" / "publication-locks" / f"{fingerprint_for(media, source)}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(
        json.dumps({"pid": preprocess_module.os.getpid(), "token": "live"}),
        encoding="utf-8",
    )

    result = preprocess_workspace(workspace, lock_wait_timeout=0.02)

    assert (result.failed_count, result.deferred_count) == (1, 1)
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        assert connection.execute(
            """
            SELECT status,processed_count,cache_hit_count,failed_count,deferred_count
            FROM preprocess_run WHERE id=?
            """,
            (result.run_id,),
        ).fetchone() == ("COMPLETE_WITH_FAILURES_AND_DEFERRED", 0, 0, 1, 1)


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_cli_lock_wait_seconds_requires_positive_finite_value(value: str) -> None:
    with pytest.raises(SystemExit, match="2"):
        main(["preprocess", "--lock-wait-seconds", value])


def test_cli_passes_explicit_lock_wait_seconds(tmp_path: Path, monkeypatch) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    observed: list[float | None] = []

    def fake_preprocess(*_args, lock_wait_timeout=None, **_kwargs) -> PreprocessResult:
        observed.append(lock_wait_timeout)
        return PreprocessResult("run", 0, 0, 0, 0)

    monkeypatch.setattr(cli_module, "preprocess_workspace", fake_preprocess)

    assert (
        main(
            [
                "preprocess",
                "--workspace",
                str(workspace.root),
                "--lock-wait-seconds",
                "1.25",
            ]
        )
        == 0
    )
    assert observed == [1.25]
