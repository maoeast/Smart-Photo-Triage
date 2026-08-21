"""Auditable capture-time extraction for Phase B media indexing."""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO

from PIL import Image

_EXIF_DATETIME_ORIGINAL = 36_867
_EXIF_CREATE_DATE = 36_868
_EXIF_OFFSET_ORIGINAL = 36_881
_EXIF_OFFSET_CREATE = 36_882
_EXIF_SUBSEC_ORIGINAL = 37_521
_EXIF_SUBSEC_CREATE = 37_522
_QUICKTIME_EPOCH = datetime(1904, 1, 1, tzinfo=UTC)
_FILENAME_PATTERNS = (
    re.compile(
        r"(?<!\d)(?P<year>\d{4})(?P<month>\d{2})(?P<day>\d{2})"
        r"[_-](?P<hour>\d{2})(?P<minute>\d{2})(?P<second>\d{2})(?!\d)"
    ),
    re.compile(
        r"(?<!\d)(?P<year>\d{4})[-_](?P<month>\d{2})[-_](?P<day>\d{2})"
        r"[_ -](?P<hour>\d{2})[-_](?P<minute>\d{2})[-_](?P<second>\d{2})(?!\d)"
    ),
)


@dataclass(frozen=True, slots=True)
class CaptureMetadata:
    captured_at: str | None
    capture_source: str
    capture_confidence: str
    capture_timezone_status: str
    width: int | None = None
    height: int | None = None
    duration_seconds: float | None = None
    error: str | None = None


def _text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("ascii", errors="strict").strip("\x00 ")
    return str(value).strip("\x00 ")


def _parse_exif_datetime(value: object, offset: object | None, subsec: object | None) -> datetime:
    captured = datetime.strptime(_text(value), "%Y:%m:%d %H:%M:%S")
    if subsec is not None:
        digits = "".join(character for character in _text(subsec) if character.isdigit())[:6]
        if digits:
            captured = captured.replace(microsecond=int(digits.ljust(6, "0")))
    if offset is None or not _text(offset):
        return captured
    offset_text = _text(offset)
    if offset_text == "Z":
        offset_text = "+00:00"
    if not re.fullmatch(r"[+-](?:0\d|1[0-4]):[0-5]\d", offset_text):
        raise ValueError(f"unsupported EXIF UTC offset {offset_text!r}")
    return datetime.fromisoformat(f"{captured.isoformat()}{offset_text}")


def _isoformat(value: datetime) -> str:
    timespec = "microseconds" if value.microsecond else "seconds"
    return value.isoformat(timespec=timespec)


def _image_metadata(path: Path) -> tuple[CaptureMetadata | None, str | None, int, int]:
    errors: list[str] = []
    with Image.open(path) as image:
        width, height = image.size
        exif = image.getexif()
        candidates = (
            (
                _EXIF_DATETIME_ORIGINAL,
                _EXIF_OFFSET_ORIGINAL,
                _EXIF_SUBSEC_ORIGINAL,
                "EXIF_DATETIME_ORIGINAL",
            ),
            (
                _EXIF_CREATE_DATE,
                _EXIF_OFFSET_CREATE,
                _EXIF_SUBSEC_CREATE,
                "EXIF_CREATE_DATE",
            ),
        )
        for datetime_tag, offset_tag, subsec_tag, source in candidates:
            value = exif.get(datetime_tag)
            if value is None:
                continue
            try:
                captured = _parse_exif_datetime(value, exif.get(offset_tag), exif.get(subsec_tag))
            except (UnicodeError, ValueError) as error:
                errors.append(f"{source}: {error}")
                continue
            timezone_status = "KNOWN" if captured.tzinfo is not None else "UNKNOWN"
            return (
                CaptureMetadata(
                    captured_at=_isoformat(captured),
                    capture_source=source,
                    capture_confidence="HIGH",
                    capture_timezone_status=timezone_status,
                    width=width,
                    height=height,
                    error="; ".join(errors) or None,
                ),
                "; ".join(errors) or None,
                width,
                height,
            )
    return None, "; ".join(errors) or None, width, height


def _iter_atoms(stream: BinaryIO, start: int, end: int):  # type: ignore[no-untyped-def]
    position = start
    while position + 8 <= end:
        stream.seek(position)
        header = stream.read(8)
        if len(header) != 8:
            raise ValueError("truncated QuickTime atom header")
        size, atom_type = struct.unpack(">I4s", header)
        header_size = 8
        if size == 1:
            extended = stream.read(8)
            if len(extended) != 8:
                raise ValueError("truncated QuickTime extended atom size")
            size = struct.unpack(">Q", extended)[0]
            header_size = 16
        elif size == 0:
            size = end - position
        if size < header_size or position + size > end:
            raise ValueError("invalid QuickTime atom size")
        yield atom_type, position + header_size, position + size
        position += size


