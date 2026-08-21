"""Workspace initialization contract for Phase A."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from smart_photo_triage.database import (
    MigrationError,
    apply_migrations,
    connect_database,
    validate_database,
)

DEFAULT_CONFIG = "allow_cloud = false\n"
OWNERSHIP_MARKER_NAME = ".spt-workspace"
_MARKER_FORMAT_VERSION = 1
_INITIALIZATION_LOCK_NAME = ".spt-init-lock"
_LOCK_TIMEOUT_SECONDS = 10.0


class WorkspaceOwnershipError(RuntimeError):
    """Raised when an existing database cannot be identified as an SPT workspace."""


@dataclass(frozen=True, slots=True)
class Workspace:
    root: Path
    config_path: Path
    database_path: Path


def _normalize_workspace_id(value: object) -> str:
    if not isinstance(value, str):
        raise WorkspaceOwnershipError("Workspace marker identity must be a string")
    try:
        normalized = UUID(value).hex
    except ValueError as error:
        raise WorkspaceOwnershipError("Workspace marker identity is not a valid UUID") from error
    if normalized != value:
        raise WorkspaceOwnershipError(
            "Workspace marker identity is not canonical lowercase UUID hex"
        )
    return normalized


def _marker_content(workspace_id: str) -> str:
    return (
        json.dumps(
            {"format_version": _MARKER_FORMAT_VERSION, "workspace_id": workspace_id},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )


def _read_marker(marker_path: Path) -> str:
    try:
        raw = marker_path.read_text(encoding="ascii")
        data = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise WorkspaceOwnershipError(f"Cannot read workspace ownership marker: {error}") from error
    if not isinstance(data, dict) or set(data) != {"format_version", "workspace_id"}:
        raise WorkspaceOwnershipError("Workspace ownership marker fields are invalid")
    if data["format_version"] != _MARKER_FORMAT_VERSION:
        raise WorkspaceOwnershipError("Workspace ownership marker version is unsupported")
    return _normalize_workspace_id(data["workspace_id"])


def _fsync_parent_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_marker_atomically(marker_path: Path, workspace_id: str) -> None:
    temporary_path = marker_path.with_name(f".{marker_path.name}.{uuid4().hex}.tmp")
    try:
        with temporary_path.open("x", encoding="ascii", newline="\n") as stream:
            stream.write(_marker_content(workspace_id))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, marker_path)
        _fsync_parent_directory(marker_path.parent)
    finally:
        temporary_path.unlink(missing_ok=True)


def _path_identity(path: Path) -> tuple[int, int]:
    state = path.stat(follow_symlinks=False)
    return state.st_dev, state.st_ino


def _cleanup_owned_lock(
    lock_path: Path,
    identity: tuple[int, int],
    owner_path: Path | None,
    token: str,
    *,
    allow_partial_owner: bool = False,
) -> None:
    try:
        if _path_identity(lock_path) != identity:
            return
        if owner_path is not None:
            if not allow_partial_owner and owner_path.read_text(encoding="ascii") != token:
                return
            try:
                owner_path.unlink()
            except FileNotFoundError:
                if not allow_partial_owner:
                    return
        if _path_identity(lock_path) == identity:
            lock_path.rmdir()
    except OSError:
        return


@contextmanager
def _initialization_lock(
    root: Path,
    *,
    timeout_seconds: float = _LOCK_TIMEOUT_SECONDS,
    sleep=time.sleep,
    clock=time.monotonic,
) -> Iterator[None]:  # type: ignore[no-untyped-def]
    lock_path = root / _INITIALIZATION_LOCK_NAME
    deadline = clock() + timeout_seconds
    observed_contention = False
    while True:
        try:
            lock_path.mkdir()
            break
        except FileExistsError:
            observed_contention = True
            if clock() >= deadline:
                raise WorkspaceOwnershipError(
                    f"Timed out waiting for workspace initialization lock: {lock_path}"
                ) from None
            sleep(0.005)
        except PermissionError:
            if not observed_contention:
                raise
            if clock() >= deadline:
                raise
            sleep(0.005)
    identity = _path_identity(lock_path)
    token = uuid4().hex
    owner_path = lock_path / f".owner-{token}"
    try:
        try:
            with owner_path.open("x", encoding="ascii", newline="\n") as stream:
                stream.write(f"{token}\n")
        except BaseException:
            _cleanup_owned_lock(
                lock_path,
                identity,
                owner_path,
                token,
                allow_partial_owner=True,
            )
            raise
        yield
    finally:
        _cleanup_owned_lock(lock_path, identity, owner_path, f"{token}\n")


def _ensure_workspace_marker(root: Path, database_path: Path) -> str:
    marker_path = root / OWNERSHIP_MARKER_NAME
    if marker_path.exists():
        return _read_marker(marker_path)

    with _initialization_lock(root):
        if marker_path.exists():
            return _read_marker(marker_path)
        if database_path.exists():
            raise WorkspaceOwnershipError(
                "Cannot establish ownership of a markerless existing spt.sqlite3"
            )
        workspace_id = uuid4().hex
        _write_marker_atomically(marker_path, workspace_id)
        return workspace_id


def initialize_workspace(root: Path) -> Workspace:
    """Create missing workspace state without replacing anything already present."""
    normalized_root = root.expanduser().resolve(strict=False)
    normalized_root.mkdir(parents=True, exist_ok=True)
    database_path = normalized_root / "spt.sqlite3"
    workspace_id = _ensure_workspace_marker(normalized_root, database_path)

    for directory in ("state", "previews", "plans", "logs"):
        (normalized_root / directory).mkdir(exist_ok=True)

    config_path = normalized_root / "config.toml"
    try:
        with config_path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(DEFAULT_CONFIG)
    except FileExistsError:
        pass

    try:
        with closing(connect_database(database_path)) as connection:
            apply_migrations(connection, workspace_id=workspace_id)
    except MigrationError as error:
        raise WorkspaceOwnershipError(
            f"Database schema or workspace identity validation failed: {error}"
        ) from error

    return Workspace(
        root=normalized_root,
        config_path=config_path,
        database_path=database_path,
    )


def open_workspace(root: Path) -> Workspace:
    """Open and validate an existing workspace without creating or changing any path."""
    try:
        normalized_root = root.expanduser().resolve(strict=True)
    except OSError as error:
        raise WorkspaceOwnershipError(
            f"Workspace does not exist or is unavailable: {root}"
        ) from error
    if not normalized_root.is_dir():
        raise WorkspaceOwnershipError(f"Workspace path is not a directory: {normalized_root}")
    marker_path = normalized_root / OWNERSHIP_MARKER_NAME
    database_path = normalized_root / "spt.sqlite3"
    if not marker_path.is_file() or not database_path.is_file():
        raise WorkspaceOwnershipError(
            f"Workspace ownership marker or database is missing: {normalized_root}"
        )
    workspace_id = _read_marker(marker_path)
    try:
        with closing(connect_database(database_path, read_only=True)) as connection:
            validate_database(connection, expected_workspace_id=workspace_id)
    except (MigrationError, sqlite3.Error) as error:
        raise WorkspaceOwnershipError(f"Workspace database validation failed: {error}") from error
    return Workspace(
        root=normalized_root,
        config_path=normalized_root / "config.toml",
        database_path=database_path,
    )
