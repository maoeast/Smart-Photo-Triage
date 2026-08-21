from __future__ import annotations

import io
import shutil
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

import pytest
from PIL import Image

from smart_photo_triage.preprocess import (
    FFmpegVideoBackend,
    PreviewConfig,
    PreviewError,
    generate_image_preview,
    generate_video_preview,
    preprocess_workspace,
    preview_fingerprint,
    video_sample_timestamps,
)
from smart_photo_triage.scanner import scan_library
from smart_photo_triage.workspace import Workspace, initialize_workspace


def make_oriented_jpeg(path: Path) -> None:
    image = Image.new("RGB", (40, 20), "red")
    for x in range(20, 40):
        for y in range(20):
            image.putpixel((x, y), (0, 0, 255))
    exif = Image.Exif()
    exif[274] = 6
    image.save(path, exif=exif, quality=100, subsampling=0)


def make_png(path: Path, color: tuple[int, int, int] = (20, 80, 140)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 20), color).save(path)


def query(workspace: Workspace, sql: str) -> list[tuple]:
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        return connection.execute(sql).fetchall()


class FakeVideoBackend:
    def __init__(self, *, failures: set[str] | None = None) -> None:
        self.failures = failures or set()
        self.timestamps: dict[str, list[float]] = {}

    def probe_duration(self, path: Path) -> float:
        if path.name in self.failures:
            raise PreviewError("synthetic corrupt video")
        return 12.0

    def read_frame(self, path: Path, timestamp: float) -> Image.Image:
        self.timestamps.setdefault(path.name, []).append(timestamp)
        return Image.new("RGB", (24, 16), (round(timestamp) % 255, 50, 100))


def test_t_c_001_image_preview_applies_exif_orientation(tmp_path: Path) -> None:
    source = tmp_path / "rotated.jpg"
    destination = tmp_path / "preview.png"
    make_oriented_jpeg(source)

    artifact = generate_image_preview(
        source,
        destination,
        source_fingerprint="source-v1",
        config=PreviewConfig(max_edge=100, image_format="PNG"),
    )

    with Image.open(artifact.path) as preview:
        assert preview.size == (20, 40)
        top = preview.convert("RGB").getpixel((10, 5))
        bottom = preview.convert("RGB").getpixel((10, 35))
    assert top[0] > top[2]
    assert bottom[2] > bottom[0]


def test_t_c_002_heic_preview_uses_registered_or_injected_decoder(tmp_path: Path) -> None:
    source = tmp_path / "synthetic.heic"
    source.write_bytes(b"synthetic container handled by injected decoder")
    destination = tmp_path / "heic-preview.png"
    opened: list[Path] = []

    def fake_heic_open(path: Path) -> Image.Image:
        opened.append(path)
        return Image.new("RGB", (30, 18), (30, 180, 60))

    artifact = generate_image_preview(
        source,
        destination,
        source_fingerprint="heic-source-v1",
        config=PreviewConfig(max_edge=64, image_format="PNG"),
        image_opener=fake_heic_open,
    )

    assert opened == [source]
    assert artifact.path.is_file()
    assert (artifact.width, artifact.height) == (30, 18)


@pytest.mark.skipif(
    ".heic" not in Image.registered_extensions(),
    reason="no Pillow HEIC/HEIF codec is registered in this environment",
)
def test_heic_registered_codec_integration(tmp_path: Path) -> None:
    source = tmp_path / "codec.heic"
    try:
        Image.new("RGB", (24, 16), (20, 100, 180)).save(source, format="HEIF")
    except (KeyError, OSError) as error:
        pytest.skip(f"registered HEIC codec cannot encode the synthetic fixture: {error}")

    artifact = generate_image_preview(
        source,
        tmp_path / "codec-preview.webp",
        source_fingerprint="codec-source-v1",
    )

    assert artifact.path.is_file()


def test_t_c_003_preview_fingerprint_is_deterministic_and_versioned() -> None:
    first = preview_fingerprint("source-signature", preview_version="preview-v7", max_edge=1024)
    second = preview_fingerprint("source-signature", preview_version="preview-v7", max_edge=1024)
    changed_version = preview_fingerprint(
        "source-signature", preview_version="preview-v8", max_edge=1024
    )
    changed_size = preview_fingerprint(
        "source-signature", preview_version="preview-v7", max_edge=512
    )
    changed_format = preview_fingerprint(
        "source-signature",
        preview_version="preview-v7",
        max_edge=1024,
        image_format="PNG",
    )

    assert first == second
    assert len(first) == 64
    assert len({first, changed_version, changed_size, changed_format}) == 4


def test_t_c_004_corrupt_image_is_failed_and_batch_continues(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "corrupt.jpg").write_bytes(b"not an image")
    make_png(source / "good.png")
    workspace = initialize_workspace(tmp_path / "workspace")
    scan_library(workspace, source)

    result = preprocess_workspace(workspace)
    statuses = {
        Path(path).name: (status, error)
        for path, status, error in query(
            workspace,
            """
            SELECT m.original_path, p.preview_status, p.preview_error
            FROM media_preprocess AS p JOIN media_item AS m ON m.id = p.media_id
            ORDER BY m.original_path
            """,
        )
    }

    assert result.processed_count == 1
    assert result.failed_count == 1
    assert statuses["good.png"] == ("READY", None)
    assert statuses["corrupt.jpg"][0] == "FAILED"
    assert statuses["corrupt.jpg"][1]


