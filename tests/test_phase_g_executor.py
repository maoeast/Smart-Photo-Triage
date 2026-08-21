from __future__ import annotations

import hashlib
import importlib
import json
import os
import sqlite3
import stat as stat_module
import subprocess
import sys
from contextlib import closing, contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import smart_photo_triage.cli as cli_module
import smart_photo_triage.planner as planner_module
from smart_photo_triage.database import MIGRATIONS
from smart_photo_triage.planner import (
    PlannerOptions,
    approve_plan,
    build_plan,
    preflight_plan,
    revoke_plan,
)
from smart_photo_triage.workspace import Workspace, initialize_workspace


class SyntheticCrash(BaseException):
    pass


def _executor() -> Any:
    try:
        return importlib.import_module("smart_photo_triage.executor")
    except ModuleNotFoundError:
        pytest.fail("Phase G executor API is unavailable")


def _snapshot(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _seed_plan(
    tmp_path: Path,
    *,
    mode: str = "COPY",
    names: tuple[str, ...] = ("photo.jpg",),
    bundle: bool = False,
) -> tuple[Workspace, Any, Any, Path, Path]:
    workspace = initialize_workspace(tmp_path / "workspace")
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        bundle_id = "bundle-phase-g" if bundle else None
        if bundle_id:
            connection.execute(
                """
                INSERT INTO asset_bundle(id,bundle_type,bundle_key,source_root_key,warning_status)
                VALUES (?, 'LIVE_PHOTO', ?, ?, NULL)
                """,
                (bundle_id, "phase-g-bundle-key", str(source).casefold()),
            )
        for index, name in enumerate(names):
            path = source / name
            payload = f"synthetic-phase-g-{index}-{name}".encode()
            path.write_bytes(payload)
            media_type = "VIDEO" if path.suffix.casefold() in {".mov", ".mp4"} else "IMAGE"
            digest = hashlib.sha256(payload).hexdigest()
            cursor = connection.execute(
                """
                INSERT INTO media_item(
                    original_path,path_key,source_root,source_root_key,parent_key,bundle_stem,
                    media_type,extension,size_bytes,mtime_ns,source_present,content_sha256,
                    captured_at,capture_source,capture_confidence,capture_timezone_status,
                    last_seen_at,last_seen_scan_id
                ) VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,'now','scan-g')
                """,
                (
                    str(path),
                    str(path).casefold(),
                    str(source),
                    str(source).casefold(),
                    str(source).casefold(),
                    path.stem,
                    media_type,
                    path.suffix.casefold(),
                    len(payload),
                    path.stat().st_mtime_ns,
                    digest,
                    "2024-06-07T08:09:10",
                    "EXIF_DATETIME_ORIGINAL",
                    "HIGH",
                    "UNKNOWN",
                ),
            )
            media_id = int(cursor.lastrowid)
            if bundle_id:
                role = "PRIMARY" if index == 0 else "MOTION"
                connection.execute(
                    "INSERT INTO bundle_member(bundle_id,media_id,role) VALUES (?,?,?)",
                    (bundle_id, media_id, role),
                )
        connection.commit()
    plan = build_plan(workspace, PlannerOptions(output_root=output, mode=mode))
    approve_plan(workspace, plan.plan_id)
    report = preflight_plan(workspace, plan.plan_id)
    assert report.ok
    return workspace, plan, report, source, output


def _journal_rows(workspace: Workspace) -> list[sqlite3.Row]:
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute("SELECT * FROM operation_journal ORDER BY id").fetchall()


def _transaction_id(workspace: Workspace) -> str:
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        row = connection.execute(
            "SELECT transaction_id FROM operation_transaction ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    return str(row[0])


def test_t_g_001_dry_run_has_zero_source_target_or_journal_mutation(tmp_path: Path) -> None:
    executor = _executor()
    workspace, _plan, report, source, output = _seed_plan(tmp_path)
    source_before = _snapshot(source)
    output_before = _snapshot(output)

    result = executor.apply_plan(workspace, report, dry_run=True)

    assert result.state == "DRY_RUN"
    assert _snapshot(source) == source_before
    assert _snapshot(output) == output_before == {}
    assert not output.exists()
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM operation_transaction").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM operation_journal").fetchone() == (0,)


def test_t_g_002_target_different_hash_is_never_overwritten(tmp_path: Path) -> None:
    executor = _executor()
    workspace, plan, report, source, _output = _seed_plan(tmp_path)
    target = Path(plan.entries[0].target_path)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"unowned-different-content")

    with pytest.raises(executor.RecoverySafetyError, match="TARGET_CONFLICT"):
        executor.apply_plan(workspace, report, dry_run=False)

    assert target.read_bytes() == b"unowned-different-content"
    assert Path(plan.entries[0].source_path).exists()
    assert _journal_rows(workspace) == []
    assert _snapshot(source)


def test_t_g_003_target_same_hash_is_already_present_and_unowned(tmp_path: Path) -> None:
    executor = _executor()
    workspace, plan, report, _source, output = _seed_plan(tmp_path)
    source_path = Path(plan.entries[0].source_path)
    target = Path(plan.entries[0].target_path)
    target.parent.mkdir(parents=True)
    target.write_bytes(source_path.read_bytes())
    before = _snapshot(output)

    applied = executor.apply_plan(workspace, report, dry_run=False)
    rolled_back = executor.rollback_transaction(workspace, applied.transaction_id)

    assert applied.state == "DONE"
    assert applied.already_present_count == 1
    assert _snapshot(output) == before
    assert rolled_back.state == "ROLLED_BACK"
    assert target.exists(), "rollback must not delete a same-hash target SPT did not create"
    assert _journal_rows(workspace)[0]["state"] == "ROLLED_BACK"


def test_move_same_hash_unowned_target_deletes_source_and_rollback_leaves_target(
    tmp_path: Path,
) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(tmp_path, mode="MOVE")
    source = Path(plan.entries[0].source_path)
    target = Path(plan.entries[0].target_path)
    expected = source.read_bytes()
    target.parent.mkdir(parents=True)
    target.write_bytes(expected)
    target_before = target.stat()

    applied = executor.apply_plan(workspace, report, dry_run=False)

    assert applied.state == "DONE"
    assert applied.already_present_count == 0
    assert not source.exists()
    assert target.read_bytes() == expected

    rolled_back = executor.rollback_transaction(workspace, applied.transaction_id)
    target_after = target.stat()

    assert rolled_back.state == "ROLLED_BACK"
    assert source.read_bytes() == expected
    assert target.read_bytes() == expected
    assert (target_after.st_dev, target_after.st_ino) == (
        target_before.st_dev,
        target_before.st_ino,
    )


@pytest.mark.parametrize(
    ("fault_stage", "expected_state", "target_exists", "partial_exists"),
    [
        ("AFTER_PREPARED", "PREPARED", False, False),
        ("AFTER_PARTIAL_COPY", "PARTIAL_COPIED", False, True),
        ("AFTER_TARGET_VERIFY", "PARTIAL_VERIFIED", False, True),
        ("AFTER_FINALIZE", "COPIED_VERIFIED", True, False),
        ("BEFORE_DONE", "COPIED_VERIFIED", True, False),
    ],
)
def test_t_g_004_to_006_and_009_copy_fault_boundaries_resume_without_recopy_or_loss(
    tmp_path: Path,
    fault_stage: str,
    expected_state: str,
    target_exists: bool,
    partial_exists: bool,
) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(tmp_path)
    target = Path(plan.entries[0].target_path)

    def crash(stage: str, _media_id: int) -> None:
        if stage == fault_stage:
            raise SyntheticCrash(stage)

    with pytest.raises(SyntheticCrash, match=fault_stage):
        executor.apply_plan(workspace, report, dry_run=False, fault_injector=crash)

    rows = _journal_rows(workspace)
    assert rows[0]["state"] == expected_state
    assert target.exists() is target_exists
    assert any(target.parent.glob("*.partial")) is partial_exists
    assert Path(plan.entries[0].source_path).exists()
    diagnosis = executor.doctor_workspace(workspace)
    assert "RESUME_AVAILABLE" in {item.code for item in diagnosis.diagnoses}

    resumed = executor.resume_transaction(workspace, _transaction_id(workspace))

    assert resumed.state == "DONE"
    assert target.read_bytes() == Path(plan.entries[0].source_path).read_bytes()
    assert not list(target.parent.glob("*.partial"))
    assert _journal_rows(workspace)[0]["state"] == "DONE"


@pytest.mark.parametrize(
    ("fault_stage", "expected_state", "source_exists"),
    [
        ("BEFORE_SOURCE_DELETE", "COPIED_VERIFIED", True),
        ("AFTER_SOURCE_DELETE", "SOURCE_REMOVED", False),
        ("BEFORE_DONE", "SOURCE_REMOVED", False),
    ],
)
def test_t_g_007_to_011_move_verifies_target_before_identity_bound_source_delete(
    tmp_path: Path, fault_stage: str, expected_state: str, source_exists: bool
) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(tmp_path, mode="MOVE")
    source_path = Path(plan.entries[0].source_path)
    target = Path(plan.entries[0].target_path)
    expected = source_path.read_bytes()

    def crash(stage: str, _media_id: int) -> None:
        if stage == fault_stage:
            raise SyntheticCrash(stage)

    with pytest.raises(SyntheticCrash, match=fault_stage):
        executor.apply_plan(workspace, report, dry_run=False, fault_injector=crash)

    assert _journal_rows(workspace)[0]["state"] == expected_state
    assert target.read_bytes() == expected
    assert source_path.exists() is source_exists

    resumed = executor.resume_transaction(workspace, _transaction_id(workspace))

    assert resumed.state == "DONE"
    assert not source_path.exists()
    assert target.read_bytes() == expected


def test_t_g_012_bundle_partial_failure_is_resumable_and_never_bundle_done(
    tmp_path: Path,
) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(
        tmp_path,
        names=("IMG_1001.HEIC", "IMG_1001.MOV"),
        bundle=True,
    )
    second_media_id = plan.entries[1].media_id

    def deny_second(stage: str, media_id: int) -> None:
        if stage == "BEFORE_PARTIAL_COPY" and media_id == second_media_id:
            raise PermissionError("synthetic permission denial")

    applied = executor.apply_plan(
        workspace,
        report,
        dry_run=False,
        fault_injector=deny_second,
    )

    states = [row["state"] for row in _journal_rows(workspace)]
    assert applied.state == "PARTIAL"
    assert states == ["DONE", "FAILED"]
    assert not all(state == "DONE" for state in states)
    assert "BUNDLE_PARTIAL" in {
        item.code for item in executor.doctor_workspace(workspace).diagnoses
    }

    resumed = executor.resume_transaction(workspace, applied.transaction_id)
    assert resumed.state == "DONE"
    assert [row["state"] for row in _journal_rows(workspace)] == ["DONE", "DONE"]


def test_t_g_013_and_014_lock_live_unknown_and_dead_owner_rules(tmp_path: Path) -> None:
    executor = _executor()
    workspace, _plan, report, _source, _output = _seed_plan(tmp_path)

    with executor.workspace_mutation_lock(workspace):
        with pytest.raises(executor.WorkspaceLockedError):
            executor.apply_plan(workspace, report, dry_run=False)
        assert executor.doctor_workspace(workspace).lock_status == "LIVE"

    lock = workspace.root / "state" / "apply.lock"
    lock.parent.mkdir(exist_ok=True)
    lock.write_text('{"pid":424242,"token":"synthetic"}', encoding="utf-8")
    unknown = executor.doctor_workspace(workspace, process_liveness=lambda _pid, _token: "UNKNOWN")
    assert unknown.lock_status == "UNKNOWN"
    assert lock.exists()
    with pytest.raises(executor.WorkspaceLockedError):
        executor.apply_plan(
            workspace,
            report,
            dry_run=False,
            process_liveness=lambda _pid, _token: "UNKNOWN",
        )
    assert lock.exists()

    dead = executor.doctor_workspace(workspace, process_liveness=lambda _pid, _token: "DEAD")
    assert dead.lock_status == "STALE"
    applied = executor.apply_plan(
        workspace,
        report,
        dry_run=False,
        process_liveness=lambda _pid, _token: "DEAD",
    )
    assert applied.state == "DONE"
    assert not lock.exists()


def test_t_g_015_same_plan_twice_is_idempotent_without_name_drift(tmp_path: Path) -> None:
    executor = _executor()
    workspace, plan, report, _source, output = _seed_plan(tmp_path)

    first = executor.apply_plan(workspace, report, dry_run=False)
    first_snapshot = _snapshot(output)
    second = executor.apply_plan(workspace, report, dry_run=False)

    assert second.transaction_id == first.transaction_id
    assert second.state == "DONE"
    assert _snapshot(output) == first_snapshot
    assert set(first_snapshot) == {Path(plan.entries[0].target_path).relative_to(output).as_posix()}
    assert not any("(1)" in name or "(2)" in name for name in first_snapshot)


def test_t_g_016_copy_rollback_is_safe_idempotent_and_output_contained(tmp_path: Path) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(tmp_path)
    applied = executor.apply_plan(workspace, report, dry_run=False)
    target = Path(plan.entries[0].target_path)
    outside = tmp_path / "outside-user-file.jpg"
    outside.write_bytes(target.read_bytes())
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        connection.execute(
            "UPDATE operation_journal SET target_path=? WHERE transaction_id=?",
            (str(outside), applied.transaction_id),
        )
        connection.commit()

    rejected = executor.rollback_transaction(workspace, applied.transaction_id)

    assert rejected.state == "ROLLBACK_PARTIAL"
    assert outside.exists()
    assert target.exists()

    with closing(sqlite3.connect(workspace.database_path)) as connection:
        connection.execute(
            "UPDATE operation_journal SET target_path=? WHERE transaction_id=?",
            (str(target), applied.transaction_id),
        )
        connection.execute(
            "UPDATE operation_journal SET state='DONE' WHERE transaction_id=?",
            (applied.transaction_id,),
        )
        connection.execute(
            "UPDATE operation_transaction SET state='DONE' WHERE transaction_id=?",
            (applied.transaction_id,),
        )
        connection.commit()
    rolled_back = executor.rollback_transaction(workspace, applied.transaction_id)
    rerun = executor.rollback_transaction(workspace, applied.transaction_id)
    assert rolled_back.state == rerun.state == "ROLLED_BACK"
    assert not target.exists()
    assert outside.exists()


def test_t_g_017_move_rollback_restores_original_path_and_hash(tmp_path: Path) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(tmp_path, mode="MOVE")
    source = Path(plan.entries[0].source_path)
    target = Path(plan.entries[0].target_path)
    expected_hash = plan.entries[0].expected_sha256

    applied = executor.apply_plan(workspace, report, dry_run=False)
    rolled_back = executor.rollback_transaction(workspace, applied.transaction_id)

    assert rolled_back.state == "ROLLED_BACK"
    assert source.exists() and not target.exists()
    assert hashlib.sha256(source.read_bytes()).hexdigest() == expected_hash


