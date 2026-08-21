from __future__ import annotations

import os
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest
from PIL import Image

import smart_photo_triage.preprocess as preprocess_module
from smart_photo_triage.preprocess import preprocess_workspace
from smart_photo_triage.scanner import scan_library
from smart_photo_triage.workspace import initialize_workspace


def make_png(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (16, 16), "navy").save(path)


@pytest.mark.skipif(os.name != "nt", reason="Windows preview junction contract")
def test_ready_cache_rejects_previews_junction_with_external_same_name(tmp_path: Path) -> None:
    source = tmp_path / "source"
    make_png(source / "photo.png")
    workspace = initialize_workspace(tmp_path / "workspace")
    scan_library(workspace, source)
    first = preprocess_workspace(workspace)
    assert first.processed_count == 1
    previews = workspace.root / "previews"
    original = workspace.root / "previews-original"
    outside = tmp_path / "outside"
    previews.rename(original)
    shutil.copytree(original, outside)
    linked = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(previews), str(outside)],
        check=False,
        capture_output=True,
        text=True,
    )
    if linked.returncode != 0:
        pytest.skip(linked.stderr or linked.stdout)

    second = preprocess_workspace(workspace)

    assert second.cache_hit_count == 0
    assert second.failed_count == 1


@pytest.mark.parametrize("failure", ["second_acquire", "owner_write"])
def test_partial_security_setup_closes_every_acquired_resource(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    original = preprocess_module._secure_workspace_directory
    calls = 0

    @contextmanager
    def injected(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if failure == "second_acquire" and calls == 2:
            raise OSError("injected second acquire failure")
        with original(*args, **kwargs) as binding:
            yield binding

    monkeypatch.setattr(preprocess_module, "_secure_workspace_directory", injected)
    if failure == "owner_write":
        monkeypatch.setattr(
            preprocess_module,
            "_secure_write_new",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("owner write failed")),
        )

    with pytest.raises(OSError):
        preprocess_workspace(workspace)

    renamed = tmp_path / f"workspace-renamed-{failure}"
    workspace.root.rename(renamed)
    assert renamed.is_dir()


@pytest.mark.skipif(os.name != "nt", reason="Windows cleanup junction contract")
def test_dead_run_cleanup_unlinks_child_junction_without_touching_external_sentinel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    dead = workspace.root / "state" / "preprocess-runs" / "dead"
    dead.mkdir(parents=True)
    (dead / "owner.json").write_text('{"pid":999,"token":"dead"}', encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    junction = dead / "hostile-child"
    linked = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        check=False,
        capture_output=True,
        text=True,
    )
    if linked.returncode != 0:
        pytest.skip(linked.stderr or linked.stdout)
    monkeypatch.setattr(preprocess_module, "_process_is_alive", lambda *_args: False)

    preprocess_workspace(workspace)

    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert not dead.exists()
