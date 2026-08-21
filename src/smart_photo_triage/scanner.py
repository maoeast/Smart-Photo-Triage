"""Read-only streaming media scan, identity, and bundle indexing."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import groupby
from pathlib import Path
from uuid import uuid4

from smart_photo_triage.database import connect_database
from smart_photo_triage.metadata import CaptureMetadata, extract_metadata, filesystem_metadata
from smart_photo_triage.workspace import Workspace

_IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"})
_VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".m4v"})
_SIDECAR_EXTENSIONS = frozenset({".aae", ".xmp", ".json"})
_SUPPORTED_EXTENSIONS = _IMAGE_EXTENSIONS | _VIDEO_EXTENSIONS | _SIDECAR_EXTENSIONS
_WINDOWS_REPARSE_POINT = 0x400
_HASH_CHUNK_SIZE = 1024 * 1024
_SCAN_BATCH_SIZE = 250


class ScanLayoutError(ValueError):
    """Raised when source/workspace/output paths cannot be scanned safely."""


@dataclass(frozen=True, slots=True)
class ScanResult:
    scan_id: str
    discovered_count: int
    indexed_count: int
    unchanged_count: int
    missing_count: int
    error_count: int
    warning_count: int
    bundle_count: int


@dataclass(frozen=True, slots=True)
class ScanLayout:
    source_root: Path
    workspace_root: Path
    output_root: Path | None
    excluded_roots: tuple[Path, ...]


MetadataExtractor = Callable[[Path, str], CaptureMetadata]
ProcessAlive = Callable[[int, str | None], bool]


def _process_is_alive(pid: int, _owner_token: str | None) -> bool:
    """Return false only when the recorded PID can be proven absent."""
    if pid <= 0 or pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        open_process.restype = wintypes.HANDLE
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        get_exit_code.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL

        handle = open_process(process_query_limited_information, False, pid)
        if not handle:
            error_code = ctypes.get_last_error()
            return error_code != 87  # ERROR_INVALID_PARAMETER means no such process.
        try:
            exit_code = wintypes.DWORD()
            if not get_exit_code(handle, ctypes.byref(exit_code)):
                return True
            return exit_code.value == still_active
        finally:
            close_handle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def validate_scan_layout(
    source: Path, workspace_root: Path, output: Path | None = None
) -> ScanLayout:
    """Validate path relationships without creating or modifying the workspace."""
    source_input = source.expanduser().absolute()
    if _is_reparse_path(source_input):
        raise ScanLayoutError("source root cannot be a symlink or junction")
    try:
        source_root = source.expanduser().resolve(strict=True)
    except OSError as error:
        raise ScanLayoutError(f"source is unavailable: {error}") from error
    if not source_root.is_dir():
        raise ScanLayoutError(f"source is not a directory: {source_root}")

    normalized_workspace = workspace_root.expanduser().resolve(strict=False)
    if _is_within(source_root, normalized_workspace):
        raise ScanLayoutError("source cannot be inside workspace")
    excluded_roots: list[Path] = []
    if _is_within(normalized_workspace, source_root):
        excluded_roots.append(normalized_workspace)

    output_root: Path | None = None
    if output is not None:
        output_root = output.expanduser().resolve(strict=False)
        if output_root.exists() and not output_root.is_dir():
            raise ScanLayoutError(f"output is not a directory: {output_root}")
        if _is_within(source_root, output_root):
            raise ScanLayoutError("source cannot be inside output")
        if _is_within(output_root, source_root):
            excluded_roots.append(output_root)
    return ScanLayout(
        source_root=source_root,
        workspace_root=normalized_workspace,
        output_root=output_root,
        excluded_roots=tuple(excluded_roots),
    )


def _scan_config_fingerprint(
    layout: ScanLayout, exclude_globs: Sequence[str], hash_content: bool
) -> str:
    payload = json.dumps(
        {
            "version": 1,
            "source": _path_key(layout.source_root),
            "workspace": _path_key(layout.workspace_root),
            "output": _path_key(layout.output_root) if layout.output_root is not None else None,
            "exclude_globs": sorted(set(exclude_globs)),
            "hash_content": hash_content,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _is_within(child: Path, parent: Path) -> bool:
    child_key = _path_key(child)
    parent_key = _path_key(parent)
    try:
        return os.path.commonpath((child_key, parent_key)) == parent_key
    except ValueError:
        return False


def _is_reparse_path(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(os.path, "isjunction", None)
    if is_junction is not None and is_junction(path):
        return True
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & _WINDOWS_REPARSE_POINT)


def _is_reparse_entry(entry: os.DirEntry[str]) -> bool:
    if entry.is_symlink():
        return True
    attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    return bool(attributes & _WINDOWS_REPARSE_POINT)


def _media_type(extension: str) -> str:
    if extension in _IMAGE_EXTENSIONS:
        return "IMAGE"
    if extension in _VIDEO_EXTENSIONS:
        return "VIDEO"
    return "SIDECAR"


def _bundle_stem(path: Path, extension: str) -> str:
    stem = path.stem
    if extension in _SIDECAR_EXTENSIONS:
        nested = Path(stem)
        if nested.suffix.casefold() in _IMAGE_EXTENSIONS | _VIDEO_EXTENSIONS:
            stem = nested.stem
    return stem.casefold()


def _is_glob_excluded(relative_path: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatch(relative_path, pattern) for pattern in patterns)


def _iter_supported_files(
    source: Path,
    excluded_roots: Sequence[Path],
    exclude_globs: Sequence[str],
    on_error: Callable[[Path, OSError], None],
    on_skipped_prefix: Callable[[Path], None] | None = None,
) -> Iterator[Path]:
    """Yield files incrementally and never traverse links or Windows junctions."""
    stack = [source]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    try:
                        if _is_reparse_entry(entry):
                            continue
                        relative = path.relative_to(source).as_posix()
                        if _is_glob_excluded(relative, exclude_globs):
                            if entry.is_dir(follow_symlinks=False) and on_skipped_prefix:
                                on_skipped_prefix(path)
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if any(_is_within(path, excluded) for excluded in excluded_roots):
                                continue
                            stack.append(path)
                        elif entry.is_file(follow_symlinks=False):
                            extension = path.suffix.casefold()
                            if extension in _SUPPORTED_EXTENSIONS:
                                yield path
                    except OSError as error:
                        on_error(path, error)
        except OSError as error:
            on_error(directory, error)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(_HASH_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _record_warning(
    connection: sqlite3.Connection,
    scan_id: str,
    *,
    code: str,
    message: str,
    media_id: int | None = None,
    path_key: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO scan_warning(scan_id, media_id, path_key, code, message)
        VALUES (?, ?, ?, ?, ?)
        """,
        (scan_id, media_id, path_key, code, message),
    )


