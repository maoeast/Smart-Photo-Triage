from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import closing
from datetime import datetime
from pathlib import Path
from threading import Event

import pytest
from PIL import Image

import smart_photo_triage.cli as cli_module
from smart_photo_triage.cli import main
from smart_photo_triage.grouping import BurstCandidate, group_burst_candidates
from smart_photo_triage.preprocess import (
    FFmpegVideoBackend,
    PreviewConfig,
    PreviewError,
    generate_video_preview,
    preprocess_workspace,
)
from smart_photo_triage.scanner import scan_library
from smart_photo_triage.workspace import Workspace, initialize_workspace


def query(workspace: Workspace, sql: str) -> list[tuple]:
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        return connection.execute(sql).fetchall()


class ControlledStream:
    def __init__(self, payload: bytes = b"", *, block: bool = False, repeat: bool = False) -> None:
        self.payload = payload
        self.block = block
        self.repeat = repeat
        self.released = Event()
        self.closed = False
        self._sent = False

    def read(self, size: int = -1) -> bytes:
        if self.block:
            self.released.wait(timeout=5)
        if self.closed:
            return b""
        if self.repeat:
            return self.payload[:size] if size >= 0 else self.payload
        if self._sent:
            return b""
        self._sent = True
        return self.payload[:size] if size >= 0 else self.payload

    def close(self) -> None:
        self.closed = True
        self.released.set()


