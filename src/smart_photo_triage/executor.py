"""Approval-gated, journaled file execution, recovery, diagnosis, and rollback."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from smart_photo_triage.database import connect_database
from smart_photo_triage.planner import (
    OrganizationPlan,
    PlanEntry,
    PlannerError,
    PreflightReport,
    _approval_contract,
    _is_within,
    inspect_plan,
    preflight_approval_is_current,
    preflight_plan,
)
from smart_photo_triage.preprocess import (
    PreviewError,
    _open_source_nofollow,
    _secure_workspace_directory,
    _SecureDirectoryBinding,
)
from smart_photo_triage.workspace import Workspace

_HASH_CHUNK_SIZE = 1024 * 1024
_LOCK_NAME = "apply.lock"
_TRANSACTION_PATTERN = re.compile(r"^tx-[0-9a-f]{32}-r([1-9][0-9]*)-([0-9a-f]{64})$")
_ENTRY_TERMINAL = frozenset({"DONE", "ALREADY_PRESENT", "ROLLED_BACK"})
_TRANSACTION_TERMINAL = frozenset({"DONE", "ROLLED_BACK"})


class ExecutorError(RuntimeError):
    """Base class for safe execution failures."""


class ApprovalContractError(ExecutorError):
    """Raised when approval or immutable payload authority is missing or stale."""


class WorkspaceLockedError(ExecutorError):
    """Raised when a live or unprovable workspace mutation owner exists."""


class RecoverySafetyError(ExecutorError):
    """Raised when recovery cannot prove ownership or file identity."""


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    transaction_id: str
    plan_id: str
    state: str
    done_count: int
    failed_count: int
    already_present_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "transaction_id": self.transaction_id,
            "plan_id": self.plan_id,
            "state": self.state,
            "done_count": self.done_count,
            "failed_count": self.failed_count,
            "already_present_count": self.already_present_count,
        }


@dataclass(frozen=True, slots=True)
class RollbackResult:
    transaction_id: str
    state: str
    rolled_back_count: int
    failed_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "transaction_id": self.transaction_id,
            "state": self.state,
            "rolled_back_count": self.rolled_back_count,
            "failed_count": self.failed_count,
        }


@dataclass(frozen=True, slots=True)
class Diagnosis:
    code: str
    message: str
    transaction_id: str | None = None
    media_id: int | None = None
    path: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "transaction_id": self.transaction_id,
            "media_id": self.media_id,
            "path": self.path,
        }


@dataclass(frozen=True, slots=True)
class DoctorReport:
    lock_status: str
    diagnoses: tuple[Diagnosis, ...]

    @property
    def ok(self) -> bool:
        unsafe = {"UNKNOWN", "LIVE"}
        return self.lock_status not in unsafe and not any(
            item.code
            in {
                "PARTIAL_HASH_MISMATCH",
                "TARGET_HASH_MISMATCH",
                "TARGET_MISSING",
                "PLAN_STALE",
                "PLAN_INVALID",
                "JOURNAL_PLAN_MISMATCH",
                "ORPHAN_PARTIAL",
                "PARTIAL_MISSING",
                "BUNDLE_PARTIAL",
                "OUTPUT_ROOT_UNSAFE",
                "DOCTOR_SCAN_LIMIT",
            }
            for item in self.diagnoses
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "lock_status": self.lock_status,
            "diagnoses": [item.to_dict() for item in self.diagnoses],
        }


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    device: int
    inode: int
    size: int
    mtime_ns: int
    sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "device": self.device,
            "inode": self.inode,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class _MutationLease:
    token: str
    identity: _FileIdentity


FaultInjector = Callable[[str, int], None]
ProcessLiveness = Callable[[int, str], str]
_IDENTITY_DELETE_TEST_HOOK: Callable[[Path], None] | None = None


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _metadata(value: object) -> dict[str, object]:
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return {"error_code": "UNPARSEABLE_JOURNAL_METADATA"}
    return parsed if isinstance(parsed, dict) else {"error_code": "INVALID_JOURNAL_METADATA"}


def _identity_from_dict(value: object) -> _FileIdentity | None:
    if not isinstance(value, dict):
        return None
    try:
        result = _FileIdentity(
            device=int(value["device"]),
            inode=int(value["inode"]),
            size=int(value["size"]),
            mtime_ns=int(value["mtime_ns"]),
            sha256=str(value["sha256"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
    if result.size < 0 or len(result.sha256) != 64:
        return None
    return result


def _same_stat(left: _FileIdentity, right: _FileIdentity) -> bool:
    return (
        left.device,
        left.inode,
        left.size,
        left.mtime_ns,
    ) == (right.device, right.inode, right.size, right.mtime_ns)


def _stable_hash(path: Path, root: Path) -> _FileIdentity:
    try:
        with _open_source_nofollow(path, root, share_delete=False) as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise RecoverySafetyError(f"not a regular no-follow file: {path}")
            digest = hashlib.sha256()
            while chunk := stream.read(_HASH_CHUNK_SIZE):
                digest.update(chunk)
            after = os.fstat(stream.fileno())
    except (OSError, PreviewError) as error:
        raise RecoverySafetyError(f"cannot inspect file safely: {path}") from error
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if any(getattr(before, field) != getattr(after, field) for field in fields):
        raise RecoverySafetyError(f"file changed during verification: {path}")
    return _FileIdentity(
        int(after.st_dev),
        int(after.st_ino),
        int(after.st_size),
        int(after.st_mtime_ns),
        digest.hexdigest(),
    )


def _entry_matches_source(entry: PlanEntry) -> _FileIdentity:
    identity = _stable_hash(Path(entry.source_path), Path(entry.source_root))
    if identity.size != entry.expected_size or identity.sha256 != entry.expected_sha256:
        raise RecoverySafetyError("STALE_SOURCE")
    return identity


def _nearest_existing_directory(path: Path) -> Path:
    current = path
    while not current.exists():
        if current.parent == current:
            break
        current = current.parent
    if not current.is_dir():
        raise RecoverySafetyError(f"existing target anchor is not a directory: {current}")
    return current


@contextmanager
def _target_parent_binding(output_root: Path, target: Path) -> Iterator[_SecureDirectoryBinding]:
    if not _is_within(target, output_root):
        raise RecoverySafetyError("TARGET_OUTSIDE_OUTPUT")
    anchor = _nearest_existing_directory(output_root)
    try:
        output_parts = output_root.relative_to(anchor).parts
        parent_parts = target.parent.relative_to(output_root).parts
    except ValueError as error:
        raise RecoverySafetyError("TARGET_OUTSIDE_OUTPUT") from error
    try:
        with _secure_workspace_directory(anchor, tuple(output_parts + parent_parts)) as binding:
            yield binding
    except (OSError, PreviewError) as error:
        raise RecoverySafetyError("TARGET_PARENT_UNSAFE") from error


def _binding_path(binding: _SecureDirectoryBinding, name: str) -> Path:
    return binding.access_path / name


def _binding_stat(binding: _SecureDirectoryBinding, name: str) -> os.stat_result:
    if binding.directory_fd is not None:  # pragma: no cover - POSIX CI
        return os.stat(name, dir_fd=binding.directory_fd, follow_symlinks=False)
    return _binding_path(binding, name).lstat()


def _binding_unlink(binding: _SecureDirectoryBinding, name: str) -> None:
    if binding.directory_fd is not None:  # pragma: no cover - POSIX CI
        os.unlink(name, dir_fd=binding.directory_fd)
    else:
        os.unlink(_binding_path(binding, name))


def _identity_bound_unlink(path: Path, root: Path, expected: _FileIdentity) -> None:
    """Delete the opened file object, never a later replacement at the same name."""
    if not _is_within(path, root):
        raise RecoverySafetyError("identity-bound delete escaped owned root")
    if os.name != "nt":  # pragma: no cover - exercised on POSIX CI
        current = _stable_hash(path, root)
        if not _same_stat(current, expected) or current.sha256 != expected.sha256:
            raise RecoverySafetyError("owned file identity changed before cleanup")
        if _IDENTITY_DELETE_TEST_HOOK is not None:
            _IDENTITY_DELETE_TEST_HOOK(path)
        rebound = _stable_hash(path, root)
        if not _same_stat(rebound, expected) or rebound.sha256 != expected.sha256:
            raise RecoverySafetyError("owned file name changed before cleanup")
        path.unlink()
        return

    import ctypes
    import msvcrt
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
    handle = create_file(
        os.fspath(path),
        0x80000000 | 0x00010000,
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,
        0x00200000 | 0x08000000,
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_last_error(), f"cannot bind file for deletion: {path}")
    descriptor: int | None = None
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        length = kernel32.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
        if not length or length >= len(buffer):
            raise RecoverySafetyError("cannot verify deletion handle path")
        final_path = buffer.value
        if final_path.startswith("\\\\?\\UNC\\"):
            final_path = "\\\\" + final_path[8:]
        elif final_path.startswith("\\\\?\\"):
            final_path = final_path[4:]
        root_path = os.path.abspath(root)
        if os.path.commonpath((root_path, final_path)).casefold() != root_path.casefold():
            raise RecoverySafetyError("deletion handle escaped owned root")
        descriptor = msvcrt.open_osfhandle(
            handle,
            os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
        handle = None
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            before = os.fstat(stream.fileno())
            digest = hashlib.sha256()
            while chunk := stream.read(_HASH_CHUNK_SIZE):
                digest.update(chunk)
            after = os.fstat(stream.fileno())
            current = _FileIdentity(
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                digest.hexdigest(),
            )
            if (
                (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                or not _same_stat(current, expected)
                or current.sha256 != expected.sha256
            ):
                raise RecoverySafetyError("owned file changed while binding cleanup")
            if _IDENTITY_DELETE_TEST_HOOK is not None:
                _IDENTITY_DELETE_TEST_HOOK(path)
            try:
                named = path.lstat()
            except FileNotFoundError:
                return
            if (
                named.st_dev,
                named.st_ino,
                named.st_size,
                named.st_mtime_ns,
            ) != (expected.device, expected.inode, expected.size, expected.mtime_ns):
                return

            class _FileDispositionInfo(ctypes.Structure):
                _fields_ = (("delete_file", wintypes.BOOL),)

            disposition = _FileDispositionInfo(True)
            os_handle = msvcrt.get_osfhandle(stream.fileno())
            if not kernel32.SetFileInformationByHandle(
                os_handle,
                4,
                ctypes.byref(disposition),
                ctypes.sizeof(disposition),
            ):
                raise OSError(ctypes.get_last_error(), f"cannot delete bound file: {path}")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if handle is not None:
            kernel32.CloseHandle(handle)


def _unlink_owned_binding_file(
    binding: _SecureDirectoryBinding, name: str, expected: _FileIdentity
) -> None:
    _identity_bound_unlink(
        binding.logical_path / name,
        binding.logical_path,
        expected,
    )
    _fsync_binding(binding)


def _binding_link_no_overwrite(binding: _SecureDirectoryBinding, source: str, target: str) -> None:
    if binding.directory_fd is not None:  # pragma: no cover - POSIX CI
        os.link(
            source,
            target,
            src_dir_fd=binding.directory_fd,
            dst_dir_fd=binding.directory_fd,
            follow_symlinks=False,
        )
    else:
        os.link(_binding_path(binding, source), _binding_path(binding, target))


def _binding_link(binding: _SecureDirectoryBinding, source: str, target: str) -> None:
    _binding_link_no_overwrite(binding, source, target)


def _finalize_link(binding: _SecureDirectoryBinding, source: str, target: str) -> None:
    try:
        _binding_link(binding, source, target)
    except FileExistsError as error:
        raise RecoverySafetyError("TARGET_CONFLICT") from error
    except OSError as error:
        raise RecoverySafetyError("NO_OVERWRITE_LINK_UNSUPPORTED") from error


def _fsync_binding(binding: _SecureDirectoryBinding) -> None:
    if binding.directory_fd is not None:  # pragma: no cover - POSIX CI
        os.fsync(binding.directory_fd)
        return
    try:
        descriptor = os.open(binding.access_path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _new_binding_file(binding: _SecureDirectoryBinding, name: str):  # type: ignore[no-untyped-def]
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
        else os.open(_binding_path(binding, name), flags, 0o600)
    )
    return os.fdopen(descriptor, "wb")


def _quarantine_delete(
    path: Path,
    root: Path,
    expected: _FileIdentity,
    *,
    prefix: str,
) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise RecoverySafetyError("path is outside its owned root") from error
    with _secure_workspace_directory(root, relative.parent.parts, create=False) as binding:
        current = _binding_stat(binding, relative.name)
        current_stat = (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
        expected_stat = (expected.device, expected.inode, expected.size, expected.mtime_ns)
        if current_stat != expected_stat:
            raise RecoverySafetyError("file identity changed before deletion")
        quarantine = f".{prefix}-{uuid4().hex}"
        try:
            _binding_link_no_overwrite(binding, relative.name, quarantine)
        except OSError as error:
            raise RecoverySafetyError("QUARANTINE_UNSUPPORTED") from error
        _fsync_binding(binding)
        quarantined_path = binding.logical_path / quarantine
        bound = _stable_hash(quarantined_path, root)
        if not _same_stat(bound, expected) or bound.sha256 != expected.sha256:
            quarantine_state = _binding_stat(binding, quarantine)
            quarantine_stat = (
                quarantine_state.st_dev,
                quarantine_state.st_ino,
                quarantine_state.st_size,
                quarantine_state.st_mtime_ns,
            )
            if quarantine_stat == expected_stat:
                _identity_bound_unlink(quarantined_path, root, expected)
                _fsync_binding(binding)
            raise RecoverySafetyError("file changed while binding deletion")
        original = _stable_hash(path, root)
        original_state = _binding_stat(binding, relative.name)
        original_stat = (
            original_state.st_dev,
            original_state.st_ino,
            original_state.st_size,
            original_state.st_mtime_ns,
        )
        if (
            not _same_stat(original, expected)
            or original.sha256 != expected.sha256
            or original_stat != expected_stat
        ):
            _identity_bound_unlink(quarantined_path, root, expected)
            _fsync_binding(binding)
            raise RecoverySafetyError("file changed while binding deletion")
        _identity_bound_unlink(path, root, expected)
        _fsync_binding(binding)
        final = _stable_hash(quarantined_path, root)
        final_state = _binding_stat(binding, quarantine)
        final_stat = (
            final_state.st_dev,
            final_state.st_ino,
            final_state.st_size,
            final_state.st_mtime_ns,
        )
        if (
            not _same_stat(final, expected)
            or final.sha256 != expected.sha256
            or final_stat != expected_stat
        ):
            raise RecoverySafetyError("file changed while binding deletion")
        _identity_bound_unlink(quarantined_path, root, expected)
        _fsync_binding(binding)


def _default_process_liveness(pid: int, _token: str) -> str:
    if pid <= 0:
        return "UNKNOWN"
    if pid == os.getpid():
        return "LIVE"
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return "DEAD" if ctypes.get_last_error() == 87 else "UNKNOWN"
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return "UNKNOWN"
            return "LIVE" if exit_code.value == 259 else "DEAD"
        finally:
            kernel32.CloseHandle(handle)
    try:  # pragma: no cover - POSIX CI
        os.kill(pid, 0)
    except ProcessLookupError:
        return "DEAD"
    except (PermissionError, OSError):
        return "UNKNOWN"
    return "LIVE"


def _read_lock(workspace: Workspace) -> tuple[dict[str, object], _FileIdentity]:
    path = workspace.root / "state" / _LOCK_NAME
    identity = _stable_hash(path, workspace.root)
    try:
        with _open_source_nofollow(path, workspace.root, share_delete=False) as stream:
            payload = stream.read(4097)
        if len(payload) > 4096:
            raise ValueError
        owner = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, PreviewError) as error:
        raise WorkspaceLockedError("apply lock owner is unknown or malformed") from error
    if not isinstance(owner, dict):
        raise WorkspaceLockedError("apply lock owner is unknown or malformed")
    return owner, identity


def _lock_status(workspace: Workspace, process_liveness: ProcessLiveness) -> str:
    path = workspace.root / "state" / _LOCK_NAME
    if not path.exists():
        return "NONE"
    try:
        owner, _identity = _read_lock(workspace)
        pid = owner.get("pid")
        token = owner.get("token")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return "UNKNOWN"
        if not isinstance(token, str) or not token:
            return "UNKNOWN"
        status = process_liveness(pid, token)
        return (
            "STALE" if status == "DEAD" else status if status in {"LIVE", "UNKNOWN"} else "UNKNOWN"
        )
    except (OSError, WorkspaceLockedError):
        return "UNKNOWN"


@contextmanager
def workspace_mutation_lock(
    workspace: Workspace,
    *,
    process_liveness: ProcessLiveness = _default_process_liveness,
) -> Iterator[_MutationLease]:
    """Hold the workspace-wide mutation lock; reclaim only a proven-dead owner."""
    token = uuid4().hex
    payload = _canonical_json(
        {"pid": os.getpid(), "token": token, "workspace_root": str(workspace.root)}
    ).encode("utf-8")
    with _secure_workspace_directory(workspace.root, ("state",)) as binding:
        acquired: _FileIdentity | None = None
        for _attempt in range(2):
            created_stat: os.stat_result | None = None
            try:
                stream = _new_binding_file(binding, _LOCK_NAME)
                created_stat = os.fstat(stream.fileno())
                try:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                finally:
                    stream.close()
                _fsync_binding(binding)
                acquired = _stable_hash(binding.logical_path / _LOCK_NAME, workspace.root)
                break
            except FileExistsError:
                try:
                    owner, existing = _read_lock(workspace)
                    pid = owner.get("pid")
                    owner_token = owner.get("token")
                except WorkspaceLockedError:
                    raise
                if (
                    isinstance(pid, bool)
                    or not isinstance(pid, int)
                    or not isinstance(owner_token, str)
                    or not owner_token
                    or process_liveness(pid, owner_token) != "DEAD"
                ):
                    raise WorkspaceLockedError(
                        "workspace mutation lock is live or unprovable"
                    ) from None
                _quarantine_delete(
                    binding.logical_path / _LOCK_NAME,
                    workspace.root,
                    existing,
                    prefix="stale-apply-lock",
                )
            except BaseException:
                if created_stat is not None:
                    try:
                        current = _stable_hash(
                            binding.logical_path / _LOCK_NAME,
                            workspace.root,
                        )
                        if (current.device, current.inode) == (
                            created_stat.st_dev,
                            created_stat.st_ino,
                        ):
                            _unlink_owned_binding_file(binding, _LOCK_NAME, current)
                    except (OSError, RecoverySafetyError):
                        pass
                raise
        if acquired is None:
            raise WorkspaceLockedError("workspace mutation lock could not be acquired")
        lease = _MutationLease(token, acquired)
        try:
            yield lease
        finally:
            try:
                owner, current = _read_lock(workspace)
                if owner.get("token") == token and _same_stat(current, acquired):
                    _unlink_owned_binding_file(binding, _LOCK_NAME, current)
            except (OSError, WorkspaceLockedError, RecoverySafetyError):
                pass


def _transaction_contract(transaction_id: str) -> tuple[int, str]:
    match = _TRANSACTION_PATTERN.fullmatch(transaction_id)
    if match is None:
        raise RecoverySafetyError("transaction approval contract is invalid")
    return int(match.group(1)), match.group(2)


def _mutation_time_preflight(
    workspace: Workspace,
    report: PreflightReport,
    lease: _MutationLease,
) -> PreflightReport:
    lock_path = workspace.root / "state" / _LOCK_NAME

    def prove_lease() -> None:
        owner, current = _read_lock(workspace)
        if (
            owner.get("pid") != os.getpid()
            or owner.get("token") != lease.token
            or not _same_stat(current, lease.identity)
        ):
            raise WorkspaceLockedError("mutation lock ownership changed during preflight")

    prove_lease()
    current = preflight_plan(workspace, report.plan_id)
    prove_lease()
    own_lock = os.path.normcase(os.path.abspath(lock_path))
    filtered = tuple(
        issue
        for issue in current.issues
        if not (
            issue.code == "APPLY_LOCKED"
            and issue.path is not None
            and os.path.normcase(os.path.abspath(issue.path)) == own_lock
        )
    )
    current = replace(current, issues=filtered)
    if (
        current.approval_state != report.approval_state
        or current.approval_revision != report.approval_revision
        or current.approval_contract != report.approval_contract
        or current.payload_sha256 != report.payload_sha256
    ):
        raise ApprovalContractError("approval changed before mutation")
    if not current.ok:
        codes = ",".join(
            sorted({issue.code for issue in current.issues if issue.severity == "ERROR"})
        )
        raise RecoverySafetyError(f"MUTATION_PREFLIGHT_FAILED:{codes}")
    return current


def _require_transaction_approval(
    workspace: Workspace, transaction_id: str, plan: OrganizationPlan
) -> None:
    revision, contract = _transaction_contract(transaction_id)
    current = _approval_contract(
        plan.plan_id, plan.payload_sha256, plan.approval_state, plan.approval_revision
    )
    if (
        plan.approval_state != "APPROVED"
        or plan.approval_revision != revision
        or current != contract
    ):
        raise ApprovalContractError("transaction approval contract is no longer current")


def _journal_update(
    workspace: Workspace,
    journal_id: int,
    state: str,
    metadata: dict[str, object],
    *,
    target_sha256: str | None = None,
) -> None:
    with closing(connect_database(workspace.database_path)) as connection:
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            UPDATE operation_journal
            SET state=?,target_sha256=COALESCE(?,target_sha256),error=?,updated_at=?
            WHERE id=?
            """,
            (state, target_sha256, _canonical_json(metadata), _utc_now(), journal_id),
        )
        connection.commit()