def test_t_g_018_modified_target_is_never_deleted_by_rollback(tmp_path: Path) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(tmp_path)
    applied = executor.apply_plan(workspace, report, dry_run=False)
    target = Path(plan.entries[0].target_path)
    target.write_bytes(b"user-modified-after-apply")

    rolled_back = executor.rollback_transaction(workspace, applied.transaction_id)

    assert rolled_back.state == "ROLLBACK_PARTIAL"
    assert rolled_back.failed_count == 1
    assert target.read_bytes() == b"user-modified-after-apply"
    assert _journal_rows(workspace)[0]["state"] == "ROLLBACK_FAILED"


def test_t_g_020_permission_failure_is_recoverable_and_never_done(tmp_path: Path) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(tmp_path)

    def denied(stage: str, _media_id: int) -> None:
        if stage == "BEFORE_PARTIAL_COPY":
            raise PermissionError("synthetic permission denial")

    result = executor.apply_plan(
        workspace,
        report,
        dry_run=False,
        fault_injector=denied,
    )

    assert result.state == "PARTIAL"
    assert result.failed_count == 1
    assert not Path(plan.entries[0].target_path).exists()
    assert Path(plan.entries[0].source_path).exists()
    assert _journal_rows(workspace)[0]["state"] == "FAILED"


def test_apply_requires_exact_current_payload_and_approval_revision_under_lock(
    tmp_path: Path,
) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(tmp_path)
    revoke_plan(workspace, plan.plan_id)
    approve_plan(workspace, plan.plan_id)

    with pytest.raises(executor.ApprovalContractError):
        executor.apply_plan(workspace, report, dry_run=False)

    assert not Path(plan.entries[0].target_path).exists()
    assert _journal_rows(workspace) == []


def test_apply_rejects_target_parent_reparse_created_after_preflight(tmp_path: Path) -> None:
    executor = _executor()
    workspace, plan, report, _source, output = _seed_plan(tmp_path)
    target = Path(plan.entries[0].target_path)
    external = tmp_path / "external"
    external.mkdir()
    output.mkdir()
    first_parent = output / target.relative_to(output).parts[0]
    if os.name == "nt":
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(first_parent), str(external)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            pytest.fail(completed.stderr or completed.stdout)
    else:  # pragma: no cover - Windows is the current P0 platform
        first_parent.symlink_to(external, target_is_directory=True)

    with pytest.raises(executor.RecoverySafetyError, match="TARGET_PARENT_REPARSE"):
        executor.apply_plan(workspace, report, dry_run=False)

    assert list(external.iterdir()) == []
    assert not target.exists()
    assert _journal_rows(workspace) == []


def test_cli_apply_doctor_resume_and_rollback_commands_are_registered() -> None:
    from smart_photo_triage.cli import build_parser

    help_text = build_parser().format_help()
    for command in ("apply", "doctor", "resume", "rollback"):
        assert command in help_text


@pytest.mark.parametrize(
    ("fault_stage", "mode", "doctor_code"),
    [
        ("AFTER_PREPARED", "COPY", "PREPARED_RESUMABLE"),
        ("AFTER_FINALIZE", "COPY", "COPIED_VERIFIED_RESUMABLE"),
        ("AFTER_SOURCE_DELETE", "MOVE", "SOURCE_MISSING_TARGET_VERIFIED"),
    ],
)
def test_doctor_diagnoses_durable_recovery_states(
    tmp_path: Path, fault_stage: str, mode: str, doctor_code: str
) -> None:
    executor = _executor()
    workspace, _plan, report, _source, _output = _seed_plan(tmp_path, mode=mode)

    def crash(stage: str, _media_id: int) -> None:
        if stage == fault_stage:
            raise SyntheticCrash(stage)

    with pytest.raises(SyntheticCrash):
        executor.apply_plan(workspace, report, dry_run=False, fault_injector=crash)

    codes = {item.code for item in executor.doctor_workspace(workspace).diagnoses}
    assert doctor_code in codes
    assert "RESUME_AVAILABLE" in codes


def test_doctor_reports_orphan_partial_target_mismatch_and_stale_plan(tmp_path: Path) -> None:
    executor = _executor()
    workspace, plan, report, _source, output = _seed_plan(tmp_path)
    orphan = output / "orphan.spt.partial"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"unowned orphan")

    def crash(stage: str, _media_id: int) -> None:
        if stage == "AFTER_FINALIZE":
            raise SyntheticCrash(stage)

    with pytest.raises(SyntheticCrash):
        executor.apply_plan(workspace, report, dry_run=False, fault_injector=crash)
    target = Path(plan.entries[0].target_path)
    target.write_bytes(b"user replacement")
    Path(plan.entries[0].source_path).write_bytes(b"stale source replacement")

    diagnosis = executor.doctor_workspace(workspace)
    codes = {item.code for item in diagnosis.diagnoses}
    assert {"ORPHAN_PARTIAL", "TARGET_HASH_MISMATCH", "PLAN_STALE"} <= codes
    assert orphan.read_bytes() == b"unowned orphan"
    assert target.read_bytes() == b"user replacement"


def test_source_changed_after_preflight_is_rejected_under_mutation_lock(tmp_path: Path) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(tmp_path)
    source = Path(plan.entries[0].source_path)
    source.write_bytes(b"changed after read-only preflight")

    with pytest.raises(executor.RecoverySafetyError, match="STALE_SOURCE"):
        executor.apply_plan(workspace, report, dry_run=False)

    assert source.read_bytes() == b"changed after read-only preflight"
    assert not Path(plan.entries[0].target_path).exists()
    assert _journal_rows(workspace) == []


def test_finalize_collision_after_temp_verify_is_no_overwrite(tmp_path: Path) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(tmp_path)
    target = Path(plan.entries[0].target_path)

    def collide(stage: str, _media_id: int) -> None:
        if stage == "BEFORE_FINALIZE":
            target.write_bytes(b"external final collision")

    result = executor.apply_plan(
        workspace,
        report,
        dry_run=False,
        fault_injector=collide,
    )

    assert result.state == "PARTIAL"
    assert target.read_bytes() == b"external final collision"
    assert _journal_rows(workspace)[0]["state"] == "FAILED"


def test_move_source_replacement_before_delete_is_retained(tmp_path: Path) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(tmp_path, mode="MOVE")
    source = Path(plan.entries[0].source_path)
    target = Path(plan.entries[0].target_path)

    def replace_source(stage: str, _media_id: int) -> None:
        if stage == "BEFORE_SOURCE_DELETE":
            source.write_bytes(b"external source replacement")

    result = executor.apply_plan(
        workspace,
        report,
        dry_run=False,
        fault_injector=replace_source,
    )

    assert result.state == "PARTIAL"
    assert source.read_bytes() == b"external source replacement"
    assert target.exists()
    assert _journal_rows(workspace)[0]["state"] == "FAILED"


def test_move_target_replacement_before_source_delete_retains_source(tmp_path: Path) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(tmp_path, mode="MOVE")
    source = Path(plan.entries[0].source_path)
    target = Path(plan.entries[0].target_path)
    expected_source = source.read_bytes()

    def replace_target(stage: str, _media_id: int) -> None:
        if stage == "BEFORE_SOURCE_DELETE":
            target.write_bytes(b"external target replacement")

    result = executor.apply_plan(
        workspace,
        report,
        dry_run=False,
        fault_injector=replace_target,
    )

    assert result.state == "PARTIAL"
    assert source.read_bytes() == expected_source
    assert target.read_bytes() == b"external target replacement"
    assert _journal_rows(workspace)[0]["state"] == "FAILED"


def test_modified_partial_is_retained_reported_and_not_resumed(tmp_path: Path) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(tmp_path)

    def crash(stage: str, _media_id: int) -> None:
        if stage == "AFTER_PARTIAL_COPY":
            raise SyntheticCrash(stage)

    with pytest.raises(SyntheticCrash):
        executor.apply_plan(workspace, report, dry_run=False, fault_injector=crash)
    target = Path(plan.entries[0].target_path)
    partials = list(target.parent.glob("*.partial"))
    assert len(partials) == 1
    partials[0].write_bytes(b"external partial replacement")

    diagnosis = executor.doctor_workspace(workspace)
    assert "PARTIAL_HASH_MISMATCH" in {item.code for item in diagnosis.diagnoses}
    resumed = executor.resume_transaction(workspace, _transaction_id(workspace))

    assert resumed.state == "PARTIAL"
    assert partials[0].read_bytes() == b"external partial replacement"
    assert not target.exists()


def test_cli_default_dry_run_execute_doctor_resume_and_rollback(
    tmp_path: Path, capsys: Any
) -> None:
    executor = _executor()
    workspace, plan, _report, _source, _output = _seed_plan(tmp_path)
    workspace_arg = str(workspace.root)

    assert cli_module.main(["apply", plan.plan_id, "--workspace", workspace_arg]) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["state"] == "DRY_RUN"
    assert not Path(plan.entries[0].target_path).exists()

    def crash(stage: str, _media_id: int) -> None:
        if stage == "AFTER_PREPARED":
            raise SyntheticCrash(stage)

    report = preflight_plan(workspace, plan.plan_id)
    with pytest.raises(SyntheticCrash):
        executor.apply_plan(workspace, report, dry_run=False, fault_injector=crash)
    transaction_id = _transaction_id(workspace)

    assert cli_module.main(["doctor", "--workspace", workspace_arg]) in {0, 2}
    capsys.readouterr()
    assert cli_module.main(["resume", transaction_id, "--workspace", workspace_arg]) == 0
    capsys.readouterr()
    assert cli_module.main(["rollback", transaction_id, "--workspace", workspace_arg]) == 0
    capsys.readouterr()


def test_published_migration_v1_through_v11_statement_bytes_are_unchanged() -> None:
    published = {
        1: "993180e45461a5bfd283dc7383e652cda8d431a138c0e325ea14d67e25d698af",
        2: "a3574c953b3441b529e2c830193d1151fc5f3a0c6ec37ad30469f00ee791d9eb",
        3: "10d58949816d12010ca228a5e996a66604b55857a7f1a38fc1eeff993bb7812c",
        4: "2cdbcb75e9a209a2031000255c8b2da428258a5e7528bd0b27d84fdbfb649605",
        5: "be7488ca67d7923b07d1c283f446db78f96ccea43dcc06d326fc04ad57fd3026",
        6: "7f75b2c509c431585e3def4a933493a3acd20c51ce9b6bb807582a9e6d4aa7b4",
        7: "2ec42c9d05042d7375d37d1ccc35feb0ab63e7febd1892c97514143425c9fb42",
        8: "ee0eabb37c1b8eb8be84f649a29b6ea6bd54cc3485d65908d22216c3ccf5f69f",
        9: "43cf402b2d36408d038098b57332cf6fd3533aebbefa5f2098bab9f2013d4a92",
        10: "ab5b45be3e8ed40dc0052158718436037a5248f725c745cd592545e6d7b50193",
        11: "e184fa3c655777c5bc25aa5220a3fdeafb83515d993012bab95ae6a9edc2714b",
    }
    by_version = {migration.version: migration for migration in MIGRATIONS}
    for version, expected in published.items():
        payload = "\0".join(by_version[version].statements).encode()
        assert hashlib.sha256(payload).hexdigest() == expected


