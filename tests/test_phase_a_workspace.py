from __future__ import annotations

import json
import shutil
import sqlite3
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier, Event, Lock

import pytest

import smart_photo_triage.workspace as workspace_module
from smart_photo_triage.config import load_config
from smart_photo_triage.database import MIGRATIONS, apply_migrations, connect_database
from smart_photo_triage.workspace import WorkspaceOwnershipError, initialize_workspace


def test_init_lock_retries_permission_only_after_observed_contention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "lock-sequence"
    root.mkdir()
    original_mkdir = Path.mkdir
    outcomes: list[type[OSError] | None] = [FileExistsError, PermissionError, None]

    def sequenced_mkdir(path: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if path.name == ".spt-init-lock" and outcomes:
            outcome = outcomes.pop(0)
            if outcome is not None:
                raise outcome("synthetic")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", sequenced_mkdir)
    with workspace_module._initialization_lock(root, timeout_seconds=0.1, sleep=lambda _s: None):
        assert (root / ".spt-init-lock").is_dir()


def test_init_lock_preserves_persistent_permission_error(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "denied"
    root.mkdir()
    denied = PermissionError("persistent ACL denial")

    def deny(_path: Path, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise denied

    monkeypatch.setattr(Path, "mkdir", deny)
    with (
        pytest.raises(PermissionError, match="persistent ACL denial"),
        workspace_module._initialization_lock(root, timeout_seconds=0.001, sleep=lambda _s: None),
    ):
        pass


def test_init_lock_old_owner_does_not_remove_replacement(tmp_path: Path) -> None:
    root = tmp_path / "replacement"
    root.mkdir()
    lock_path = root / ".spt-init-lock"
    manager = workspace_module._initialization_lock(root)
    manager.__enter__()
    for child in lock_path.iterdir():
        child.unlink()
    lock_path.rmdir()
    lock_path.mkdir()
    (lock_path / ".owner-new-token").write_text("new-token", encoding="ascii")

    manager.__exit__(None, None, None)

    assert lock_path.is_dir()
    assert (lock_path / ".owner-new-token").exists()


def test_init_lock_owner_write_failure_cleans_created_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "owner-write-failure"
    root.mkdir()
    original_open = Path.open

    def fail_owner(path: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if path.parent.name == ".spt-init-lock" and path.name.startswith(".owner-"):
            raise OSError("synthetic owner write failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_owner)
    with (
        pytest.raises(OSError, match="owner write failure"),
        workspace_module._initialization_lock(root),
    ):
        pass
    assert not (root / ".spt-init-lock").exists()


@pytest.mark.parametrize("failure_site", ["mid_write", "close"])
def test_init_lock_partial_owner_file_is_cleaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_site: str
) -> None:
    root = tmp_path / failure_site
    root.mkdir()
    original_open = Path.open

    class FaultingOwnerStream:
        def __init__(self, stream):  # type: ignore[no-untyped-def]
            self.stream = stream

        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def write(self, value: str) -> int:
            if failure_site == "mid_write":
                self.stream.write(value[:4])
                self.stream.flush()
                raise OSError("synthetic mid-write failure")
            return self.stream.write(value)

        def __exit__(self, *_args: object) -> None:
            self.stream.close()
            if failure_site == "close":
                raise OSError("synthetic close failure")

    def fault_owner(path: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        stream = original_open(path, *args, **kwargs)
        if path.parent.name == ".spt-init-lock" and path.name.startswith(".owner-"):
            return FaultingOwnerStream(stream)
        return stream

    monkeypatch.setattr(Path, "open", fault_owner)
    with (
        pytest.raises(OSError, match=failure_site.replace("_", "-")),
        workspace_module._initialization_lock(root),
    ):
        pass
    assert not (root / ".spt-init-lock").exists()


def user_version(database_path: Path) -> int:
    with closing(sqlite3.connect(database_path)) as connection:
        row = connection.execute("PRAGMA user_version").fetchone()
    assert row is not None
    return int(row[0])


def test_t_a_003_workspace_init_is_idempotent_and_preserves_state(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    first = initialize_workspace(root)
    first_config = first.config_path.read_bytes()
    first_version = user_version(first.database_path)
    marker = root / "state" / "existing-user-state.txt"
    marker.write_text("keep me", encoding="utf-8")

    second = initialize_workspace(root)

    assert second == first
    assert second.config_path.read_bytes() == first_config
    assert user_version(second.database_path) == first_version >= 1
    assert marker.read_text(encoding="utf-8") == "keep me"
    assert load_config(second.config_path).allow_cloud is False


def test_init_rejects_unknown_existing_database_without_modifying_it(tmp_path: Path) -> None:
    root = tmp_path / "unknown-db"
    root.mkdir()
    database_path = root / "spt.sqlite3"
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute("CREATE TABLE foreign_application_data (value TEXT)")
        connection.execute("INSERT INTO foreign_application_data VALUES ('keep me')")
        connection.commit()
    before_bytes = database_path.read_bytes()
    before_stat = database_path.stat()

    with pytest.raises(WorkspaceOwnershipError, match="ownership"):
        initialize_workspace(root)

    after_stat = database_path.stat()
    assert database_path.read_bytes() == before_bytes
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
    assert not (root / ".spt-workspace").exists()


def test_init_conservatively_rejects_markerless_spt_database(tmp_path: Path) -> None:
    root = tmp_path / "markerless-spt"
    database_path = root / "spt.sqlite3"
    with closing(connect_database(database_path)) as connection:
        apply_migrations(connection)
    before_bytes = database_path.read_bytes()
    before_stat = database_path.stat()
    assert not (root / ".spt-workspace").exists()

    with pytest.raises(WorkspaceOwnershipError, match="markerless"):
        initialize_workspace(root)

    after_stat = database_path.stat()
    assert database_path.read_bytes() == before_bytes
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
    assert not (root / ".spt-workspace").exists()


def test_concurrent_workspace_init_safely_converges(tmp_path: Path) -> None:
    root = tmp_path / "concurrent workspace"

    with ThreadPoolExecutor(max_workers=8) as executor:
        workspaces = list(executor.map(initialize_workspace, [root] * 16))

    assert {workspace.root for workspace in workspaces} == {root.resolve()}
    assert user_version(root / "spt.sqlite3") == MIGRATIONS[-1].version
    with closing(sqlite3.connect(root / "spt.sqlite3")) as connection:
        assert connection.execute("SELECT COUNT(*) FROM schema_migration").fetchone() == (
            len(MIGRATIONS),
        )


def test_marker_is_never_observable_before_its_complete_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "atomic marker"
    marker_created = Event()
    release_writer = Event()
    intercept_lock = Lock()
    intercepted = False
    original_open = Path.open

    def delayed_marker_open(
        path: Path,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ):  # type: ignore[no-untyped-def]
        nonlocal intercepted
        stream = original_open(path, mode, buffering, encoding, errors, newline)
        should_delay = False
        is_marker_temp = path.name.startswith("..spt-workspace.") and path.name.endswith(".tmp")
        if is_marker_temp and mode == "x":
            with intercept_lock:
                if not intercepted:
                    intercepted = True
                    should_delay = True
        if should_delay:
            marker_created.set()
            release_writer.wait(timeout=5)
        return stream

    monkeypatch.setattr(Path, "open", delayed_marker_open)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(initialize_workspace, root)
        second: Future[object] | None = None
        try:
            hook_triggered = marker_created.wait(timeout=1)
            assert hook_triggered, (
                "marker writer pause hook did not intercept the unique temp write"
            )
            assert not (root / ".spt-workspace").exists()
            second = executor.submit(initialize_workspace, root)
            with pytest.raises(TimeoutError):
                second.result(timeout=0.1)
        finally:
            release_writer.set()
        first_result = first.result(timeout=5)
        assert second is not None
        second_result = second.result(timeout=5)

    assert second_result == first_result


def test_barrier_concurrent_init_is_stable_across_multiple_rounds(tmp_path: Path) -> None:
    worker_count = 12
    for round_number in range(5):
        root = tmp_path / f"barrier-round-{round_number}"
        barrier = Barrier(worker_count)

        def initialize_after_barrier(current_barrier: Barrier, current_root: Path) -> object:
            current_barrier.wait(timeout=5)
            return initialize_workspace(current_root)

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures: list[Future[object]] = [
                executor.submit(initialize_after_barrier, barrier, root)
                for _ in range(worker_count)
            ]
            workspaces = [future.result(timeout=10) for future in futures]

        assert {workspace.root for workspace in workspaces} == {root.resolve()}  # type: ignore[attr-defined]


def test_marker_identity_matches_database_identity(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "identity")

    marker = json.loads((workspace.root / ".spt-workspace").read_text(encoding="utf-8"))
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        row = connection.execute(
            "SELECT value FROM workspace_metadata WHERE key = 'workspace_id'"
        ).fetchone()

    assert marker["format_version"] == 1
    assert isinstance(marker["workspace_id"], str)
    assert row == (marker["workspace_id"],)


def test_replaced_database_is_rejected_without_modifying_replacement(tmp_path: Path) -> None:
    first = initialize_workspace(tmp_path / "first")
    second = initialize_workspace(tmp_path / "second")
    shutil.copy2(second.database_path, first.database_path)
    replacement_bytes = first.database_path.read_bytes()
    replacement_stat = first.database_path.stat()

    with pytest.raises(WorkspaceOwnershipError, match="identity"):
        initialize_workspace(first.root)

    after_stat = first.database_path.stat()
    assert first.database_path.read_bytes() == replacement_bytes
    assert after_stat.st_mtime_ns == replacement_stat.st_mtime_ns
