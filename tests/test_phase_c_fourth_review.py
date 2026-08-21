from __future__ import annotations

import os
import subprocess
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


@pytest.mark.skipif(os.name != "nt", reason="Windows ancestor pin contract")
def test_windows_pins_previews_ancestor_through_publish(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    make_png(source / "photo.png")
    workspace = initialize_workspace(tmp_path / "workspace")
    scan_library(workspace, source)
    outside = tmp_path / "outside"
    outside.mkdir()
    attempted = False

    def swap_previews_ancestor(_hash_directory: Path) -> None:
        nonlocal attempted
        attempted = True
        previews = workspace.root / "previews"
        previews.rename(workspace.root / "previews-original")
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(previews), str(outside)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise OSError(result.stderr or result.stdout)

    monkeypatch.setattr(preprocess_module, "_SECURE_PUBLISH_TEST_HOOK", swap_previews_ancestor)
    result = preprocess_workspace(workspace)

    assert attempted
    assert result.failed_count == 1
    assert not list(outside.rglob("*.webp"))


@pytest.mark.parametrize("component", ["state", "preprocess-runs"])
def test_preprocess_run_tree_rejects_workspace_link_ancestor(
    tmp_path: Path, component: str
) -> None:
    source = tmp_path / "source"
    make_png(source / "photo.png")
    workspace = initialize_workspace(tmp_path / "workspace")
    scan_library(workspace, source)
    outside = tmp_path / "outside"
    outside.mkdir()
    state = workspace.root / "state"
    if component == "preprocess-runs":
        target = state / "preprocess-runs"
    else:
        target = state
        state.rename(workspace.root / "state-original")
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(target), str(outside)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            pytest.skip(result.stderr or result.stdout)
    else:
        target.symlink_to(outside, target_is_directory=True)

    with pytest.raises((OSError, preprocess_module.PreviewError)):
        preprocess_workspace(workspace)

    assert not (outside / "preprocess-runs").exists()
    assert not list(outside.rglob("candidate-*"))