def test_apply_after_crash_never_creates_a_second_transaction_for_same_plan(
    tmp_path: Path,
) -> None:
    executor = _executor()
    workspace, _plan, report, _source, _output = _seed_plan(tmp_path)

    def crash(stage: str, _media_id: int) -> None:
        if stage == "AFTER_PREPARED":
            raise SyntheticCrash(stage)

    with pytest.raises(SyntheticCrash):
        executor.apply_plan(workspace, report, dry_run=False, fault_injector=crash)
    first_transaction = _transaction_id(workspace)

    with pytest.raises(executor.RecoverySafetyError, match="resume"):
        executor.apply_plan(workspace, report, dry_run=False)

    with closing(sqlite3.connect(workspace.database_path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM operation_transaction").fetchone() == (1,)
    assert _transaction_id(workspace) == first_transaction


def test_resume_cleans_owned_partial_after_copied_verified_commit_crash(tmp_path: Path) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(tmp_path)

    def crash(stage: str, _media_id: int) -> None:
        if stage == "AFTER_COPIED_VERIFIED":
            raise SyntheticCrash(stage)

    with pytest.raises(SyntheticCrash):
        executor.apply_plan(workspace, report, dry_run=False, fault_injector=crash)
    target = Path(plan.entries[0].target_path)
    partials = list(target.parent.glob("*.partial"))
    assert target.exists()
    assert len(partials) == 1
    assert _journal_rows(workspace)[0]["state"] == "COPIED_VERIFIED"

    resumed = executor.resume_transaction(workspace, _transaction_id(workspace))

    assert resumed.state == "DONE"
    assert target.exists()
    assert not list(target.parent.glob("*.partial"))


def test_lock_owner_write_failure_removes_only_its_malformed_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = _executor()
    workspace = initialize_workspace(tmp_path / "workspace")
    original_new_file = executor._new_binding_file

    class FailingLockStream:
        def __init__(self, stream: Any) -> None:
            self.stream = stream

        def fileno(self) -> int:
            return self.stream.fileno()

        def write(self, payload: bytes) -> None:
            self.stream.write(payload[:5])
            raise OSError("synthetic lock write failure")

        def flush(self) -> None:
            self.stream.flush()

        def close(self) -> None:
            self.stream.close()

    def failing_new_file(binding: Any, name: str) -> Any:
        stream = original_new_file(binding, name)
        return FailingLockStream(stream) if name == "apply.lock" else stream

    monkeypatch.setattr(executor, "_new_binding_file", failing_new_file)

    with (
        pytest.raises(OSError, match="synthetic lock write failure"),
        executor.workspace_mutation_lock(workspace),
    ):
        pytest.fail("lock body must not run")

    assert not (workspace.root / "state" / "apply.lock").exists()


def test_rollback_removes_only_identity_bound_owned_partial_before_finalize(
    tmp_path: Path,
) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(tmp_path)

    def crash(stage: str, _media_id: int) -> None:
        if stage == "AFTER_PARTIAL_COPY":
            raise SyntheticCrash(stage)

    with pytest.raises(SyntheticCrash):
        executor.apply_plan(workspace, report, dry_run=False, fault_injector=crash)
    target = Path(plan.entries[0].target_path)
    partials = list(target.parent.glob("*.partial"))
    assert len(partials) == 1
    assert not target.exists()

    rolled_back = executor.rollback_transaction(workspace, _transaction_id(workspace))

    assert rolled_back.state == "ROLLED_BACK"
    assert not partials[0].exists()
    assert not target.exists()


def test_doctor_rejects_reparse_output_root_without_scanning_external_partials(
    tmp_path: Path,
) -> None:
    executor = _executor()
    workspace, plan, report, _source, output = _seed_plan(tmp_path)

    def crash(stage: str, _media_id: int) -> None:
        if stage == "AFTER_PREPARED":
            raise SyntheticCrash(stage)

    with pytest.raises(SyntheticCrash):
        executor.apply_plan(workspace, report, dry_run=False, fault_injector=crash)
    assert not output.exists()
    external = tmp_path / "external-output"
    external.mkdir()
    sentinel = external / "external.partial"
    sentinel.write_bytes(b"must-not-be-scanned")
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(output), str(external)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            pytest.skip(f"junction creation unavailable: {result.stderr or result.stdout}")
    else:  # pragma: no cover - POSIX CI
        output.symlink_to(external, target_is_directory=True)

    doctor = executor.doctor_workspace(workspace)
    codes = {item.code for item in doctor.diagnoses}

    assert Path(plan.output_root) == output
    assert "OUTPUT_ROOT_UNSAFE" in codes
    assert not any(
        item.code == "ORPHAN_PARTIAL" and item.path == str(sentinel) for item in doctor.diagnoses
    )
    assert sentinel.read_bytes() == b"must-not-be-scanned"


@pytest.mark.parametrize(
    "payload",
    [
        b"x" * 4097,
        b"[]",
        json.dumps({"pid": True, "token": "token"}).encode(),
        json.dumps({"pid": 123, "token": ""}).encode(),
    ],
)
def test_doctor_and_mutation_lock_retain_malformed_or_unprovable_owner(
    tmp_path: Path, payload: bytes
) -> None:
    executor = _executor()
    workspace = initialize_workspace(tmp_path / hashlib.sha256(payload).hexdigest()[:12])
    lock = workspace.root / "state" / "apply.lock"
    lock.write_bytes(payload)

    doctor = executor.doctor_workspace(workspace, process_liveness=lambda _pid, _token: "BOGUS")

    assert doctor.lock_status == "UNKNOWN"
    assert "UNKNOWN_LOCK" in {item.code for item in doctor.diagnoses}
    with (
        pytest.raises(executor.WorkspaceLockedError),
        executor.workspace_mutation_lock(workspace, process_liveness=lambda _pid, _token: "DEAD"),
    ):
        pytest.fail("unprovable owner must not be reclaimed")
    assert lock.read_bytes() == payload


def test_default_process_liveness_observes_real_live_and_dead_process() -> None:
    executor = _executor()
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert executor._default_process_liveness(0, "token") == "UNKNOWN"
        assert executor._default_process_liveness(os.getpid(), "token") == "LIVE"
        assert executor._default_process_liveness(process.pid, "token") == "LIVE"
    finally:
        process.terminate()
        process.wait(timeout=10)
    assert executor._default_process_liveness(process.pid, "token") == "DEAD"


def test_resume_rejects_missing_revoked_and_rolled_back_transactions(tmp_path: Path) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(tmp_path)
    missing = f"tx-{'0' * 32}-r1-{'0' * 64}"
    with pytest.raises(executor.RecoverySafetyError, match="not found"):
        executor.resume_transaction(workspace, missing)

    def crash(stage: str, _media_id: int) -> None:
        if stage == "AFTER_PREPARED":
            raise SyntheticCrash(stage)

    with pytest.raises(SyntheticCrash):
        executor.apply_plan(workspace, report, dry_run=False, fault_injector=crash)
    transaction_id = _transaction_id(workspace)
    revoke_plan(workspace, plan.plan_id)
    with pytest.raises(executor.ApprovalContractError):
        executor.resume_transaction(workspace, transaction_id)
    approve_plan(workspace, plan.plan_id)
    rolled_back = executor.rollback_transaction(workspace, transaction_id)
    assert rolled_back.state == "ROLLED_BACK"
    with pytest.raises(executor.RecoverySafetyError, match="rolled-back"):
        executor.resume_transaction(workspace, transaction_id)


def test_resume_done_is_idempotent_and_invalid_transaction_contract_is_rejected(
    tmp_path: Path,
) -> None:
    executor = _executor()
    workspace, _plan, report, _source, _output = _seed_plan(tmp_path)
    applied = executor.apply_plan(workspace, report, dry_run=False)
    assert executor.resume_transaction(workspace, applied.transaction_id) == applied

    with closing(sqlite3.connect(workspace.database_path)) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            "UPDATE operation_transaction SET transaction_id='invalid-contract',state='PARTIAL' "
            "WHERE transaction_id=?",
            (applied.transaction_id,),
        )
        connection.execute(
            "UPDATE operation_journal SET transaction_id='invalid-contract' WHERE transaction_id=?",
            (applied.transaction_id,),
        )
        connection.commit()
    with pytest.raises(executor.RecoverySafetyError, match="approval contract is invalid"):
        executor.resume_transaction(workspace, "invalid-contract")


def test_resume_finalizing_with_linked_target_converges_without_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(tmp_path)
    original_link = executor._binding_link

    def link_then_crash(binding: Any, source: str, target: str) -> None:
        original_link(binding, source, target)
        raise SyntheticCrash("AFTER_LINK_BEFORE_VERIFY")

    monkeypatch.setattr(executor, "_binding_link", link_then_crash)
    with pytest.raises(SyntheticCrash):
        executor.apply_plan(workspace, report, dry_run=False)
    assert _journal_rows(workspace)[0]["state"] == "FINALIZING"
    monkeypatch.setattr(executor, "_binding_link", original_link)

    resumed = executor.resume_transaction(workspace, _transaction_id(workspace))

    assert resumed.state == "DONE"
    assert Path(plan.entries[0].target_path).exists()
    assert not list(Path(plan.entries[0].target_path).parent.glob("*.partial"))


def test_rollback_refuses_changed_preexisting_move_target_when_source_is_missing(
    tmp_path: Path,
) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(tmp_path, mode="MOVE")
    source = Path(plan.entries[0].source_path)
    target = Path(plan.entries[0].target_path)
    target.parent.mkdir(parents=True)
    target.write_bytes(source.read_bytes())
    applied = executor.apply_plan(workspace, report, dry_run=False)
    target.write_bytes(b"changed unowned target")

    rolled_back = executor.rollback_transaction(workspace, applied.transaction_id)

    assert rolled_back.state == "ROLLBACK_PARTIAL"
    assert not source.exists()
    assert target.read_bytes() == b"changed unowned target"


def test_rollback_refuses_external_target_created_after_prepared(tmp_path: Path) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(tmp_path)

    def crash(stage: str, _media_id: int) -> None:
        if stage == "AFTER_PREPARED":
            raise SyntheticCrash(stage)

    with pytest.raises(SyntheticCrash):
        executor.apply_plan(workspace, report, dry_run=False, fault_injector=crash)
    target = Path(plan.entries[0].target_path)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"external target")

    rolled_back = executor.rollback_transaction(workspace, _transaction_id(workspace))

    assert rolled_back.state == "ROLLBACK_PARTIAL"
    assert target.read_bytes() == b"external target"


def test_rollback_retains_modified_owned_partial(tmp_path: Path) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(tmp_path)

    def crash(stage: str, _media_id: int) -> None:
        if stage == "AFTER_PARTIAL_COPY":
            raise SyntheticCrash(stage)

    with pytest.raises(SyntheticCrash):
        executor.apply_plan(workspace, report, dry_run=False, fault_injector=crash)
    partial = next(Path(plan.entries[0].target_path).parent.glob("*.partial"))
    partial.write_bytes(b"externally modified partial")

    rolled_back = executor.rollback_transaction(workspace, _transaction_id(workspace))

    assert rolled_back.state == "ROLLBACK_PARTIAL"
    assert partial.read_bytes() == b"externally modified partial"


def test_move_rollback_refuses_conflicting_recreated_source(tmp_path: Path) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(tmp_path, mode="MOVE")
    source = Path(plan.entries[0].source_path)
    target = Path(plan.entries[0].target_path)
    applied = executor.apply_plan(workspace, report, dry_run=False)
    source.write_bytes(b"external source conflict")

    rolled_back = executor.rollback_transaction(workspace, applied.transaction_id)

    assert rolled_back.state == "ROLLBACK_PARTIAL"
    assert source.read_bytes() == b"external source conflict"
    assert target.exists()


def test_doctor_reports_invalid_plan_journal_mismatch_and_missing_owned_files(
    tmp_path: Path,
) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(tmp_path)
    applied = executor.apply_plan(workspace, report, dry_run=False)
    target = Path(plan.entries[0].target_path)
    target.unlink()

    missing = executor.doctor_workspace(workspace)
    assert "TARGET_MISSING" in {item.code for item in missing.diagnoses}

    with closing(sqlite3.connect(workspace.database_path)) as connection:
        connection.execute(
            "UPDATE operation_journal SET source_path=source_path || '.tampered' "
            "WHERE transaction_id=?",
            (applied.transaction_id,),
        )
        connection.commit()
    mismatch = executor.doctor_workspace(workspace)
    assert "JOURNAL_PLAN_MISMATCH" in {item.code for item in mismatch.diagnoses}

    with closing(sqlite3.connect(workspace.database_path)) as connection:
        connection.execute("DROP TRIGGER organization_plan_no_update")
        connection.execute(
            "UPDATE organization_plan SET canonical_json=canonical_json || 'x' WHERE plan_id=?",
            (plan.plan_id,),
        )
        connection.commit()
    invalid = executor.doctor_workspace(workspace)
    assert "PLAN_INVALID" in {item.code for item in invalid.diagnoses}


def test_doctor_reports_missing_owned_partial(tmp_path: Path) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(tmp_path)

    def crash(stage: str, _media_id: int) -> None:
        if stage == "AFTER_PARTIAL_COPY":
            raise SyntheticCrash(stage)

    with pytest.raises(SyntheticCrash):
        executor.apply_plan(workspace, report, dry_run=False, fault_injector=crash)
    partial = next(Path(plan.entries[0].target_path).parent.glob("*.partial"))
    partial.unlink()

    doctor = executor.doctor_workspace(workspace)

    assert "PARTIAL_MISSING" in {item.code for item in doctor.diagnoses}


@pytest.mark.parametrize(
    "metadata_value",
    [
        "{not-json",
        json.dumps({"partial_path": "placeholder", "partial_identity": {}}),
        json.dumps(
            {
                "partial_path": "placeholder",
                "partial_identity": {
                    "device": 1,
                    "inode": 1,
                    "size": -1,
                    "mtime_ns": 1,
                    "sha256": "short",
                },
            }
        ),
    ],
)
def test_doctor_normalizes_corrupt_journal_metadata_without_mutation(
    tmp_path: Path, metadata_value: str
) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(tmp_path)

    def crash(stage: str, _media_id: int) -> None:
        if stage == "AFTER_PARTIAL_COPY":
            raise SyntheticCrash(stage)

    with pytest.raises(SyntheticCrash):
        executor.apply_plan(workspace, report, dry_run=False, fault_injector=crash)
    partial = next(Path(plan.entries[0].target_path).parent.glob("*.partial"))
    stored = metadata_value.replace("placeholder", str(partial).replace("\\", "\\\\"))
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        connection.execute("UPDATE operation_journal SET error=?", (stored,))
        connection.commit()

    doctor = executor.doctor_workspace(workspace)

    codes = {item.code for item in doctor.diagnoses}
    assert codes & {"ORPHAN_PARTIAL", "PARTIAL_HASH_MISMATCH"}
    assert partial.exists()


def test_source_change_during_copy_and_partial_change_during_verify_are_retained(
    tmp_path: Path,
) -> None:
    executor = _executor()
    first, first_plan, first_report, _source, _output = _seed_plan(tmp_path / "source-race")
    first_source = Path(first_plan.entries[0].source_path)

    def change_source(stage: str, _media_id: int) -> None:
        if stage == "BEFORE_PARTIAL_COPY":
            first_source.write_bytes(b"changed at copy boundary")

    source_result = executor.apply_plan(
        first, first_report, dry_run=False, fault_injector=change_source
    )
    assert source_result.state == "PARTIAL"
    assert first_source.read_bytes() == b"changed at copy boundary"

    second, second_plan, second_report, _source, _output = _seed_plan(tmp_path / "partial-race")
    second_target = Path(second_plan.entries[0].target_path)

    def change_partial(stage: str, _media_id: int) -> None:
        if stage == "AFTER_PARTIAL_COPY":
            next(second_target.parent.glob("*.partial")).write_bytes(b"changed partial")

    partial_result = executor.apply_plan(
        second, second_report, dry_run=False, fault_injector=change_partial
    )
    assert partial_result.state == "PARTIAL"
    assert next(second_target.parent.glob("*.partial")).read_bytes() == b"changed partial"


@pytest.mark.parametrize("case", ["missing_metadata", "outside_partial", "target_conflict"])
def test_resume_partial_safety_failures_retain_all_unowned_paths(tmp_path: Path, case: str) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(tmp_path)

    def crash(stage: str, _media_id: int) -> None:
        if stage == "AFTER_PARTIAL_COPY":
            raise SyntheticCrash(stage)

    with pytest.raises(SyntheticCrash):
        executor.apply_plan(workspace, report, dry_run=False, fault_injector=crash)
    target = Path(plan.entries[0].target_path)
    partial = next(target.parent.glob("*.partial"))
    if case == "target_conflict":
        target.write_bytes(b"external target")
    else:
        row = _journal_rows(workspace)[0]
        metadata = json.loads(row["error"])
        if case == "missing_metadata":
            metadata.pop("partial_identity", None)
        else:
            outside = tmp_path / "outside.partial"
            outside.write_bytes(partial.read_bytes())
            metadata["partial_path"] = str(outside)
        with closing(sqlite3.connect(workspace.database_path)) as connection:
            connection.execute("UPDATE operation_journal SET error=?", (json.dumps(metadata),))
            connection.commit()

    resumed = executor.resume_transaction(workspace, _transaction_id(workspace))

    assert resumed.state == "PARTIAL"
    assert partial.exists()
    if target.exists():
        assert target.read_bytes() == b"external target"


@pytest.mark.parametrize("case", ["partial_identity", "target_changed", "unsupported_state"])
def test_resume_rejects_corrupt_durable_final_state(tmp_path: Path, case: str) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(tmp_path)
    fault_stage = "AFTER_COPIED_VERIFIED" if case == "partial_identity" else "BEFORE_DONE"

    def crash(stage: str, _media_id: int) -> None:
        if stage == fault_stage:
            raise SyntheticCrash(stage)

    with pytest.raises(SyntheticCrash):
        executor.apply_plan(workspace, report, dry_run=False, fault_injector=crash)
    row = _journal_rows(workspace)[0]
    if case == "partial_identity":
        metadata = json.loads(row["error"])
        metadata.pop("partial_identity", None)
        update = (json.dumps(metadata), row["state"])
    elif case == "target_changed":
        Path(plan.entries[0].target_path).write_bytes(b"changed durable target")
        update = (row["error"], row["state"])
    else:
        update = (row["error"], "UNKNOWN_STATE")
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        connection.execute("UPDATE operation_journal SET error=?,state=?", update)
        connection.execute(
            "UPDATE operation_transaction SET state='PARTIAL' WHERE transaction_id=?",
            (_transaction_id(workspace),),
        )
        connection.commit()

    resumed = executor.resume_transaction(workspace, _transaction_id(workspace))

    assert resumed.state == "PARTIAL"
    assert _journal_rows(workspace)[0]["state"] == "FAILED"


def test_resume_rejects_journal_path_mismatch(tmp_path: Path) -> None:
    executor = _executor()
    workspace, _plan, report, _source, _output = _seed_plan(tmp_path)

    def crash(stage: str, _media_id: int) -> None:
        if stage == "AFTER_PREPARED":
            raise SyntheticCrash(stage)

    with pytest.raises(SyntheticCrash):
        executor.apply_plan(workspace, report, dry_run=False, fault_injector=crash)
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        connection.execute("UPDATE operation_journal SET target_path=target_path || '.tampered'")
        connection.commit()

    resumed = executor.resume_transaction(workspace, _transaction_id(workspace))

    assert resumed.state == "PARTIAL"
    assert "JOURNAL_PLAN_MISMATCH" in str(_journal_rows(workspace)[0]["error"])