def _transaction_update(workspace: Workspace, transaction_id: str, state: str) -> None:
    with closing(connect_database(workspace.database_path)) as connection:
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE operation_transaction SET state=?,updated_at=? WHERE transaction_id=?",
            (state, _utc_now(), transaction_id),
        )
        connection.commit()


def _create_transaction(
    workspace: Workspace, plan: OrganizationPlan, report: PreflightReport
) -> str:
    transaction_id = f"tx-{uuid4().hex}-r{report.approval_revision}-{report.approval_contract}"
    now = _utc_now()
    with closing(connect_database(workspace.database_path)) as connection:
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("BEGIN IMMEDIATE")
        authority = connection.execute(
            """
            SELECT p.payload_sha256,a.state,a.revision
            FROM organization_plan AS p
            JOIN plan_approval AS a ON a.plan_id=p.plan_id
            WHERE p.plan_id=?
            """,
            (plan.plan_id,),
        ).fetchone()
        if authority is None:
            raise ApprovalContractError("approval authority disappeared before transaction")
        current_payload = str(authority[0])
        current_state = str(authority[1])
        current_revision = int(authority[2])
        current_contract = _approval_contract(
            plan.plan_id,
            current_payload,
            current_state,
            current_revision,
        )
        if (
            current_state != "APPROVED"
            or current_revision != report.approval_revision
            or current_payload != report.payload_sha256
            or current_contract != report.approval_contract
        ):
            raise ApprovalContractError("approval changed before transaction creation")
        connection.execute(
            """
            INSERT INTO operation_transaction(
                transaction_id,plan_id,mode,state,created_at,updated_at
            ) VALUES (?,?,?,'ACTIVE',?,?)
            """,
            (transaction_id, plan.plan_id, plan.mode, now, now),
        )
        for entry in plan.entries:
            metadata = {
                "approval_contract": report.approval_contract,
                "approval_revision": report.approval_revision,
                "payload_sha256": report.payload_sha256,
                "position": entry.position,
                "target_owned": False,
            }
            connection.execute(
                """
                INSERT INTO operation_journal(
                    transaction_id,media_id,bundle_id,operation,source_path,target_path,
                    source_sha256,target_sha256,state,error,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,NULL,'PREPARED',?,?,?)
                """,
                (
                    transaction_id,
                    entry.media_id,
                    entry.bundle_id,
                    entry.action,
                    entry.source_path,
                    entry.target_path,
                    entry.expected_sha256,
                    _canonical_json(metadata),
                    now,
                    now,
                ),
            )
        connection.commit()
    return transaction_id