def _bundle_identity(bundle_type: str, bundle_key: str, path_keys: Sequence[str]) -> str:
    payload = "\0".join(("bundle-v1", bundle_type, bundle_key, *sorted(path_keys)))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _insert_bundle(
    connection: sqlite3.Connection,
    *,
    source_root_key: str,
    bundle_type: str,
    bundle_key: str,
    members: Sequence[tuple[int, str, str]],
    warning_status: str | None = None,
) -> None:
    bundle_id = _bundle_identity(bundle_type, bundle_key, [member[1] for member in members])
    connection.execute(
        """
        INSERT INTO asset_bundle(id, bundle_type, bundle_key, source_root_key, warning_status)
        VALUES (?, ?, ?, ?, ?)
        """,
        (bundle_id, bundle_type, bundle_key, source_root_key, warning_status),
    )
    connection.executemany(
        "INSERT INTO bundle_member(bundle_id, media_id, role) VALUES (?, ?, ?)",
        ((bundle_id, media_id, role) for media_id, _path, role in members),
    )


def _single_bundle(
    connection: sqlite3.Connection,
    source_root_key: str,
    row: tuple[str, str, int, str, str, str],
    *,
    warning_status: str | None = None,
) -> None:
    _parent_key, _stem, media_id, path_key, _media_type_name, _extension = row
    _insert_bundle(
        connection,
        source_root_key=source_root_key,
        bundle_type="SINGLE",
        bundle_key=f"single:{path_key}",
        members=((media_id, path_key, "PRIMARY"),),
        warning_status=warning_status,
    )


