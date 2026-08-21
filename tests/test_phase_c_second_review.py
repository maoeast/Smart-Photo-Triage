from __future__ import annotations

import os
import sqlite3
import struct
import subprocess
from contextlib import closing
from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image

from smart_photo_triage.cli import main
from smart_photo_triage.grouping import (
    BURST_ALGORITHM_VERSION,
    BurstCandidate,
    group_burst_candidates,
    group_workspace,
)
from smart_photo_triage.preprocess import (
    PreviewConfig,
    PreviewError,
    generate_image_preview,
    preprocess_workspace,
)
from smart_photo_triage.scanner import scan_library
from smart_photo_triage.workspace import Workspace, initialize_workspace


def query(workspace: Workspace, sql: str) -> list[tuple]:
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        return connection.execute(sql).fetchall()


def make_png(path: Path, size: tuple[int, int], color: str = "navy") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)


def test_preprocess_does_not_take_ownership_of_scanner_stat_fields(tmp_path: Path) -> None:
    source = tmp_path / "source"
    path = source / "photo.png"
    make_png(path, (16, 16))
    workspace = initialize_workspace(tmp_path / "workspace")
    scan_library(workspace, source)
    old_size, old_mtime, old_width, old_height = query(
        workspace, "SELECT size_bytes, mtime_ns, width, height FROM media_item"
    )[0]

    make_png(path, (32, 24), "orange")
    os.utime(path, ns=(path.stat().st_atime_ns, old_mtime + 10_000_000_000))
    replacement_size = path.stat().st_size
    replacement_mtime = path.stat().st_mtime_ns
    preprocess_workspace(workspace)
    after_preprocess = query(
        workspace,
        "SELECT size_bytes, mtime_ns, width, height, content_sha256 FROM media_item",
    )[0]
    rescan = scan_library(workspace, source)
    after_rescan = query(workspace, "SELECT size_bytes, mtime_ns, width, height FROM media_item")[0]

    assert after_preprocess[:4] == (old_size, old_mtime, old_width, old_height)
    assert after_preprocess[4]
    assert rescan.unchanged_count == 0
    assert after_rescan == (replacement_size, replacement_mtime, 32, 24)


def _replace_with_file_symlink(link: Path, target: Path) -> None:
    link.unlink()
    try:
        link.symlink_to(target)
    except OSError as error:
        pytest.skip(f"file symlink creation is unavailable: {error}")


@pytest.mark.parametrize("extension", [".png", ".mp4"])
def test_preprocess_rejects_leaf_symlink_swap_for_images_and_videos(
    tmp_path: Path, extension: str
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    path = source / f"media{extension}"
    if extension == ".png":
        make_png(path, (16, 16))
    else:
        path.write_bytes(b"original synthetic video")
    outside = tmp_path / f"outside{extension}"
    if extension == ".png":
        make_png(outside, (16, 16), "red")
    else:
        outside.write_bytes(b"outside synthetic video")
    workspace = initialize_workspace(tmp_path / "workspace")
    scan_library(workspace, source)
    _replace_with_file_symlink(path, outside)

    result = preprocess_workspace(workspace)
    status, error, preview_path = query(
        workspace, "SELECT preview_status, preview_error, preview_path FROM media_preprocess"
    )[0]

    assert result.failed_count == 1
    assert status == "FAILED"
    assert "link" in error.lower() or "reparse" in error.lower()
    assert preview_path is None
    assert list((workspace.root / "previews").rglob("*.webp")) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows junction contract")
def test_preprocess_rejects_parent_junction_swap(tmp_path: Path) -> None:
    source = tmp_path / "source"
    original_directory = source / "nested"
    path = original_directory / "photo.png"
    make_png(path, (16, 16))
    outside = tmp_path / "outside"
    make_png(outside / "photo.png", (16, 16), "red")
    workspace = initialize_workspace(tmp_path / "workspace")
    scan_library(workspace, source)
    original_directory.rename(source / "nested-original")
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(original_directory), str(outside)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"junction creation unavailable: {result.stderr or result.stdout}")

    processed = preprocess_workspace(workspace)
    status, error = query(workspace, "SELECT preview_status, preview_error FROM media_preprocess")[
        0
    ]

    assert processed.failed_count == 1
    assert status == "FAILED"
    assert "link" in error.lower() or "reparse" in error.lower()