def _load_transaction(
    workspace: Workspace, transaction_id: str
) -> tuple[sqlite3.Row, list[sqlite3.Row]]:
    with closing(connect_database(workspace.database_path, read_only=True)) as connection:
        connection.row_factory = sqlite3.Row
        transaction = connection.execute(
            "SELECT * FROM operation_transaction WHERE transaction_id=?", (transaction_id,)
        ).fetchone()
        rows = connection.execute(
            "SELECT * FROM operation_journal WHERE transaction_id=? ORDER BY id",
            (transaction_id,),
        ).fetchall()
    if transaction is None:
        raise RecoverySafetyError(f"transaction was not found: {transaction_id}")
    return transaction, rows


def _entry_by_media(plan: OrganizationPlan) -> dict[int, PlanEntry]:
    return {entry.media_id: entry for entry in plan.entries}


def _partial_name(target: Path, transaction_id: str, journal_id: int) -> str:
    return f".{target.name}.{transaction_id[3:15]}.{journal_id}.partial"


def _source_quarantine_name(source: Path, transaction_id: str, journal_id: int) -> str:
    return f".{source.name}.spt-move-{transaction_id[3:15]}-{journal_id}.quarantine"


def _inject(fault_injector: FaultInjector | None, stage: str, media_id: int) -> None:
    if fault_injector is not None:
        fault_injector(stage, media_id)


def _verify_identity(path: Path, root: Path, expected: _FileIdentity, digest: str) -> _FileIdentity:
    current = _stable_hash(path, root)
    if not _same_stat(current, expected) or current.sha256 != digest:
        raise RecoverySafetyError(f"file identity or hash changed: {path}")
    return current


def _continue_move_source_delete(
    workspace: Workspace,
    plan: OrganizationPlan,
    entry: PlanEntry,
    transaction_id: str,
    journal_id: int,
    state: str,
    metadata: dict[str, object],
    final: _FileIdentity,
    fault_injector: FaultInjector | None,
) -> tuple[str, dict[str, object]]:
    source = Path(entry.source_path)
    source_root = Path(entry.source_root)
    target = Path(entry.target_path)
    output = Path(plan.output_root)
    quarantine_name = _source_quarantine_name(source, transaction_id, journal_id)
    quarantine = source.parent / quarantine_name
    expected_source = _identity_from_dict(metadata.get("source_delete_identity"))

    if state == "COPIED_VERIFIED":
        _inject(fault_injector, "BEFORE_SOURCE_DELETE", entry.media_id)
        _verify_identity(target, output, final, entry.expected_sha256)
        source_identity = _entry_matches_source(entry)
        _inject(fault_injector, "BEFORE_SOURCE_QUARANTINE", entry.media_id)
        _verify_identity(target, output, final, entry.expected_sha256)
        metadata["source_delete_identity"] = source_identity.to_dict()
        metadata["source_quarantine_path"] = str(quarantine)
        metadata["source_delete_phase"] = "PREPARED"
        _journal_update(
            workspace,
            journal_id,
            "SOURCE_DELETE_PREPARED",
            metadata,
            target_sha256=final.sha256,
        )
        state = "SOURCE_DELETE_PREPARED"
        expected_source = source_identity
        _inject(fault_injector, "AFTER_SOURCE_DELETE_PREPARED", entry.media_id)

    if expected_source is None:
        raise RecoverySafetyError("SOURCE_DELETE_OWNERSHIP_UNKNOWN")
    if str(metadata.get("source_quarantine_path")) != str(quarantine):
        raise RecoverySafetyError("SOURCE_DELETE_QUARANTINE_MISMATCH")

    try:
        relative = source.relative_to(source_root)
    except ValueError as error:
        raise RecoverySafetyError("SOURCE_DELETE_OUTSIDE_ROOT") from error
    with _secure_workspace_directory(source_root, relative.parent.parts, create=False) as binding:
        source_present = False
        quarantine_present = False
        try:
            _binding_stat(binding, source.name)
            source_present = True
        except FileNotFoundError:
            pass
        try:
            _binding_stat(binding, quarantine_name)
            quarantine_present = True
        except FileNotFoundError:
            pass

        if state == "SOURCE_DELETE_PREPARED":
            if source_present:
                _verify_identity(source, source_root, expected_source, entry.expected_sha256)
            if quarantine_present:
                _verify_identity(quarantine, source_root, expected_source, entry.expected_sha256)
            if source_present and not quarantine_present:
                try:
                    _binding_link(binding, source.name, quarantine_name)
                except OSError as error:
                    raise RecoverySafetyError("SOURCE_QUARANTINE_UNSUPPORTED") from error
                _fsync_binding(binding)
                _verify_identity(quarantine, source_root, expected_source, entry.expected_sha256)
                quarantine_present = True
            if source_present and quarantine_present:
                _verify_identity(source, source_root, expected_source, entry.expected_sha256)
                source_state = _binding_stat(binding, source.name)
                if (
                    source_state.st_dev,
                    source_state.st_ino,
                    source_state.st_size,
                    source_state.st_mtime_ns,
                ) != (
                    expected_source.device,
                    expected_source.inode,
                    expected_source.size,
                    expected_source.mtime_ns,
                ):
                    raise RecoverySafetyError("SOURCE_CHANGED_BEFORE_QUARANTINE")
                _verify_identity(target, output, final, entry.expected_sha256)
                _identity_bound_unlink(source, source_root, expected_source)
                _fsync_binding(binding)
                source_present = False
            if source_present or not quarantine_present:
                raise RecoverySafetyError("SOURCE_QUARANTINE_STATE_UNPROVABLE")
            metadata["source_delete_phase"] = "QUARANTINED"
            _journal_update(
                workspace,
                journal_id,
                "SOURCE_QUARANTINED",
                metadata,
                target_sha256=final.sha256,
            )
            state = "SOURCE_QUARANTINED"
            _inject(fault_injector, "AFTER_SOURCE_DELETE_RENAME", entry.media_id)

        if state == "SOURCE_QUARANTINED":
            if source_present:
                raise RecoverySafetyError("SOURCE_REAPPEARED_DURING_DELETE")
            if quarantine_present:
                current = _verify_identity(
                    quarantine, source_root, expected_source, entry.expected_sha256
                )
                quarantine_stat = _binding_stat(binding, quarantine_name)
                if (
                    quarantine_stat.st_dev,
                    quarantine_stat.st_ino,
                    quarantine_stat.st_size,
                    quarantine_stat.st_mtime_ns,
                ) != (current.device, current.inode, current.size, current.mtime_ns):
                    raise RecoverySafetyError("SOURCE_QUARANTINE_CHANGED")
                _identity_bound_unlink(
                    quarantine,
                    source_root,
                    expected_source,
                )
                _fsync_binding(binding)
                quarantine_present = False
                _inject(fault_injector, "AFTER_SOURCE_DELETE_UNLINK", entry.media_id)
            metadata["source_delete_phase"] = "UNLINKED"
            _journal_update(
                workspace,
                journal_id,
                "SOURCE_DELETE_UNLINKED",
                metadata,
                target_sha256=final.sha256,
            )
            state = "SOURCE_DELETE_UNLINKED"

        if source_present or quarantine_present:
            raise RecoverySafetyError("SOURCE_DELETE_UNLINKED_STATE_MISMATCH")
        metadata["source_removed_identity"] = expected_source.to_dict()
        _journal_update(
            workspace,
            journal_id,
            "SOURCE_REMOVED",
            metadata,
            target_sha256=final.sha256,
        )
        state = "SOURCE_REMOVED"
        _inject(fault_injector, "AFTER_SOURCE_DELETE", entry.media_id)
    return state, metadata