def test_preview_cache_resume_and_version_invalidation(tmp_path: Path) -> None:
    source = tmp_path / "source"
    make_png(source / "photo.png")
    workspace = initialize_workspace(tmp_path / "workspace")
    scan_library(workspace, source)

    first = preprocess_workspace(workspace, config=PreviewConfig(version="preview-v1"))
    first_row = query(
        workspace,
        "SELECT preview_fingerprint, preview_path FROM media_preprocess",
    )[0]
    second = preprocess_workspace(workspace, config=PreviewConfig(version="preview-v1"))
    Path(first_row[1]).unlink()
    resumed = preprocess_workspace(workspace, config=PreviewConfig(version="preview-v1"))
    changed = preprocess_workspace(workspace, config=PreviewConfig(version="preview-v2"))
    changed_row = query(
        workspace,
        "SELECT preview_fingerprint, preview_version FROM media_preprocess",
    )[0]

    assert (first.processed_count, first.cache_hit_count) == (1, 0)
    assert (second.processed_count, second.cache_hit_count) == (0, 1)
    assert (resumed.processed_count, resumed.cache_hit_count) == (1, 0)
    assert (changed.processed_count, changed.cache_hit_count) == (1, 0)
    assert changed_row[0] != first_row[0]
    assert changed_row[1] == "preview-v2"


def test_failed_preview_retries_after_source_is_repaired(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    path = source / "repair.png"
    path.write_bytes(b"broken")
    workspace = initialize_workspace(tmp_path / "workspace")
    scan_library(workspace, source)
    first = preprocess_workspace(workspace)
    make_png(path, (120, 30, 80))
    scan_library(workspace, source)

    second = preprocess_workspace(workspace)

    assert first.failed_count == 1
    assert second.processed_count == 1
    assert second.failed_count == 0
    assert query(workspace, "SELECT preview_status FROM media_preprocess") == [("READY",)]


@pytest.mark.parametrize(
    ("duration", "expected_count"),
    [(5.0, 3), (10.0, 3), (10.001, 6), (60.0, 6), (60.001, 9), (600.0, 9)],
)
def test_t_c_005_video_contact_sheet_default_sample_count(
    duration: float, expected_count: int
) -> None:
    timestamps = video_sample_timestamps(duration)

    assert len(timestamps) == expected_count
    assert tuple(sorted(timestamps)) == timestamps
    assert all(0.0 < value < duration for value in timestamps)
    assert timestamps[0] >= duration * 0.05
    assert timestamps[-1] <= duration * 0.95


def test_t_c_006_corrupt_video_is_failed_and_other_video_continues(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "broken.mp4").write_bytes(b"broken")
    (source / "good.mp4").write_bytes(b"synthetic")
    workspace = initialize_workspace(tmp_path / "workspace")
    scan_library(workspace, source)
    backend = FakeVideoBackend(failures={"broken.mp4"})

    result = preprocess_workspace(workspace, video_backend=backend)
    statuses = {
        Path(path).name: status
        for path, status in query(
            workspace,
            """
            SELECT m.original_path, p.preview_status
            FROM media_preprocess AS p JOIN media_item AS m ON m.id = p.media_id
            """,
        )
    }

    assert result.processed_count == 1
    assert result.failed_count == 1
    assert statuses == {"broken.mp4": "FAILED", "good.mp4": "READY"}
    assert len(backend.timestamps["good.mp4"]) == 6


class FakeProcess:
    def __init__(self, payload: bytes, returncode: int = 0) -> None:
        self.stdout = io.BytesIO(payload)
        self.stderr = io.BytesIO()
        self.returncode = returncode
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def test_t_c_007_ffmpeg_reads_one_bounded_frame_not_full_video(tmp_path: Path) -> None:
    frame_buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "purple").save(frame_buffer, format="PNG")
    commands: list[list[str]] = []

    def fake_popen(command: list[str], **_kwargs: object) -> FakeProcess:
        commands.append(command)
        return FakeProcess(frame_buffer.getvalue())

    backend = FFmpegVideoBackend(max_frame_bytes=4096, popen_factory=fake_popen)
    frame = backend.read_frame(tmp_path / "huge.mp4", 12.5)

    assert frame.size == (8, 8)
    assert len(commands) == 1
    command = commands[0]
    assert command.index("-ss") < command.index("-i")
    assert command[command.index("-frames:v") + 1] == "1"
    assert command[-1] == "-"


def test_ffmpeg_frame_limit_rejects_unbounded_output(tmp_path: Path) -> None:
    processes: list[FakeProcess] = []

    def fake_popen(_command: list[str], **_kwargs: object) -> FakeProcess:
        process = FakeProcess(b"x" * 65)
        processes.append(process)
        return process

    backend = FFmpegVideoBackend(max_frame_bytes=64, popen_factory=fake_popen)

    with pytest.raises(PreviewError, match="limit|large|bounded"):
        backend.read_frame(tmp_path / "huge.mp4", 1.0)
    assert processes[0].killed is True


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg integration tools are unavailable",
)
def test_real_ffmpeg_contact_sheet_integration_uses_generated_video(tmp_path: Path) -> None:
    source = tmp_path / "generated.mp4"
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=64x48:rate=2:duration=2",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        pytest.skip(result.stderr.decode(errors="replace"))

    artifact = generate_video_preview(
        source,
        tmp_path / "contact.webp",
        source_fingerprint="generated-video-v1",
    )

    assert artifact.path.is_file()
    assert artifact.width > 0 and artifact.height > 0
    assert artifact.fingerprint == preview_fingerprint("generated-video-v1")