def _quicktime_metadata(path: Path) -> CaptureMetadata | None:
    file_size = path.stat().st_size
    with path.open("rb") as stream:
        for atom_type, content_start, atom_end in _iter_atoms(stream, 0, file_size):
            if atom_type != b"moov":
                continue
            for child_type, child_start, child_end in _iter_atoms(stream, content_start, atom_end):
                if child_type != b"mvhd":
                    continue
                stream.seek(child_start)
                prefix = stream.read(min(32, child_end - child_start))
                if len(prefix) < 8:
                    raise ValueError("truncated QuickTime mvhd atom")
                version = prefix[0]
                if version == 0:
                    creation_seconds = struct.unpack(">I", prefix[4:8])[0]
                    timing_offset = 12
                    timing_size = 8
                elif version == 1:
                    if len(prefix) < 16:
                        raise ValueError("truncated version 1 QuickTime mvhd atom")
                    creation_seconds = struct.unpack(">Q", prefix[4:12])[0]
                    timing_offset = 20
                    timing_size = 12
                else:
                    raise ValueError(f"unsupported QuickTime mvhd version {version}")
                if creation_seconds == 0:
                    return None
                captured = _QUICKTIME_EPOCH + timedelta(seconds=creation_seconds)
                duration_seconds: float | None = None
                if len(prefix) >= timing_offset + timing_size:
                    if version == 0:
                        timescale, duration = struct.unpack(
                            ">II", prefix[timing_offset : timing_offset + 8]
                        )
                    else:
                        timescale = struct.unpack(">I", prefix[timing_offset : timing_offset + 4])[
                            0
                        ]
                        duration = struct.unpack(
                            ">Q", prefix[timing_offset + 4 : timing_offset + 12]
                        )[0]
                    if timescale:
                        duration_seconds = duration / timescale
                return CaptureMetadata(
                    captured_at=_isoformat(captured),
                    capture_source="QUICKTIME_CREATION_TIME",
                    capture_confidence="HIGH",
                    capture_timezone_status="UTC",
                    duration_seconds=duration_seconds,
                )
    return None


def _filename_metadata(path: Path) -> CaptureMetadata | None:
    for pattern in _FILENAME_PATTERNS:
        match = pattern.search(path.stem)
        if match is None:
            continue
        try:
            captured = datetime(**{name: int(value) for name, value in match.groupdict().items()})
        except ValueError:
            continue
        return CaptureMetadata(
            captured_at=_isoformat(captured),
            capture_source="FILENAME",
            capture_confidence="MEDIUM",
            capture_timezone_status="UNKNOWN",
        )
    return None


def filesystem_metadata(
    path: Path,
    *,
    mtime: float | None = None,
    error: str | None = None,
    width: int | None = None,
    height: int | None = None,
) -> CaptureMetadata:
    """Return the explicit low-confidence filesystem fallback."""
    selected_mtime = path.stat().st_mtime if mtime is None else mtime
    captured = datetime.fromtimestamp(selected_mtime, tz=UTC)
    return CaptureMetadata(
        captured_at=_isoformat(captured),
        capture_source="FILESYSTEM_MTIME",
        capture_confidence="LOW",
        capture_timezone_status="UTC",
        width=width,
        height=height,
        error=error,
    )


def extract_metadata(path: Path, media_type: str) -> CaptureMetadata:
    """Extract capture metadata in precedence order without modifying *path*."""
    embedded_error: str | None = None
    width: int | None = None
    height: int | None = None
    try:
        embedded: CaptureMetadata | None = None
        if media_type == "IMAGE":
            embedded, embedded_error, width, height = _image_metadata(path)
        elif media_type == "VIDEO":
            embedded = _quicktime_metadata(path)
        if embedded is not None:
            return embedded
    except (OSError, OverflowError, struct.error, UnicodeError, ValueError) as error:
        embedded_error = f"{type(error).__name__}: {error}"

    filename = _filename_metadata(path)
    if filename is not None:
        return CaptureMetadata(
            captured_at=filename.captured_at,
            capture_source=filename.capture_source,
            capture_confidence=filename.capture_confidence,
            capture_timezone_status=filename.capture_timezone_status,
            width=width,
            height=height,
            error=embedded_error,
        )
    return filesystem_metadata(path, error=embedded_error, width=width, height=height)