def _copy_entry_to_target(
    workspace: Workspace,
    plan: OrganizationPlan,
    entry: PlanEntry,
    row: sqlite3.Row,
    transaction_id: str,
    fault_injector: FaultInjector | None,
) -> tuple[_FileIdentity, dict[str, object]]:
    source = Path(entry.source_path)
    target = Path(entry.target_path)
    output = Path(plan.output_root)
    journal_id = int(row["id"])
    metadata = _metadata(row["error"])
    partial_name = _partial_name(target, transaction_id, journal_id)

    source_identity = _entry_matches_source(entry)
    metadata["source_identity"] = source_identity.to_dict()
    with _target_parent_binding(output, target) as binding:
        target_path = binding.logical_path / target.name
        try:
            existing = _stable_hash(target_path, output)
        except RecoverySafetyError:
            existing = None
        if target_path.exists():
            if existing is not None and existing.sha256 == entry.expected_sha256:
                metadata["target_owned"] = False
                metadata["target_preexisting"] = True
                metadata["target_identity"] = existing.to_dict()
                _journal_update(
                    workspace,
                    journal_id,
                    "ALREADY_PRESENT" if entry.action == "COPY" else "COPIED_VERIFIED",
                    metadata,
                    target_sha256=existing.sha256,
                )
                return existing, metadata
            raise RecoverySafetyError("TARGET_CONFLICT")

        _inject(fault_injector, "BEFORE_PARTIAL_COPY", entry.media_id)
        digest = hashlib.sha256()
        with (
            _open_source_nofollow(
                source, Path(entry.source_root), share_delete=False
            ) as input_stream,
            _new_binding_file(binding, partial_name) as output_stream,
        ):
            before = os.fstat(input_stream.fileno())
            while chunk := input_stream.read(_HASH_CHUNK_SIZE):
                digest.update(chunk)
                output_stream.write(chunk)
            output_stream.flush()
            os.fsync(output_stream.fileno())
            after = os.fstat(input_stream.fileno())
        _fsync_binding(binding)
        stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
        if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
            raise RecoverySafetyError("STALE_SOURCE")
        if after.st_size != entry.expected_size or digest.hexdigest() != entry.expected_sha256:
            raise RecoverySafetyError("STALE_SOURCE")
        partial_path = binding.logical_path / partial_name
        partial_identity = _stable_hash(partial_path, output)
        metadata["partial_path"] = str(partial_path)
        metadata["partial_identity"] = partial_identity.to_dict()
        _journal_update(workspace, journal_id, "PARTIAL_COPIED", metadata)
        _inject(fault_injector, "AFTER_PARTIAL_COPY", entry.media_id)

        verified = _stable_hash(partial_path, output)
        if not _same_stat(verified, partial_identity) or verified.sha256 != entry.expected_sha256:
            raise RecoverySafetyError("PARTIAL_HASH_MISMATCH")
        metadata["partial_identity"] = verified.to_dict()
        _journal_update(
            workspace,
            journal_id,
            "PARTIAL_VERIFIED",
            metadata,
            target_sha256=verified.sha256,
        )
        _inject(fault_injector, "AFTER_TARGET_VERIFY", entry.media_id)
        _inject(fault_injector, "BEFORE_FINALIZE", entry.media_id)
        _journal_update(
            workspace,
            journal_id,
            "FINALIZING",
            metadata,
            target_sha256=verified.sha256,
        )
        _finalize_link(binding, partial_name, target.name)
        _fsync_binding(binding)
        final = _stable_hash(target_path, output)
        if final.sha256 != entry.expected_sha256 or final.size != entry.expected_size:
            raise RecoverySafetyError("TARGET_HASH_MISMATCH")
        if (final.device, final.inode) != (verified.device, verified.inode):
            raise RecoverySafetyError("TARGET_IDENTITY_MISMATCH")
        metadata["target_owned"] = True
        metadata["target_identity"] = final.to_dict()
        _journal_update(
            workspace,
            journal_id,
            "COPIED_VERIFIED",
            metadata,
            target_sha256=final.sha256,
        )
        _inject(fault_injector, "AFTER_COPIED_VERIFIED", entry.media_id)
        _quarantine_delete(partial_path, output, verified, prefix="verified-partial")
        metadata.pop("partial_path", None)
        metadata.pop("partial_identity", None)
        _journal_update(
            workspace,
            journal_id,
            "COPIED_VERIFIED",
            metadata,
            target_sha256=final.sha256,
        )
        _inject(fault_injector, "AFTER_FINALIZE", entry.media_id)
        return final, metadata


def _resume_partial(
    workspace: Workspace,
    plan: OrganizationPlan,
    entry: PlanEntry,
    row: sqlite3.Row,
    transaction_id: str,
    fault_injector: FaultInjector | None,
) -> tuple[_FileIdentity, dict[str, object]]:
    metadata = _metadata(row["error"])
    target = Path(entry.target_path)
    output = Path(plan.output_root)
    journal_id = int(row["id"])
    partial_value = metadata.get("partial_path")
    partial_expected = _identity_from_dict(metadata.get("partial_identity"))
    if not isinstance(partial_value, str) or partial_expected is None:
        raise RecoverySafetyError("PARTIAL_OWNERSHIP_UNKNOWN")
    partial = Path(partial_value)
    expected_name = _partial_name(target, transaction_id, journal_id)
    if partial.name != expected_name or not _is_within(partial, output):
        raise RecoverySafetyError("PARTIAL_OWNERSHIP_UNKNOWN")
    current = _stable_hash(partial, output)
    if not _same_stat(current, partial_expected) or current.sha256 != entry.expected_sha256:
        raise RecoverySafetyError("PARTIAL_HASH_MISMATCH")
    if str(row["state"]) == "PARTIAL_COPIED":
        _journal_update(
            workspace,
            journal_id,
            "PARTIAL_VERIFIED",
            metadata,
            target_sha256=current.sha256,
        )
        _inject(fault_injector, "AFTER_TARGET_VERIFY", entry.media_id)
    with _target_parent_binding(output, target) as binding:
        target_path = binding.logical_path / target.name
        if target_path.exists():
            if str(row["state"]) != "FINALIZING":
                raise RecoverySafetyError("TARGET_CONFLICT")
            final = _stable_hash(target_path, output)
            if final.sha256 != entry.expected_sha256 or (final.device, final.inode) != (
                current.device,
                current.inode,
            ):
                raise RecoverySafetyError("TARGET_CONFLICT")
        else:
            _inject(fault_injector, "BEFORE_FINALIZE", entry.media_id)
            _journal_update(
                workspace,
                journal_id,
                "FINALIZING",
                metadata,
                target_sha256=current.sha256,
            )
            _finalize_link(binding, partial.name, target.name)
            _fsync_binding(binding)
            final = _stable_hash(target_path, output)
        metadata["target_owned"] = True
        metadata["target_identity"] = final.to_dict()
        _journal_update(
            workspace,
            journal_id,
            "COPIED_VERIFIED",
            metadata,
            target_sha256=final.sha256,
        )
        _inject(fault_injector, "AFTER_COPIED_VERIFIED", entry.media_id)
        _quarantine_delete(partial, output, current, prefix="verified-partial")
        metadata.pop("partial_path", None)
        metadata.pop("partial_identity", None)
        _journal_update(
            workspace,
            journal_id,
            "COPIED_VERIFIED",
            metadata,
            target_sha256=final.sha256,
        )
        _inject(fault_injector, "AFTER_FINALIZE", entry.media_id)
        return final, metadata


def _finish_entry(
    workspace: Workspace,
    plan: OrganizationPlan,
    entry: PlanEntry,
    row: sqlite3.Row,
    transaction_id: str,
    fault_injector: FaultInjector | None,
) -> None:
    state = str(row["state"])
    metadata = _metadata(row["error"])
    journal_id = int(row["id"])
    if state in {"DONE", "ALREADY_PRESENT", "ROLLED_BACK"}:
        return
    if state == "FAILED":
        delete_states = {
            "PREPARED": "SOURCE_DELETE_PREPARED",
            "QUARANTINED": "SOURCE_QUARANTINED",
            "UNLINKED": "SOURCE_DELETE_UNLINKED",
        }
        delete_state = delete_states.get(str(metadata.get("source_delete_phase")))
        if entry.action == "MOVE" and delete_state is not None:
            metadata.pop("error_code", None)
            state = delete_state
        else:
            partial = metadata.get("partial_path")
            if isinstance(partial, str) and Path(partial).exists():
                raise RecoverySafetyError(
                    str(metadata.get("error_code") or "PARTIAL_REQUIRES_REVIEW")
                )
            metadata.pop("error_code", None)
            _journal_update(workspace, journal_id, "PREPARED", metadata)
            with closing(connect_database(workspace.database_path, read_only=True)) as connection:
                connection.row_factory = sqlite3.Row
                refreshed = connection.execute(
                    "SELECT * FROM operation_journal WHERE id=?", (journal_id,)
                ).fetchone()
            if refreshed is None:
                raise RecoverySafetyError("journal row disappeared during retry")
            row = refreshed
            state = "PREPARED"
    if state == "PREPARED":
        final, metadata = _copy_entry_to_target(
            workspace, plan, entry, row, transaction_id, fault_injector
        )
        state = (
            "ALREADY_PRESENT"
            if entry.action == "COPY" and not bool(metadata.get("target_owned"))
            else "COPIED_VERIFIED"
        )
        if state == "ALREADY_PRESENT":
            return
    elif state in {"PARTIAL_COPIED", "PARTIAL_VERIFIED", "FINALIZING"}:
        final, metadata = _resume_partial(
            workspace, plan, entry, row, transaction_id, fault_injector
        )
        state = "COPIED_VERIFIED"
    elif state in {
        "COPIED_VERIFIED",
        "SOURCE_DELETE_PREPARED",
        "SOURCE_QUARANTINED",
        "SOURCE_DELETE_UNLINKED",
        "SOURCE_REMOVED",
    }:
        if state == "COPIED_VERIFIED" and isinstance(metadata.get("partial_path"), str):
            partial = Path(str(metadata["partial_path"]))
            expected_partial = _identity_from_dict(metadata.get("partial_identity"))
            if expected_partial is None:
                raise RecoverySafetyError("PARTIAL_OWNERSHIP_UNKNOWN")
            current_partial = _stable_hash(partial, Path(plan.output_root))
            if (
                not _same_stat(current_partial, expected_partial)
                or current_partial.sha256 != entry.expected_sha256
            ):
                raise RecoverySafetyError("PARTIAL_HASH_MISMATCH")
            _quarantine_delete(
                partial,
                Path(plan.output_root),
                current_partial,
                prefix="verified-partial-resume",
            )
            metadata.pop("partial_path", None)
            metadata.pop("partial_identity", None)
            _journal_update(
                workspace,
                journal_id,
                "COPIED_VERIFIED",
                metadata,
                target_sha256=entry.expected_sha256,
            )
        final_identity = _identity_from_dict(metadata.get("target_identity"))
        if final_identity is None:
            raise RecoverySafetyError("TARGET_OWNERSHIP_UNKNOWN")
        current = _stable_hash(Path(entry.target_path), Path(plan.output_root))
        if not _same_stat(current, final_identity) or current.sha256 != entry.expected_sha256:
            raise RecoverySafetyError("TARGET_HASH_MISMATCH")
        final = current
    else:
        raise RecoverySafetyError(f"unsupported journal state: {state}")

    if entry.action == "MOVE" and state != "SOURCE_REMOVED":
        state, metadata = _continue_move_source_delete(
            workspace,
            plan,
            entry,
            transaction_id,
            journal_id,
            state,
            metadata,
            final,
            fault_injector,
        )
    _inject(fault_injector, "BEFORE_DONE", entry.media_id)
    _journal_update(
        workspace,
        journal_id,
        "DONE",
        metadata,
        target_sha256=entry.expected_sha256,
    )