class ControlledProcess:
    def __init__(
        self,
        *,
        stdout: ControlledStream,
        stderr: ControlledStream | None = None,
        running: bool = False,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr or ControlledStream()
        self.running = running
        self.killed = False
        self.wait_count = 0

    def wait(self, timeout: float | None = None) -> int:
        self.wait_count += 1
        if self.running and not self.killed:
            raise subprocess.TimeoutExpired("synthetic-media-process", timeout)
        return -9 if self.killed else 0

    def kill(self) -> None:
        self.killed = True
        self.running = False
        self.stdout.close()
        self.stderr.close()


def test_runner_does_not_permanently_block_on_stdout_reader(tmp_path: Path) -> None:
    process = ControlledProcess(stdout=ControlledStream(block=True))
    backend = FFmpegVideoBackend(
        timeout_seconds=0.2,
        cleanup_timeout_seconds=0.2,
        popen_factory=lambda *_args, **_kwargs: process,
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(backend.read_frame, tmp_path / "video.mp4", 1.0)
        try:
            with pytest.raises(PreviewError, match="invalid frame|empty"):
                future.result(timeout=1.0)
        except FutureTimeoutError:
            pytest.fail("bounded runner remained blocked on stdout")
        finally:
            process.stdout.close()

    assert process.stdout.closed is True
    assert process.stderr.closed is True


def test_runner_caps_continuous_stderr_and_kills_process(tmp_path: Path) -> None:
    process = ControlledProcess(
        stdout=ControlledStream(block=True),
        stderr=ControlledStream(b"stderr-loop", repeat=True),
        running=True,
    )
    backend = FFmpegVideoBackend(
        max_stderr_bytes=64,
        timeout_seconds=1.0,
        cleanup_timeout_seconds=0.2,
        popen_factory=lambda *_args, **_kwargs: process,
    )

    with pytest.raises(PreviewError, match="stderr.*limit|bounded stderr"):
        backend.read_frame(tmp_path / "video.mp4", 1.0)

    assert process.killed is True
    assert process.wait_count >= 1
    assert process.stdout.closed is True
    assert process.stderr.closed is True


def test_runner_timeout_kills_waits_and_closes_both_pipes(tmp_path: Path) -> None:
    process = ControlledProcess(
        stdout=ControlledStream(block=True),
        stderr=ControlledStream(block=True),
        running=True,
    )
    backend = FFmpegVideoBackend(
        timeout_seconds=0.05,
        cleanup_timeout_seconds=0.2,
        popen_factory=lambda *_args, **_kwargs: process,
    )
    started = time.monotonic()

    with pytest.raises(PreviewError, match="timed out"):
        backend.read_frame(tmp_path / "video.mp4", 1.0)

    assert time.monotonic() - started < 1.0
    assert process.killed is True
    assert process.wait_count >= 2
    assert process.stdout.closed is True
    assert process.stderr.closed is True


def test_runner_stdout_overflow_kills_waits_and_cleans_up(tmp_path: Path) -> None:
    process = ControlledProcess(
        stdout=ControlledStream(b"x" * 256, repeat=True),
        stderr=ControlledStream(),
        running=True,
    )
    backend = FFmpegVideoBackend(
        max_frame_bytes=64,
        timeout_seconds=1.0,
        cleanup_timeout_seconds=0.2,
        popen_factory=lambda *_args, **_kwargs: process,
    )

    with pytest.raises(PreviewError, match="stdout.*limit|bounded stdout"):
        backend.read_frame(tmp_path / "video.mp4", 1.0)

    assert process.killed is True
    assert process.wait_count >= 1
    assert process.stdout.closed is True
    assert process.stderr.closed is True


def make_disguised_bmp(path: Path, color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (16, 16), color).save(path, format="BMP")


def test_same_path_size_and_mtime_content_replacement_misses_preview_cache(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    path = source / "photo.png"
    make_disguised_bmp(path, (200, 10, 10))
    original_stat = path.stat()
    original_size = original_stat.st_size
    workspace = initialize_workspace(tmp_path / "workspace")
    scan_library(workspace, source)
    first = preprocess_workspace(workspace)
    first_fingerprint, first_sha = query(
        workspace,
        """
        SELECT p.preview_fingerprint, m.content_sha256
        FROM media_preprocess AS p JOIN media_item AS m ON m.id = p.media_id
        """,
    )[0]

    make_disguised_bmp(path, (10, 10, 200))
    assert path.stat().st_size == original_size
    os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    assert path.stat().st_mtime_ns == original_stat.st_mtime_ns
    second = preprocess_workspace(workspace)
    second_fingerprint, second_sha = query(
        workspace,
        """
        SELECT p.preview_fingerprint, m.content_sha256
        FROM media_preprocess AS p JOIN media_item AS m ON m.id = p.media_id
        """,
    )[0]

    assert first.processed_count == 1
    assert second.processed_count == 1
    assert second.cache_hit_count == 0
    assert second_fingerprint != first_fingerprint
    assert second_sha != first_sha
    assert second_sha == hashlib.sha256(path.read_bytes()).hexdigest()


def test_source_change_during_generation_fails_and_removes_stale_artifact(tmp_path: Path) -> None:
    source = tmp_path / "source"
    path = source / "photo.png"
    make_disguised_bmp(path, (200, 10, 10))
    replacement = tmp_path / "replacement.bmp"
    make_disguised_bmp(replacement, (10, 200, 10))
    original_stat = path.stat()
    workspace = initialize_workspace(tmp_path / "workspace")
    scan_library(workspace, source)

    def mutate_after_open(current_path: Path) -> Image.Image:
        with Image.open(current_path) as opened:
            decoded = opened.copy()
        path.write_bytes(replacement.read_bytes())
        os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        return decoded

    result = preprocess_workspace(workspace, image_opener=mutate_after_open)

    status, error, preview_path = query(
        workspace,
        "SELECT preview_status, preview_error, preview_path FROM media_preprocess",
    )[0]
    assert result.failed_count == 1
    assert status == "FAILED"
    assert "changed" in error.lower()
    assert preview_path is None
    assert list((workspace.root / "previews").rglob("*.webp")) == []


def test_burst_shared_band_adversary_has_strict_linear_comparison_cap() -> None:
    origin = datetime.fromisoformat("2024-01-01T00:00:00")
    generated = [
        BurstCandidate(
            media_id=index + 1,
            captured_at=origin.isoformat(),
            perceptual_hash="00000000"
            + hashlib.sha256(f"candidate-{index}".encode()).hexdigest()[:8],
            path_key=f"item-{index:04d}",
        )
        for index in range(1000)
    ]
    comparison_cap = 24

    first = group_burst_candidates(
        generated,
        distance_threshold=1,
        comparison_cap=comparison_cap,
    )
    second = group_burst_candidates(
        list(reversed(generated)),
        distance_threshold=1,
        comparison_cap=comparison_cap,
    )

    assert first == second
    assert first.comparison_count <= len(generated) * comparison_cap


class SmallFrameBackend:
    def probe_duration(self, _path: Path) -> float:
        return 12.0

    def read_frame(self, _path: Path, timestamp: float) -> Image.Image:
        return Image.new("RGB", (48, 32), (round(timestamp) % 255, 40, 90))


@pytest.mark.parametrize("max_edge", [1, 8, 16, 31])
def test_video_contact_sheet_final_longest_edge_respects_small_limit(
    tmp_path: Path, max_edge: int
) -> None:
    artifact = generate_video_preview(
        tmp_path / "synthetic.mp4",
        tmp_path / f"contact-{max_edge}.png",
        source_fingerprint=f"video-{max_edge}",
        config=PreviewConfig(max_edge=max_edge, image_format="PNG"),
        backend=SmallFrameBackend(),
    )

    assert 0 < artifact.width <= max_edge
    assert 0 < artifact.height <= max_edge


@pytest.mark.parametrize(
    ("command", "attribute", "error", "prefix"),
    [
        ("preprocess", "preprocess_workspace", sqlite3.OperationalError("db failed"), "Preprocess"),
        ("group", "group_workspace", ValueError("bad threshold"), "Grouping"),
    ],
)
def test_phase_c_cli_expected_errors_are_stable_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    attribute: str,
    error: Exception,
    prefix: str,
) -> None:
    workspace = initialize_workspace(tmp_path / f"workspace-{command}")

    def fail(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise error

    monkeypatch.setattr(cli_module, attribute, fail)
    exit_code = main([command, "--workspace", str(workspace.root)])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert captured.out == ""
    assert captured.err.startswith(f"{prefix} failed:")
    assert str(error) in captured.err
    assert "Traceback" not in captured.err
