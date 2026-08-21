"""Versioned, resumable Phase C previews and local quality metrics."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import math
import os
import sqlite3
import stat
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import ExitStack, closing, contextmanager, suppress
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Protocol, cast
from uuid import uuid4

from PIL import Image, ImageFilter, ImageOps, ImageStat

from smart_photo_triage.database import connect_database
from smart_photo_triage.workspace import Workspace

try:
    from pillow_heif import register_heif_opener
except ImportError:  # item failure remains explicit when the codec is absent
    register_heif_opener = None
else:
    register_heif_opener()

PREVIEW_VERSION = "preview-v1"
PERCEPTUAL_HASH_VERSION = "dhash64-v1"
_SOURCE_FINGERPRINT_VERSION = "source-sha256-v2"
_PREPROCESS_BATCH_SIZE = 50
_DIAGNOSTIC_LIMIT = 64 * 1024
_HASH_CHUNK_SIZE = 1024 * 1024
_PIPE_CHUNK_SIZE = 8192
MAX_PREVIEW_EDGE = 4096
MAX_DECODE_PIXELS = 64 * 1024 * 1024
_WINDOWS_REPARSE_ATTRIBUTE = 0x400
_SECURE_PUBLISH_TEST_HOOK: Callable[[Path], None] | None = None
_POST_VERIFY_TEST_HOOK: Callable[[Path], None] | None = None
_PUBLICATION_LOCK_POLL_SECONDS = 0.05
_PUBLICATION_LOCK_METADATA_GRACE_SECONDS = 0.25
_LOGGER = logging.getLogger(__name__)


class PreviewError(RuntimeError):
    """Raised when one preview cannot be produced."""


class PreviewBusyError(PreviewError):
    """Raised when a live or unprovable publication owner remains busy."""


@dataclass(frozen=True, slots=True)
class PreviewConfig:
    max_edge: int = 1024
    version: str = PREVIEW_VERSION
    image_format: str = "WEBP"

    def __post_init__(self) -> None:
        if self.max_edge <= 0:
            raise ValueError("preview max_edge must be positive")
        if self.max_edge > MAX_PREVIEW_EDGE:
            raise ValueError(f"preview max_edge must not exceed {MAX_PREVIEW_EDGE}")
        if not self.version:
            raise ValueError("preview version must not be empty")
        if self.image_format.upper() not in {"WEBP", "PNG", "JPEG"}:
            raise ValueError("preview image_format must be WEBP, PNG, or JPEG")


@dataclass(frozen=True, slots=True)
class QualityMetrics:
    sharpness: float
    exposure: float
    clipping: float
    resolution: int
    score: float
    advisory: str


@dataclass(frozen=True, slots=True)
class PreviewArtifact:
    fingerprint: str
    path: Path
    width: int
    height: int
    perceptual_hash: str
    quality: QualityMetrics


@dataclass(frozen=True, slots=True)
class PreprocessResult:
    run_id: str
    processed_count: int
    cache_hit_count: int
    failed_count: int
    deferred_count: int


class VideoBackend(Protocol):
    def probe_duration(self, path: Path) -> float: ...

    def read_frame(self, path: Path, timestamp: float) -> Image.Image: ...


class _Process(Protocol):
    stdout: BinaryIO | None
    stderr: BinaryIO | None

    def wait(self, timeout: float | None = None) -> int: ...

    def kill(self) -> None: ...


class FFmpegVideoBackend:
    """Extract only requested frames through bounded subprocess pipes."""

    def __init__(
        self,
        *,
        ffmpeg: str = "ffmpeg",
        ffprobe: str = "ffprobe",
        max_frame_bytes: int = 16 * 1024 * 1024,
        max_stderr_bytes: int = _DIAGNOSTIC_LIMIT,
        timeout_seconds: float = 30.0,
        cleanup_timeout_seconds: float = 2.0,
        popen_factory: Callable[..., object] | None = None,
    ) -> None:
        if max_frame_bytes <= 0:
            raise ValueError("max_frame_bytes must be positive")
        if max_stderr_bytes <= 0:
            raise ValueError("max_stderr_bytes must be positive")
        if timeout_seconds <= 0 or cleanup_timeout_seconds <= 0:
            raise ValueError("subprocess timeouts must be positive")
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.max_frame_bytes = max_frame_bytes
        self.max_stderr_bytes = max_stderr_bytes
        self.timeout_seconds = timeout_seconds
        self.cleanup_timeout_seconds = cleanup_timeout_seconds
        self._popen = cast(Callable[..., _Process], popen_factory or subprocess.Popen)

    def _run_bounded(self, command: list[str], limit: int) -> bytes:
        popen_options: dict[str, object] = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
        }
        if os.name != "nt":  # pragma: no cover - exercised on POSIX
            inherited_descriptors: set[int] = set()
            for argument in command:
                parts = Path(argument).parts
                if (
                    parts[:4] == ("/", "proc", "self", "fd")
                    and len(parts) > 4
                    and parts[4].isdigit()
                ):
                    inherited_descriptors.add(int(parts[4]))
                elif parts[:3] == ("/", "dev", "fd") and len(parts) > 3 and parts[3].isdigit():
                    inherited_descriptors.add(int(parts[3]))
            if inherited_descriptors:
                popen_options["pass_fds"] = tuple(sorted(inherited_descriptors))
        process = self._popen(command, **popen_options)
        stdout = process.stdout
        stderr = process.stderr
        if stdout is None or stderr is None:
            try:
                process.kill()
                process.wait(timeout=self.cleanup_timeout_seconds)
            finally:
                if stdout is not None:
                    stdout.close()
                if stderr is not None:
                    stderr.close()
            raise PreviewError("media subprocess did not expose bounded stdout and stderr pipes")

        outputs = {"stdout": bytearray(), "stderr": bytearray()}
        overflow = threading.Event()
        reader_errors: list[tuple[str, Exception]] = []

        def drain(name: str, stream: BinaryIO, cap: int) -> None:
            try:
                while chunk := stream.read(_PIPE_CHUNK_SIZE):
                    remaining = cap - len(outputs[name])
                    if len(chunk) > remaining:
                        if remaining > 0:
                            outputs[name].extend(chunk[:remaining])
                        overflow.name = name  # type: ignore[attr-defined]
                        overflow.set()
                        return
                    outputs[name].extend(chunk)
            except (OSError, ValueError) as error:
                reader_errors.append((name, error))

        readers = [
            threading.Thread(
                target=drain,
                args=("stdout", stdout, limit),
                daemon=True,
                name="spt-media-stdout",
            ),
            threading.Thread(
                target=drain,
                args=("stderr", stderr, self.max_stderr_bytes),
                daemon=True,
                name="spt-media-stderr",
            ),
        ]
        for reader in readers:
            reader.start()

        deadline = time.monotonic() + self.timeout_seconds
        return_code: int | None = None
        failure: PreviewError | None = None
        killed = False
        try:
            while return_code is None:
                overflow_name = getattr(overflow, "name", None)
                if overflow_name is not None:
                    failure = PreviewError(
                        f"media subprocess {overflow_name} exceeds bounded limit "
                        f"{limit if overflow_name == 'stdout' else self.max_stderr_bytes}"
                    )
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    failure = PreviewError(
                        f"media subprocess timed out after {self.timeout_seconds:g} seconds"
                    )
                    break
                try:
                    return_code = process.wait(timeout=min(0.02, remaining))
                except subprocess.TimeoutExpired:
                    continue

            if failure is not None:
                process.kill()
                killed = True
                try:
                    return_code = process.wait(timeout=self.cleanup_timeout_seconds)
                except subprocess.TimeoutExpired as error:
                    raise PreviewError("media subprocess did not exit after kill") from error
        finally:
            if return_code is not None and failure is None:
                for reader in readers:
                    reader.join(self.cleanup_timeout_seconds)
            overflow_name = getattr(overflow, "name", None)
            if failure is None and overflow_name is not None:
                failure = PreviewError(
                    f"media subprocess {overflow_name} exceeds bounded limit "
                    f"{limit if overflow_name == 'stdout' else self.max_stderr_bytes}"
                )
                process.kill()
                killed = True
                with suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=self.cleanup_timeout_seconds)
            stdout.close()
            stderr.close()
            for reader in readers:
                reader.join(self.cleanup_timeout_seconds)
            if not killed and any(reader.is_alive() for reader in readers):
                process.kill()
                with suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=self.cleanup_timeout_seconds)

        if failure is not None:
            raise failure
        if reader_errors:
            name, error = reader_errors[0]
            raise PreviewError(f"media subprocess {name} reader failed: {error}")
        if return_code:
            message = bytes(outputs["stderr"]).decode("utf-8", errors="replace")
            raise PreviewError(
                f"media subprocess exited {return_code}: {message.strip() or 'no diagnostics'}"
            )
        return bytes(outputs["stdout"])

    def probe_duration(self, path: Path) -> float:
        payload = self._run_bounded(
            [
                self.ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                os.fspath(path),
            ],
            _DIAGNOSTIC_LIMIT,
        )
        try:
            data = json.loads(payload)
            duration = float(data["format"]["duration"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise PreviewError(f"ffprobe returned invalid duration metadata: {error}") from error
        if not math.isfinite(duration) or duration <= 0:
            raise PreviewError(f"video duration must be positive, got {duration!r}")
        return duration

    def read_frame(self, path: Path, timestamp: float) -> Image.Image:
        command = [
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{timestamp:.6f}",
            "-i",
            os.fspath(path),
            "-frames:v",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "-",
        ]
        payload = self._run_bounded(command, self.max_frame_bytes)
        try:
            with Image.open(io.BytesIO(payload)) as image:
                if image.width * image.height > MAX_DECODE_PIXELS:
                    raise PreviewError(
                        f"video frame pixel count exceeds decode limit {MAX_DECODE_PIXELS}"
                    )
                image.load()
                return image.convert("RGB")
        except PreviewError:
            raise
        except (OSError, ValueError) as error:
            raise PreviewError(f"FFmpeg returned an invalid frame: {error}") from error


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def preview_fingerprint(
    source_fingerprint: str,
    *,
    preview_version: str = PREVIEW_VERSION,
    max_edge: int = 1024,
    image_format: str = "WEBP",
) -> str:
    """Return a canonical cache key for a source signature and preview contract."""
    normalized_format = image_format.upper()
    if (
        not source_fingerprint
        or not preview_version
        or max_edge <= 0
        or normalized_format not in {"WEBP", "PNG", "JPEG"}
    ):
        raise ValueError("preview fingerprint inputs must be non-empty and positive")
    payload = json.dumps(
        {
            "algorithm": "spt-preview-fingerprint-v1",
            "image_format": normalized_format,
            "max_edge": max_edge,
            "preview_version": preview_version,
            "source_fingerprint": source_fingerprint,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def video_sample_timestamps(duration_seconds: float) -> tuple[float, ...]:
    """Choose 3, 6, or 9 evenly spaced samples inside the first and last 5 percent."""
    if not math.isfinite(duration_seconds) or duration_seconds <= 0:
        raise PreviewError("video duration must be a positive finite number")
    count = 3 if duration_seconds <= 10 else 6 if duration_seconds <= 60 else 9
    start = duration_seconds / (count + 1)
    end = duration_seconds * count / (count + 1)
    step = (end - start) / (count - 1)
    return tuple(start + step * index for index in range(count))


def _perceptual_hash(image: Image.Image) -> str:
    grayscale = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    pixels = list(grayscale.getdata())
    value = 0
    for row in range(8):
        for column in range(8):
            value = (value << 1) | int(pixels[row * 9 + column] > pixels[row * 9 + column + 1])
    return f"{value:016x}"


def measure_quality(image: Image.Image) -> QualityMetrics:
    """Compute bounded local advice. This result intentionally has no file-action field."""
    rgb = image.convert("RGB")
    grayscale = rgb.convert("L")
    mean = float(ImageStat.Stat(grayscale).mean[0])
    exposure = max(0.0, 1.0 - abs(mean - 127.5) / 127.5)
    histogram = grayscale.histogram()
    pixels = max(1, grayscale.width * grayscale.height)
    clipping = min(1.0, (sum(histogram[:6]) + sum(histogram[250:])) / pixels)
    edge_variance = float(ImageStat.Stat(grayscale.filter(ImageFilter.FIND_EDGES)).var[0])
    sharpness = min(1.0, edge_variance / 4096.0)
    resolution = rgb.width * rgb.height
    resolution_score = min(1.0, math.log2(max(1, resolution)) / 22.0)
    score = max(
        0.0,
        min(
            1.0,
            sharpness * 0.35 + exposure * 0.25 + (1.0 - clipping) * 0.20 + resolution_score * 0.20,
        ),
    )
    return QualityMetrics(
        sharpness=round(sharpness, 6),
        exposure=round(exposure, 6),
        clipping=round(clipping, 6),
        resolution=resolution,
        score=round(score, 6),
        advisory="BEST_SHOT_CANDIDATE" if score >= 0.55 else "REVIEW",
    )


def _save_preview(image: Image.Image, destination: Path, image_format: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.partial")
    try:
        image.save(temporary, format=image_format.upper())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


_DEFAULT_PREVIEW_CONFIG = PreviewConfig()


def _artifact(
    image: Image.Image,
    destination: Path,
    source_fingerprint: str,
    config: PreviewConfig,
) -> PreviewArtifact:
    fingerprint = preview_fingerprint(
        source_fingerprint,
        preview_version=config.version,
        max_edge=config.max_edge,
        image_format=config.image_format,
    )
    quality = measure_quality(image)
    perceptual_hash = _perceptual_hash(image)
    _save_preview(image, destination, config.image_format)
    return PreviewArtifact(
        fingerprint=fingerprint,
        path=destination,
        width=image.width,
        height=image.height,
        perceptual_hash=perceptual_hash,
        quality=quality,
    )


def generate_image_preview(
    source: Path,
    destination: Path,
    *,
    source_fingerprint: str,
    config: PreviewConfig = _DEFAULT_PREVIEW_CONFIG,
    image_opener: Callable[[Path], Image.Image] = Image.open,
) -> PreviewArtifact:
    """Decode, orient, bound, and atomically publish one image preview."""
    with image_opener(source) as opened:
        if opened.width * opened.height > MAX_DECODE_PIXELS:
            raise PreviewError(f"image pixel count exceeds decode limit {MAX_DECODE_PIXELS}")
        opened.load()
        oriented = ImageOps.exif_transpose(opened).convert("RGB")
    oriented.thumbnail((config.max_edge, config.max_edge), Image.Resampling.LANCZOS)
    return _artifact(oriented, destination, source_fingerprint, config)


def generate_video_preview(
    source: Path,
    destination: Path,
    *,
    source_fingerprint: str,
    config: PreviewConfig = _DEFAULT_PREVIEW_CONFIG,
    backend: VideoBackend | None = None,
) -> PreviewArtifact:
    """Build a bounded contact sheet from individually requested frames."""
    selected_backend = backend or FFmpegVideoBackend()
    timestamps = video_sample_timestamps(selected_backend.probe_duration(source))
    frames: list[Image.Image] = []
    cell_edge = max(32, min(320, config.max_edge // 3))
    for timestamp in timestamps:
        decoded = selected_backend.read_frame(source, timestamp)
        if decoded.width * decoded.height > MAX_DECODE_PIXELS:
            raise PreviewError(f"video frame pixel count exceeds decode limit {MAX_DECODE_PIXELS}")
        frame = decoded.convert("RGB")
        frame.thumbnail((cell_edge, cell_edge), Image.Resampling.LANCZOS)
        frames.append(frame)
    if not frames:
        raise PreviewError("video produced no preview frames")
    cell_width = max(frame.width for frame in frames)
    cell_height = max(frame.height for frame in frames)
    columns = min(3, len(frames))
    rows = math.ceil(len(frames) / columns)
    sheet = Image.new("RGB", (cell_width * columns, cell_height * rows), "black")
    for index, frame in enumerate(frames):
        x = (index % columns) * cell_width + (cell_width - frame.width) // 2
        y = (index // columns) * cell_height + (cell_height - frame.height) // 2
        sheet.paste(frame, (x, y))
    sheet.thumbnail((config.max_edge, config.max_edge), Image.Resampling.LANCZOS)
    return _artifact(sheet, destination, source_fingerprint, config)


@dataclass(frozen=True, slots=True)
class _SourceIdentity:
    size_bytes: int
    mtime_ns: int
    content_sha256: str
    device: int
    inode: int


@dataclass(frozen=True, slots=True)
class _SecureDirectoryBinding:
    logical_path: Path
    access_path: Path
    directory_fd: int | None = None


@dataclass(frozen=True, slots=True)
class _ArtifactVerification:
    sha256: str
    identity: tuple[int, int, int, int]


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
    )


def _secure_write_new(binding: _SecureDirectoryBinding, name: str, payload: bytes) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = (
        os.open(name, flags, 0o600, dir_fd=binding.directory_fd)
        if binding.directory_fd is not None
        else os.open(binding.access_path / name, flags, 0o600)
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)


def _binding_entry_stat(binding: _SecureDirectoryBinding, name: str) -> os.stat_result:
    return (
        os.stat(name, dir_fd=binding.directory_fd, follow_symlinks=False)
        if binding.directory_fd is not None
        else (binding.access_path / name).lstat()
    )


def _quarantine_lock_entry(
    binding: _SecureDirectoryBinding, name: str, expected: os.stat_result
) -> bool:
    quarantine = f".lock-delete-{uuid4().hex}"
    try:
        if binding.directory_fd is not None:  # pragma: no cover - POSIX only
            os.rename(
                name,
                quarantine,
                src_dir_fd=binding.directory_fd,
                dst_dir_fd=binding.directory_fd,
            )
            bound = os.stat(quarantine, dir_fd=binding.directory_fd, follow_symlinks=False)
        else:
            os.replace(binding.access_path / name, binding.access_path / quarantine)
            bound = (binding.access_path / quarantine).lstat()
    except FileNotFoundError:
        return False
    if (bound.st_dev, bound.st_ino) != (expected.st_dev, expected.st_ino):
        raise PreviewBusyError("publication lock identity changed during reclamation")
    if binding.directory_fd is not None:  # pragma: no cover - POSIX only
        os.unlink(quarantine, dir_fd=binding.directory_fd)
    else:
        os.unlink(binding.access_path / quarantine)
    return True


def _read_publication_lock(
    workspace_root: Path, binding: _SecureDirectoryBinding, name: str
) -> tuple[dict[str, object], os.stat_result]:
    path = binding.logical_path / name
    with _open_source_nofollow(path, workspace_root, share_delete=False) as stream:
        opened = os.fstat(stream.fileno())
        payload = stream.read(4097)
        if len(payload) > 4096:
            raise PreviewBusyError("publication lock owner metadata is oversized")
    try:
        owner = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PreviewBusyError("publication lock owner metadata is unreadable") from error
    if not isinstance(owner, dict):
        raise PreviewBusyError("publication lock owner metadata is invalid")
    return owner, opened


@contextmanager
def _publication_lock(
    workspace_root: Path,
    fingerprint: str,
    owner_token: str,
    wait_timeout: float | None,
):
    name = f"{fingerprint}.lock"
    deadline = None if wait_timeout is None else time.monotonic() + wait_timeout
    with _secure_workspace_directory(workspace_root, ("state", "publication-locks")) as binding:
        acquired: os.stat_result | None = None
        metadata_grace_identity: tuple[int, int] | None = None
        metadata_grace_deadline: float | None = None
        while acquired is None:
            payload = json.dumps(
                {"pid": os.getpid(), "token": owner_token},
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            try:
                _secure_write_new(binding, name, payload)
                acquired = _binding_entry_stat(binding, name)
                break
            except FileExistsError:
                try:
                    owner, existing = _read_publication_lock(workspace_root, binding, name)
                except FileNotFoundError:
                    continue
                except (OSError, PreviewBusyError) as error:
                    try:
                        incomplete = _binding_entry_stat(binding, name)
                    except FileNotFoundError:
                        continue
                    now = time.monotonic()
                    incomplete_identity = (incomplete.st_dev, incomplete.st_ino)
                    if incomplete.st_size == 0:
                        if metadata_grace_identity != incomplete_identity:
                            metadata_grace_identity = incomplete_identity
                            metadata_grace_deadline = now + _PUBLICATION_LOCK_METADATA_GRACE_SECONDS
                        grace_deadline = cast(float, metadata_grace_deadline)
                        effective_deadline = (
                            grace_deadline if deadline is None else min(grace_deadline, deadline)
                        )
                        if now < effective_deadline:
                            time.sleep(
                                min(
                                    _PUBLICATION_LOCK_POLL_SECONDS,
                                    effective_deadline - now,
                                )
                            )
                            continue
                    raise PreviewBusyError(
                        "publication lock owner metadata is unknown or malformed"
                    ) from error
                pid_value = owner.get("pid")
                token_value = owner.get("token")
                if (
                    isinstance(pid_value, bool)
                    or not isinstance(pid_value, int)
                    or pid_value <= 0
                    or not isinstance(token_value, str)
                    or not token_value
                ):
                    raise PreviewBusyError(
                        "publication lock owner metadata is unknown or malformed"
                    ) from None
                if not _process_is_alive(pid_value, token_value):
                    _quarantine_lock_entry(binding, name, existing)
                    continue
                if deadline is not None and time.monotonic() >= deadline:
                    raise PreviewBusyError(
                        f"publication fingerprint remains busy: {fingerprint}"
                    ) from None
                time.sleep(_PUBLICATION_LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            if acquired is not None:
                with suppress(OSError, PreviewBusyError):
                    _quarantine_lock_entry(binding, name, acquired)


def _default_publication_lock_wait_timeout(video_backend: VideoBackend) -> float:
    media_timeout = float(getattr(video_backend, "timeout_seconds", 30.0))
    cleanup_margin = float(getattr(video_backend, "cleanup_timeout_seconds", 2.0))
    return (1 + 9) * media_timeout + cleanup_margin + 5.0


def _assert_no_link_components(path: Path, source_root: Path) -> None:
    try:
        relative = path.relative_to(source_root)
    except ValueError as error:
        raise PreviewError("source path is outside its scanned source root") from error
    current = source_root
    root_stat = current.lstat()
    root_attributes = getattr(root_stat, "st_file_attributes", 0)
    if stat.S_ISLNK(root_stat.st_mode) or root_attributes & _WINDOWS_REPARSE_ATTRIBUTE:
        raise PreviewError(f"source root is a symlink or reparse point: {current}")
    for component in relative.parts:
        current /= component
        current_stat = current.lstat()
        attributes = getattr(current_stat, "st_file_attributes", 0)
        if stat.S_ISLNK(current_stat.st_mode) or attributes & _WINDOWS_REPARSE_ATTRIBUTE:
            raise PreviewError(f"source path contains a symlink or reparse point: {current}")


@contextmanager
def _open_source_nofollow(  # type: ignore[no-untyped-def]
    path: Path, source_root: Path, *, share_delete: bool = True
):
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if os.name != "nt":  # pragma: no cover - exercised on POSIX
        try:
            relative = path.relative_to(source_root)
        except ValueError as error:
            raise PreviewError("source path is outside its scanned source root") from error
        directory_flags = flags | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        directory_descriptor = os.open(source_root, directory_flags)
        try:
            for component in relative.parts[:-1]:
                next_descriptor = os.open(
                    component,
                    directory_flags,
                    dir_fd=directory_descriptor,
                )
                os.close(directory_descriptor)
                directory_descriptor = next_descriptor
            descriptor = os.open(
                relative.parts[-1],
                flags | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_descriptor,
            )
        finally:
            os.close(directory_descriptor)
    else:
        _assert_no_link_components(path, source_root)
        import ctypes
        import msvcrt
        from ctypes import wintypes

        create_file = ctypes.windll.kernel32.CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        handle = create_file(
            os.fspath(path),
            0x80000000,
            0x00000001 | 0x00000002 | (0x00000004 if share_delete else 0),
            None,
            3,
            0x00200000 | 0x08000000,
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if handle == invalid_handle:
            raise OSError(
                ctypes.get_last_error(), f"cannot open source without following links: {path}"
            )
        try:
            final_path_buffer = ctypes.create_unicode_buffer(32768)
            final_length = ctypes.windll.kernel32.GetFinalPathNameByHandleW(
                handle, final_path_buffer, len(final_path_buffer), 0
            )
            if not final_length or final_length >= len(final_path_buffer):
                raise OSError("cannot resolve the safely opened source handle")
            final_path = final_path_buffer.value
            if final_path.startswith("\\\\?\\UNC\\"):
                final_path = "\\\\" + final_path[8:]
            elif final_path.startswith("\\\\?\\"):
                final_path = final_path[4:]
            root_path = os.path.abspath(source_root)
            if os.path.commonpath((root_path, final_path)).casefold() != root_path.casefold():
                raise PreviewError("source handle resolves outside its scanned source root")
            descriptor = msvcrt.open_osfhandle(handle, flags)
        except Exception:
            ctypes.windll.kernel32.CloseHandle(handle)
            raise
    with os.fdopen(descriptor, "rb") as stream:
        opened_stat = os.fstat(stream.fileno())
        attributes = getattr(opened_stat, "st_file_attributes", 0)
        if attributes & _WINDOWS_REPARSE_ATTRIBUTE:
            raise PreviewError("source handle is a symlink or reparse point")
        yield stream


def _process_is_alive(pid: int, _token: str | None) -> bool:
    if pid <= 0 or pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        open_process.restype = wintypes.HANDLE
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        get_exit_code.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        handle = open_process(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() != 87
        try:
            exit_code = wintypes.DWORD()
            if not get_exit_code(handle, ctypes.byref(exit_code)):
                return True
            return exit_code.value == 259
        finally:
            close_handle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def _cleanup_stale_preprocess_runs(runs_binding: _SecureDirectoryBinding) -> None:
    """Remove only proven-dead run directories through their held root binding."""
    if runs_binding.directory_fd is not None:  # pragma: no cover - exercised on POSIX CI
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        for name in os.listdir(runs_binding.directory_fd):
            try:
                directory_stat = os.stat(
                    name,
                    dir_fd=runs_binding.directory_fd,
                    follow_symlinks=False,
                )
            except OSError:
                continue
            if not stat.S_ISDIR(directory_stat.st_mode) or stat.S_ISLNK(directory_stat.st_mode):
                continue
            try:
                directory_fd = os.open(name, flags, dir_fd=runs_binding.directory_fd)
                try:
                    owner_fd = os.open(
                        "owner.json",
                        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=directory_fd,
                    )
                    with os.fdopen(owner_fd, "r", encoding="utf-8") as stream:
                        owner = json.load(stream)
                finally:
                    os.close(directory_fd)
                pid = int(owner["pid"])
                token = str(owner["token"])
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                continue
            if not _process_is_alive(pid, token):
                _remove_posix_entry(runs_binding.directory_fd, name)
        return

    runs_root = runs_binding.access_path
    if not runs_root.is_dir():
        return
    for directory in runs_root.iterdir():
        try:
            directory_stat = directory.lstat()
        except OSError:
            continue
        if (
            not stat.S_ISDIR(directory_stat.st_mode)
            or stat.S_ISLNK(directory_stat.st_mode)
            or getattr(directory_stat, "st_file_attributes", 0) & _WINDOWS_REPARSE_ATTRIBUTE
        ):
            continue
        try:
            owner = json.loads((directory / "owner.json").read_text(encoding="utf-8"))
            pid = int(owner["pid"])
            token = str(owner["token"])
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue
        if not _process_is_alive(pid, token):
            _nofollow_remove_tree(directory)


def _remove_posix_entry(  # pragma: no cover - exercised on POSIX
    parent_fd: int,
    name: str,
    expected_identity: tuple[int, int, int, int] | None = None,
) -> None:
    entry_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    actual_identity = _stat_identity(entry_stat)
    if expected_identity is not None and actual_identity != expected_identity:
        raise PreviewBusyError("artifact changed before secure cleanup")
    if stat.S_ISDIR(entry_stat.st_mode):
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(name, flags, dir_fd=parent_fd)
        try:
            opened_stat = os.fstat(descriptor)
            if (opened_stat.st_dev, opened_stat.st_ino) != (entry_stat.st_dev, entry_stat.st_ino):
                raise PreviewError("directory identity changed before secure deletion")
        finally:
            os.close(descriptor)
    quarantine = f".delete-{uuid4().hex}"
    os.rename(name, quarantine, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    bound_stat = os.stat(quarantine, dir_fd=parent_fd, follow_symlinks=False)
    if (bound_stat.st_dev, bound_stat.st_ino) != (entry_stat.st_dev, entry_stat.st_ino):
        raise PreviewError("entry identity changed during secure deletion")
    if not stat.S_ISDIR(bound_stat.st_mode) or stat.S_ISLNK(bound_stat.st_mode):
        os.unlink(quarantine, dir_fd=parent_fd)
        return
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(quarantine, flags, dir_fd=parent_fd)
    try:
        opened_stat = os.fstat(descriptor)
        if (opened_stat.st_dev, opened_stat.st_ino) != (
            bound_stat.st_dev,
            bound_stat.st_ino,
        ):
            raise PreviewError("directory identity changed during secure deletion")
        for child in os.listdir(descriptor):
            _remove_posix_entry(descriptor, child)
        final_stat = os.fstat(descriptor)
        if (final_stat.st_dev, final_stat.st_ino) != (
            bound_stat.st_dev,
            bound_stat.st_ino,
        ):
            raise PreviewError("directory identity changed during secure deletion")
        os.rmdir(quarantine, dir_fd=parent_fd)
    finally:
        os.close(descriptor)


def _nofollow_remove_tree(
    path: Path,
    expected_identity: tuple[int, int, int, int] | None = None,
) -> None:
    if os.name != "nt":  # pragma: no cover - exercised on POSIX
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        parent_fd = os.open(path.parent, flags)
        try:
            _remove_posix_entry(parent_fd, path.name, expected_identity)
        finally:
            os.close(parent_fd)
        return
    path_stat = path.lstat()
    if expected_identity is not None and _stat_identity(path_stat) != expected_identity:
        raise PreviewBusyError("artifact changed before secure cleanup")
    quarantine = path.with_name(f".delete-{uuid4().hex}")
    os.replace(path, quarantine)
    bound_stat = quarantine.lstat()
    if (bound_stat.st_dev, bound_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino):
        raise PreviewError("entry identity changed during secure deletion")
    attributes = getattr(bound_stat, "st_file_attributes", 0)
    if stat.S_ISLNK(bound_stat.st_mode) or attributes & _WINDOWS_REPARSE_ATTRIBUTE:
        if stat.S_ISDIR(bound_stat.st_mode):
            os.rmdir(quarantine)
        else:
            os.unlink(quarantine)
        return
    if stat.S_ISDIR(bound_stat.st_mode):
        for child in quarantine.iterdir():
            _nofollow_remove_tree(child)
        os.rmdir(quarantine)
    else:
        os.unlink(quarantine)


def _secure_remove_run(workspace_root: Path, run_id: str) -> None:
    relative = ("state", "preprocess-runs", run_id)
    with _secure_workspace_directory(workspace_root, relative) as run_binding:
        if run_binding.directory_fd is not None:  # pragma: no cover - POSIX only
            for child in os.listdir(run_binding.directory_fd):
                _remove_posix_entry(run_binding.directory_fd, child)
        else:
            for child in run_binding.access_path.iterdir():
                _nofollow_remove_tree(child)
    with _secure_workspace_directory(workspace_root, ("state", "preprocess-runs")) as runs_binding:
        if runs_binding.directory_fd is not None:  # pragma: no cover - POSIX only
            _remove_posix_entry(runs_binding.directory_fd, run_id)
        else:
            os.rmdir(runs_binding.access_path / run_id)


@contextmanager
def _owned_run_security(workspace_root: Path, run_id: str, owner_token: str):
    try:
        with ExitStack() as stack:
            runs_binding = stack.enter_context(
                _secure_workspace_directory(workspace_root, ("state", "preprocess-runs"))
            )
            _cleanup_stale_preprocess_runs(runs_binding)
            run_binding = stack.enter_context(
                _secure_workspace_directory(workspace_root, ("state", "preprocess-runs", run_id))
            )
            _secure_write_new(
                run_binding,
                "owner.json",
                json.dumps(
                    {"pid": os.getpid(), "token": owner_token}, separators=(",", ":")
                ).encode("utf-8"),
            )
            yield run_binding
    finally:
        with suppress(OSError):
            _secure_remove_run(workspace_root, run_id)


@contextmanager
def _item_security(workspace_root: Path, run_id: str, media_id: int):
    with ExitStack() as stack:
        snapshot_binding = stack.enter_context(
            _secure_workspace_directory(
                workspace_root,
                (
                    "state",
                    "preprocess-runs",
                    run_id,
                    "snapshots",
                    str(media_id),
                ),
            )
        )
        candidate_binding = stack.enter_context(
            _secure_workspace_directory(
                workspace_root,
                ("state", "preprocess-runs", run_id, "candidates"),
            )
        )
        yield snapshot_binding, candidate_binding


@contextmanager
def _secure_workspace_directory(
    workspace_root: Path, relative_parts: tuple[str, ...], *, create: bool = True
):
    """Create and pin a workspace-relative directory tree without following links."""
    logical_path = workspace_root.joinpath(*relative_parts)
    if os.name != "nt":  # pragma: no cover - exercised on POSIX
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        descriptors: list[int] = [os.open(workspace_root, flags)]
        try:
            for component in relative_parts:
                try:
                    descriptor = os.open(component, flags, dir_fd=descriptors[-1])
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(component, mode=0o700, dir_fd=descriptors[-1])
                    descriptor = os.open(component, flags, dir_fd=descriptors[-1])
                descriptors.append(descriptor)
            fd_root = Path("/proc/self/fd")
            if not fd_root.is_dir():
                fd_root = Path("/dev/fd")
            yield _SecureDirectoryBinding(
                logical_path,
                fd_root / str(descriptors[-1]),
                descriptors[-1],
            )
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
        return

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    handles: list[object] = []
    try:
        root_path = os.path.abspath(workspace_root)
        current = workspace_root
        for component in (None, *relative_parts):
            if component is not None:
                current /= component
                if not current.exists():
                    if not create:
                        raise FileNotFoundError(current)
                    current.mkdir()
            current_stat = current.lstat()
            if getattr(current_stat, "st_file_attributes", 0) & _WINDOWS_REPARSE_ATTRIBUTE:
                raise PreviewError(f"workspace tree contains a reparse point: {current}")
            handle = create_file(
                os.fspath(current),
                0x80000000,
                0x00000001 | 0x00000002,
                None,
                3,
                0x00200000 | 0x02000000,
                None,
            )
            if handle == ctypes.c_void_p(-1).value:
                raise OSError(ctypes.get_last_error(), f"cannot pin workspace directory: {current}")
            handles.append(handle)
            buffer = ctypes.create_unicode_buffer(32768)
            length = kernel32.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
            if not length or length >= len(buffer):
                raise PreviewError("cannot verify workspace directory handle")
            final_path = buffer.value
            if final_path.startswith("\\\\?\\"):
                final_path = final_path[4:]
            if os.path.commonpath((root_path, final_path)).casefold() != root_path.casefold():
                raise PreviewError("workspace directory resolves outside workspace")
        yield _SecureDirectoryBinding(logical_path, logical_path)
    finally:
        for handle in reversed(handles):
            close_handle(handle)


def _secure_publish(
    candidate: Path,
    destination: Path,
    workspace_root: Path,
    *,
    candidate_binding: _SecureDirectoryBinding | None = None,
) -> None:
    relative = destination.relative_to(workspace_root)
    with _secure_workspace_directory(workspace_root, relative.parts[:-1]) as destination_binding:
        if os.name == "nt":
            if _SECURE_PUBLISH_TEST_HOOK is not None:
                _SECURE_PUBLISH_TEST_HOOK(destination.parent)
            os.replace(candidate, destination)
            return

        directory_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        candidate_descriptor = (
            candidate_binding.directory_fd
            if candidate_binding is not None
            else os.open(candidate.parent, directory_flags)
        )
        owns_candidate_descriptor = candidate_binding is None
        try:
            if _SECURE_PUBLISH_TEST_HOOK is not None:
                _SECURE_PUBLISH_TEST_HOOK(destination.parent)
            os.replace(
                candidate.name,
                destination.name,
                src_dir_fd=candidate_descriptor,
                dst_dir_fd=destination_binding.directory_fd,
            )
        finally:
            if owns_candidate_descriptor:
                os.close(candidate_descriptor)


def _validate_cached_preview(
    cached: tuple[object, ...], destination: Path, workspace_root: Path
) -> bool:
    if not cached or str(cached[0]) != str(destination):
        return False
    try:
        relative = destination.relative_to(workspace_root)
        with (
            _secure_workspace_directory(workspace_root, relative.parts[:-1], create=False),
            _open_source_nofollow(destination, workspace_root, share_delete=False) as stream,
        ):
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return False
            digest = hashlib.sha256()
            for chunk in iter(lambda: stream.read(_HASH_CHUNK_SIZE), b""):
                digest.update(chunk)
            if not cached[3] or digest.hexdigest() != str(cached[3]):
                return False
            stream.seek(0)
            with Image.open(stream) as image:
                if (image.width, image.height) != (int(cached[1]), int(cached[2])):
                    return False
                image.load()
                return True
    except (OSError, ValueError, TypeError, PreviewError):
        return False


def _published_artifact_sha256(
    destination: Path,
    workspace_root: Path,
    *,
    expected_width: int,
    expected_height: int,
) -> _ArtifactVerification:
    relative = destination.relative_to(workspace_root)
    with (
        _secure_workspace_directory(workspace_root, relative.parts[:-1], create=False),
        _open_source_nofollow(destination, workspace_root, share_delete=False) as stream,
    ):
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise PreviewError("published preview is not a regular file")
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(_HASH_CHUNK_SIZE), b""):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
        identity_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
        if any(getattr(before, field) != getattr(after, field) for field in identity_fields):
            raise PreviewError("published preview identity changed during verification")
        stream.seek(0)
        with Image.open(stream) as image:
            if (image.width, image.height) != (expected_width, expected_height):
                raise PreviewError("published preview dimensions do not match generated artifact")
            image.load()
        final = os.fstat(stream.fileno())
        if any(getattr(before, field) != getattr(final, field) for field in identity_fields):
            raise PreviewError("published preview identity changed during verification")
        return _ArtifactVerification(digest.hexdigest(), _stat_identity(final))


def _secure_remove_artifact(
    path: Path,
    workspace_root: Path,
    expected: _ArtifactVerification,
    *,
    expected_width: int,
    expected_height: int,
) -> None:
    current = _published_artifact_sha256(
        path,
        workspace_root,
        expected_width=expected_width,
        expected_height=expected_height,
    )
    if current != expected:
        raise PreviewBusyError("published artifact changed before cleanup")
    relative = path.relative_to(workspace_root)
    with _secure_workspace_directory(workspace_root, relative.parts[:-1], create=False) as binding:
        if binding.directory_fd is not None:  # pragma: no cover - POSIX only
            _remove_posix_entry(binding.directory_fd, path.name, expected.identity)
        else:
            _nofollow_remove_tree(path, expected.identity)


def _identity_from_open_stream(stream: BinaryIO) -> _SourceIdentity:
    before = os.fstat(stream.fileno())
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(_HASH_CHUNK_SIZE), b""):
        digest.update(chunk)
    after = os.fstat(stream.fileno())
    stable_fields = ("st_size", "st_mtime_ns", "st_dev", "st_ino")
    if tuple(getattr(before, field) for field in stable_fields) != tuple(
        getattr(after, field) for field in stable_fields
    ):
        raise PreviewError("source changed while its content identity was being read")
    return _SourceIdentity(
        after.st_size,
        after.st_mtime_ns,
        digest.hexdigest(),
        after.st_dev,
        after.st_ino,
    )


def _stable_source_identity(path: Path, source_root: Path) -> _SourceIdentity:
    with _open_source_nofollow(path, source_root) as stream:
        return _identity_from_open_stream(stream)


def _snapshot_source(path: Path, source_root: Path, snapshot: Path) -> _SourceIdentity:
    with _open_source_nofollow(path, source_root) as source_stream, snapshot.open("xb") as target:
        before = os.fstat(source_stream.fileno())
        digest = hashlib.sha256()
        for chunk in iter(lambda: source_stream.read(_HASH_CHUNK_SIZE), b""):
            digest.update(chunk)
            target.write(chunk)
        after = os.fstat(source_stream.fileno())
    stable_fields = ("st_size", "st_mtime_ns", "st_dev", "st_ino")
    if tuple(getattr(before, field) for field in stable_fields) != tuple(
        getattr(after, field) for field in stable_fields
    ):
        raise PreviewError("source changed while its safe snapshot was being created")
    return _SourceIdentity(
        after.st_size,
        after.st_mtime_ns,
        digest.hexdigest(),
        after.st_dev,
        after.st_ino,
    )


def _source_fingerprint(media_type: str, content_sha256: str) -> str:
    payload = json.dumps(
        {
            "content_sha256": content_sha256,
            "media_type": media_type,
            "version": _SOURCE_FINGERPRINT_VERSION,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _config_fingerprint(config: PreviewConfig) -> str:
    payload = json.dumps(asdict(config), separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(_HASH_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _preview_extension(image_format: str) -> str:
    return {"WEBP": ".webp", "PNG": ".png", "JPEG": ".jpg"}[image_format.upper()]


def _upsert_ready(
    connection: sqlite3.Connection,
    media_id: int,
    source_signature: str,
    artifact: PreviewArtifact,
    config: PreviewConfig,
    identity: _SourceIdentity,
    preview_sha256: str,
) -> None:
    connection.execute(
        """
        INSERT INTO media_preprocess(
            media_id, source_fingerprint, preview_fingerprint, preview_path,
            preview_version, preview_status, preview_error, perceptual_hash,
            perceptual_hash_version, quality_json, quality_score, preview_width,
            preview_height, updated_at, preview_sha256
        ) VALUES (?, ?, ?, ?, ?, 'READY', NULL, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(media_id) DO UPDATE SET
            source_fingerprint = excluded.source_fingerprint,
            preview_fingerprint = excluded.preview_fingerprint,
            preview_path = excluded.preview_path,
            preview_version = excluded.preview_version,
            preview_status = excluded.preview_status,
            preview_error = NULL,
            perceptual_hash = excluded.perceptual_hash,
            perceptual_hash_version = excluded.perceptual_hash_version,
            quality_json = excluded.quality_json,
            quality_score = excluded.quality_score,
            preview_width = excluded.preview_width,
            preview_height = excluded.preview_height,
            preview_sha256 = excluded.preview_sha256,
            updated_at = excluded.updated_at
        """,
        (
            media_id,
            source_signature,
            artifact.fingerprint,
            str(artifact.path),
            config.version,
            artifact.perceptual_hash,
            PERCEPTUAL_HASH_VERSION,
            json.dumps(asdict(artifact.quality), separators=(",", ":"), sort_keys=True),
            artifact.quality.score,
            artifact.width,
            artifact.height,
            _utc_now(),
            preview_sha256,
        ),
    )
    connection.execute(
        """
        UPDATE media_item
        SET preview_path = ?, preview_version = ?, content_sha256 = ?
        WHERE id = ?
        """,
        (
            str(artifact.path),
            config.version,
            identity.content_sha256,
            media_id,
        ),
    )


def _upsert_failed(
    connection: sqlite3.Connection,
    media_id: int,
    source_signature: str,
    fingerprint: str,
    config: PreviewConfig,
    error: Exception,
) -> None:
    message = f"{type(error).__name__}: {error}"
    connection.execute(
        """
        INSERT INTO media_preprocess(
            media_id, source_fingerprint, preview_fingerprint, preview_path,
            preview_version, preview_status, preview_error, updated_at
        ) VALUES (?, ?, ?, NULL, ?, 'FAILED', ?, ?)
        ON CONFLICT(media_id) DO UPDATE SET
            source_fingerprint = excluded.source_fingerprint,
            preview_fingerprint = excluded.preview_fingerprint,
            preview_path = NULL,
            preview_version = excluded.preview_version,
            preview_status = 'FAILED',
            preview_error = excluded.preview_error,
            perceptual_hash = NULL,
            perceptual_hash_version = NULL,
            quality_json = NULL,
            quality_score = NULL,
            preview_width = NULL,
            preview_height = NULL,
            preview_sha256 = '',
            updated_at = excluded.updated_at
        """,
        (media_id, source_signature, fingerprint, config.version, message, _utc_now()),
    )
    connection.execute(
        "UPDATE media_item SET preview_path = NULL, preview_version = ? WHERE id = ?",
        (config.version, media_id),
    )


def preprocess_workspace(
    workspace: Workspace,
    *,
    config: PreviewConfig = _DEFAULT_PREVIEW_CONFIG,
    image_opener: Callable[[Path], Image.Image] = Image.open,
    video_backend: VideoBackend | None = None,
    lock_wait_timeout: float | None = None,
) -> PreprocessResult:
    """Preprocess all present images and videos with item-level failure isolation."""
    run_id = uuid4().hex
    owner_token = uuid4().hex
    processed_count = 0
    cache_hit_count = 0
    failed_count = 0
    deferred_count = 0
    selected_video_backend = video_backend or FFmpegVideoBackend()
    publication_lock_wait = lock_wait_timeout
    if publication_lock_wait is not None and (
        not math.isfinite(publication_lock_wait) or publication_lock_wait <= 0
    ):
        raise ValueError("publication lock wait timeout must be positive and finite")
    extension = _preview_extension(config.image_format)
    with (
        _owned_run_security(workspace.root, run_id, owner_token),
        closing(connect_database(workspace.database_path)) as connection,
    ):
        connection.execute(
            """
            INSERT INTO preprocess_run(
                id, config_fingerprint, preview_version, started_at, status
            ) VALUES (?, ?, ?, ?, 'RUNNING')
            """,
            (run_id, _config_fingerprint(config), config.version, _utc_now()),
        )
        connection.commit()
        try:
            cursor = connection.execute(
                """
                SELECT id, original_path, source_root, path_key, media_type, size_bytes, mtime_ns,
                       content_sha256
                FROM media_item
                WHERE source_present = 1 AND media_type IN ('IMAGE', 'VIDEO')
                ORDER BY id
                """
            )
            while rows := cursor.fetchmany(_PREPROCESS_BATCH_SIZE):
                for row in rows:
                    media_id = int(row[0])
                    source = Path(str(row[1]))
                    source_root = Path(str(row[2]))
                    media_type = str(row[4])
                    source_signature = hashlib.sha256(
                        f"unreadable\0{media_type}\0{source}".encode()
                    ).hexdigest()
                    fingerprint = preview_fingerprint(source_signature)
                    candidate: Path | None = None
                    published: _ArtifactVerification | None = None
                    snapshot: Path | None = None
                    item_context = None
                    lock_context = None
                    try:
                        identity_probe = _stable_source_identity(source, source_root)
                        source_signature = _source_fingerprint(
                            media_type, identity_probe.content_sha256
                        )
                        fingerprint = preview_fingerprint(
                            source_signature,
                            preview_version=config.version,
                            max_edge=config.max_edge,
                            image_format=config.image_format,
                        )
                        destination = (
                            workspace.root
                            / "previews"
                            / fingerprint[:2]
                            / (fingerprint + extension)
                        )
                        lock_context = _publication_lock(
                            workspace.root,
                            fingerprint,
                            owner_token,
                            publication_lock_wait,
                        )
                        lock_context.__enter__()
                        item_context = _item_security(workspace.root, run_id, media_id)
                        snapshot_binding, candidate_binding = item_context.__enter__()
                        snapshot = snapshot_binding.access_path / source.name
                        identity_before = _snapshot_source(source, source_root, snapshot)
                        if identity_before != identity_probe:
                            raise PreviewError("source changed before locked preview generation")
                        cached = connection.execute(
                            """
                            SELECT preview_path, preview_width, preview_height,
                                   preview_sha256
                            FROM media_preprocess
                            WHERE media_id = ? AND source_fingerprint = ?
                              AND preview_fingerprint = ? AND preview_version = ?
                              AND preview_status = 'READY'
                            """,
                            (media_id, source_signature, fingerprint, config.version),
                        ).fetchone()
                        if cached is not None and _validate_cached_preview(
                            tuple(cached), destination, workspace.root
                        ):
                            if _stable_source_identity(source, source_root) != identity_before:
                                raise PreviewError("source changed during preview cache validation")
                            connection.execute(
                                """
                                UPDATE media_item
                                SET content_sha256 = ?
                                WHERE id = ?
                                """,
                                (
                                    identity_before.content_sha256,
                                    media_id,
                                ),
                            )
                            cache_hit_count += 1
                            continue
                        shared = connection.execute(
                            """
                            SELECT preview_path, preview_width, preview_height,
                                   preview_sha256, perceptual_hash, quality_json
                            FROM media_preprocess
                            WHERE preview_fingerprint = ? AND preview_version = ?
                              AND preview_status = 'READY'
                            ORDER BY media_id
                            LIMIT 1
                            """,
                            (fingerprint, config.version),
                        ).fetchone()
                        if shared is not None and _validate_cached_preview(
                            tuple(shared[:4]), destination, workspace.root
                        ):
                            if _stable_source_identity(source, source_root) != identity_before:
                                raise PreviewError("source changed during shared preview reuse")
                            quality_payload = json.loads(str(shared[5]))
                            quality = QualityMetrics(**quality_payload)
                            artifact = PreviewArtifact(
                                fingerprint,
                                destination,
                                int(shared[1]),
                                int(shared[2]),
                                str(shared[4]),
                                quality,
                            )
                            _upsert_ready(
                                connection,
                                media_id,
                                source_signature,
                                artifact,
                                config,
                                identity_before,
                                str(shared[3]),
                            )
                            cache_hit_count += 1
                            continue
                        candidate = (
                            candidate_binding.access_path / f"candidate-{media_id}{extension}"
                        )
                        if media_type == "IMAGE":
                            artifact = generate_image_preview(
                                snapshot,
                                candidate,
                                source_fingerprint=source_signature,
                                config=config,
                                image_opener=image_opener,
                            )
                        else:
                            artifact = generate_video_preview(
                                snapshot,
                                candidate,
                                source_fingerprint=source_signature,
                                config=config,
                                backend=selected_video_backend,
                            )
                        candidate_sha256 = _file_sha256(candidate)
                        identity_after = _stable_source_identity(source, source_root)
                        if identity_after != identity_before:
                            raise PreviewError("source changed during preview generation")
                        _secure_publish(
                            candidate,
                            destination,
                            workspace.root,
                            candidate_binding=candidate_binding,
                        )
                        try:
                            published = _published_artifact_sha256(
                                destination,
                                workspace.root,
                                expected_width=artifact.width,
                                expected_height=artifact.height,
                            )
                            preview_sha256 = published.sha256
                            if preview_sha256 != candidate_sha256:
                                raise PreviewError(
                                    "published preview differs from the generated candidate"
                                )
                        except Exception:
                            if published is not None:
                                with suppress(OSError, PreviewError):
                                    _secure_remove_artifact(
                                        destination,
                                        workspace.root,
                                        published,
                                        expected_width=artifact.width,
                                        expected_height=artifact.height,
                                    )
                            raise
                        artifact = replace(artifact, path=destination)
                        if _POST_VERIFY_TEST_HOOK is not None:
                            _POST_VERIFY_TEST_HOOK(destination)
                        confirmed = _published_artifact_sha256(
                            destination,
                            workspace.root,
                            expected_width=artifact.width,
                            expected_height=artifact.height,
                        )
                        if confirmed != published:
                            raise PreviewError("published preview changed before READY persistence")
                    except PreviewBusyError as error:
                        connection.rollback()
                        deferred_count += 1
                        connection.execute(
                            "UPDATE preprocess_run SET deferred_count = ? WHERE id = ?",
                            (deferred_count, run_id),
                        )
                        _LOGGER.warning(
                            "preprocess deferred media_id=%s reason=%s", media_id, error
                        )
                    except Exception as error:
                        _LOGGER.warning(
                            "preprocess failed media_id=%s reason=%s: %s",
                            media_id,
                            type(error).__name__,
                            error,
                            exc_info=True,
                        )
                        if published is not None:
                            with suppress(OSError, PreviewError):
                                _secure_remove_artifact(
                                    destination,
                                    workspace.root,
                                    published,
                                    expected_width=artifact.width,
                                    expected_height=artifact.height,
                                )
                        _upsert_failed(
                            connection,
                            media_id,
                            source_signature,
                            fingerprint,
                            config,
                            error,
                        )
                        failed_count += 1
                    else:
                        _upsert_ready(
                            connection,
                            media_id,
                            source_signature,
                            artifact,
                            config,
                            identity_after,
                            preview_sha256,
                        )
                        processed_count += 1
                    finally:
                        try:
                            if candidate is not None:
                                with suppress(OSError):
                                    candidate.unlink(missing_ok=True)
                            if snapshot is not None:
                                with suppress(OSError):
                                    snapshot.unlink(missing_ok=True)
                        finally:
                            try:
                                connection.commit()
                            finally:
                                try:
                                    if item_context is not None:
                                        item_context.__exit__(None, None, None)
                                finally:
                                    if lock_context is not None:
                                        lock_context.__exit__(None, None, None)
                connection.execute(
                    """
                    UPDATE preprocess_run
                    SET processed_count = ?, cache_hit_count = ?, failed_count = ?,
                        deferred_count = ?
                    WHERE id = ?
                    """,
                    (processed_count, cache_hit_count, failed_count, deferred_count, run_id),
                )
                connection.commit()
            status = (
                "COMPLETE_WITH_FAILURES_AND_DEFERRED"
                if failed_count and deferred_count
                else "COMPLETE_WITH_FAILURES"
                if failed_count
                else "COMPLETE_WITH_DEFERRED"
                if deferred_count
                else "COMPLETE"
            )
            connection.execute(
                """
                UPDATE preprocess_run
                SET completed_at = ?, status = ?, processed_count = ?,
                    cache_hit_count = ?, failed_count = ?, deferred_count = ?
                WHERE id = ?
                """,
                (
                    _utc_now(),
                    status,
                    processed_count,
                    cache_hit_count,
                    failed_count,
                    deferred_count,
                    run_id,
                ),
            )
            connection.commit()
        except KeyboardInterrupt:
            connection.rollback()
            connection.execute(
                "UPDATE preprocess_run SET completed_at = ?, status = 'INTERRUPTED' WHERE id = ?",
                (_utc_now(), run_id),
            )
            connection.commit()
            raise
        except BaseException:
            connection.rollback()
            connection.execute(
                "UPDATE preprocess_run SET completed_at = ?, status = 'FAILED' WHERE id = ?",
                (_utc_now(), run_id),
            )
            connection.commit()
            raise
    return PreprocessResult(run_id, processed_count, cache_hit_count, failed_count, deferred_count)