def test_apply_rechecks_approval_inside_acquired_mutation_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(tmp_path)
    original_lock = executor.workspace_mutation_lock

    @contextmanager
    def revoke_inside_lock(*args: Any, **kwargs: Any) -> Any:
        with original_lock(*args, **kwargs) as lease:
            revoke_plan(workspace, plan.plan_id)
            yield lease

    monkeypatch.setattr(executor, "workspace_mutation_lock", revoke_inside_lock)
    with pytest.raises(executor.ApprovalContractError, match="before mutation"):
        executor.apply_plan(workspace, report, dry_run=False)

    assert _journal_rows(workspace) == []
    assert not Path(plan.entries[0].target_path).exists()


def test_apply_rejects_unsuccessful_preflight_contract_without_lock(tmp_path: Path) -> None:
    executor = _executor()
    workspace, _plan, report, _source, _output = _seed_plan(tmp_path)
    rejected = replace(report, approval_state="REVOKED")

    with pytest.raises(executor.ApprovalContractError, match="successful approved"):
        executor.apply_plan(workspace, rejected, dry_run=False)

    assert not (workspace.root / "state" / "apply.lock").exists()


def test_move_rollback_accepts_already_restored_same_hash_source_and_is_idempotent(
    tmp_path: Path,
) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(tmp_path, mode="MOVE")
    source = Path(plan.entries[0].source_path)
    target = Path(plan.entries[0].target_path)
    expected = source.read_bytes()
    applied = executor.apply_plan(workspace, report, dry_run=False)
    source.write_bytes(expected)

    first = executor.rollback_transaction(workspace, applied.transaction_id)
    second = executor.rollback_transaction(workspace, applied.transaction_id)

    assert first.state == second.state == "ROLLED_BACK"
    assert source.read_bytes() == expected
    assert not target.exists()


def test_rollback_rejects_journal_mismatch_and_converges_when_owned_partial_missing(
    tmp_path: Path,
) -> None:
    executor = _executor()
    mismatch, _plan, mismatch_report, _source, _output = _seed_plan(tmp_path / "mismatch")

    def prepared_crash(stage: str, _media_id: int) -> None:
        if stage == "AFTER_PREPARED":
            raise SyntheticCrash(stage)

    with pytest.raises(SyntheticCrash):
        executor.apply_plan(mismatch, mismatch_report, dry_run=False, fault_injector=prepared_crash)
    with closing(sqlite3.connect(mismatch.database_path)) as connection:
        connection.execute("UPDATE operation_journal SET target_path=target_path || '.tampered'")
        connection.commit()
    refused = executor.rollback_transaction(mismatch, _transaction_id(mismatch))
    assert refused.state == "ROLLBACK_PARTIAL"

    missing, missing_plan, missing_report, _source, _output = _seed_plan(tmp_path / "missing")

    def partial_crash(stage: str, _media_id: int) -> None:
        if stage == "AFTER_PARTIAL_COPY":
            raise SyntheticCrash(stage)

    with pytest.raises(SyntheticCrash):
        executor.apply_plan(missing, missing_report, dry_run=False, fault_injector=partial_crash)
    partial = next(Path(missing_plan.entries[0].target_path).parent.glob("*.partial"))
    partial.unlink()
    converged = executor.rollback_transaction(missing, _transaction_id(missing))
    assert converged.state == "ROLLED_BACK"


def test_doctor_rejects_journal_media_mismatch_and_unsafe_target_type(tmp_path: Path) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(tmp_path)
    applied = executor.apply_plan(workspace, report, dry_run=False)
    target = Path(plan.entries[0].target_path)
    target.unlink()
    target.mkdir()
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            "UPDATE operation_journal SET media_id=media_id+99999 WHERE transaction_id=?",
            (applied.transaction_id,),
        )
        connection.commit()

    doctor = executor.doctor_workspace(workspace)

    assert "JOURNAL_PLAN_MISMATCH" in {item.code for item in doctor.diagnoses}


def test_doctor_skips_nested_reparse_partial_and_rejects_file_output_root(
    tmp_path: Path,
) -> None:
    executor = _executor()
    workspace, plan, _report, _source, output = _seed_plan(tmp_path / "nested")
    output.mkdir()
    external = tmp_path / "external-doctor"
    external.mkdir()
    sentinel = external / "outside.partial"
    sentinel.write_bytes(b"external")
    link = output / "nested-link"
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(external)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            pytest.skip(f"junction creation unavailable: {result.stderr or result.stdout}")
    else:  # pragma: no cover - POSIX CI
        link.symlink_to(external, target_is_directory=True)
    nested = executor.doctor_workspace(workspace)
    assert not any(item.path == str(sentinel) for item in nested.diagnoses)

    other, _other_plan, other_report, _source, other_output = _seed_plan(tmp_path / "file")

    def crash(stage: str, _media_id: int) -> None:
        if stage == "AFTER_PREPARED":
            raise SyntheticCrash(stage)

    with pytest.raises(SyntheticCrash):
        executor.apply_plan(other, other_report, dry_run=False, fault_injector=crash)
    other_output.write_bytes(b"not a directory")
    unsafe = executor.doctor_workspace(other)
    assert "OUTPUT_ROOT_UNSAFE" in {item.code for item in unsafe.diagnoses}
    assert Path(plan.output_root) == output


def test_stable_hash_rejects_nonregular_and_inflight_identity_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = _executor()
    root = tmp_path / "root"
    root.mkdir()
    path = root / "file.bin"
    path.write_bytes(b"stable")
    real_fstat = executor.os.fstat

    directory_state = SimpleNamespace(st_mode=stat_module.S_IFDIR)
    monkeypatch.setattr(executor.os, "fstat", lambda _fd: directory_state)
    with pytest.raises(executor.RecoverySafetyError, match="not a regular"):
        executor._stable_hash(path, root)

    calls = 0

    def changing_fstat(fd: int) -> Any:
        nonlocal calls
        calls += 1
        state = real_fstat(fd)
        if calls == 2:
            return SimpleNamespace(
                st_mode=state.st_mode,
                st_dev=state.st_dev,
                st_ino=state.st_ino,
                st_size=state.st_size,
                st_mtime_ns=state.st_mtime_ns + 1,
            )
        return state

    monkeypatch.setattr(executor.os, "fstat", changing_fstat)
    with pytest.raises(executor.RecoverySafetyError, match="changed during verification"):
        executor._stable_hash(path, root)


def test_core_path_and_quarantine_primitives_refuse_escape_and_identity_races(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = _executor()
    root = tmp_path / "owned"
    root.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    outside_identity = executor._stable_hash(outside, tmp_path)
    with pytest.raises(executor.RecoverySafetyError, match="outside"):
        executor._quarantine_delete(outside, root, outside_identity, prefix="escape")
    assert outside.read_bytes() == b"outside"

    changed = root / "changed.bin"
    changed.write_bytes(b"before")
    changed_identity = executor._stable_hash(changed, root)
    changed.write_bytes(b"after")
    with pytest.raises(executor.RecoverySafetyError, match="identity changed"):
        executor._quarantine_delete(changed, root, changed_identity, prefix="identity")
    assert changed.read_bytes() == b"after"

    race = root / "race.bin"
    race.write_bytes(b"race")
    race_identity = executor._stable_hash(race, root)
    original_hash = executor._stable_hash

    def mismatched_quarantine(path: Path, owned_root: Path) -> Any:
        identity = original_hash(path, owned_root)
        if path.name.startswith(".race-"):
            return replace(identity, sha256="0" * 64)
        return identity

    monkeypatch.setattr(executor, "_stable_hash", mismatched_quarantine)
    with pytest.raises(executor.RecoverySafetyError, match="binding deletion"):
        executor._quarantine_delete(race, root, race_identity, prefix="race")
    assert race.read_bytes() == b"race"

    missing_drive = (
        Path("Z:/phase-g-missing/child") if os.name == "nt" else Path("/phase-g-missing/child")
    )
    with pytest.raises(executor.RecoverySafetyError):
        executor._nearest_existing_directory(missing_drive)
    with (
        pytest.raises(executor.RecoverySafetyError, match="TARGET_OUTSIDE_OUTPUT"),
        executor._target_parent_binding(root, outside),
    ):
        pytest.fail("outside target binding must not open")
    with pytest.raises(executor.RecoverySafetyError, match="escaped source root"):
        executor._copy_restore(outside, tmp_path, outside, root, outside_identity.sha256)


def test_doctor_treats_empty_optional_journal_metadata_as_empty_object(tmp_path: Path) -> None:
    executor = _executor()
    workspace, _plan, report, _source, _output = _seed_plan(tmp_path)

    def crash(stage: str, _media_id: int) -> None:
        if stage == "AFTER_PREPARED":
            raise SyntheticCrash(stage)

    with pytest.raises(SyntheticCrash):
        executor.apply_plan(workspace, report, dry_run=False, fault_injector=crash)
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        connection.execute("UPDATE operation_journal SET error=NULL")
        connection.commit()

    doctor = executor.doctor_workspace(workspace)

    assert "PREPARED_RESUMABLE" in {item.code for item in doctor.diagnoses}


def test_final_link_hash_change_and_finalizing_conflict_retain_recoverable_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = _executor()
    first, first_plan, first_report, _source, _output = _seed_plan(tmp_path / "link-hash")
    first_target = Path(first_plan.entries[0].target_path)
    original_link = executor._binding_link

    def link_then_modify(binding: Any, source: str, target: str) -> None:
        original_link(binding, source, target)
        first_target.write_bytes(b"changed after hard link")

    monkeypatch.setattr(executor, "_binding_link", link_then_modify)
    changed = executor.apply_plan(first, first_report, dry_run=False)
    assert changed.state == "PARTIAL"
    assert Path(first_plan.entries[0].source_path).exists()

    second, second_plan, second_report, _source, _output = _seed_plan(
        tmp_path / "finalizing-conflict"
    )
    second_target = Path(second_plan.entries[0].target_path)

    def link_then_crash(binding: Any, source: str, target: str) -> None:
        original_link(binding, source, target)
        raise SyntheticCrash("after-link")

    monkeypatch.setattr(executor, "_binding_link", link_then_crash)
    with pytest.raises(SyntheticCrash):
        executor.apply_plan(second, second_report, dry_run=False)
    second_target.unlink()
    second_target.write_bytes(b"external finalizing conflict")
    monkeypatch.setattr(executor, "_binding_link", original_link)

    resumed = executor.resume_transaction(second, _transaction_id(second))
    assert resumed.state == "PARTIAL"
    assert second_target.read_bytes() == b"external finalizing conflict"


def test_failed_partial_and_corrupt_copied_verified_metadata_are_not_discarded(
    tmp_path: Path,
) -> None:
    executor = _executor()
    failed, failed_plan, failed_report, _source, _output = _seed_plan(tmp_path / "failed")

    def ordinary_failure(stage: str, _media_id: int) -> None:
        if stage == "AFTER_PARTIAL_COPY":
            raise RuntimeError("ordinary failure")

    result = executor.apply_plan(
        failed, failed_report, dry_run=False, fault_injector=ordinary_failure
    )
    assert result.state == "PARTIAL"
    failed_partial = next(Path(failed_plan.entries[0].target_path).parent.glob("*.partial"))
    retried = executor.resume_transaction(failed, result.transaction_id)
    assert retried.state == "PARTIAL"
    assert failed_partial.exists()

    copied, _copied_plan, copied_report, _source, _output = _seed_plan(tmp_path / "copied")

    def copied_crash(stage: str, _media_id: int) -> None:
        if stage == "BEFORE_DONE":
            raise SyntheticCrash(stage)

    with pytest.raises(SyntheticCrash):
        executor.apply_plan(copied, copied_report, dry_run=False, fault_injector=copied_crash)
    row = _journal_rows(copied)[0]
    metadata = json.loads(row["error"])
    metadata.pop("target_identity", None)
    with closing(sqlite3.connect(copied.database_path)) as connection:
        connection.execute("UPDATE operation_journal SET error=?", (json.dumps(metadata),))
        connection.commit()
    resumed = executor.resume_transaction(copied, _transaction_id(copied))
    assert resumed.state == "PARTIAL"


def test_copy_source_identity_drift_and_final_identity_mismatch_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = _executor()
    first, first_plan, first_report, _source, _output = _seed_plan(tmp_path / "source-id")
    real_fstat = executor.os.fstat
    mutate_fstat = False
    calls = 0

    def drift_source(stage: str, _media_id: int) -> None:
        nonlocal mutate_fstat
        if stage == "BEFORE_PARTIAL_COPY":
            mutate_fstat = True

    def drifting_fstat(fd: int) -> Any:
        nonlocal calls
        state = real_fstat(fd)
        if mutate_fstat:
            calls += 1
            if calls == 2:
                return SimpleNamespace(
                    st_mode=state.st_mode,
                    st_dev=state.st_dev,
                    st_ino=state.st_ino,
                    st_size=state.st_size,
                    st_mtime_ns=state.st_mtime_ns + 1,
                )
        return state

    monkeypatch.setattr(executor.os, "fstat", drifting_fstat)
    drifted = executor.apply_plan(first, first_report, dry_run=False, fault_injector=drift_source)
    assert drifted.state == "PARTIAL"
    assert Path(first_plan.entries[0].source_path).exists()

    monkeypatch.setattr(executor.os, "fstat", real_fstat)
    second, second_plan, second_report, _source, _output = _seed_plan(tmp_path / "target-id")
    target = Path(second_plan.entries[0].target_path)
    original_hash = executor._stable_hash

    def changed_final_identity(path: Path, root: Path) -> Any:
        identity = original_hash(path, root)
        if path == target:
            return replace(identity, inode=identity.inode + 1)
        return identity

    monkeypatch.setattr(executor, "_stable_hash", changed_final_identity)
    mismatched = executor.apply_plan(second, second_report, dry_run=False)
    assert mismatched.state == "PARTIAL"
    assert Path(second_plan.entries[0].source_path).exists()


def test_copied_verified_modified_partial_and_missing_target_identity_fail_closed(
    tmp_path: Path,
) -> None:
    executor = _executor()
    partial_ws, partial_plan, partial_report, _source, _output = _seed_plan(tmp_path / "partial")

    def copied_crash(stage: str, _media_id: int) -> None:
        if stage == "AFTER_COPIED_VERIFIED":
            raise SyntheticCrash(stage)

    with pytest.raises(SyntheticCrash):
        executor.apply_plan(partial_ws, partial_report, dry_run=False, fault_injector=copied_crash)
    partial = next(Path(partial_plan.entries[0].target_path).parent.glob("*.partial"))
    partial.write_bytes(b"changed after copied verified")
    partial_result = executor.resume_transaction(partial_ws, _transaction_id(partial_ws))
    assert partial_result.state == "PARTIAL"
    assert partial.exists()

    target_ws, _target_plan, target_report, _source, _output = _seed_plan(tmp_path / "target")

    def before_done(stage: str, _media_id: int) -> None:
        if stage == "BEFORE_DONE":
            raise SyntheticCrash(stage)

    with pytest.raises(SyntheticCrash):
        executor.apply_plan(target_ws, target_report, dry_run=False, fault_injector=before_done)
    row = _journal_rows(target_ws)[0]
    metadata = json.loads(row["error"])
    metadata.pop("target_identity", None)
    with closing(sqlite3.connect(target_ws.database_path)) as connection:
        connection.execute("UPDATE operation_journal SET error=?", (json.dumps(metadata),))
        connection.commit()
    target_result = executor.resume_transaction(target_ws, _transaction_id(target_ws))
    assert target_result.state == "PARTIAL"


def test_resume_missing_plan_entry_and_preexisting_move_rollback_before_delete(
    tmp_path: Path,
) -> None:
    executor = _executor()
    missing, _plan, missing_report, _source, _output = _seed_plan(tmp_path / "missing-entry")

    def prepared_crash(stage: str, _media_id: int) -> None:
        if stage == "AFTER_PREPARED":
            raise SyntheticCrash(stage)

    with pytest.raises(SyntheticCrash):
        executor.apply_plan(missing, missing_report, dry_run=False, fault_injector=prepared_crash)
    with closing(sqlite3.connect(missing.database_path)) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("UPDATE operation_journal SET media_id=media_id+99999")
        connection.commit()
    missing_result = executor.resume_transaction(missing, _transaction_id(missing))
    assert missing_result.state == "PARTIAL"

    move, move_plan, move_report, _source, _output = _seed_plan(
        tmp_path / "preexisting", mode="MOVE"
    )
    source = Path(move_plan.entries[0].source_path)
    target = Path(move_plan.entries[0].target_path)
    target.parent.mkdir(parents=True)
    target.write_bytes(source.read_bytes())

    def delete_crash(stage: str, _media_id: int) -> None:
        if stage == "BEFORE_SOURCE_DELETE":
            raise SyntheticCrash(stage)

    with pytest.raises(SyntheticCrash):
        executor.apply_plan(move, move_report, dry_run=False, fault_injector=delete_crash)
    rolled_back = executor.rollback_transaction(move, _transaction_id(move))
    assert rolled_back.state == "ROLLED_BACK"
    assert source.exists() and target.exists()


def test_rollback_rejects_outside_partial_metadata_and_handles_existing_rolled_row(
    tmp_path: Path,
) -> None:
    executor = _executor()
    workspace, _plan, report, _source, _output = _seed_plan(tmp_path / "outside")

    def partial_crash(stage: str, _media_id: int) -> None:
        if stage == "AFTER_PARTIAL_COPY":
            raise SyntheticCrash(stage)

    with pytest.raises(SyntheticCrash):
        executor.apply_plan(workspace, report, dry_run=False, fault_injector=partial_crash)
    row = _journal_rows(workspace)[0]
    metadata = json.loads(row["error"])
    external = tmp_path / "external.partial"
    external.write_bytes(b"external")
    metadata["partial_path"] = str(external)
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        connection.execute("UPDATE operation_journal SET error=?", (json.dumps(metadata),))
        connection.commit()
    refused = executor.rollback_transaction(workspace, _transaction_id(workspace))
    assert refused.state == "ROLLBACK_PARTIAL"
    assert external.read_bytes() == b"external"

    done, _done_plan, done_report, _source, _output = _seed_plan(tmp_path / "rolled-row")
    applied = executor.apply_plan(done, done_report, dry_run=False)
    assert executor.rollback_transaction(done, applied.transaction_id).state == "ROLLED_BACK"
    with closing(sqlite3.connect(done.database_path)) as connection:
        connection.execute(
            "UPDATE operation_transaction SET state='PARTIAL' WHERE transaction_id=?",
            (applied.transaction_id,),
        )
        connection.commit()
    converged = executor.rollback_transaction(done, applied.transaction_id)
    assert converged.state == "ROLLED_BACK"


@pytest.mark.parametrize("blocked_path", ["source", "target", "partial", "output"])
def test_doctor_reports_selected_nofollow_inspection_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blocked_path: str
) -> None:
    executor = _executor()
    workspace, plan, report, _source, output = _seed_plan(tmp_path)
    source = Path(plan.entries[0].source_path)
    target = Path(plan.entries[0].target_path)
    if blocked_path == "partial":

        def crash(stage: str, _media_id: int) -> None:
            if stage == "AFTER_PARTIAL_COPY":
                raise SyntheticCrash(stage)

        with pytest.raises(SyntheticCrash):
            executor.apply_plan(workspace, report, dry_run=False, fault_injector=crash)
        blocked = next(target.parent.glob("*.partial"))
        expected_code = "PARTIAL_HASH_MISMATCH"
    else:
        executor.apply_plan(workspace, report, dry_run=False)
        blocked = {"source": source, "target": target, "output": output}[blocked_path]
        expected_code = {
            "source": "PLAN_STALE",
            "target": "TARGET_HASH_MISMATCH",
            "output": "OUTPUT_ROOT_UNSAFE",
        }[blocked_path]
    original_lstat = Path.lstat

    def selected_denial(path: Path) -> Any:
        if path == blocked:
            raise PermissionError("synthetic nofollow inspection denial")
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", selected_denial)

    doctor = executor.doctor_workspace(workspace)

    assert expected_code in {item.code for item in doctor.diagnoses}