def _mark_failed(workspace: Workspace, row: sqlite3.Row, error: Exception) -> None:
    journal_id = int(row["id"])
    with closing(connect_database(workspace.database_path, read_only=True)) as connection:
        current = connection.execute(
            "SELECT error FROM operation_journal WHERE id=?", (journal_id,)
        ).fetchone()
    metadata = _metadata(current[0] if current is not None else row["error"])
    code = str(error) if isinstance(error, RecoverySafetyError) else type(error).__name__.upper()
    metadata["error_code"] = code[:120]
    _journal_update(workspace, journal_id, "FAILED", metadata)


def _result(workspace: Workspace, transaction_id: str, plan_id: str) -> ExecutionResult:
    transaction, rows = _load_transaction(workspace, transaction_id)
    states = [str(row["state"]) for row in rows]
    return ExecutionResult(
        transaction_id=transaction_id,
        plan_id=plan_id,
        state=str(transaction["state"]),
        done_count=sum(state == "DONE" for state in states),
        failed_count=sum(state in {"FAILED", "ROLLBACK_FAILED"} for state in states),
        already_present_count=sum(state == "ALREADY_PRESENT" for state in states),
    )


def _run_transaction(
    workspace: Workspace,
    plan: OrganizationPlan,
    transaction_id: str,
    fault_injector: FaultInjector | None,
) -> ExecutionResult:
    transaction, rows = _load_transaction(workspace, transaction_id)
    entries = _entry_by_media(plan)
    for row in rows:
        entry = entries.get(int(row["media_id"]))
        if entry is None:
            _mark_failed(workspace, row, RecoverySafetyError("JOURNAL_PLAN_MISMATCH"))
            continue
        if (
            str(row["source_path"]) != entry.source_path
            or str(row["target_path"]) != entry.target_path
        ):
            _mark_failed(workspace, row, RecoverySafetyError("JOURNAL_PLAN_MISMATCH"))
            continue
        try:
            _finish_entry(workspace, plan, entry, row, transaction_id, fault_injector)
        except Exception as error:
            _mark_failed(workspace, row, error)
    _transaction, final_rows = _load_transaction(workspace, transaction_id)
    states = {str(row["state"]) for row in final_rows}
    final_state = "DONE" if states <= {"DONE", "ALREADY_PRESENT"} else "PARTIAL"
    _transaction_update(workspace, transaction_id, final_state)
    return _result(workspace, transaction_id, plan.plan_id)


def apply_plan(
    workspace: Workspace,
    report: PreflightReport,
    *,
    dry_run: bool = True,
    fault_injector: FaultInjector | None = None,
    process_liveness: ProcessLiveness = _default_process_liveness,
) -> ExecutionResult:
    """Apply the exact approved preflight contract. Dry-run is the default."""
    if not report.ok or report.approval_state != "APPROVED":
        raise ApprovalContractError("a successful approved preflight report is required")
    if not preflight_approval_is_current(workspace, report):
        raise ApprovalContractError("preflight approval contract is stale")
    if dry_run:
        return ExecutionResult("", report.plan_id, "DRY_RUN", 0, 0, 0)
    with workspace_mutation_lock(workspace, process_liveness=process_liveness) as lease:
        if not preflight_approval_is_current(workspace, report):
            raise ApprovalContractError("approval changed before mutation")
        try:
            _mutation_time_preflight(workspace, report, lease)
        except RecoverySafetyError as error:
            if "INCOMPLETE_TRANSACTION" in str(error):
                with closing(
                    connect_database(workspace.database_path, read_only=True)
                ) as connection:
                    prior = connection.execute(
                        """
                        SELECT transaction_id FROM operation_transaction
                        WHERE plan_id=? AND state NOT IN ('DONE','ROLLED_BACK')
                        ORDER BY created_at LIMIT 1
                        """,
                        (report.plan_id,),
                    ).fetchone()
                if prior is not None:
                    raise RecoverySafetyError(
                        f"incomplete transaction {prior[0]} already exists; use resume"
                    ) from error
            raise
        plan = inspect_plan(workspace, report.plan_id)
        with closing(connect_database(workspace.database_path, read_only=True)) as connection:
            prior_done = connection.execute(
                """
                SELECT transaction_id FROM operation_transaction
                WHERE plan_id=? AND state='DONE'
                ORDER BY created_at LIMIT 1
                """,
                (plan.plan_id,),
            ).fetchone()
        if prior_done is not None:
            return _result(workspace, str(prior_done[0]), plan.plan_id)
        transaction_id = _create_transaction(workspace, plan, report)
        for entry in plan.entries:
            _inject(fault_injector, "AFTER_PREPARED", entry.media_id)
        return _run_transaction(workspace, plan, transaction_id, fault_injector)


def resume_transaction(
    workspace: Workspace,
    transaction_id: str,
    *,
    fault_injector: FaultInjector | None = None,
    process_liveness: ProcessLiveness = _default_process_liveness,
) -> ExecutionResult:
    """Resume a durable incomplete transaction without regenerating names or authority."""
    with workspace_mutation_lock(workspace, process_liveness=process_liveness):
        transaction, _rows = _load_transaction(workspace, transaction_id)
        if str(transaction["state"]) == "ROLLED_BACK":
            raise RecoverySafetyError("rolled-back transaction cannot be resumed")
        if str(transaction["state"]) == "DONE":
            return _result(workspace, transaction_id, str(transaction["plan_id"]))
        plan = inspect_plan(workspace, str(transaction["plan_id"]))
        _require_transaction_approval(workspace, transaction_id, plan)
        return _run_transaction(workspace, plan, transaction_id, fault_injector)


def _copy_restore(
    source: Path, source_root: Path, target: Path, target_root: Path, expected: str
) -> None:
    try:
        relative = target.relative_to(target_root)
    except ValueError as error:
        raise RecoverySafetyError("restore target escaped source root") from error
    with _secure_workspace_directory(target_root, relative.parent.parts, create=False) as binding:
        if target.exists():
            current = _stable_hash(target, target_root)
            if current.sha256 != expected:
                raise RecoverySafetyError("RESTORE_SOURCE_CONFLICT")
            return
        partial_name = f".{target.name}.spt-rollback-{uuid4().hex}.partial"
        digest = hashlib.sha256()
        with (
            _open_source_nofollow(source, source_root, share_delete=False) as input_stream,
            _new_binding_file(binding, partial_name) as output_stream,
        ):
            while chunk := input_stream.read(_HASH_CHUNK_SIZE):
                digest.update(chunk)
                output_stream.write(chunk)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        _fsync_binding(binding)
        partial = binding.logical_path / partial_name
        identity = _stable_hash(partial, target_root)
        if digest.hexdigest() != expected or identity.sha256 != expected:
            raise RecoverySafetyError("RESTORE_VERIFY_FAILED")
        try:
            _binding_link(binding, partial_name, target.name)
        except OSError as error:
            raise RecoverySafetyError("SOURCE_RESTORE_UNSUPPORTED") from error
        _fsync_binding(binding)
        restored = _stable_hash(target, target_root)
        if restored.sha256 != expected:
            raise RecoverySafetyError("RESTORE_VERIFY_FAILED")
        _quarantine_delete(partial, target_root, identity, prefix="rollback-partial")