def test_preview_config_and_cli_reject_excessive_max_edge(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(ValueError, match="4096|maximum|large"):
        PreviewConfig(max_edge=4097)

    workspace = initialize_workspace(tmp_path / "workspace")
    exit_code = main(["preprocess", "--workspace", str(workspace.root), "--max-edge", "4097"])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "Preprocess failed:" in captured.err
    assert "Traceback" not in captured.err


def test_image_decode_pixel_limit_rejects_bomb_header_before_load(tmp_path: Path) -> None:
    source = tmp_path / "oversized.bmp"
    width, height = 9000, 8000
    header = struct.pack(
        "<2sIHHI IiiHHIIiiII",
        b"BM",
        54,
        0,
        0,
        54,
        40,
        width,
        height,
        1,
        24,
        0,
        width * height * 3,
        2835,
        2835,
        0,
        0,
    )
    source.write_bytes(header)

    with pytest.raises(PreviewError, match="pixel|decode.*limit|large"):
        generate_image_preview(
            source,
            tmp_path / "preview.webp",
            source_fingerprint="oversized",
        )


def test_burst_similarity_ranking_keeps_late_true_neighbor_under_cap() -> None:
    origin = datetime.fromisoformat("2024-01-01T00:00:00").isoformat()
    far_hashes = [f"00000000{index * 0x9E3779B1 & 0xFFFFFFFF:08x}" for index in range(1, 22)]
    candidates = [
        BurstCandidate(index, origin, value, path_key=f"anchor-{index:02d}")
        for index, value in enumerate(far_hashes, start=1)
    ]
    late_anchor = candidates[-1]
    near_hash = f"{int(late_anchor.perceptual_hash, 16) ^ 1:016x}"
    candidates.append(BurstCandidate(22, origin, near_hash, path_key="zz-near"))

    result = group_burst_candidates(candidates, distance_threshold=1, comparison_cap=24)
    reversed_result = group_burst_candidates(
        list(reversed(candidates)), distance_threshold=1, comparison_cap=24
    )

    assert result == reversed_result
    assert any(set(cluster.member_ids) == {late_anchor.media_id, 22} for cluster in result.clusters)
    assert result.comparison_count <= len(candidates) * 24
    assert result.warning_count >= 1
    assert result.algorithm_version == BURST_ALGORITHM_VERSION


def test_burst_candidate_truncation_warning_is_persisted_with_algorithm_metadata(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    for index in range(22):
        make_png(source / f"photo-{index:02d}.png", (8, 8), "navy")
    workspace = initialize_workspace(tmp_path / "workspace")
    scan_library(workspace, source)
    preprocess_workspace(workspace)
    far_hashes = [f"00000000{index * 0x9E3779B1 & 0xFFFFFFFF:08x}" for index in range(1, 22)]
    near_hash = f"{int(far_hashes[-1], 16) ^ 1:016x}"
    hashes = far_hashes + [near_hash]
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        media_ids = [
            int(row[0]) for row in connection.execute("SELECT id FROM media_item ORDER BY path_key")
        ]
        for media_id, perceptual_hash in zip(media_ids, hashes, strict=True):
            connection.execute(
                "UPDATE media_item SET captured_at = '2024-01-01T00:00:00+00:00' WHERE id = ?",
                (media_id,),
            )
            connection.execute(
                "UPDATE media_preprocess SET perceptual_hash = ? WHERE media_id = ?",
                (perceptual_hash, media_id),
            )
        connection.commit()

    result = group_workspace(workspace, distance_threshold=1, comparison_cap=24)
    persisted = query(
        workspace,
        """
        SELECT algorithm_version, comparison_cap, warning_count, warning_json
        FROM grouping_run ORDER BY created_at DESC LIMIT 1
        """,
    )[0]

    assert result.warning_count >= 1
    assert persisted[0] == BURST_ALGORITHM_VERSION
    assert persisted[1] == 24
    assert persisted[2] == result.warning_count
    assert "ranked" in persisted[3]