def test_doctor_reports_journal_partial_outside_owned_output(tmp_path: Path) -> None:
    executor = _executor()
    workspace, _plan, report, _source, _output = _seed_plan(tmp_path)

    def crash(stage: str, _media_id: int) -> None:
        if stage == "AFTER_PARTIAL_COPY":
            raise SyntheticCrash(stage)

    with pytest.raises(SyntheticCrash):
        executor.apply_plan(workspace, report, dry_run=False, fault_injector=crash)
    row = _journal_rows(workspace)[0]
    metadata = json.loads(row["error"])
    outside = tmp_path / "outside-doctor.partial"
    outside.write_bytes(b"outside")
    metadata["partial_path"] = str(outside)
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        connection.execute("UPDATE operation_journal SET error=?", (json.dumps(metadata),))
        connection.commit()

    doctor = executor.doctor_workspace(workspace)

    assert "PARTIAL_HASH_MISMATCH" in {item.code for item in doctor.diagnoses}
    assert outside.read_bytes() == b"outside"


def test_lock_release_retains_externally_rewritten_owner_and_surfaces_original_write_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = _executor()
    workspace = initialize_workspace(tmp_path / "workspace")
    lock = workspace.root / "state" / "apply.lock"

    with executor.workspace_mutation_lock(workspace):
        lock.write_text(json.dumps({"pid": os.getpid(), "token": "external"}), encoding="utf-8")
    assert lock.exists()
    assert json.loads(lock.read_text(encoding="utf-8"))["token"] == "external"

    lock.unlink()
    original_new_file = executor._new_binding_file

    class VanishingFailedStream:
        def __init__(self, stream: Any) -> None:
            self.stream = stream

        def fileno(self) -> int:
            return self.stream.fileno()

        def write(self, _payload: bytes) -> None:
            self.stream.close()
            lock.unlink()
            raise OSError("synthetic vanished lock")

        def close(self) -> None:
            if not self.stream.closed:
                self.stream.close()

    def vanishing_new_file(binding: Any, name: str) -> Any:
        stream = original_new_file(binding, name)
        return VanishingFailedStream(stream) if name == "apply.lock" else stream

    monkeypatch.setattr(executor, "_new_binding_file", vanishing_new_file)
    with (
        pytest.raises(OSError, match="synthetic vanished lock"),
        executor.workspace_mutation_lock(workspace),
    ):
        pytest.fail("lock body must not run")
    assert not lock.exists()


@pytest.mark.parametrize(
    "drift",
    ["bundle", "source", "target", "capacity", "writability", "global_transaction"],
)
def test_mutation_time_preflight_under_owned_lock_refuses_all_post_report_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    executor = _executor()
    names = ("photo.jpg", "photo.mov") if drift == "bundle" else ("photo.jpg",)
    workspace, plan, report, _source, output = _seed_plan(
        tmp_path, names=names, bundle=drift == "bundle"
    )
    second_plan = None
    if drift == "global_transaction":
        second_plan = build_plan(
            workspace,
            PlannerOptions(output_root=tmp_path / "other-output", mode="COPY"),
        )
    original_lock = executor.workspace_mutation_lock
    original_access = planner_module.os.access
    original_disk_usage = planner_module.shutil.disk_usage

    @contextmanager
    def mutate_inside_lock(*args: Any, **kwargs: Any) -> Any:
        with original_lock(*args, **kwargs) as lease:
            if drift == "bundle":
                with closing(sqlite3.connect(workspace.database_path)) as connection:
                    connection.execute(
                        "DELETE FROM bundle_member WHERE media_id=?",
                        (plan.entries[-1].media_id,),
                    )
                    connection.commit()
            elif drift == "source":
                Path(plan.entries[0].source_path).write_bytes(b"post-report source drift")
            elif drift == "target":
                target = Path(plan.entries[0].target_path)
                target.parent.mkdir(parents=True)
                target.write_bytes(b"post-report target collision")
            elif drift == "capacity":
                monkeypatch.setattr(
                    planner_module.shutil,
                    "disk_usage",
                    lambda _path: SimpleNamespace(total=1, used=1, free=0),
                )
            elif drift == "writability":
                monkeypatch.setattr(planner_module.os, "access", lambda _path, _mode: False)
            else:
                assert second_plan is not None
                with closing(sqlite3.connect(workspace.database_path)) as connection:
                    connection.execute(
                        """
                        INSERT INTO operation_transaction(
                            transaction_id,plan_id,mode,state,created_at,updated_at
                        ) VALUES (?,?,?,'ACTIVE','now','now')
                        """,
                        (f"tx-external-{drift}", second_plan.plan_id, second_plan.mode),
                    )
                    connection.commit()
            yield lease

    monkeypatch.setattr(executor, "workspace_mutation_lock", mutate_inside_lock)
    before_output = _snapshot(output)
    with pytest.raises(executor.RecoverySafetyError, match="MUTATION_PREFLIGHT_FAILED"):
        executor.apply_plan(workspace, report, dry_run=False)
    monkeypatch.setattr(planner_module.os, "access", original_access)
    monkeypatch.setattr(planner_module.shutil, "disk_usage", original_disk_usage)

    with closing(sqlite3.connect(workspace.database_path)) as connection:
        transaction_count = connection.execute(
            "SELECT COUNT(*) FROM operation_transaction"
        ).fetchone()[0]
    assert transaction_count == (1 if drift == "global_transaction" else 0)
    assert _snapshot(output) == before_output or drift == "target"
    if drift == "target":
        target_key = Path(plan.entries[0].target_path).relative_to(output).as_posix()
        assert _snapshot(output) == {target_key: b"post-report target collision"}


@pytest.mark.parametrize(
    ("fault_stage", "expected_state", "source_exists", "quarantine_exists"),
    [
        ("AFTER_SOURCE_DELETE_PREPARED", "SOURCE_DELETE_PREPARED", True, False),
        ("AFTER_SOURCE_DELETE_RENAME", "SOURCE_QUARANTINED", False, True),
        ("AFTER_SOURCE_DELETE_UNLINK", "SOURCE_QUARANTINED", False, False),
    ],
)
def test_move_delete_journal_states_survive_each_crash_boundary_and_resume(
    tmp_path: Path,
    fault_stage: str,
    expected_state: str,
    source_exists: bool,
    quarantine_exists: bool,
) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(tmp_path, mode="MOVE")
    source = Path(plan.entries[0].source_path)
    target = Path(plan.entries[0].target_path)
    expected = source.read_bytes()

    def crash(stage: str, _media_id: int) -> None:
        if stage == fault_stage:
            raise SyntheticCrash(stage)

    with pytest.raises(SyntheticCrash, match=fault_stage):
        executor.apply_plan(workspace, report, dry_run=False, fault_injector=crash)
    row = _journal_rows(workspace)[0]
    metadata = json.loads(row["error"])
    quarantine = Path(str(metadata["source_quarantine_path"]))

    assert row["state"] == expected_state
    assert source.exists() is source_exists
    assert quarantine.exists() is quarantine_exists
    assert target.read_bytes() == expected
    doctor_codes = {item.code for item in executor.doctor_workspace(workspace).diagnoses}
    assert "SOURCE_DELETE_RESUMABLE" in doctor_codes

    resumed = executor.resume_transaction(workspace, _transaction_id(workspace))

    assert resumed.state == "DONE"
    assert not source.exists()
    assert not quarantine.exists()
    assert target.read_bytes() == expected


def test_move_quarantined_source_is_doctor_visible_and_rollback_restores_original(
    tmp_path: Path,
) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(tmp_path, mode="MOVE")
    source = Path(plan.entries[0].source_path)
    target = Path(plan.entries[0].target_path)
    expected = source.read_bytes()

    def crash(stage: str, _media_id: int) -> None:
        if stage == "AFTER_SOURCE_DELETE_RENAME":
            raise SyntheticCrash(stage)

    with pytest.raises(SyntheticCrash):
        executor.apply_plan(workspace, report, dry_run=False, fault_injector=crash)
    row = _journal_rows(workspace)[0]
    quarantine = Path(json.loads(row["error"])["source_quarantine_path"])
    diagnosis = executor.doctor_workspace(workspace)

    assert quarantine.exists() and not source.exists()
    assert any(
        item.code == "SOURCE_DELETE_RESUMABLE" and item.path == str(quarantine)
        for item in diagnosis.diagnoses
    )
    rolled_back = executor.rollback_transaction(workspace, _transaction_id(workspace))
    assert rolled_back.state == "ROLLED_BACK"
    assert source.read_bytes() == expected
    assert not quarantine.exists()
    assert not target.exists()


def test_move_source_removed_commit_failure_resumes_from_durable_delete_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(tmp_path, mode="MOVE")
    source = Path(plan.entries[0].source_path)
    target = Path(plan.entries[0].target_path)
    expected = source.read_bytes()
    original_update = executor._journal_update
    failed_once = False

    def fail_source_removed(*args: Any, **kwargs: Any) -> None:
        nonlocal failed_once
        state = args[2]
        if state == "SOURCE_REMOVED" and not failed_once:
            failed_once = True
            raise sqlite3.OperationalError("synthetic SOURCE_REMOVED commit failure")
        original_update(*args, **kwargs)

    monkeypatch.setattr(executor, "_journal_update", fail_source_removed)
    applied = executor.apply_plan(workspace, report, dry_run=False)
    row = _journal_rows(workspace)[0]
    metadata = json.loads(row["error"])

    assert applied.state == "PARTIAL"
    assert row["state"] == "FAILED"
    assert metadata["source_delete_phase"] in {"QUARANTINED", "UNLINKED"}
    assert target.read_bytes() == expected
    monkeypatch.setattr(executor, "_journal_update", original_update)

    resumed = executor.resume_transaction(workspace, applied.transaction_id)
    assert resumed.state == "DONE"
    rolled_back = executor.rollback_transaction(workspace, applied.transaction_id)
    assert rolled_back.state == "ROLLED_BACK"
    assert source.read_bytes() == expected
    assert not target.exists()