def _rebuild_bundles(
    connection: sqlite3.Connection,
    source_root_key: str,
    scan_id: str,
) -> tuple[int, int]:
    connection.execute("DELETE FROM asset_bundle WHERE source_root_key = ?", (source_root_key,))
    cursor = connection.execute(
        """
        SELECT parent_key, bundle_stem, id, path_key, media_type, extension
        FROM media_item
        WHERE source_root_key = ? AND source_present = 1
        ORDER BY parent_key, bundle_stem, path_key
        """,
        (source_root_key,),
    )
    bundle_count = 0
    warning_count = 0
    for (_parent_key, _stem), group_rows in groupby(cursor, key=lambda row: (row[0], row[1])):
        group = [tuple(row) for row in group_rows]
        if len(group) == 1:
            _single_bundle(connection, source_root_key, group[0])
            bundle_count += 1
            continue

        non_sidecars = [row for row in group if row[4] != "SIDECAR"]
        sidecars = [row for row in group if row[4] == "SIDECAR"]
        live_images = [row for row in group if row[5] in {".heic", ".heif"}]
        live_videos = [row for row in group if row[5] == ".mov"]
        bundle_key = f"group:{group[0][0]}:{group[0][1]}"

        if len(non_sidecars) == 2 and len(live_images) == 1 and len(live_videos) == 1:
            primary = live_images[0]
            video = live_videos[0]
            members = [
                (primary[2], primary[3], "PRIMARY"),
                (video[2], video[3], "LIVE_VIDEO"),
                *((row[2], row[3], "SIDECAR") for row in sidecars),
            ]
            _insert_bundle(
                connection,
                source_root_key=source_root_key,
                bundle_type="LIVE_PHOTO",
                bundle_key=bundle_key,
                members=members,
            )
            bundle_count += 1
            continue

        if len(non_sidecars) == 1 and sidecars:
            primary = non_sidecars[0]
            members = [
                (primary[2], primary[3], "PRIMARY"),
                *((row[2], row[3], "SIDECAR") for row in sidecars),
            ]
            _insert_bundle(
                connection,
                source_root_key=source_root_key,
                bundle_type="SIDECAR_SET",
                bundle_key=bundle_key,
                members=members,
            )
            bundle_count += 1
            continue

        _record_warning(
            connection,
            scan_id,
            code="AMBIGUOUS_BUNDLE",
            message=f"Ambiguous companion set for stem {group[0][1]!r}; items kept separate",
        )
        warning_count += 1
        for row in group:
            _single_bundle(connection, source_root_key, row, warning_status="AMBIGUOUS")
            bundle_count += 1
    return bundle_count, warning_count


def _reject_overlapping_source(
    connection: sqlite3.Connection, source_root: Path, source_root_key: str
) -> None:
    existing_roots = connection.execute(
        """
        SELECT DISTINCT source_root, source_root_key
        FROM scan_run
        WHERE source_root_key != ?
        """,
        (source_root_key,),
    )
    for existing_root_text, _existing_key in existing_roots:
        existing_root = Path(str(existing_root_text))
        if _is_within(source_root, existing_root) or _is_within(existing_root, source_root):
            raise ScanLayoutError(
                f"source root overlap is unsupported: {source_root} and {existing_root}"
            )


def _is_protected_historical_path(
    path: Path,
    source_root: Path,
    excluded_roots: Sequence[Path],
    exclude_globs: Sequence[str],
) -> bool:
    if any(_is_within(path, excluded) for excluded in excluded_roots):
        return True
    try:
        relative = path.relative_to(source_root).as_posix()
    except ValueError:
        return True
    return _is_glob_excluded(relative, exclude_globs)


