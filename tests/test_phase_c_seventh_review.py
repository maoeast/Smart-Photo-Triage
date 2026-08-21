from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from PIL import Image

import smart_photo_triage.preprocess as preprocess_module
from smart_photo_triage.preprocess import preprocess_workspace
from smart_photo_triage.scanner import scan_library
from smart_photo_triage.workspace import initialize_workspace


def make_png(path: Path, color: str = "navy") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (16, 16), color).save(path)


def test_publish_candidate_swap_cannot_create_ready_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    make_png(source / "photo.png")
    workspace = initialize_workspace(tmp_path / "workspace")
    scan_library(workspace, source)

    def replace_candidate(_destination_parent: Path) -> None:
        candidate = next((workspace.root / "state").rglob("candidate-*.webp"))
        Image.new("RGB", (16, 16), "red").save(candidate, format="WEBP")

    monkeypatch.setattr(preprocess_module, "_SECURE_PUBLISH_TEST_HOOK", replace_candidate)
    result = preprocess_workspace(workspace)

    assert result.processed_count == 0
    assert result.failed_count == 1
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        status, path, digest = connection.execute(
            "SELECT preview_status,preview_path,preview_sha256 FROM media_preprocess"
        ).fetchone()
    assert status == "FAILED"
    assert path is None
    assert digest == ""
    assert not list((workspace.root / "previews").rglob("*.webp"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX renameat quarantine contract")
@pytest.mark.parametrize("directory", [False, True], ids=["leaf", "directory"])
def test_posix_delete_quarantine_rejects_link_swap_and_preserves_external_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, directory: bool
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    victim = parent / "victim"
    if directory:
        victim.mkdir()
        (victim / "old.txt").write_text("old", encoding="utf-8")
    else:
        victim.write_text("old", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    real_rename = os.rename
    hooked = False

    def racing_rename(src, dst, *args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal hooked
        if src == "victim" and not hooked:
            hooked = True
            real_rename(src, "victim-old", *args, **kwargs)
            os.symlink(outside, victim, target_is_directory=True)
        return real_rename(src, dst, *args, **kwargs)

    monkeypatch.setattr(preprocess_module.os, "rename", racing_rename)
    with pytest.raises(preprocess_module.PreviewError, match="identity|changed"):
        preprocess_module._nofollow_remove_tree(victim)

    assert hooked
    assert sentinel.read_text(encoding="utf-8") == "keep"