def test_move_target_changed_during_source_hash_is_rechecked_before_source_quarantine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(tmp_path, mode="MOVE")
    source = Path(plan.entries[0].source_path)
    target = Path(plan.entries[0].target_path)
    expected_source = source.read_bytes()
    original_match = executor._entry_matches_source
    match_calls = 0

    def hash_source_then_replace_target(entry: Any) -> Any:
        nonlocal match_calls
        match_calls += 1
        identity = original_match(entry)
        if match_calls == 2:
            target.write_bytes(b"target changed during source verification")
        return identity

    monkeypatch.setattr(executor, "_entry_matches_source", hash_source_then_replace_target)
    result = executor.apply_plan(workspace, report, dry_run=False)

    assert result.state == "PARTIAL"
    assert source.read_bytes() == expected_source
    assert target.read_bytes() == b"target changed during source verification"


def test_bundle_rollback_preflights_every_member_before_mutating_any_member(
    tmp_path: Path,
) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(
        tmp_path,
        names=("pair.jpg", "pair.mov"),
        bundle=True,
    )
    applied = executor.apply_plan(workspace, report, dry_run=False)
    targets = [Path(entry.target_path) for entry in plan.entries]
    targets[0].write_bytes(b"externally modified first bundle member")
    before = {path: path.read_bytes() for path in targets}

    rolled_back = executor.rollback_transaction(workspace, applied.transaction_id)

    assert rolled_back.state == "ROLLBACK_PARTIAL"
    assert {path: path.read_bytes() for path in targets} == before
    assert all(path.exists() for path in targets)


def test_move_prepared_rechecks_target_after_final_source_identity_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(tmp_path, mode="MOVE")
    source = Path(plan.entries[0].source_path)
    target = Path(plan.entries[0].target_path)
    expected_source = source.read_bytes()

    def crash_after_prepare(stage: str, _media_id: int) -> None:
        if stage == "AFTER_SOURCE_DELETE_PREPARED":
            raise SyntheticCrash(stage)

    with pytest.raises(SyntheticCrash):
        executor.apply_plan(
            workspace,
            report,
            dry_run=False,
            fault_injector=crash_after_prepare,
        )

    original_verify = executor._verify_identity
    source_verify_calls = 0

    def replace_target_after_final_source_hash(
        path: Path, root: Path, expected: Any, digest: str
    ) -> Any:
        nonlocal source_verify_calls
        identity = original_verify(path, root, expected, digest)
        if path == source:
            source_verify_calls += 1
            if source_verify_calls == 2:
                target.write_bytes(b"changed after final source hash")
        return identity

    monkeypatch.setattr(executor, "_verify_identity", replace_target_after_final_source_hash)
    resumed = executor.resume_transaction(workspace, _transaction_id(workspace))

    assert resumed.state == "PARTIAL"
    assert source.read_bytes() == expected_source
    assert target.read_bytes() == b"changed after final source hash"


def test_quarantine_delete_collision_is_no_overwrite_and_retains_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = _executor()
    root = tmp_path / "owned"
    root.mkdir()
    original = root / "payload.bin"
    original.write_bytes(b"owned payload")
    identity = executor._stable_hash(original, root)
    quarantine = root / ".collision-fixed"
    quarantine.write_bytes(b"external collision")
    monkeypatch.setattr(executor, "uuid4", lambda: SimpleNamespace(hex="fixed"))

    with pytest.raises(executor.RecoverySafetyError):
        executor._quarantine_delete(original, root, identity, prefix="collision")

    assert original.read_bytes() == b"owned payload"
    assert quarantine.read_bytes() == b"external collision"