def _update_scan_run(
    connection: sqlite3.Connection,
    scan_id: str,
    *,
    status: str,
    discovered_count: int,
    indexed_count: int,
    unchanged_count: int,
    missing_count: int,
    error_count: int,
    warning_count: int,
    bundle_count: int,
    completed: bool,
) -> None:
    connection.execute(
        """
        UPDATE scan_run
        SET completed_at = ?, status = ?, discovered_count = ?, indexed_count = ?,
            unchanged_count = ?, missing_count = ?, error_count = ?, warning_count = ?,
            bundle_count = ?
        WHERE id = ?
        """,
        (
            _utc_now() if completed else None,
            status,
            discovered_count,
            indexed_count,
            unchanged_count,
            missing_count,
            error_count,
            warning_count,
            bundle_count,
            scan_id,
        ),
    )


def _terminal_failure(
    connection: sqlite3.Connection,
    scan_id: str,
    status: str,
) -> None:
    connection.rollback()
    connection.execute(
        "UPDATE scan_run SET completed_at = ?, status = ? WHERE id = ?",
        (_utc_now(), status, scan_id),
    )
    connection.commit()


def scan_library(
    workspace: Workspace,
    source: Path,
    *,
    output: Path | None = None,
    exclude_globs: Sequence[str] = (),
    hash_content: bool = False,
    metadata_extractor: MetadataExtractor = extract_metadata,
    process_alive: ProcessAlive = _process_is_alive,
) -> ScanResult:
    """Scan supported media into the workspace without changing source files."""
    layout = validate_scan_layout(source, workspace.root, output)
    source_root = layout.source_root
    excluded_roots = layout.excluded_roots
    scan_id = uuid4().hex
    owner_pid = os.getpid()
    owner_token = uuid4().hex
    source_root_key = _path_key(source_root)
    started_at = _utc_now()
    config_fingerprint = _scan_config_fingerprint(layout, exclude_globs, hash_content)
    discovered_count = 0
    indexed_count = 0
    unchanged_count = 0
    error_count = 0
    warning_count = 0
    missing_count = 0
    bundle_count = 0
    scope_complete = True
    pending_items = 0
    skipped_prefixes = list(excluded_roots)

    with closing(connect_database(workspace.database_path)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            _reject_overlapping_source(connection, source_root, source_root_key)
            active = connection.execute(
                """
                SELECT id, owner_pid, owner_token FROM scan_run
                WHERE source_root_key = ? AND status = 'RUNNING'
                LIMIT 1
                """,
                (source_root_key,),
            ).fetchone()
            recovered_run_id: str | None = None
            if active is not None:
                active_pid = int(active[1]) if active[1] is not None else None
                active_token = str(active[2]) if active[2] is not None else None
                if active_pid is None or process_alive(active_pid, active_token):
                    raise ScanLayoutError(f"scan already running for source: {source_root}")
                recovered_run_id = str(active[0])
                connection.execute(
                    """
                    UPDATE scan_run
                    SET completed_at = ?, status = 'INTERRUPTED',
                        error_count = error_count + 1,
                        terminal_reason = 'OWNER_PROCESS_NOT_FOUND'
                    WHERE id = ? AND status = 'RUNNING'
                    """,
                    (_utc_now(), recovered_run_id),
                )
            previous = None
            if recovered_run_id is None:
                previous = connection.execute(
                    """
                    SELECT id FROM scan_run
                    WHERE source_root_key = ? AND config_fingerprint = ?
                      AND status IN ('INTERRUPTED', 'FAILED')
                    ORDER BY started_at DESC LIMIT 1
                    """,
                    (source_root_key, config_fingerprint),
                ).fetchone()
            resume_of = (
                recovered_run_id
                if recovered_run_id is not None
                else str(previous[0])
                if previous is not None
                else None
            )
            connection.execute(
                """
                INSERT INTO scan_run(
                    id, source_root, source_root_key, config_fingerprint, resume_of,
                    started_at, status, owner_pid, owner_token
                ) VALUES (?, ?, ?, ?, ?, ?, 'RUNNING', ?, ?)
                """,
                (
                    scan_id,
                    str(source_root),
                    source_root_key,
                    config_fingerprint,
                    resume_of,
                    started_at,
                    owner_pid,
                    owner_token,
                ),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

        try:

            def on_walk_error(path: Path, error: OSError) -> None:
                nonlocal error_count, scope_complete, warning_count
                scope_complete = False
                error_count += 1
                warning_count += 1
                _record_warning(
                    connection,
                    scan_id,
                    code="ITEM_SCAN_ERROR",
                    message=f"{type(error).__name__}: {error}",
                    path_key=_path_key(path),
                )

            for path in _iter_supported_files(
                source_root,
                excluded_roots,
                exclude_globs,
                on_walk_error,
                skipped_prefixes.append,
            ):
                discovered_count += 1
                extension = path.suffix.casefold()
                media_type_name = _media_type(extension)
                path_key = _path_key(path)
                try:
                    stat = path.stat(follow_symlinks=False)
                except OSError as error:
                    on_walk_error(path, error)
                    continue

                existing = connection.execute(
                    """
                    SELECT id, size_bytes, mtime_ns, content_sha256, metadata_error
                    FROM media_item WHERE path_key = ?
                    """,
                    (path_key,),
                ).fetchone()
                is_unchanged = (
                    existing is not None
                    and int(existing[1]) == stat.st_size
                    and int(existing[2]) == stat.st_mtime_ns
                )
                item_errors: list[str] = []
                content_sha256: str | None = None
                if hash_content and (existing is None or existing[3] is None or not is_unchanged):
                    try:
                        content_sha256 = _sha256(path)
                    except OSError as error:
                        item_errors.append(f"hash {type(error).__name__}: {error}")

                seen_at = _utc_now()
                needs_metadata_retry = bool(
                    is_unchanged and existing is not None and existing[4] is not None
                )
                source_present = 1
                try:
                    path.stat(follow_symlinks=False)
                except OSError as error:
                    source_present = 0
                    item_errors.append(f"stat {type(error).__name__}: {error}")
                if is_unchanged and not needs_metadata_retry and not item_errors and source_present:
                    connection.execute(
                        """
                        UPDATE media_item
                        SET original_path = ?, source_root = ?, source_root_key = ?,
                            source_present = 1, last_seen_at = ?, last_seen_scan_id = ?,
                            content_sha256 = COALESCE(?, content_sha256)
                        WHERE id = ?
                        """,
                        (
                            str(path),
                            str(source_root),
                            source_root_key,
                            seen_at,
                            scan_id,
                            content_sha256,
                            int(existing[0]),
                        ),
                    )
                    unchanged_count += 1
                    indexed_count += 1
                else:
                    try:
                        metadata = metadata_extractor(path, media_type_name)
                    except Exception as error:  # one third-party metadata failure must remain local
                        message = f"{type(error).__name__}: {error}"
                        item_errors.append(message)
                        metadata = filesystem_metadata(
                            path,
                            mtime=stat.st_mtime,
                            error=message,
                        )
                    if metadata.error:
                        item_errors.append(metadata.error)
                    if source_present:
                        try:
                            path.stat(follow_symlinks=False)
                        except OSError as error:
                            source_present = 0
                            item_errors.append(f"stat {type(error).__name__}: {error}")
                    metadata_error = "; ".join(dict.fromkeys(item_errors)) or None
                    if metadata_error:
                        error_count += 1
                    selected_sha256 = (
                        content_sha256
                        if content_sha256 is not None
                        else str(existing[3])
                        if is_unchanged and existing is not None and existing[3] is not None
                        else None
                    )

                    connection.execute(
                        """
                        INSERT INTO media_item(
                            original_path, path_key, source_root, source_root_key, parent_key,
                            bundle_stem, media_type, extension, size_bytes, mtime_ns,
                            source_present, content_sha256, captured_at, capture_source,
                            capture_confidence, capture_timezone_status, width, height,
                            duration_seconds, last_seen_at, last_seen_scan_id, metadata_error
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(path_key) DO UPDATE SET
                            original_path = excluded.original_path,
                            source_root = excluded.source_root,
                            source_root_key = excluded.source_root_key,
                            parent_key = excluded.parent_key,
                            bundle_stem = excluded.bundle_stem,
                            media_type = excluded.media_type,
                            extension = excluded.extension,
                            size_bytes = excluded.size_bytes,
                            mtime_ns = excluded.mtime_ns,
                            source_present = excluded.source_present,
                            content_sha256 = excluded.content_sha256,
                            captured_at = excluded.captured_at,
                            capture_source = excluded.capture_source,
                            capture_confidence = excluded.capture_confidence,
                            capture_timezone_status = excluded.capture_timezone_status,
                            width = excluded.width,
                            height = excluded.height,
                            duration_seconds = excluded.duration_seconds,
                            last_seen_at = excluded.last_seen_at,
                            last_seen_scan_id = excluded.last_seen_scan_id,
                            metadata_error = excluded.metadata_error
                        """,
                        (
                            str(path),
                            path_key,
                            str(source_root),
                            source_root_key,
                            _path_key(path.parent),
                            _bundle_stem(path, extension),
                            media_type_name,
                            extension,
                            stat.st_size,
                            stat.st_mtime_ns,
                            source_present,
                            selected_sha256,
                            metadata.captured_at,
                            metadata.capture_source,
                            metadata.capture_confidence,
                            metadata.capture_timezone_status,
                            metadata.width,
                            metadata.height,
                            metadata.duration_seconds,
                            seen_at,
                            scan_id,
                            metadata_error,
                        ),
                    )
                    if is_unchanged:
                        unchanged_count += 1
                    indexed_count += 1

                pending_items += 1
                if pending_items >= max(1, _SCAN_BATCH_SIZE):
                    _update_scan_run(
                        connection,
                        scan_id,
                        status="RUNNING",
                        discovered_count=discovered_count,
                        indexed_count=indexed_count,
                        unchanged_count=unchanged_count,
                        missing_count=0,
                        error_count=error_count,
                        warning_count=warning_count,
                        bundle_count=0,
                        completed=False,
                    )
                    connection.commit()
                    pending_items = 0

            _update_scan_run(
                connection,
                scan_id,
                status="RUNNING",
                discovered_count=discovered_count,
                indexed_count=indexed_count,
                unchanged_count=unchanged_count,
                missing_count=0,
                error_count=error_count,
                warning_count=warning_count,
                bundle_count=0,
                completed=False,
            )
            connection.commit()

            if scope_complete:
                missing_candidates = connection.execute(
                    """
                    SELECT id, original_path FROM media_item
                    WHERE source_root_key = ? AND source_present = 1 AND last_seen_scan_id != ?
                    ORDER BY path_key
                    """,
                    (source_root_key, scan_id),
                )
                for media_id, original_path in missing_candidates:
                    historical_path = Path(str(original_path))
                    if _is_protected_historical_path(
                        historical_path,
                        source_root,
                        skipped_prefixes,
                        exclude_globs,
                    ):
                        continue
                    connection.execute(
                        "UPDATE media_item SET source_present = 0 WHERE id = ?",
                        (int(media_id),),
                    )
                    missing_count += 1

            bundle_count, bundle_warnings = _rebuild_bundles(connection, source_root_key, scan_id)
            warning_count += bundle_warnings
            final_status = (
                "COMPLETE_WITH_WARNINGS"
                if error_count or warning_count or not scope_complete
                else "COMPLETE"
            )
            _update_scan_run(
                connection,
                scan_id,
                status=final_status,
                discovered_count=discovered_count,
                indexed_count=indexed_count,
                unchanged_count=unchanged_count,
                missing_count=missing_count,
                error_count=error_count,
                warning_count=warning_count,
                bundle_count=bundle_count,
                completed=True,
            )
            connection.commit()
        except KeyboardInterrupt:
            _terminal_failure(connection, scan_id, "INTERRUPTED")
            raise
        except BaseException:
            _terminal_failure(connection, scan_id, "FAILED")
            raise

    return ScanResult(
        scan_id=scan_id,
        discovered_count=discovered_count,
        indexed_count=indexed_count,
        unchanged_count=unchanged_count,
        missing_count=missing_count,
        error_count=error_count,
        warning_count=warning_count,
        bundle_count=bundle_count,
    )