def _path_present_nofollow(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _source_delete_paths(
    entry: PlanEntry,
    transaction_id: str,
    journal_id: int,
    metadata: dict[str, object],
) -> tuple[Path, Path, _FileIdentity] | None:
    phase = metadata.get("source_delete_phase")
    if phase not in {"PREPARED", "QUARANTINED", "UNLINKED"}:
        return None
    source = Path(entry.source_path)
    quarantine = source.parent / _source_quarantine_name(source, transaction_id, journal_id)
    identity = _identity_from_dict(metadata.get("source_delete_identity"))
    if identity is None or str(metadata.get("source_quarantine_path")) != str(quarantine):
        raise RecoverySafetyError("SOURCE_DELETE_OWNERSHIP_UNKNOWN")
    return source, quarantine, identity


def _restore_source_quarantine(
    entry: PlanEntry,
    transaction_id: str,
    journal_id: int,
    metadata: dict[str, object],
) -> None:
    delete_paths = _source_delete_paths(entry, transaction_id, journal_id, metadata)
    if delete_paths is None:
        return
    source, quarantine, identity = delete_paths
    source_root = Path(entry.source_root)
    relative = source.relative_to(source_root)
    with _secure_workspace_directory(source_root, relative.parent.parts, create=False) as binding:
        source_present = _path_present_nofollow(source)
        quarantine_present = _path_present_nofollow(quarantine)
        if source_present:
            current_source = _stable_hash(source, source_root)
            if current_source.sha256 != entry.expected_sha256:
                raise RecoverySafetyError("RESTORE_SOURCE_CONFLICT")
        if quarantine_present:
            _verify_identity(quarantine, source_root, identity, entry.expected_sha256)
        if not source_present and quarantine_present:
            try:
                _binding_link(binding, quarantine.name, source.name)
            except OSError as error:
                raise RecoverySafetyError("SOURCE_RESTORE_UNSUPPORTED") from error
            _fsync_binding(binding)
            _verify_identity(source, source_root, identity, entry.expected_sha256)
            source_present = True
        if source_present and quarantine_present:
            quarantine_identity = _stable_hash(quarantine, source_root)
            _quarantine_delete(
                quarantine,
                source_root,
                quarantine_identity,
                prefix=f"rollback-source-quarantine-{journal_id}",
            )


def _rollback_row_problem(
    plan: OrganizationPlan,
    entry: PlanEntry,
    row: sqlite3.Row,
    metadata: dict[str, object],
) -> str | None:
    try:
        target = Path(entry.target_path)
        output = Path(plan.output_root)
        state = str(row["state"])
        if state in {"ROLLED_BACK", "ALREADY_PRESENT"}:
            return None
        if not _is_within(target, output):
            raise RecoverySafetyError("TARGET_OUTSIDE_OUTPUT")
        delete_paths = _source_delete_paths(
            entry,
            str(row["transaction_id"]),
            int(row["id"]),
            metadata,
        )
        source_present = _path_present_nofollow(Path(entry.source_path))
        quarantine_present = False
        if delete_paths is not None:
            source, quarantine, source_identity = delete_paths
            quarantine_present = _path_present_nofollow(quarantine)
            if source_present:
                _verify_identity(
                    source,
                    Path(entry.source_root),
                    source_identity,
                    entry.expected_sha256,
                )
            if quarantine_present:
                _verify_identity(
                    quarantine,
                    Path(entry.source_root),
                    source_identity,
                    entry.expected_sha256,
                )
        if bool(metadata.get("target_preexisting")):
            if source_present or quarantine_present:
                return None
            expected_target = _identity_from_dict(metadata.get("target_identity"))
            current_target = _stable_hash(target, output)
            if (
                expected_target is None
                or not _same_stat(current_target, expected_target)
                or current_target.sha256 != entry.expected_sha256
            ):
                raise RecoverySafetyError("PREEXISTING_TARGET_CHANGED")
            return None
        if bool(metadata.get("target_owned")):
            expected_target = _identity_from_dict(metadata.get("target_identity"))
            current_target = _stable_hash(target, output)
            if (
                expected_target is None
                or not _same_stat(current_target, expected_target)
                or current_target.sha256 != entry.expected_sha256
            ):
                raise RecoverySafetyError("TARGET_MODIFIED")
            if entry.action == "MOVE" and source_present and delete_paths is None:
                _entry_matches_source(entry)
            return None
        if _path_present_nofollow(target):
            raise RecoverySafetyError("TARGET_NOT_OWNED")
        partial_value = metadata.get("partial_path")
        if isinstance(partial_value, str):
            partial = Path(partial_value)
            partial_identity = _identity_from_dict(metadata.get("partial_identity"))
            if not _is_within(partial, output) or partial_identity is None:
                raise RecoverySafetyError("PARTIAL_OWNERSHIP_UNKNOWN")
            if _path_present_nofollow(partial):
                _verify_identity(partial, output, partial_identity, entry.expected_sha256)
        return None
    except (OSError, RecoverySafetyError, ValueError) as error:
        return str(error) or type(error).__name__


@dataclass(frozen=True, slots=True)
class _RollbackStage:
    original: Path
    root: Path
    staged: Path
    identity: _FileIdentity


def _stage_rollback_path(
    path: Path,
    root: Path,
    expected: _FileIdentity,
    transaction_id: str,
    journal_id: int,
) -> _RollbackStage:
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise RecoverySafetyError("ROLLBACK_STAGE_OUTSIDE_ROOT") from error
    current = _stable_hash(path, root)
    if not _same_stat(current, expected) or current.sha256 != expected.sha256:
        raise RecoverySafetyError("ROLLBACK_STAGE_IDENTITY_CHANGED")
    stage_name = f".{path.name}.spt-rollback-{transaction_id[3:15]}-{journal_id}.stage"
    with _secure_workspace_directory(root, relative.parent.parts, create=False) as binding:
        try:
            _binding_link_no_overwrite(binding, relative.name, stage_name)
        except OSError as error:
            raise RecoverySafetyError("ROLLBACK_STAGE_UNSUPPORTED") from error
        _fsync_binding(binding)
        staged = binding.logical_path / stage_name
        staged_identity = _stable_hash(staged, root)
        if not _same_stat(staged_identity, expected) or staged_identity.sha256 != expected.sha256:
            raise RecoverySafetyError("ROLLBACK_STAGE_VERIFY_FAILED")
        _identity_bound_unlink(path, root, expected)
        _fsync_binding(binding)
    return _RollbackStage(path, root, staged, staged_identity)


def _restore_rollback_stage(stage: _RollbackStage) -> None:
    if _path_present_nofollow(stage.original):
        current = _stable_hash(stage.original, stage.root)
        if not _same_stat(current, stage.identity) or current.sha256 != stage.identity.sha256:
            raise RecoverySafetyError("ROLLBACK_STAGE_RESTORE_CONFLICT")
    else:
        relative = stage.original.relative_to(stage.root)
        with _secure_workspace_directory(
            stage.root,
            relative.parent.parts,
            create=False,
        ) as binding:
            try:
                _binding_link_no_overwrite(binding, stage.staged.name, relative.name)
            except OSError as error:
                raise RecoverySafetyError("ROLLBACK_STAGE_RESTORE_UNSUPPORTED") from error
            _fsync_binding(binding)
            restored = _stable_hash(stage.original, stage.root)
            if not _same_stat(restored, stage.identity) or restored.sha256 != stage.identity.sha256:
                raise RecoverySafetyError("ROLLBACK_STAGE_RESTORE_FAILED")
    _identity_bound_unlink(stage.staged, stage.root, stage.identity)


def _bulk_journal_state(
    workspace: Workspace,
    rows: list[sqlite3.Row],
    state: str,
    error_code: str | None = None,
) -> None:
    with closing(connect_database(workspace.database_path)) as connection:
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("BEGIN IMMEDIATE")
        for row in rows:
            metadata = _metadata(row["error"])
            if error_code is None:
                metadata["target_owned"] = False
                metadata.pop("partial_path", None)
                metadata.pop("partial_identity", None)
                metadata.pop("error_code", None)
            else:
                metadata["error_code"] = error_code[:120]
            connection.execute(
                "UPDATE operation_journal SET state=?,error=?,updated_at=? WHERE id=?",
                (state, _canonical_json(metadata), _utc_now(), int(row["id"])),
            )
        connection.commit()


def _rollback_bundle_atomically(
    workspace: Workspace,
    plan: OrganizationPlan,
    transaction_id: str,
    rows: list[sqlite3.Row],
    entries: dict[int, PlanEntry],
    fault_injector: FaultInjector | None,
) -> None:
    stages: list[_RollbackStage] = []
    stages_by_journal: dict[int, _RollbackStage] = {}
    created_sources: list[tuple[Path, Path, _FileIdentity]] = []
    quarantines: list[tuple[Path, Path, _FileIdentity]] = []
    try:
        for row in reversed(rows):
            entry = entries[int(row["media_id"])]
            metadata = _metadata(row["error"])
            journal_id = int(row["id"])
            if bool(metadata.get("target_owned")):
                identity = _identity_from_dict(metadata.get("target_identity"))
                if identity is None:
                    raise RecoverySafetyError("TARGET_OWNERSHIP_UNKNOWN")
                stage = _stage_rollback_path(
                    Path(entry.target_path),
                    Path(plan.output_root),
                    identity,
                    transaction_id,
                    journal_id,
                )
                stages.append(stage)
                stages_by_journal[journal_id] = stage
                _inject(
                    fault_injector,
                    "AFTER_BUNDLE_ROLLBACK_STAGE",
                    entry.media_id,
                )
            else:
                partial_value = metadata.get("partial_path")
                partial_identity = _identity_from_dict(metadata.get("partial_identity"))
                if isinstance(partial_value, str) and partial_identity is not None:
                    partial = Path(partial_value)
                    if _path_present_nofollow(partial):
                        stage = _stage_rollback_path(
                            partial,
                            Path(plan.output_root),
                            partial_identity,
                            transaction_id,
                            journal_id,
                        )
                        stages.append(stage)
                        stages_by_journal[journal_id] = stage
                        _inject(
                            fault_injector,
                            "AFTER_BUNDLE_ROLLBACK_STAGE",
                            entry.media_id,
                        )

        for row in rows:
            entry = entries[int(row["media_id"])]
            metadata = _metadata(row["error"])
            if entry.action != "MOVE":
                continue
            source = Path(entry.source_path)
            source_root = Path(entry.source_root)
            delete_paths = _source_delete_paths(
                entry,
                transaction_id,
                int(row["id"]),
                metadata,
            )
            quarantine: Path | None = None
            source_identity: _FileIdentity | None = None
            if delete_paths is not None:
                _source, quarantine, source_identity = delete_paths
                if _path_present_nofollow(quarantine):
                    _verify_identity(
                        quarantine,
                        source_root,
                        source_identity,
                        entry.expected_sha256,
                    )
                    quarantines.append((quarantine, source_root, source_identity))
            if _path_present_nofollow(source):
                current_source = _stable_hash(source, source_root)
                if current_source.sha256 != entry.expected_sha256:
                    raise RecoverySafetyError("RESTORE_SOURCE_CONFLICT")
                continue
            stage = stages_by_journal.get(int(row["id"]))
            if quarantine is not None and _path_present_nofollow(quarantine):
                restore_from = quarantine
                restore_root = source_root
            elif stage is not None and bool(metadata.get("target_owned")):
                restore_from = stage.staged
                restore_root = stage.root
            elif bool(metadata.get("target_preexisting")):
                restore_from = Path(entry.target_path)
                restore_root = Path(plan.output_root)
            else:
                raise RecoverySafetyError("RESTORE_SOURCE_BYTES_UNAVAILABLE")
            _copy_restore(
                restore_from,
                restore_root,
                source,
                source_root,
                entry.expected_sha256,
            )
            created_sources.append((source, source_root, _stable_hash(source, source_root)))

        for stage in stages:
            if _path_present_nofollow(stage.original):
                raise RecoverySafetyError("ROLLBACK_STAGE_ORIGINAL_REAPPEARED")
            _verify_identity(
                stage.staged,
                stage.root,
                stage.identity,
                stage.identity.sha256,
            )
        for row in rows:
            entry = entries[int(row["media_id"])]
            metadata = _metadata(row["error"])
            if bool(metadata.get("target_preexisting")):
                expected_target = _identity_from_dict(metadata.get("target_identity"))
                if expected_target is None:
                    raise RecoverySafetyError("PREEXISTING_TARGET_OWNERSHIP_UNKNOWN")
                _verify_identity(
                    Path(entry.target_path),
                    Path(plan.output_root),
                    expected_target,
                    entry.expected_sha256,
                )
            if entry.action == "MOVE":
                current_source = _stable_hash(
                    Path(entry.source_path),
                    Path(entry.source_root),
                )
                if current_source.sha256 != entry.expected_sha256:
                    raise RecoverySafetyError("RESTORE_VERIFY_FAILED")

        for quarantine, root, identity in quarantines:
            _identity_bound_unlink(quarantine, root, identity)
        for stage in stages:
            _identity_bound_unlink(stage.staged, stage.root, stage.identity)
    except BaseException:
        for source, root, identity in reversed(created_sources):
            try:
                current = _stable_hash(source, root)
                if _same_stat(current, identity) and current.sha256 == identity.sha256:
                    _identity_bound_unlink(source, root, identity)
            except (OSError, RecoverySafetyError):
                pass
        for stage in reversed(stages):
            with suppress(OSError, RecoverySafetyError):
                _restore_rollback_stage(stage)
        raise


def rollback_transaction(
    workspace: Workspace,
    transaction_id: str,
    *,
    fault_injector: FaultInjector | None = None,
    process_liveness: ProcessLiveness = _default_process_liveness,
) -> RollbackResult:
    """Rollback only targets proven owned, output-contained, and byte-identical."""
    with workspace_mutation_lock(workspace, process_liveness=process_liveness):
        transaction, rows = _load_transaction(workspace, transaction_id)
        if str(transaction["state"]) == "ROLLED_BACK":
            return RollbackResult(transaction_id, "ROLLED_BACK", len(rows), 0)
        plan = inspect_plan(workspace, str(transaction["plan_id"]))
        entries = _entry_by_media(plan)
        blocked_bundles: dict[str, str] = {}
        bundle_rows: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for candidate in rows:
            bundle_id = candidate["bundle_id"]
            if not bundle_id:
                continue
            bundle_key = str(bundle_id)
            bundle_rows[bundle_key].append(candidate)
            if bundle_key in blocked_bundles:
                continue
            candidate_entry = entries.get(int(candidate["media_id"]))
            if (
                candidate_entry is None
                or str(candidate["target_path"]) != candidate_entry.target_path
            ):
                blocked_bundles[bundle_key] = "JOURNAL_PLAN_MISMATCH"
                continue
            problem = _rollback_row_problem(
                plan,
                candidate_entry,
                candidate,
                _metadata(candidate["error"]),
            )
            if problem is not None:
                blocked_bundles[bundle_key] = problem
        failed = 0
        rolled_back = 0
        for bundle_id, grouped_rows in bundle_rows.items():
            problem = blocked_bundles.get(bundle_id)
            if problem is not None:
                _bulk_journal_state(
                    workspace,
                    grouped_rows,
                    "ROLLBACK_FAILED",
                    f"BUNDLE_ROLLBACK_PREFLIGHT_FAILED:{problem}",
                )
                failed += len(grouped_rows)
                continue
            try:
                _rollback_bundle_atomically(
                    workspace,
                    plan,
                    transaction_id,
                    grouped_rows,
                    entries,
                    fault_injector,
                )
            except BaseException as error:
                _bulk_journal_state(
                    workspace,
                    grouped_rows,
                    "ROLLBACK_FAILED",
                    f"BUNDLE_ROLLBACK_STAGING_FAILED:{error}",
                )
                failed += len(grouped_rows)
            else:
                _bulk_journal_state(workspace, grouped_rows, "ROLLED_BACK")
                rolled_back += len(grouped_rows)
        for row in reversed(rows):
            journal_id = int(row["id"])
            state = str(row["state"])
            entry = entries.get(int(row["media_id"]))
            metadata = _metadata(row["error"])
            bundle_id = str(row["bundle_id"]) if row["bundle_id"] else None
            if bundle_id is not None:
                continue
            if state == "ROLLED_BACK":
                rolled_back += 1
                continue
            if entry is None or str(row["target_path"]) != entry.target_path:
                metadata["error_code"] = "JOURNAL_PLAN_MISMATCH"
                _journal_update(workspace, journal_id, "ROLLBACK_FAILED", metadata)
                failed += 1
                continue
            if state == "ALREADY_PRESENT":
                _journal_update(workspace, journal_id, "ROLLED_BACK", metadata)
                rolled_back += 1
                continue
            target = Path(entry.target_path)
            output = Path(plan.output_root)
            if not _is_within(target, output):
                metadata["error_code"] = "TARGET_NOT_OWNED"
                _journal_update(workspace, journal_id, "ROLLBACK_FAILED", metadata)
                failed += 1
                continue
            if entry.action == "MOVE":
                try:
                    _restore_source_quarantine(
                        entry,
                        transaction_id,
                        journal_id,
                        metadata,
                    )
                except Exception as error:
                    metadata["error_code"] = str(error)[:120]
                    _journal_update(workspace, journal_id, "ROLLBACK_FAILED", metadata)
                    failed += 1
                    continue
            if bool(metadata.get("target_preexisting")):
                try:
                    try:
                        Path(entry.source_path).lstat()
                    except FileNotFoundError:
                        source_exists = False
                    else:
                        source_exists = True
                    if source_exists:
                        _entry_matches_source(entry)
                    else:
                        expected_target = _identity_from_dict(metadata.get("target_identity"))
                        current_target = _stable_hash(target, output)
                        if (
                            expected_target is None
                            or not _same_stat(current_target, expected_target)
                            or current_target.sha256 != entry.expected_sha256
                        ):
                            raise RecoverySafetyError("PREEXISTING_TARGET_CHANGED")
                        _copy_restore(
                            target,
                            output,
                            Path(entry.source_path),
                            Path(entry.source_root),
                            entry.expected_sha256,
                        )
                    metadata.pop("error_code", None)
                    _journal_update(workspace, journal_id, "ROLLED_BACK", metadata)
                    rolled_back += 1
                except Exception as error:
                    metadata["error_code"] = str(error)[:120]
                    _journal_update(workspace, journal_id, "ROLLBACK_FAILED", metadata)
                    failed += 1
                continue
            if not bool(metadata.get("target_owned")):
                try:
                    target.lstat()
                except FileNotFoundError:
                    target_exists = False
                except OSError:
                    target_exists = True
                else:
                    target_exists = True
                if (
                    state
                    not in {
                        "PREPARED",
                        "PARTIAL_COPIED",
                        "PARTIAL_VERIFIED",
                        "FINALIZING",
                        "FAILED",
                        "ROLLBACK_FAILED",
                    }
                    or target_exists
                ):
                    metadata["error_code"] = "TARGET_NOT_OWNED"
                    _journal_update(workspace, journal_id, "ROLLBACK_FAILED", metadata)
                    failed += 1
                    continue
                partial_value = metadata.get("partial_path")
                partial_identity = _identity_from_dict(metadata.get("partial_identity"))
                try:
                    if isinstance(partial_value, str):
                        partial = Path(partial_value)
                        if not _is_within(partial, output) or partial_identity is None:
                            raise RecoverySafetyError("PARTIAL_OWNERSHIP_UNKNOWN")
                        try:
                            partial.lstat()
                        except FileNotFoundError:
                            pass
                        else:
                            current_partial = _stable_hash(partial, output)
                            if (
                                not _same_stat(current_partial, partial_identity)
                                or current_partial.sha256 != entry.expected_sha256
                            ):
                                raise RecoverySafetyError("PARTIAL_HASH_MISMATCH")
                            _quarantine_delete(
                                partial,
                                output,
                                current_partial,
                                prefix=f"rollback-partial-{journal_id}",
                            )
                    metadata["target_owned"] = False
                    metadata.pop("partial_path", None)
                    metadata.pop("partial_identity", None)
                    metadata.pop("error_code", None)
                    _journal_update(workspace, journal_id, "ROLLED_BACK", metadata)
                    rolled_back += 1
                except Exception as error:
                    metadata["error_code"] = str(error)[:120]
                    _journal_update(workspace, journal_id, "ROLLBACK_FAILED", metadata)
                    failed += 1
                continue
            expected_identity = _identity_from_dict(metadata.get("target_identity"))
            try:
                current = _stable_hash(target, output)
                if (
                    expected_identity is None
                    or not _same_stat(current, expected_identity)
                    or current.sha256 != entry.expected_sha256
                ):
                    raise RecoverySafetyError("TARGET_MODIFIED")
                if entry.action == "MOVE":
                    _copy_restore(
                        target,
                        output,
                        Path(entry.source_path),
                        Path(entry.source_root),
                        entry.expected_sha256,
                    )
                _quarantine_delete(target, output, current, prefix=f"rollback-target-{journal_id}")
                metadata["target_owned"] = False
                metadata.pop("error_code", None)
                _journal_update(workspace, journal_id, "ROLLED_BACK", metadata)
                rolled_back += 1
            except Exception as error:
                metadata["error_code"] = str(error)[:120]
                _journal_update(workspace, journal_id, "ROLLBACK_FAILED", metadata)
                failed += 1
        state = "ROLLED_BACK" if failed == 0 else "ROLLBACK_PARTIAL"
        _transaction_update(workspace, transaction_id, state)
        return RollbackResult(transaction_id, state, rolled_back, failed)


class _DoctorScanLimit(RuntimeError):
    pass


@dataclass(slots=True)
class _DoctorScanBudget:
    remaining: int

    def consume(self) -> None:
        if self.remaining <= 0:
            raise _DoctorScanLimit
        self.remaining -= 1


def _iter_partials(root: Path, budget: _DoctorScanBudget) -> Iterator[Path]:
    try:
        root_state = root.lstat()
    except FileNotFoundError:
        return
    except OSError:
        return
    if (
        not stat.S_ISDIR(root_state.st_mode)
        or stat.S_ISLNK(root_state.st_mode)
        or getattr(root_state, "st_file_attributes", 0) & 0x400
    ):
        return
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = os.scandir(directory)
        except OSError:
            continue
        with entries:
            for entry in entries:
                budget.consume()
                try:
                    if entry.is_symlink():
                        continue
                    state = entry.stat(follow_symlinks=False)
                    if getattr(state, "st_file_attributes", 0) & 0x400:
                        continue
                    path = Path(entry.path)
                    if stat.S_ISDIR(state.st_mode):
                        stack.append(path)
                    elif stat.S_ISREG(state.st_mode) and path.name.endswith(".partial"):
                        yield path
                except OSError:
                    continue


def doctor_workspace(
    workspace: Workspace,
    *,
    process_liveness: ProcessLiveness = _default_process_liveness,
    max_records: int = 100_000,
) -> DoctorReport:
    """Read-only diagnosis of locks, journals, partials, stale inputs, and bundle state."""
    if isinstance(max_records, bool) or not isinstance(max_records, int) or max_records <= 0:
        raise ValueError("max_records must be a positive integer")
    lock_status = _lock_status(workspace, process_liveness)
    diagnoses: list[Diagnosis] = []
    scan_limited = False
    if lock_status in {"LIVE", "UNKNOWN", "STALE"}:
        diagnoses.append(Diagnosis(f"{lock_status}_LOCK", f"apply lock is {lock_status.lower()}"))
    with closing(connect_database(workspace.database_path, read_only=True)) as connection:
        connection.row_factory = sqlite3.Row
        transactions = connection.execute(
            "SELECT * FROM operation_transaction ORDER BY created_at,transaction_id LIMIT ?",
            (max_records + 1,),
        ).fetchall()
        rows = connection.execute(
            "SELECT * FROM operation_journal ORDER BY transaction_id,id LIMIT ?",
            (max_records + 1,),
        ).fetchall()
        plan_headers = [
            (str(row[0]), int(row[1]))
            for row in connection.execute(
                "SELECT plan_id,entry_count FROM organization_plan ORDER BY plan_id LIMIT ?",
                (max_records + 1,),
            ).fetchall()
        ]
    if len(transactions) > max_records:
        scan_limited = True
        transactions = transactions[:max_records]
    if len(rows) > max_records:
        scan_limited = True
        rows = rows[:max_records]
    if len(plan_headers) > max_records:
        scan_limited = True
        plan_headers = plan_headers[:max_records]
    rows_by_tx: dict[str, list[sqlite3.Row]] = defaultdict(list)
    known_partials: set[str] = set()
    for row in rows:
        rows_by_tx[str(row["transaction_id"])].append(row)
    plans: dict[str, OrganizationPlan] = {}
    entries_by_plan: dict[str, dict[int, PlanEntry]] = {}
    invalid_plans: set[str] = set()
    remaining_plan_entries = max_records
    for plan_id, entry_count in plan_headers:
        if entry_count > remaining_plan_entries:
            scan_limited = True
            continue
        remaining_plan_entries -= entry_count
        try:
            plan = inspect_plan(workspace, plan_id)
        except (OSError, PlannerError, sqlite3.Error) as error:
            invalid_plans.add(plan_id)
            diagnoses.append(
                Diagnosis("PLAN_INVALID", f"immutable plan cannot be verified: {error}")
            )
            continue
        plans[plan_id] = plan
        entries_by_plan[plan_id] = _entry_by_media(plan)
    for transaction in transactions:
        transaction_id = str(transaction["transaction_id"])
        state = str(transaction["state"])
        plan_id = str(transaction["plan_id"])
        plan = plans.get(plan_id)
        plan_entries = entries_by_plan.get(plan_id, {})
        if state not in _TRANSACTION_TERMINAL:
            diagnoses.append(
                Diagnosis(
                    "RESUME_AVAILABLE", "transaction has durable resumable state", transaction_id
                )
            )
        group_states: dict[str, set[str]] = defaultdict(set)
        for row in rows_by_tx[transaction_id]:
            media_id = int(row["media_id"])
            row_state = str(row["state"])
            metadata = _metadata(row["error"])
            entry = plan_entries.get(media_id)
            bundle_id = row["bundle_id"]
            if bundle_id:
                group_states[str(bundle_id)].add(row_state)
            if row_state == "PREPARED":
                diagnoses.append(
                    Diagnosis("PREPARED_RESUMABLE", "entry is prepared", transaction_id, media_id)
                )
            delete_phase = metadata.get("source_delete_phase")
            if row_state in {
                "SOURCE_DELETE_PREPARED",
                "SOURCE_QUARANTINED",
                "SOURCE_DELETE_UNLINKED",
            } or (
                row_state == "FAILED" and delete_phase in {"PREPARED", "QUARANTINED", "UNLINKED"}
            ):
                quarantine_value = metadata.get("source_quarantine_path")
                diagnoses.append(
                    Diagnosis(
                        "SOURCE_DELETE_RESUMABLE",
                        f"MOVE source deletion is recoverable from phase {delete_phase}",
                        transaction_id,
                        media_id,
                        str(quarantine_value) if isinstance(quarantine_value, str) else None,
                    )
                )
            if row_state in {"COPIED_VERIFIED", "SOURCE_REMOVED"}:
                code = "COPIED_VERIFIED_RESUMABLE"
                source_missing = False
                if entry is not None:
                    try:
                        Path(entry.source_path).lstat()
                    except FileNotFoundError:
                        source_missing = True
                    except OSError:
                        pass
                if row_state == "SOURCE_REMOVED" and source_missing:
                    code = "SOURCE_MISSING_TARGET_VERIFIED"
                diagnoses.append(
                    Diagnosis(code, "verified target can converge", transaction_id, media_id)
                )
            partial_value = metadata.get("partial_path")
            partial_expected = _identity_from_dict(metadata.get("partial_identity"))
            if isinstance(partial_value, str) and plan is not None:
                partial = Path(partial_value)
                output = Path(plan.output_root)
                if _is_within(partial, output):
                    known_partials.add(os.path.normcase(os.path.abspath(partial)))
                try:
                    partial.lstat()
                except FileNotFoundError:
                    diagnoses.append(
                        Diagnosis(
                            "PARTIAL_MISSING",
                            "journal-owned partial is missing",
                            transaction_id,
                            media_id,
                            str(partial),
                        )
                    )
                except OSError:
                    diagnoses.append(
                        Diagnosis(
                            "PARTIAL_HASH_MISMATCH",
                            "partial cannot be inspected safely and was retained",
                            transaction_id,
                            media_id,
                            str(partial),
                        )
                    )
                else:
                    try:
                        if not _is_within(partial, output):
                            raise RecoverySafetyError("PARTIAL_OUTSIDE_OUTPUT")
                        current = _stable_hash(partial, output)
                    except RecoverySafetyError:
                        diagnoses.append(
                            Diagnosis(
                                "PARTIAL_HASH_MISMATCH",
                                "partial changed and was retained",
                                transaction_id,
                                media_id,
                                str(partial),
                            )
                        )
                    else:
                        if partial_expected is None or not _same_stat(current, partial_expected):
                            diagnoses.append(
                                Diagnosis(
                                    "PARTIAL_HASH_MISMATCH",
                                    "partial changed and was retained",
                                    transaction_id,
                                    media_id,
                                    str(partial),
                                )
                            )
            if entry is None or plan is None:
                if plan_id not in invalid_plans:
                    diagnoses.append(
                        Diagnosis(
                            "JOURNAL_PLAN_MISMATCH",
                            "journal entry is not present in immutable plan",
                            transaction_id,
                            media_id,
                        )
                    )
                continue
            if (
                str(row["source_path"]) != entry.source_path
                or str(row["target_path"]) != entry.target_path
                or str(row["source_sha256"]) != entry.expected_sha256
            ):
                diagnoses.append(
                    Diagnosis(
                        "JOURNAL_PLAN_MISMATCH",
                        "journal paths or source digest differ from immutable plan",
                        transaction_id,
                        media_id,
                    )
                )
                continue
            source = Path(entry.source_path)
            try:
                source_state = source.lstat()
            except FileNotFoundError:
                source_state = None
            except OSError:
                diagnoses.append(
                    Diagnosis(
                        "PLAN_STALE",
                        "source cannot be inspected safely",
                        transaction_id,
                        media_id,
                        str(source),
                    )
                )
                source_state = None
            if source_state is not None:
                try:
                    current_source = _stable_hash(source, Path(entry.source_root))
                except RecoverySafetyError:
                    diagnoses.append(
                        Diagnosis(
                            "PLAN_STALE",
                            "source cannot be inspected safely",
                            transaction_id,
                            media_id,
                            str(source),
                        )
                    )
                else:
                    if (
                        current_source.size != entry.expected_size
                        or current_source.sha256 != entry.expected_sha256
                    ):
                        diagnoses.append(
                            Diagnosis(
                                "PLAN_STALE",
                                "source differs from immutable plan",
                                transaction_id,
                                media_id,
                                str(source),
                            )
                        )
            target = Path(entry.target_path)
            if row["target_sha256"]:
                try:
                    target.lstat()
                except FileNotFoundError:
                    if row_state in {
                        "COPIED_VERIFIED",
                        "SOURCE_REMOVED",
                        "DONE",
                        "ALREADY_PRESENT",
                    }:
                        diagnoses.append(
                            Diagnosis(
                                "TARGET_MISSING",
                                "verified journal target is missing",
                                transaction_id,
                                media_id,
                                str(target),
                            )
                        )
                except OSError:
                    diagnoses.append(
                        Diagnosis(
                            "TARGET_HASH_MISMATCH",
                            "target cannot be inspected safely",
                            transaction_id,
                            media_id,
                            str(target),
                        )
                    )
                else:
                    try:
                        current_target = _stable_hash(target, Path(plan.output_root))
                    except RecoverySafetyError:
                        diagnoses.append(
                            Diagnosis(
                                "TARGET_HASH_MISMATCH",
                                "target cannot be inspected safely",
                                transaction_id,
                                media_id,
                                str(target),
                            )
                        )
                    else:
                        if current_target.sha256 != str(row["target_sha256"]):
                            diagnoses.append(
                                Diagnosis(
                                    "TARGET_HASH_MISMATCH",
                                    "target differs from journal",
                                    transaction_id,
                                    media_id,
                                    str(target),
                                )
                            )
        for bundle_id, states in group_states.items():
            if states - {"DONE", "ALREADY_PRESENT", "ROLLED_BACK"} and states & {
                "DONE",
                "ALREADY_PRESENT",
                "ROLLED_BACK",
            }:
                diagnoses.append(
                    Diagnosis(
                        "BUNDLE_PARTIAL",
                        f"bundle {bundle_id} is partially complete",
                        transaction_id,
                    )
                )
    scan_budget = _DoctorScanBudget(max_records)
    for plan in plans.values():
        output = Path(plan.output_root)
        try:
            output_state = output.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            diagnoses.append(
                Diagnosis(
                    "OUTPUT_ROOT_UNSAFE",
                    "output root cannot be inspected without following links",
                    path=str(output),
                )
            )
            continue
        if (
            not stat.S_ISDIR(output_state.st_mode)
            or stat.S_ISLNK(output_state.st_mode)
            or getattr(output_state, "st_file_attributes", 0) & 0x400
        ):
            diagnoses.append(
                Diagnosis(
                    "OUTPUT_ROOT_UNSAFE",
                    "output root is not a no-follow ordinary directory",
                    path=str(output),
                )
            )
            continue
        try:
            for partial in _iter_partials(output, scan_budget):
                key = os.path.normcase(os.path.abspath(partial))
                if key not in known_partials:
                    diagnoses.append(
                        Diagnosis(
                            "ORPHAN_PARTIAL", "unowned partial was retained", path=str(partial)
                        )
                    )
        except _DoctorScanLimit:
            scan_limited = True
            break
    if scan_limited:
        diagnoses.append(
            Diagnosis(
                "DOCTOR_SCAN_LIMIT",
                f"doctor stopped after the configured limit of {max_records} records",
            )
        )
    return DoctorReport(lock_status, tuple(diagnoses))