def test_quarantine_delete_unsupported_hardlink_retains_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = _executor()
    root = tmp_path / "owned"
    root.mkdir()
    original = root / "payload.bin"
    original.write_bytes(b"owned payload")
    identity = executor._stable_hash(original, root)

    def unsupported_link(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("synthetic hard-link unsupported")

    monkeypatch.setattr(executor, "_binding_link_no_overwrite", unsupported_link)
    with pytest.raises(executor.RecoverySafetyError, match="QUARANTINE_UNSUPPORTED"):
        executor._quarantine_delete(original, root, identity, prefix="unsupported")

    assert original.read_bytes() == b"owned payload"


def test_doctor_bounds_database_and_filesystem_iteration(tmp_path: Path) -> None:
    executor = _executor()
    workspace, plan, _report, _source, output = _seed_plan(tmp_path)
    output.mkdir(parents=True)
    for index in range(5):
        (output / f"orphan-{index}.partial").write_bytes(b"partial")

    diagnosis = executor.doctor_workspace(workspace, max_records=2)

    assert diagnosis.ok is False
    assert "DOCTOR_SCAN_LIMIT" in {item.code for item in diagnosis.diagnoses}
    assert plan.plan_id


@pytest.mark.parametrize(
    ("corruption", "error_code"),
    [
        ("missing_identity", "SOURCE_DELETE_OWNERSHIP_UNKNOWN"),
        ("wrong_quarantine", "SOURCE_DELETE_QUARANTINE_MISMATCH"),
    ],
)
def test_move_resume_rejects_corrupt_durable_delete_envelope(
    tmp_path: Path, corruption: str, error_code: str
) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(tmp_path, mode="MOVE")
    source = Path(plan.entries[0].source_path)
    expected = source.read_bytes()

    def crash(stage: str, _media_id: int) -> None:
        if stage == "AFTER_SOURCE_DELETE_PREPARED":
            raise SyntheticCrash(stage)

    with pytest.raises(SyntheticCrash):
        executor.apply_plan(workspace, report, dry_run=False, fault_injector=crash)
    row = _journal_rows(workspace)[0]
    metadata = json.loads(row["error"])
    if corruption == "missing_identity":
        metadata.pop("source_delete_identity")
    else:
        metadata["source_quarantine_path"] = str(source.parent / "wrong.quarantine")
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        connection.execute(
            "UPDATE operation_journal SET error=? WHERE id=?",
            (json.dumps(metadata), row["id"]),
        )
        connection.commit()

    resumed = executor.resume_transaction(workspace, _transaction_id(workspace))

    assert resumed.state == "PARTIAL"
    assert source.read_bytes() == expected
    assert error_code in json.loads(_journal_rows(workspace)[0]["error"])["error_code"]


def test_move_resume_fails_closed_when_source_quarantine_link_is_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(tmp_path, mode="MOVE")
    source = Path(plan.entries[0].source_path)
    expected = source.read_bytes()

    def crash(stage: str, _media_id: int) -> None:
        if stage == "AFTER_SOURCE_DELETE_PREPARED":
            raise SyntheticCrash(stage)

    with pytest.raises(SyntheticCrash):
        executor.apply_plan(workspace, report, dry_run=False, fault_injector=crash)

    def unsupported(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("synthetic source hard-link unsupported")

    monkeypatch.setattr(executor, "_binding_link", unsupported)
    resumed = executor.resume_transaction(workspace, _transaction_id(workspace))

    assert resumed.state == "PARTIAL"
    assert source.read_bytes() == expected
    assert (
        "SOURCE_QUARANTINE_UNSUPPORTED"
        in json.loads(_journal_rows(workspace)[0]["error"])["error_code"]
    )


def test_move_prepared_state_with_only_verified_quarantine_converges(tmp_path: Path) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(tmp_path, mode="MOVE")
    source = Path(plan.entries[0].source_path)
    expected = source.read_bytes()

    def crash(stage: str, _media_id: int) -> None:
        if stage == "AFTER_SOURCE_DELETE_RENAME":
            raise SyntheticCrash(stage)

    with pytest.raises(SyntheticCrash):
        executor.apply_plan(workspace, report, dry_run=False, fault_injector=crash)
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        connection.execute("UPDATE operation_journal SET state='SOURCE_DELETE_PREPARED'")
        connection.commit()

    resumed = executor.resume_transaction(workspace, _transaction_id(workspace))

    assert resumed.state == "DONE"
    assert not source.exists()
    assert Path(plan.entries[0].target_path).read_bytes() == expected


def test_move_prepared_state_with_no_source_or_quarantine_is_retained_for_review(
    tmp_path: Path,
) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(tmp_path, mode="MOVE")
    source = Path(plan.entries[0].source_path)
    target = Path(plan.entries[0].target_path)

    def crash(stage: str, _media_id: int) -> None:
        if stage == "AFTER_SOURCE_DELETE_PREPARED":
            raise SyntheticCrash(stage)

    with pytest.raises(SyntheticCrash):
        executor.apply_plan(workspace, report, dry_run=False, fault_injector=crash)
    source.unlink()

    resumed = executor.resume_transaction(workspace, _transaction_id(workspace))

    assert resumed.state == "PARTIAL"
    assert target.exists()
    assert (
        "SOURCE_QUARANTINE_STATE_UNPROVABLE"
        in json.loads(_journal_rows(workspace)[0]["error"])["error_code"]
    )


def test_move_quarantined_state_rejects_reappeared_source(tmp_path: Path) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(tmp_path, mode="MOVE")
    source = Path(plan.entries[0].source_path)
    expected = source.read_bytes()

    def crash(stage: str, _media_id: int) -> None:
        if stage == "AFTER_SOURCE_DELETE_RENAME":
            raise SyntheticCrash(stage)

    with pytest.raises(SyntheticCrash):
        executor.apply_plan(workspace, report, dry_run=False, fault_injector=crash)
    source.write_bytes(expected)

    resumed = executor.resume_transaction(workspace, _transaction_id(workspace))

    assert resumed.state == "PARTIAL"
    assert source.read_bytes() == expected
    assert (
        "SOURCE_REAPPEARED_DURING_DELETE"
        in json.loads(_journal_rows(workspace)[0]["error"])["error_code"]
    )


def test_move_unlinked_state_rejects_reappeared_source(tmp_path: Path) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(tmp_path, mode="MOVE")
    source = Path(plan.entries[0].source_path)
    expected = source.read_bytes()

    def crash(stage: str, _media_id: int) -> None:
        if stage == "AFTER_SOURCE_DELETE_UNLINK":
            raise SyntheticCrash(stage)

    with pytest.raises(SyntheticCrash):
        executor.apply_plan(workspace, report, dry_run=False, fault_injector=crash)
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        connection.execute("UPDATE operation_journal SET state='SOURCE_DELETE_UNLINKED'")
        connection.commit()
    source.write_bytes(expected)

    resumed = executor.resume_transaction(workspace, _transaction_id(workspace))

    assert resumed.state == "PARTIAL"
    assert source.read_bytes() == expected
    assert (
        "SOURCE_DELETE_UNLINKED_STATE_MISMATCH"
        in json.loads(_journal_rows(workspace)[0]["error"])["error_code"]
    )


@pytest.mark.parametrize(
    ("mode", "fault_stage"),
    [
        ("MOVE", "AFTER_SOURCE_DELETE_PREPARED"),
        ("MOVE", "AFTER_SOURCE_DELETE_RENAME"),
        ("MOVE", "AFTER_FINALIZE"),
        ("COPY", "AFTER_TARGET_VERIFY"),
    ],
)
def test_bundle_rollback_preflight_accepts_each_owned_recovery_envelope(
    tmp_path: Path, mode: str, fault_stage: str
) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(
        tmp_path,
        mode=mode,
        names=("pair.jpg", "pair.mov"),
        bundle=True,
    )

    def crash(stage: str, _media_id: int) -> None:
        if stage == fault_stage:
            raise SyntheticCrash(stage)

    with pytest.raises(SyntheticCrash):
        executor.apply_plan(workspace, report, dry_run=False, fault_injector=crash)
    rolled_back = executor.rollback_transaction(workspace, _transaction_id(workspace))

    assert rolled_back.state == "ROLLED_BACK"
    assert all(Path(entry.source_path).exists() for entry in plan.entries)
    assert all(not Path(entry.target_path).exists() for entry in plan.entries)


def test_move_bundle_preexisting_targets_are_verified_restored_and_retained(
    tmp_path: Path,
) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(
        tmp_path,
        mode="MOVE",
        names=("pair.jpg", "pair.mov"),
        bundle=True,
    )
    targets = [Path(entry.target_path) for entry in plan.entries]
    for entry, target in zip(plan.entries, targets, strict=True):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(Path(entry.source_path).read_bytes())
    applied = executor.apply_plan(workspace, report, dry_run=False)

    rolled_back = executor.rollback_transaction(workspace, applied.transaction_id)

    assert rolled_back.state == "ROLLED_BACK"
    assert all(Path(entry.source_path).exists() for entry in plan.entries)
    assert all(target.exists() for target in targets)


def test_bundle_rollback_preflight_blocks_unowned_target_before_any_mutation(
    tmp_path: Path,
) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(
        tmp_path,
        names=("pair.jpg", "pair.mov"),
        bundle=True,
    )

    def crash(stage: str, _media_id: int) -> None:
        if stage == "AFTER_PREPARED":
            raise SyntheticCrash(stage)

    with pytest.raises(SyntheticCrash):
        executor.apply_plan(workspace, report, dry_run=False, fault_injector=crash)
    collision = Path(plan.entries[0].target_path)
    collision.parent.mkdir(parents=True, exist_ok=True)
    collision.write_bytes(b"external collision")
    before_sources = {
        Path(entry.source_path): Path(entry.source_path).read_bytes() for entry in plan.entries
    }

    rolled_back = executor.rollback_transaction(workspace, _transaction_id(workspace))

    assert rolled_back.state == "ROLLBACK_PARTIAL"
    assert collision.read_bytes() == b"external collision"
    assert {path: path.read_bytes() for path in before_sources} == before_sources


def test_doctor_rejects_invalid_limit_and_bounds_each_database_collection(
    tmp_path: Path,
) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(
        tmp_path,
        names=("pair.jpg", "pair.mov"),
    )
    executor.apply_plan(workspace, report, dry_run=False)
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        connection.execute(
            """
            INSERT INTO operation_transaction(
                transaction_id,plan_id,mode,state,created_at,updated_at
            ) VALUES ('tx-doctor-extra',?,?,'DONE','later','later')
            """,
            (plan.plan_id, plan.mode),
        )
        connection.commit()
    second_plan = build_plan(
        workspace,
        PlannerOptions(output_root=tmp_path / "second-output", mode="COPY"),
    )
    assert second_plan.plan_id != plan.plan_id

    for invalid in (0, True):
        with pytest.raises(ValueError, match="positive integer"):
            executor.doctor_workspace(workspace, max_records=invalid)
    report_with_limit = executor.doctor_workspace(workspace, max_records=1)

    assert report_with_limit.ok is False
    assert "DOCTOR_SCAN_LIMIT" in {item.code for item in report_with_limit.diagnoses}


def test_mutation_time_preflight_detects_lock_identity_and_contract_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = _executor()
    workspace, _plan, report, _source, _output = _seed_plan(tmp_path / "lock")
    original_preflight = executor.preflight_plan

    def change_lock(plan_workspace: Workspace, plan_id: str) -> Any:
        current = original_preflight(plan_workspace, plan_id)
        lock = plan_workspace.root / "state" / "apply.lock"
        lock.write_text('{"pid":0,"token":"replacement"}', encoding="utf-8")
        return current

    monkeypatch.setattr(executor, "preflight_plan", change_lock)
    with pytest.raises(executor.WorkspaceLockedError, match="ownership changed"):
        executor.apply_plan(workspace, report, dry_run=False)
    assert _journal_rows(workspace) == []

    other, _plan, other_report, _source, _output = _seed_plan(tmp_path / "contract")

    def change_contract(plan_workspace: Workspace, plan_id: str) -> Any:
        current = original_preflight(plan_workspace, plan_id)
        return replace(current, approval_revision=current.approval_revision + 1)

    monkeypatch.setattr(executor, "preflight_plan", change_contract)
    with pytest.raises(executor.ApprovalContractError, match="approval changed"):
        executor.apply_plan(other, other_report, dry_run=False)
    assert _journal_rows(other) == []


def test_fsync_binding_closes_descriptor_after_success_and_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = _executor()
    binding = SimpleNamespace(directory_fd=None, access_path=tmp_path)
    closed: list[int] = []
    monkeypatch.setattr(executor.os, "open", lambda *_args, **_kwargs: 41)
    monkeypatch.setattr(executor.os, "close", closed.append)

    monkeypatch.setattr(executor.os, "fsync", lambda descriptor: None)
    executor._fsync_binding(binding)
    monkeypatch.setattr(
        executor.os,
        "fsync",
        lambda descriptor: (_ for _ in ()).throw(OSError("unsupported")),
    )
    executor._fsync_binding(binding)

    assert closed == [41, 41]


def test_target_collision_after_locked_preflight_is_detected_before_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(tmp_path)
    target = Path(plan.entries[0].target_path)
    original_binding = executor._target_parent_binding

    @contextmanager
    def collide_after_preflight(output_root: Path, requested_target: Path) -> Any:
        with original_binding(output_root, requested_target) as binding:
            requested_target.write_bytes(b"late target collision")
            yield binding

    monkeypatch.setattr(executor, "_target_parent_binding", collide_after_preflight)
    applied = executor.apply_plan(workspace, report, dry_run=False)

    assert applied.state == "PARTIAL"
    assert target.read_bytes() == b"late target collision"
    assert Path(plan.entries[0].source_path).exists()


def test_finalize_hardlink_unsupported_fails_closed_and_releases_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(tmp_path)

    def unsupported(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("synthetic filesystem without hard links")

    monkeypatch.setattr(executor, "_binding_link_no_overwrite", unsupported)
    applied = executor.apply_plan(workspace, report, dry_run=False)

    assert applied.state == "PARTIAL"
    assert not Path(plan.entries[0].target_path).exists()
    assert Path(plan.entries[0].source_path).exists()
    assert not (workspace.root / "state" / "apply.lock").exists()
    assert (
        "NO_OVERWRITE_LINK_UNSUPPORTED"
        in json.loads(_journal_rows(workspace)[0]["error"])["error_code"]
    )


def test_quarantine_delete_retains_external_replacement_and_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = _executor()
    root = tmp_path / "owned"
    root.mkdir()
    original = root / "payload.bin"
    original.write_bytes(b"owned payload")
    identity = executor._stable_hash(original, root)
    quarantine = root / ".replacement-fixed"
    original_hash = executor._stable_hash
    monkeypatch.setattr(executor, "uuid4", lambda: SimpleNamespace(hex="fixed"))

    def replace_quarantine(path: Path, owned_root: Path) -> Any:
        current = original_hash(path, owned_root)
        if path == quarantine:
            path.unlink()
            path.write_bytes(b"external replacement")
            return replace(current, sha256="0" * 64)
        return current

    monkeypatch.setattr(executor, "_stable_hash", replace_quarantine)
    with pytest.raises(executor.RecoverySafetyError, match="binding deletion"):
        executor._quarantine_delete(original, root, identity, prefix="replacement")

    assert original.read_bytes() == b"owned payload"
    assert quarantine.read_bytes() == b"external replacement"


def test_quarantine_delete_rechecks_original_after_binding_quarantine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = _executor()
    root = tmp_path / "owned"
    root.mkdir()
    original = root / "payload.bin"
    original.write_bytes(b"owned payload")
    identity = executor._stable_hash(original, root)
    original_hash = executor._stable_hash

    def report_changed_original(path: Path, owned_root: Path) -> Any:
        current = original_hash(path, owned_root)
        if path == original:
            return replace(current, sha256="0" * 64)
        return current

    monkeypatch.setattr(executor, "_stable_hash", report_changed_original)
    with pytest.raises(executor.RecoverySafetyError, match="binding deletion"):
        executor._quarantine_delete(original, root, identity, prefix="changed")

    assert original.read_bytes() == b"owned payload"
    assert not list(root.glob(".changed-*"))


@pytest.mark.parametrize(
    "fault_stage",
    ["AFTER_SOURCE_DELETE_PREPARED", "AFTER_SOURCE_DELETE_RENAME"],
)
def test_move_resume_rejects_binding_stat_change_before_unlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault_stage: str
) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(tmp_path, mode="MOVE")
    source = Path(plan.entries[0].source_path)
    expected = source.read_bytes()

    def crash(stage: str, _media_id: int) -> None:
        if stage == fault_stage:
            raise SyntheticCrash(stage)

    with pytest.raises(SyntheticCrash):
        executor.apply_plan(workspace, report, dry_run=False, fault_injector=crash)
    row = _journal_rows(workspace)[0]
    quarantine = Path(json.loads(row["error"])["source_quarantine_path"])
    original_stat = executor._binding_stat
    watched_name = source.name if fault_stage.endswith("PREPARED") else quarantine.name
    calls = 0

    def changed_stat(binding: Any, name: str) -> Any:
        nonlocal calls
        current = original_stat(binding, name)
        if name == watched_name:
            calls += 1
            if calls == 2:
                return SimpleNamespace(
                    st_dev=current.st_dev,
                    st_ino=current.st_ino,
                    st_size=current.st_size,
                    st_mtime_ns=current.st_mtime_ns + 1,
                )
        return current

    monkeypatch.setattr(executor, "_binding_stat", changed_stat)
    resumed = executor.resume_transaction(workspace, _transaction_id(workspace))

    assert resumed.state == "PARTIAL"
    assert Path(plan.entries[0].target_path).read_bytes() == expected
    assert source.exists() or quarantine.exists()


def test_move_bundle_rollback_accepts_preexisting_target_with_source_present(
    tmp_path: Path,
) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(
        tmp_path,
        mode="MOVE",
        names=("pair.jpg", "pair.mov"),
        bundle=True,
    )
    for entry in plan.entries:
        target = Path(entry.target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(Path(entry.source_path).read_bytes())
    last_media_id = plan.entries[-1].media_id

    def crash(stage: str, media_id: int) -> None:
        if stage == "AFTER_SOURCE_DELETE_PREPARED" and media_id == last_media_id:
            raise SyntheticCrash(stage)

    with pytest.raises(SyntheticCrash):
        executor.apply_plan(workspace, report, dry_run=False, fault_injector=crash)
    rolled_back = executor.rollback_transaction(workspace, _transaction_id(workspace))

    assert rolled_back.state == "ROLLED_BACK"
    assert all(Path(entry.source_path).exists() for entry in plan.entries)
    assert all(Path(entry.target_path).exists() for entry in plan.entries)


def test_move_bundle_rollback_blocks_changed_preexisting_target_without_mutation(
    tmp_path: Path,
) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(
        tmp_path,
        mode="MOVE",
        names=("pair.jpg", "pair.mov"),
        bundle=True,
    )
    targets = [Path(entry.target_path) for entry in plan.entries]
    for entry, target in zip(plan.entries, targets, strict=True):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(Path(entry.source_path).read_bytes())
    applied = executor.apply_plan(workspace, report, dry_run=False)
    targets[0].write_bytes(b"externally changed preexisting target")
    before = {target: target.read_bytes() for target in targets}

    rolled_back = executor.rollback_transaction(workspace, applied.transaction_id)

    assert rolled_back.state == "ROLLBACK_PARTIAL"
    assert {target: target.read_bytes() for target in targets} == before
    assert all(not Path(entry.source_path).exists() for entry in plan.entries)


@pytest.mark.parametrize("partial_change", ["unknown", "missing"])
def test_bundle_rollback_preflight_rejects_unprovable_partial_without_mutation(
    tmp_path: Path, partial_change: str
) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(
        tmp_path,
        names=("pair.jpg", "pair.mov"),
        bundle=True,
    )

    def crash(stage: str, _media_id: int) -> None:
        if stage == "AFTER_TARGET_VERIFY":
            raise SyntheticCrash(stage)

    with pytest.raises(SyntheticCrash):
        executor.apply_plan(workspace, report, dry_run=False, fault_injector=crash)
    row = _journal_rows(workspace)[0]
    metadata = json.loads(row["error"])
    partial = Path(metadata["partial_path"])
    if partial_change == "unknown":
        metadata.pop("partial_identity")
        with closing(sqlite3.connect(workspace.database_path)) as connection:
            connection.execute(
                "UPDATE operation_journal SET error=? WHERE id=?",
                (json.dumps(metadata), row["id"]),
            )
            connection.commit()
    else:
        partial.unlink()
    before_sources = {
        Path(entry.source_path): Path(entry.source_path).read_bytes() for entry in plan.entries
    }

    rolled_back = executor.rollback_transaction(workspace, _transaction_id(workspace))

    expected_state = "ROLLBACK_PARTIAL" if partial_change == "unknown" else "ROLLED_BACK"
    assert rolled_back.state == expected_state
    assert {path: path.read_bytes() for path in before_sources} == before_sources


def test_bundle_rollback_preflight_blocks_journal_plan_mismatch_before_mutation(
    tmp_path: Path,
) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(
        tmp_path,
        names=("pair.jpg", "pair.mov"),
        bundle=True,
    )
    applied = executor.apply_plan(workspace, report, dry_run=False)
    targets = [Path(entry.target_path) for entry in plan.entries]
    before = {target: target.read_bytes() for target in targets}
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        connection.execute(
            """
            UPDATE operation_journal SET target_path='C:/synthetic/mismatch'
            WHERE id=(SELECT MIN(id) FROM operation_journal)
            """
        )
        connection.commit()

    rolled_back = executor.rollback_transaction(workspace, applied.transaction_id)

    assert rolled_back.state == "ROLLBACK_PARTIAL"
    assert {target: target.read_bytes() for target in targets} == before


def test_transaction_creation_rechecks_approval_inside_begin_immediate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = _executor()
    workspace, plan, report, source, output = _seed_plan(tmp_path)
    original_create = executor._create_transaction

    def revoke_then_create(*args: Any, **kwargs: Any) -> str:
        revoke_plan(workspace, plan.plan_id)
        return original_create(*args, **kwargs)

    monkeypatch.setattr(executor, "_create_transaction", revoke_then_create)
    with pytest.raises(executor.ApprovalContractError, match="approval"):
        executor.apply_plan(workspace, report, dry_run=False)

    assert _snapshot(source)
    assert _snapshot(output) == {}
    assert _journal_rows(workspace) == []
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM operation_transaction").fetchone() == (0,)


def test_move_target_changed_from_final_source_stat_is_rejected_before_unlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(tmp_path, mode="MOVE")
    source = Path(plan.entries[0].source_path)
    target = Path(plan.entries[0].target_path)
    expected = source.read_bytes()

    def crash(stage: str, _media_id: int) -> None:
        if stage == "AFTER_SOURCE_DELETE_PREPARED":
            raise SyntheticCrash(stage)

    with pytest.raises(SyntheticCrash):
        executor.apply_plan(workspace, report, dry_run=False, fault_injector=crash)
    original_stat = executor._binding_stat
    source_stat_calls = 0

    def replace_target_from_source_stat(binding: Any, name: str) -> Any:
        nonlocal source_stat_calls
        current = original_stat(binding, name)
        if name == source.name:
            source_stat_calls += 1
            if source_stat_calls == 2:
                target.write_bytes(b"external target from final source stat")
        return current

    monkeypatch.setattr(executor, "_binding_stat", replace_target_from_source_stat)
    resumed = executor.resume_transaction(workspace, _transaction_id(workspace))

    assert resumed.state == "PARTIAL"
    assert source.read_bytes() == expected
    assert target.read_bytes() == b"external target from final source stat"


def test_owned_lock_release_does_not_delete_replacement_at_same_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = _executor()
    workspace = initialize_workspace(tmp_path / "workspace")
    lock = workspace.root / "state" / "apply.lock"
    replacement = b'{"pid":424242,"token":"external-replacement"}'

    def replace_opened_file(path: Path) -> None:
        if path == lock:
            path.unlink()
            path.write_bytes(replacement)

    monkeypatch.setattr(
        executor,
        "_IDENTITY_DELETE_TEST_HOOK",
        replace_opened_file,
        raising=False,
    )
    with executor.workspace_mutation_lock(workspace):
        pass

    assert lock.read_bytes() == replacement


def test_two_consecutive_stale_lock_replacements_never_enter_without_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = _executor()
    workspace = initialize_workspace(tmp_path / "workspace")
    lock = workspace.root / "state" / "apply.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    replacement_number = 0

    def stale_payload(number: int) -> bytes:
        return json.dumps(
            {
                "pid": 999_999,
                "token": f"stale-{number}",
                "workspace_root": str(workspace.root),
            }
        ).encode()

    lock.write_bytes(stale_payload(0))
    original_delete = executor._quarantine_delete

    def replace_after_reclaim(*args: Any, **kwargs: Any) -> None:
        nonlocal replacement_number
        original_delete(*args, **kwargs)
        replacement_number += 1
        lock.write_bytes(stale_payload(replacement_number))

    monkeypatch.setattr(executor, "_quarantine_delete", replace_after_reclaim)
    entered = False
    with (
        pytest.raises(executor.WorkspaceLockedError),
        executor.workspace_mutation_lock(
            workspace,
            process_liveness=lambda _pid, _token: "DEAD",
        ),
    ):
        entered = True

    assert replacement_number == 2
    assert entered is False
    assert lock.exists()


def test_bundle_rollback_race_restores_staged_sibling_before_failure(
    tmp_path: Path,
) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(
        tmp_path,
        names=("pair.jpg", "pair.mov"),
        bundle=True,
    )
    applied = executor.apply_plan(workspace, report, dry_run=False)
    targets = [Path(entry.target_path) for entry in plan.entries]
    expected_second = targets[1].read_bytes()

    def change_first_after_sibling_stage(stage: str, media_id: int) -> None:
        if stage == "AFTER_BUNDLE_ROLLBACK_STAGE" and media_id == plan.entries[1].media_id:
            targets[0].write_bytes(b"external bundle race")

    rolled_back = executor.rollback_transaction(
        workspace,
        applied.transaction_id,
        fault_injector=change_first_after_sibling_stage,
    )

    assert rolled_back.state == "ROLLBACK_PARTIAL"
    assert targets[0].read_bytes() == b"external bundle race"
    assert targets[1].read_bytes() == expected_second
    assert all(row["state"] == "ROLLBACK_FAILED" for row in _journal_rows(workspace))


def test_quarantine_delete_does_not_unlink_replacement_at_original_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = _executor()
    root = tmp_path / "owned"
    root.mkdir()
    original = root / "payload.bin"
    original.write_bytes(b"owned payload")
    identity = executor._stable_hash(original, root)
    replacement = b"external replacement"

    def replace_opened_file(path: Path) -> None:
        if path == original:
            path.unlink()
            path.write_bytes(replacement)

    monkeypatch.setattr(
        executor,
        "_IDENTITY_DELETE_TEST_HOOK",
        replace_opened_file,
        raising=False,
    )
    executor._quarantine_delete(original, root, identity, prefix="identity-race")

    assert original.read_bytes() == replacement


def test_doctor_bounds_entries_before_materializing_large_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = _executor()
    workspace, _plan, _report, _source, _output = _seed_plan(
        tmp_path,
        names=tuple(f"photo-{index}.jpg" for index in range(6)),
    )
    original_row_entry = planner_module._row_entry
    materialized = 0

    def bounded_row_entry(row: sqlite3.Row) -> Any:
        nonlocal materialized
        materialized += 1
        if materialized > 2:
            raise AssertionError("doctor materialized entries beyond max_records")
        return original_row_entry(row)

    monkeypatch.setattr(planner_module, "_row_entry", bounded_row_entry)
    diagnosis = executor.doctor_workspace(workspace, max_records=2)

    assert diagnosis.ok is False
    assert "DOCTOR_SCAN_LIMIT" in {item.code for item in diagnosis.diagnoses}
    assert materialized <= 2


def test_executor_path_and_liveness_error_edges_are_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = _executor()
    root = tmp_path / "owned"
    root.mkdir()
    outside = tmp_path / "outside"
    monkeypatch.setattr(executor, "_is_within", lambda *_args: True)
    with (
        pytest.raises(executor.RecoverySafetyError, match="TARGET_OUTSIDE_OUTPUT"),
        executor._target_parent_binding(root, outside / "payload.bin"),
    ):
        pass

    payload = root / "payload.bin"
    payload.write_bytes(b"payload")
    executor._binding_unlink(SimpleNamespace(directory_fd=None, access_path=root), payload.name)
    assert not payload.exists()

    import ctypes

    class MissingHandleKernel:
        def OpenProcess(self, *_args: Any) -> int:
            return 0

    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: MissingHandleKernel())
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5)
    assert executor._default_process_liveness(999_999, "synthetic") == "UNKNOWN"

    class ExitQueryFailureKernel:
        def OpenProcess(self, *_args: Any) -> int:
            return 1

        def GetExitCodeProcess(self, *_args: Any) -> bool:
            return False

        def CloseHandle(self, *_args: Any) -> None:
            return None

    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: ExitQueryFailureKernel())
    assert executor._default_process_liveness(999_999, "synthetic") == "UNKNOWN"


def test_executor_quarantine_and_transaction_authority_fail_closed(
    tmp_path: Path,
) -> None:
    executor = _executor()
    root = tmp_path / "owned"
    root.mkdir()
    payload = root / "payload.bin"
    payload.write_bytes(b"payload")
    identity = executor._stable_hash(payload, root)
    original_hash = executor._stable_hash
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(executor, "uuid4", lambda: SimpleNamespace(hex="final-check"))

    def final_hash_mismatch(path: Path, owned_root: Path) -> Any:
        current = original_hash(path, owned_root)
        if path.name == ".final-check-final-check" and current.sha256 == identity.sha256:
            return replace(current, sha256="0" * 64)
        return current

    # The generated quarantine name is deterministic under the synthetic UUID.
    monkeypatch.setattr(executor, "_stable_hash", final_hash_mismatch)
    with pytest.raises(executor.RecoverySafetyError, match="binding deletion"):
        executor._quarantine_delete(payload, root, identity, prefix="final-check")
    monkeypatch.undo()

    workspace, plan, report, _source, _output = _seed_plan(tmp_path / "authority")
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        connection.execute("DELETE FROM plan_approval WHERE plan_id=?", (plan.plan_id,))
        connection.commit()
    with pytest.raises(executor.ApprovalContractError, match="authority disappeared"):
        executor._create_transaction(workspace, plan, report)


@pytest.mark.parametrize("case", ["conflict", "bad_digest", "unsupported", "final_mismatch"])
def test_copy_restore_recovery_error_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    executor = _executor()
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    source_root.mkdir()
    target_root.mkdir()
    source = source_root / "payload.bin"
    target = target_root / "payload.bin"
    source.write_bytes(b"restore payload")
    expected = executor._stable_hash(source, source_root).sha256

    if case == "conflict":
        target.write_bytes(b"external")
        with pytest.raises(executor.RecoverySafetyError, match="RESTORE_SOURCE_CONFLICT"):
            executor._copy_restore(source, source_root, target, target_root, expected)
    elif case == "bad_digest":
        with pytest.raises(executor.RecoverySafetyError, match="RESTORE_VERIFY_FAILED"):
            executor._copy_restore(source, source_root, target, target_root, "0" * 64)
    elif case == "unsupported":
        monkeypatch.setattr(
            executor,
            "_binding_link",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("unsupported")),
        )
        with pytest.raises(executor.RecoverySafetyError, match="SOURCE_RESTORE_UNSUPPORTED"):
            executor._copy_restore(source, source_root, target, target_root, expected)
    else:
        original_hash = executor._stable_hash

        def changed_final_hash(path: Path, root: Path) -> Any:
            current = original_hash(path, root)
            if path == target:
                return replace(current, sha256="0" * 64)
            return current

        monkeypatch.setattr(executor, "_stable_hash", changed_final_hash)
        with pytest.raises(executor.RecoverySafetyError, match="RESTORE_VERIFY_FAILED"):
            executor._copy_restore(source, source_root, target, target_root, expected)


def test_executor_recovery_envelope_and_stage_error_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = _executor()
    workspace, plan, _report, source_root, _output = _seed_plan(tmp_path / "envelope", mode="MOVE")
    entry = plan.entries[0]
    with pytest.raises(executor.RecoverySafetyError, match="SOURCE_DELETE_OWNERSHIP_UNKNOWN"):
        executor._source_delete_paths(
            entry,
            "tx-" + "a" * 32 + "-r1-" + "b" * 64,
            1,
            {"source_delete_phase": "PREPARED"},
        )

    owned = tmp_path / "stage"
    owned.mkdir()
    original = owned / "payload.bin"
    original.write_bytes(b"stage")
    identity = executor._stable_hash(original, owned)
    with pytest.raises(executor.RecoverySafetyError, match="OUTSIDE_ROOT"):
        executor._stage_rollback_path(
            tmp_path / "outside.bin", owned, identity, "tx-" + "a" * 32 + "-r1-" + "b" * 64, 1
        )
    monkeypatch.setattr(
        executor,
        "_binding_link_no_overwrite",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("unsupported")),
    )
    with pytest.raises(executor.RecoverySafetyError, match="ROLLBACK_STAGE_UNSUPPORTED"):
        executor._stage_rollback_path(
            original, owned, identity, "tx-" + "a" * 32 + "-r1-" + "b" * 64, 1
        )


def test_executor_rollback_stage_restore_error_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = _executor()
    root = tmp_path / "root"
    root.mkdir()
    original = root / "original.bin"
    staged = root / ".original.bin.stage"
    original.write_bytes(b"original")
    staged.write_bytes(b"original")
    identity = executor._stable_hash(original, root)
    stage = executor._RollbackStage(original, root, staged, identity)
    original.write_bytes(b"changed")
    with pytest.raises(executor.RecoverySafetyError, match="RESTORE_CONFLICT"):
        executor._restore_rollback_stage(stage)

    original.unlink()
    monkeypatch.setattr(
        executor,
        "_binding_link_no_overwrite",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("unsupported")),
    )
    with pytest.raises(executor.RecoverySafetyError, match="RESTORE_UNSUPPORTED"):
        executor._restore_rollback_stage(stage)

    monkeypatch.undo()
    staged.write_bytes(b"original")
    original_hash = executor._stable_hash

    def changed_restore(path: Path, owned_root: Path) -> Any:
        current = original_hash(path, owned_root)
        if path == original:
            return replace(current, sha256="0" * 64)
        return current

    monkeypatch.setattr(executor, "_stable_hash", changed_restore)
    with pytest.raises(executor.RecoverySafetyError, match="RESTORE_FAILED"):
        executor._restore_rollback_stage(stage)


def test_iter_partials_handles_unreadable_roots_and_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = _executor()
    assert list(executor._iter_partials(tmp_path / "missing", executor._DoctorScanBudget(4))) == []
    unreadable_root = tmp_path / "unreadable"
    unreadable_root.write_bytes(b"file")
    assert list(executor._iter_partials(unreadable_root, executor._DoctorScanBudget(4))) == []

    directory = tmp_path / "directory"
    directory.mkdir()

    class ScanEntries:
        def __init__(self, entries: list[Any]) -> None:
            self.entries = entries

        def __enter__(self) -> ScanEntries:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def __iter__(self):  # type: ignore[no-untyped-def]
            return iter(self.entries)

    class SymlinkEntry:
        path = str(directory / "link")

        def is_symlink(self) -> bool:
            return True

    class BrokenEntry:
        path = str(directory / "broken.partial")

        def is_symlink(self) -> bool:
            return False

        def stat(self, *, follow_symlinks: bool) -> Any:
            raise OSError("synthetic stat failure")

    monkeypatch.setattr(
        executor.os,
        "scandir",
        lambda _path: ScanEntries([SymlinkEntry(), BrokenEntry()]),
    )
    assert list(executor._iter_partials(directory, executor._DoctorScanBudget(4))) == []

    monkeypatch.setattr(
        executor.os,
        "scandir",
        lambda _path: (_ for _ in ()).throw(OSError("synthetic scandir failure")),
    )
    assert list(executor._iter_partials(directory, executor._DoctorScanBudget(4))) == []


def test_doctor_reports_source_missing_after_verified_move(
    tmp_path: Path,
) -> None:
    executor = _executor()
    workspace, plan, report, _source, _output = _seed_plan(tmp_path, mode="MOVE")
    executor.apply_plan(workspace, report, dry_run=False)
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        connection.execute(
            "UPDATE operation_journal SET state='SOURCE_REMOVED' WHERE transaction_id=?",
            (_transaction_id(workspace),),
        )
        connection.commit()
    diagnosis = executor.doctor_workspace(workspace)
    assert "SOURCE_MISSING_TARGET_VERIFIED" in {item.code for item in diagnosis.diagnoses}


def test_identity_bound_unlink_rejects_windows_open_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name != "nt":
        pytest.skip("Windows identity-bound deletion path")
    executor = _executor()
    root = tmp_path / "owned"
    root.mkdir()
    path = root / "payload.bin"
    path.write_bytes(b"payload")
    expected = executor._stable_hash(path, root)

    import ctypes

    class FailingKernel:
        class Function:
            def __init__(self, result: int) -> None:
                self.result = result

            def __call__(self, *_args: Any) -> int:
                return self.result

        CreateFileW = Function(ctypes.c_void_p(-1).value)
        CloseHandle = Function(1)

    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: FailingKernel())
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5)

    with pytest.raises(OSError, match="cannot bind file for deletion"):
        executor._identity_bound_unlink(path, root, expected)


@pytest.mark.parametrize(
    "final_path",
    [
        "",
        r"\\?\UNC\server\share\payload.bin",
        r"\\?\C:\outside\payload.bin",
    ],
)
def test_identity_bound_unlink_rejects_windows_unverifiable_handle_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    final_path: str,
) -> None:
    if os.name != "nt":
        pytest.skip("Windows identity-bound deletion path")
    executor = _executor()
    root = tmp_path / "owned"
    root.mkdir()
    path = root / "payload.bin"
    path.write_bytes(b"payload")
    expected = executor._stable_hash(path, root)

    import ctypes

    class HandleKernel:
        class Function:
            def __init__(self, result: int) -> None:
                self.result = result

            def __call__(self, *_args: Any) -> int:
                return self.result

        CreateFileW = Function(123)
        CloseHandle = Function(1)

        def GetFinalPathNameByHandleW(
            self, _handle: int, buffer: Any, _length: int, _flags: int
        ) -> int:
            buffer.value = final_path
            return len(final_path)

    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: HandleKernel())
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5)

    if not final_path:
        with pytest.raises(
            executor.RecoverySafetyError, match="cannot verify deletion handle path"
        ):
            executor._identity_bound_unlink(path, root, expected)
    elif final_path.startswith(r"\\?\UNC"):
        with pytest.raises(ValueError, match="same drive"):
            executor._identity_bound_unlink(path, root, expected)
    else:
        with pytest.raises(executor.RecoverySafetyError, match="escaped owned root"):
            executor._identity_bound_unlink(path, root, expected)


def test_identity_bound_unlink_returns_when_windows_name_disappears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name != "nt":
        pytest.skip("Windows identity-bound deletion path")
    executor = _executor()
    root = tmp_path / "owned"
    root.mkdir()
    path = root / "payload.bin"
    path.write_bytes(b"payload")
    expected = executor._stable_hash(path, root)

    import ctypes
    import msvcrt

    class HandleKernel:
        class Function:
            def __init__(self, result: int) -> None:
                self.result = result

            def __call__(self, *_args: Any) -> int:
                return self.result

        CreateFileW = Function(123)
        CloseHandle = Function(1)

        def GetFinalPathNameByHandleW(
            self, _handle: int, buffer: Any, _length: int, _flags: int
        ) -> int:
            buffer.value = str(path)
            return len(str(path))

    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: HandleKernel())
    monkeypatch.setattr(msvcrt, "open_osfhandle", lambda *_args: os.open(path, os.O_RDONLY))
    monkeypatch.setattr(executor, "_IDENTITY_DELETE_TEST_HOOK", lambda _candidate: None)
    original_lstat = Path.lstat

    def missing_name(candidate: Path) -> Any:
        if candidate == path:
            raise FileNotFoundError(candidate)
        return original_lstat(candidate)

    monkeypatch.setattr(Path, "lstat", missing_name)

    executor._identity_bound_unlink(path, root, expected)
    assert path.exists()


def test_identity_bound_unlink_rejects_windows_delete_api_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name != "nt":
        pytest.skip("Windows identity-bound deletion path")
    executor = _executor()
    root = tmp_path / "owned"
    root.mkdir()
    path = root / "payload.bin"
    path.write_bytes(b"payload")
    expected = executor._stable_hash(path, root)

    import ctypes
    import msvcrt

    class HandleKernel:
        class Function:
            def __init__(self, result: int) -> None:
                self.result = result

            def __call__(self, *_args: Any) -> int:
                return self.result

        CreateFileW = Function(123)
        CloseHandle = Function(1)
        SetFileInformationByHandle = Function(0)

        def GetFinalPathNameByHandleW(
            self, _handle: int, buffer: Any, _length: int, _flags: int
        ) -> int:
            buffer.value = str(path)
            return len(str(path))

    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: HandleKernel())
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5)
    monkeypatch.setattr(msvcrt, "open_osfhandle", lambda *_args: os.open(path, os.O_RDONLY))
    monkeypatch.setattr(msvcrt, "get_osfhandle", lambda _fd: 456)

    with pytest.raises(OSError, match="cannot delete bound file"):
        executor._identity_bound_unlink(path, root, expected)
