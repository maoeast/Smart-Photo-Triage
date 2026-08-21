from __future__ import annotations

import hashlib
import importlib
import json
import os
import sqlite3
import subprocess
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import smart_photo_triage.cli as cli_module
from smart_photo_triage.database import (
    MIGRATIONS,
    MigrationError,
    apply_migrations,
    connect_database,
    validate_database,
)
from smart_photo_triage.workspace import Workspace, initialize_workspace

_LEGACY_MIGRATION_HASHES = {
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
}
_PUBLISHED_V11_MIGRATION_HASH = "e184fa3c655777c5bc25aa5220a3fdeafb83515d993012bab95ae6a9edc2714b"


def _api(name: str) -> Any:
    value = getattr(cli_module, name, None)
    assert value is not None, f"Phase F planner API {name} is unavailable"
    return value


def _planner_module() -> Any:
    _api("build_plan")
    return importlib.import_module("smart_photo_triage.planner")


def _source_snapshot(source: Path) -> dict[str, bytes]:
    return {
        path.relative_to(source).as_posix(): path.read_bytes()
        for path in sorted(source.rglob("*"))
        if path.is_file()
    }


def _seed_media(
    workspace: Workspace,
    source: Path,
    items: list[dict[str, Any]],
    *,
    bundle_id: str | None = None,
) -> tuple[int, ...]:
    source.mkdir(parents=True, exist_ok=True)
    media_ids: list[int] = []
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        if bundle_id is not None:
            connection.execute(
                """
                INSERT INTO asset_bundle(id,bundle_type,bundle_key,source_root_key,warning_status)
                VALUES (?, 'LIVE_PHOTO', ?, ?, NULL)
                """,
                (bundle_id, f"bundle-key-{bundle_id}", str(source).casefold()),
            )
        for index, item in enumerate(items):
            path = source / str(item["name"])
            data = bytes(item.get("data", f"synthetic-{index}".encode()))
            path.write_bytes(data)
            extension = path.suffix.lower()
            media_type = str(item.get("media_type", "IMAGE"))
            captured_at = item.get("captured_at", "2024-05-06T07:08:09")
            capture_source = str(item.get("capture_source", "EXIF_DATETIME_ORIGINAL"))
            confidence = str(item.get("capture_confidence", "HIGH"))
            digest = hashlib.sha256(data).hexdigest()
            cursor = connection.execute(
                """
                INSERT INTO media_item(
                    original_path,path_key,source_root,source_root_key,parent_key,bundle_stem,
                    media_type,extension,size_bytes,mtime_ns,source_present,content_sha256,
                    captured_at,capture_source,capture_confidence,capture_timezone_status,
                    last_seen_at,last_seen_scan_id
                ) VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,'now','scan-f')
                """,
                (
                    str(path),
                    str(path).casefold(),
                    str(source),
                    str(source).casefold(),
                    str(path.parent).casefold(),
                    path.stem,
                    media_type,
                    extension,
                    len(data),
                    path.stat().st_mtime_ns,
                    digest,
                    captured_at,
                    capture_source,
                    confidence,
                    "UNKNOWN",
                ),
            )
            media_id = int(cursor.lastrowid)
            media_ids.append(media_id)
            if bundle_id is not None:
                connection.execute(
                    "INSERT INTO bundle_member(bundle_id,media_id,role) VALUES (?,?,?)",
                    (bundle_id, media_id, str(item.get("role", "PRIMARY"))),
                )
            if media_type in {"IMAGE", "VIDEO"} and item.get("with_ai", True):
                connection.execute(
                    """
                    INSERT INTO ai_analysis(
                        media_id,input_fingerprint,preview_fingerprint,preview_version,
                        provider,model,prompt_version,schema_version,scene_category,disposition,
                        confidence,quality_score,tags_json,short_desc,reason,created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,0.95,0.8,'[]',?,?,'2024-01-01T00:00:00')
                    """,
                    (
                        media_id,
                        f"input-{media_id}",
                        f"preview-{media_id}",
                        "preview-v1",
                        "fake",
                        "fake-v1",
                        "prompt-v1",
                        "schema-v1",
                        str(item.get("category", "01_家庭生活")),
                        str(item.get("disposition", "KEEP")),
                        str(item.get("short_desc", path.stem)),
                        "synthetic reason",
                    ),
                )
        connection.commit()
    return tuple(media_ids)


def _options(output: Path, *, mode: str = "COPY") -> Any:
    return _api("PlannerOptions")(output_root=output, mode=mode)


def _build(workspace: Workspace, output: Path, *, mode: str = "COPY", **kwargs: Any) -> Any:
    return _api("build_plan")(workspace, _options(output, mode=mode), **kwargs)


def test_phase_f_planner_fallback_capture_stale_approval_and_file_output_are_observable(
    tmp_path: Path,
) -> None:
    """Exercise reachable policy outcomes without changing planner behavior."""
    planner = _planner_module()
    assert planner._sanitize_component(" . ", fallback="fallback") == "fallback"
    candidate = SimpleNamespace(
        capture_source="EXIF_DATETIME_ORIGINAL",
        capture_confidence="LOW",
        captured_at="2024-05-06T07:08:09",
    )
    assert planner._reliable_capture(candidate) is None

    workspace = initialize_workspace(tmp_path / "workspace")
    source = tmp_path / "source"
    _seed_media(workspace, source, [{"name": "one.png"}])
    plan = _build(workspace, tmp_path / "output")
    pending = planner.preflight_plan(workspace, plan.plan_id)
    assert planner.preflight_approval_is_current(workspace, pending) is False

    file_output = tmp_path / "file-output"
    file_output.write_bytes(b"not a directory")
    with pytest.raises(planner.PlanPolicyError, match="output root is not a directory"):
        _build(workspace, file_output)


def _issue_codes(report: Any) -> set[str]:
    return {issue.code for issue in report.issues}


def test_t_f_001_plan_is_deterministic_immutable_auditable_and_source_read_only(
    tmp_path: Path,
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    source = tmp_path / "source"
    _seed_media(
        workspace,
        source,
        [
            {"name": "b.jpg", "data": b"bbb", "short_desc": "family"},
            {"name": "a.jpg", "data": b"aaa", "short_desc": "family"},
        ],
    )
    before = _source_snapshot(source)

    first = _build(workspace, tmp_path / "organized")
    second = _build(workspace, tmp_path / "organized")

    assert first.plan_id == second.plan_id
    assert first.canonical_json() == second.canonical_json()
    assert [entry.media_id for entry in first.entries] == sorted(
        entry.media_id for entry in first.entries
    )
    assert first.approval_state == "PENDING"
    assert first.config_fingerprint and first.source_root_fingerprint
    assert first.payload_sha256 == hashlib.sha256(first.canonical_payload()).hexdigest()
    assert _source_snapshot(source) == before
    assert not (tmp_path / "organized").exists()

    with closing(connect_database(workspace.database_path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM organization_plan").fetchone() == (1,)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE organization_plan SET mode='MOVE' WHERE plan_id=?", (first.plan_id,)
            )


def test_t_f_002_through_008_order_and_windows_filename_collisions_are_safe(
    tmp_path: Path,
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    source = tmp_path / "source"
    _seed_media(
        workspace,
        source,
        [
            {"name": "z.jpg", "short_desc": 'CON<>:"/\\|?*.\x01 '},
            {"name": "y.jpg", "short_desc": "Photo"},
            {"name": "x.jpg", "short_desc": "photo"},
            {"name": "w.jpg", "short_desc": "é"},
            {"name": "v.jpg", "short_desc": "e\u0301"},
            {"name": "u.jpg", "short_desc": "NUL. "},
        ],
    )

    plan = _build(workspace, tmp_path / "organized")
    targets = [Path(entry.target_path) for entry in plan.entries]
    target_keys = [unicodedata.normalize("NFC", str(path)).casefold() for path in targets]

    assert len(target_keys) == len(set(target_keys))
    assert [entry.position for entry in plan.entries] == list(range(len(plan.entries)))
    for target in targets:
        assert not any(char in target.name for char in '<>:"/\\|?*')
        assert not any(ord(char) < 32 for char in target.name)
        assert not target.name.endswith((".", " "))
        assert target.stem.split("_")[-1].upper() not in {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *(f"COM{number}" for number in range(1, 10)),
            *(f"LPT{number}" for number in range(1, 10)),
        }
        assert len(str(target)) <= 240


def test_t_f_009_unreliable_filesystem_time_uses_explicit_review_folder(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    source = tmp_path / "source"
    _seed_media(
        workspace,
        source,
        [
            {
                "name": "unknown.jpg",
                "capture_source": "FILESYSTEM_MTIME",
                "capture_confidence": "LOW",
            }
        ],
    )

    plan = _build(workspace, tmp_path / "organized")

    assert "_时间待确认" in Path(plan.entries[0].target_path).parts


def test_t_f_010_bundle_keeps_one_directory_and_linked_basename(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    source = tmp_path / "source"
    before_ids = _seed_media(
        workspace,
        source,
        [
            {"name": "IMG_0001.HEIC", "role": "PRIMARY", "short_desc": "trip"},
            {"name": "IMG_0001.MOV", "media_type": "VIDEO", "role": "MOTION"},
            {
                "name": "IMG_0001.AAE",
                "media_type": "SIDECAR",
                "role": "SIDECAR",
                "with_ai": False,
            },
        ],
        bundle_id="bundle-live-1",
    )

    plan = _build(workspace, tmp_path / "organized")
    entries = [entry for entry in plan.entries if entry.media_id in before_ids]
    paths = [Path(entry.target_path) for entry in entries]

    assert len(entries) == 3
    assert {entry.bundle_id for entry in entries} == {"bundle-live-1"}
    assert len({path.parent for path in paths}) == 1
    assert len({path.stem.casefold() for path in paths}) == 1
    assert {path.suffix.lower() for path in paths} == {".heic", ".mov", ".aae"}


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (lambda path: path.write_bytes(b"changed-after-plan"), "STALE_SOURCE"),
        (lambda path: path.unlink(), "SOURCE_MISSING"),
    ],
)
def test_t_f_011_and_012_preflight_rejects_stale_or_missing_source(
    tmp_path: Path, mutation: Any, expected_code: str
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    source = tmp_path / "source"
    _seed_media(workspace, source, [{"name": "photo.jpg", "data": b"before"}])
    plan = _build(workspace, tmp_path / "organized")
    _api("approve_plan")(workspace, plan.plan_id)
    mutation(source / "photo.jpg")

    report = _api("preflight_plan")(workspace, plan.plan_id)

    assert report.ok is False
    assert expected_code in _issue_codes(report)


@pytest.mark.parametrize("layout", ["output_inside_source", "source_inside_output"])
def test_t_f_013_and_014_dangerous_source_output_layout_is_rejected(
    tmp_path: Path, layout: str
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    if layout == "output_inside_source":
        source = tmp_path / "source"
        output = source / "organized"
    else:
        output = tmp_path / "organized"
        source = output / "source"
    _seed_media(workspace, source, [{"name": "photo.jpg"}])

    with pytest.raises(_api("PlanPolicyError"), match="overlap"):
        _build(workspace, output)


def test_t_f_015_preflight_rejects_output_that_became_a_file(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    source = tmp_path / "source"
    output = tmp_path / "organized"
    _seed_media(workspace, source, [{"name": "photo.jpg"}])
    plan = _build(workspace, output)
    _api("approve_plan")(workspace, plan.plan_id)
    output.write_bytes(b"not-a-directory")

    report = _api("preflight_plan")(workspace, plan.plan_id)

    assert report.ok is False
    assert "OUTPUT_NOT_DIRECTORY" in _issue_codes(report)


def test_t_f_016_incomplete_transaction_blocks_new_apply_preflight(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    source = tmp_path / "source"
    _seed_media(workspace, source, [{"name": "photo.jpg"}])
    plan = _build(workspace, tmp_path / "organized")
    _api("approve_plan")(workspace, plan.plan_id)
    with closing(connect_database(workspace.database_path)) as connection:
        connection.execute(
            """
            INSERT INTO operation_transaction(
                transaction_id,plan_id,mode,state,created_at,updated_at
            ) VALUES ('old-transaction',?,'COPY','PREPARED','now','now')
            """,
            (plan.plan_id,),
        )
        connection.commit()

    report = _api("preflight_plan")(workspace, plan.plan_id)

    assert report.ok is False
    assert "INCOMPLETE_TRANSACTION" in _issue_codes(report)


def test_plan_approval_is_separate_idempotent_and_required_by_preflight(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    source = tmp_path / "source"
    _seed_media(workspace, source, [{"name": "photo.jpg"}])
    plan = _build(workspace, tmp_path / "organized")

    pending = _api("preflight_plan")(workspace, plan.plan_id)
    first_approval = _api("approve_plan")(workspace, plan.plan_id)
    second_approval = _api("approve_plan")(workspace, plan.plan_id)
    approved = _api("preflight_plan")(workspace, plan.plan_id)

    assert "PLAN_NOT_APPROVED" in _issue_codes(pending)
    assert first_approval.state == second_approval.state == "APPROVED"
    assert first_approval.revision == second_approval.revision == 2
    assert approved.ok is True
    assert _api("inspect_plan")(workspace, plan.plan_id).approval_state == "APPROVED"


def test_plan_policy_conflicts_and_rollback_ready_entry_data_are_previewable(
    tmp_path: Path,
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    source = tmp_path / "source"
    media_ids = _seed_media(
        workspace,
        source,
        [
            {"name": "keep.jpg", "data": b"keep", "disposition": "KEEP"},
            {
                "name": "reject.jpg",
                "data": b"reject",
                "disposition": "REJECT_CANDIDATE",
            },
        ],
    )
    plan = _build(workspace, tmp_path / "organized", mode="MOVE")
    _api("approve_plan")(workspace, plan.plan_id)

    assert {entry.action for entry in plan.entries} == {"MOVE"}
    assert all(entry.action != "DELETE" for entry in plan.entries)
    assert {entry.media_id for entry in plan.entries} == set(media_ids)
    assert all(
        entry.expected_size > 0 and len(entry.expected_sha256) == 64 for entry in plan.entries
    )
    assert all(
        entry.source_mtime_ns > 0 and entry.decision_source == "AI" for entry in plan.entries
    )
    reject_entry = next(entry for entry in plan.entries if entry.disposition == "REJECT_CANDIDATE")
    assert "_待审废片" in Path(reject_entry.target_path).parts
    assert json.loads(plan.canonical_json())["entries"]

    conflict = plan.entries[0]
    target = Path(conflict.target_path)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"different")
    report = _api("preflight_plan")(workspace, plan.plan_id)
    assert report.ok is False
    assert "TARGET_CONFLICT" in _issue_codes(report)


def test_existing_same_hash_target_is_idempotent_preflight_information(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    source = tmp_path / "source"
    _seed_media(workspace, source, [{"name": "photo.jpg", "data": b"same"}])
    plan = _build(workspace, tmp_path / "organized")
    _api("approve_plan")(workspace, plan.plan_id)
    entry = plan.entries[0]
    target = Path(entry.target_path)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"same")

    report = _api("preflight_plan")(workspace, plan.plan_id)

    assert report.ok is True
    assert "ALREADY_PRESENT" in _issue_codes(report)


def test_concurrent_identical_builds_converge_on_one_immutable_plan(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    source = tmp_path / "source"
    _seed_media(workspace, source, [{"name": f"{index}.jpg"} for index in range(8)])
    output = tmp_path / "organized"

    with ThreadPoolExecutor(max_workers=4) as pool:
        plans = list(pool.map(lambda _index: _build(workspace, output), range(8)))

    assert len({plan.plan_id for plan in plans}) == 1
    with closing(connect_database(workspace.database_path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM organization_plan").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM plan_entry").fetchone() == (8,)


def test_fault_after_entry_insert_rolls_back_plan_and_leaves_sources_untouched(
    tmp_path: Path,
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    source = tmp_path / "source"
    _seed_media(workspace, source, [{"name": "photo.jpg"}])
    before = _source_snapshot(source)

    def fail(point: str) -> None:
        if point == "AFTER_ENTRY_INSERT":
            raise RuntimeError("synthetic planner fault")

    with pytest.raises(RuntimeError, match="synthetic planner fault"):
        _build(workspace, tmp_path / "organized", fault_injector=fail)

    assert _source_snapshot(source) == before
    with closing(connect_database(workspace.database_path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM organization_plan").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM plan_entry").fetchone() == (0,)


def test_phase_f_migration_is_additive_and_legacy_statement_bytes_are_unchanged(
    tmp_path: Path,
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")

    assert MIGRATIONS[-1].version == 11
    for migration in MIGRATIONS[:10]:
        digest = hashlib.sha256("\0".join(migration.statements).encode()).hexdigest()
        assert digest == _LEGACY_MIGRATION_HASHES[migration.version]
    assert (
        hashlib.sha256("\0".join(MIGRATIONS[10].statements).encode()).hexdigest()
        == _PUBLISHED_V11_MIGRATION_HASH
    )
    with closing(connect_database(workspace.database_path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (11,)
        assert {
            row[0]
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type='table' AND name IN (
                    'organization_plan','plan_entry','plan_approval',
                    'operation_transaction','operation_journal'
                )
                """
            )
        } == {
            "organization_plan",
            "plan_entry",
            "plan_approval",
            "operation_transaction",
            "operation_journal",
        }


def test_populated_v10_database_upgrades_additively_and_missing_immutable_trigger_is_rejected(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy-v10.sqlite3"
    workspace_id = "a" * 32
    with closing(connect_database(database_path)) as connection:
        for migration in MIGRATIONS[:10]:
            for statement in migration.statements:
                connection.execute(statement)
            if migration.version == 1:
                connection.execute(
                    "INSERT INTO workspace_metadata(key,value) VALUES ('workspace_id',?)",
                    (workspace_id,),
                )
            connection.execute(
                "INSERT INTO schema_migration(version,name) VALUES (?,?)",
                (migration.version, migration.name),
            )
            connection.execute(f"PRAGMA user_version={migration.version}")
        connection.execute(
            """
            INSERT INTO media_item(
                original_path,path_key,source_root,source_root_key,parent_key,bundle_stem,
                media_type,extension,size_bytes,mtime_ns,source_present,content_sha256,
                captured_at,capture_source,capture_confidence,capture_timezone_status,
                last_seen_at,last_seen_scan_id
            ) VALUES (
                'legacy.jpg','legacy.jpg','legacy-root','legacy-root','legacy-root','legacy',
                'IMAGE','.jpg',6,7,1,'abc',NULL,'FILESYSTEM_MTIME','LOW','UTC','now','scan'
            )
            """
        )
        connection.commit()

        assert apply_migrations(connection, workspace_id=workspace_id) == 11
        assert connection.execute("SELECT original_path FROM media_item").fetchone() == (
            "legacy.jpg",
        )
        assert apply_migrations(connection, workspace_id=workspace_id) == 11
        connection.execute("DROP TRIGGER plan_entry_no_update")
        connection.commit()
        with pytest.raises(MigrationError, match="missing critical trigger"):
            validate_database(connection, expected_workspace_id=workspace_id)


def test_phase_f_cli_build_inspect_approve_and_preflight(tmp_path: Path, capsys: Any) -> None:
    workspace_root = tmp_path / "workspace"
    workspace = initialize_workspace(workspace_root)
    source = tmp_path / "source"
    _seed_media(workspace, source, [{"name": "photo.jpg"}])
    output = tmp_path / "organized"

    assert (
        cli_module.main(
            ["plan", "build", "--workspace", str(workspace_root), "--output", str(output)]
        )
        == 0
    )
    built = json.loads(capsys.readouterr().out)
    plan_id = built["plan_id"]
    assert built["approval_state"] == "PENDING"

    assert cli_module.main(["plan", "inspect", plan_id, "--workspace", str(workspace_root)]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["plan_id"] == plan_id
    assert inspected["entries"][0]["action"] == "COPY"

    assert cli_module.main(["plan", "approve", plan_id, "--workspace", str(workspace_root)]) == 0
    capsys.readouterr()
    assert cli_module.main(["plan", "preflight", plan_id, "--workspace", str(workspace_root)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True


@pytest.mark.parametrize(
    "kwargs",
    [
        {"mode": "DELETE"},
        {"max_path_chars": 63},
        {"max_path_chars": True},
        {"output_root": "not-a-path"},
        {"schema_version": ""},
        {"planner_version": "x" * 101},
    ],
)
def test_planner_options_reject_unsafe_policy_values(
    tmp_path: Path, kwargs: dict[str, Any]
) -> None:
    values: dict[str, Any] = {"output_root": tmp_path / "output"}
    values.update(kwargs)

    with pytest.raises(_api("PlanPolicyError")):
        _api("PlannerOptions")(**values)


def test_build_rejects_empty_library_stale_index_and_impossible_path_budget(tmp_path: Path) -> None:
    planner = _planner_module()
    empty_workspace = initialize_workspace(tmp_path / "empty-workspace")
    with pytest.raises(planner.PlanSourceError, match="no present media"):
        _build(empty_workspace, tmp_path / "empty-output")

    workspace = initialize_workspace(tmp_path / "workspace")
    source = tmp_path / "source"
    media_id = _seed_media(workspace, source, [{"name": "photo.jpg", "data": b"indexed"}])[0]
    with closing(connect_database(workspace.database_path)) as connection:
        connection.execute("UPDATE media_item SET size_bytes=size_bytes+1 WHERE id=?", (media_id,))
        connection.commit()
    with pytest.raises(planner.PlanSourceError, match="size differs"):
        _build(workspace, tmp_path / "output")

    with closing(connect_database(workspace.database_path)) as connection:
        connection.execute("UPDATE media_item SET size_bytes=size_bytes-1 WHERE id=?", (media_id,))
        connection.commit()
    long_output = tmp_path / ("o" * 58)
    with pytest.raises(_api("PlanPolicyError"), match="path budget"):
        _api("build_plan")(
            workspace,
            _api("PlannerOptions")(output_root=long_output, max_path_chars=64),
        )


def test_rule_and_human_policy_are_audited_and_unbound_sidecar_is_warned(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    source = tmp_path / "source"
    media_ids = _seed_media(
        workspace,
        source,
        [
            {"name": "rule.jpg", "with_ai": False},
            {"name": "human.jpg", "with_ai": False},
            {"name": "orphan.xmp", "media_type": "SIDECAR", "with_ai": False},
        ],
    )
    with closing(connect_database(workspace.database_path)) as connection:
        connection.execute(
            """
            INSERT INTO review_decision(
                media_id,scene_category,disposition,decision_source,
                revision,created_at,updated_at
            ) VALUES (?,'03_工作与文档','KEEP','HUMAN',1,'now','now')
            """,
            (media_ids[1],),
        )
        connection.commit()

    plan = _build(workspace, tmp_path / "output")

    assert [(entry.media_id, entry.decision_source) for entry in plan.entries] == [
        (media_ids[0], "RULE"),
        (media_ids[1], "HUMAN"),
    ]
    assert plan.warnings == (f"UNBOUND_SIDECAR:{media_ids[2]}",)


def test_revoke_is_idempotent_and_plan_lookup_errors_are_stable(tmp_path: Path) -> None:
    planner = _planner_module()
    workspace = initialize_workspace(tmp_path / "workspace")
    source = tmp_path / "source"
    _seed_media(workspace, source, [{"name": "photo.jpg"}])
    plan = _build(workspace, tmp_path / "output")
    _api("approve_plan")(workspace, plan.plan_id)

    first = _api("revoke_plan")(workspace, plan.plan_id)
    second = _api("revoke_plan")(workspace, plan.plan_id)

    assert first.state == second.state == "REVOKED"
    assert first.revision == second.revision == 3
    for function_name in ("inspect_plan", "approve_plan", "revoke_plan"):
        with pytest.raises(planner.PlanStateError, match="not found"):
            _api(function_name)(workspace, "plan-does-not-exist")


def test_inspect_detects_tampered_canonical_state(tmp_path: Path) -> None:
    planner = _planner_module()
    workspace = initialize_workspace(tmp_path / "workspace")
    source = tmp_path / "source"
    _seed_media(workspace, source, [{"name": "photo.jpg"}])
    plan = _build(workspace, tmp_path / "output")
    with closing(connect_database(workspace.database_path)) as connection:
        connection.execute("DROP TRIGGER organization_plan_no_update")
        connection.execute(
            "UPDATE organization_plan SET canonical_json='{}' WHERE plan_id=?", (plan.plan_id,)
        )
        connection.commit()

    with pytest.raises(planner.PlanStateError, match="canonical"):
        _api("inspect_plan")(workspace, plan.plan_id)


def test_approve_rejects_corrupted_canonical_plan_without_state_change(tmp_path: Path) -> None:
    planner = _planner_module()
    workspace = initialize_workspace(tmp_path / "workspace")
    source = tmp_path / "source"
    _seed_media(workspace, source, [{"name": "photo.jpg"}])
    plan = _build(workspace, tmp_path / "output")

    with closing(connect_database(workspace.database_path)) as connection:
        trigger_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='organization_plan_no_update'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER organization_plan_no_update")
        connection.execute(
            "UPDATE organization_plan SET canonical_json='{}' WHERE plan_id=?", (plan.plan_id,)
        )
        connection.execute(trigger_sql)
        connection.commit()
        assert validate_database(connection) == MIGRATIONS[-1].version

    with pytest.raises(planner.PlanStateError, match="canonical"):
        _api("approve_plan")(workspace, plan.plan_id)

    with closing(connect_database(workspace.database_path, read_only=True)) as connection:
        assert connection.execute(
            "SELECT state,revision FROM plan_approval WHERE plan_id=?", (plan.plan_id,)
        ).fetchone() == ("PENDING", 1)

    revoked = _api("revoke_plan")(workspace, plan.plan_id)
    assert (revoked.state, revoked.revision) == ("REVOKED", 2)


def test_preflight_reports_bundle_drift_warning_lock_permissions_and_space(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    planner = _planner_module()
    workspace = initialize_workspace(tmp_path / "workspace")
    source = tmp_path / "source"
    media_ids = _seed_media(
        workspace,
        source,
        [
            {"name": "IMG_1.HEIC", "role": "PRIMARY"},
            {"name": "IMG_1.MOV", "media_type": "VIDEO", "role": "MOTION"},
        ],
        bundle_id="bundle-warning",
    )
    plan = _build(workspace, tmp_path / "output")
    _api("approve_plan")(workspace, plan.plan_id)
    with closing(connect_database(workspace.database_path)) as connection:
        connection.execute(
            "UPDATE asset_bundle SET warning_status='AMBIGUOUS' WHERE id='bundle-warning'"
        )
        connection.execute("UPDATE media_item SET source_present=0 WHERE id=?", (media_ids[1],))
        connection.commit()
    (workspace.root / "state" / "apply.lock").write_text("synthetic", encoding="ascii")
    monkeypatch.setattr(planner.os, "access", lambda _path, _mode: False)
    monkeypatch.setattr(
        planner.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=0),
    )

    report = _api("preflight_plan")(workspace, plan.plan_id)
    codes = _issue_codes(report)

    assert {
        "BUNDLE_INCOMPLETE",
        "BUNDLE_WARNING",
        "APPLY_LOCKED",
        "OUTPUT_NOT_WRITABLE",
        "WORKSPACE_NOT_WRITABLE",
        "INSUFFICIENT_SPACE",
    } <= codes
    assert report.to_dict()["ok"] is False


def test_preflight_isolates_unreadable_source_target_directory_and_unknown_space(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    planner = _planner_module()
    workspace = initialize_workspace(tmp_path / "workspace")
    source = tmp_path / "source"
    _seed_media(workspace, source, [{"name": "one.jpg"}, {"name": "two.jpg"}])
    plan = _build(workspace, tmp_path / "output")
    _api("approve_plan")(workspace, plan.plan_id)
    first_source = Path(plan.entries[0].source_path)
    first_source.unlink()
    first_source.mkdir()
    second_target = Path(plan.entries[1].target_path)
    second_target.mkdir(parents=True)

    def no_space_probe(_path: Path) -> Any:
        raise OSError("synthetic disk probe failure")

    monkeypatch.setattr(planner.shutil, "disk_usage", no_space_probe)
    report = _api("preflight_plan")(workspace, plan.plan_id)

    assert {"SOURCE_UNREADABLE", "TARGET_CONFLICT", "SPACE_UNKNOWN"} <= _issue_codes(report)


def test_preflight_warns_on_mtime_only_and_detects_concurrent_approval_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    planner = _planner_module()
    workspace = initialize_workspace(tmp_path / "workspace")
    source = tmp_path / "source"
    _seed_media(workspace, source, [{"name": "photo.jpg"}])
    plan = _build(workspace, tmp_path / "output")
    _api("approve_plan")(workspace, plan.plan_id)
    source_path = Path(plan.entries[0].source_path)
    current = source_path.stat()
    os.utime(source_path, ns=(current.st_atime_ns, current.st_mtime_ns + 1_000_000))
    real_hash = planner._hash_regular_file
    revoked = False

    def revoke_while_hashing(path: Path, *args: Any) -> tuple[int, int, str]:
        nonlocal revoked
        result = real_hash(path, *args)
        if not revoked:
            revoked = True
            _api("revoke_plan")(workspace, plan.plan_id)
        return result

    monkeypatch.setattr(planner, "_hash_regular_file", revoke_while_hashing)
    report = _api("preflight_plan")(workspace, plan.plan_id)

    assert {"SOURCE_METADATA_CHANGED", "APPROVAL_CHANGED"} <= _issue_codes(report)
    assert report.approval_state == "REVOKED"


def test_cli_pending_preflight_and_missing_plan_return_stable_errors(
    tmp_path: Path, capsys: Any
) -> None:
    workspace_root = tmp_path / "workspace"
    workspace = initialize_workspace(workspace_root)
    source = tmp_path / "source"
    _seed_media(workspace, source, [{"name": "photo.jpg"}])
    plan = _build(workspace, tmp_path / "output")

    assert (
        cli_module.main(["plan", "preflight", plan.plan_id, "--workspace", str(workspace_root)])
        == 2
    )
    pending = json.loads(capsys.readouterr().out)
    assert "PLAN_NOT_APPROVED" in {issue["code"] for issue in pending["issues"]}

    assert (
        cli_module.main(["plan", "inspect", "missing-plan", "--workspace", str(workspace_root)])
        == 2
    )
    assert "not found" in capsys.readouterr().err


def test_phase_f_remediation_target_parent_reparse_and_file_obstruction_fail(
    tmp_path: Path,
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    source = tmp_path / "source"
    _seed_media(workspace, source, [{"name": "photo.jpg"}])

    junction_output = tmp_path / "junction-output"
    junction_plan = _build(workspace, junction_output)
    _api("approve_plan")(workspace, junction_plan.plan_id)
    junction_output.mkdir()
    junction_parent = (
        junction_output
        / Path(junction_plan.entries[0].target_path).relative_to(junction_output).parts[0]
    )
    external = tmp_path / "external"
    external.mkdir()
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction_parent), str(external)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            pytest.fail(f"unable to create junction fixture: {result.stderr or result.stdout}")
    else:  # pragma: no cover - Windows is the current P0 execution platform
        junction_parent.symlink_to(external, target_is_directory=True)

    junction_report = _api("preflight_plan")(workspace, junction_plan.plan_id)
    assert junction_report.ok is False
    assert "TARGET_PARENT_REPARSE" in _issue_codes(junction_report)
    assert list(external.iterdir()) == []

    file_output = tmp_path / "file-output"
    file_plan = _build(workspace, file_output)
    _api("approve_plan")(workspace, file_plan.plan_id)
    file_output.mkdir()
    file_parent = (
        file_output / Path(file_plan.entries[0].target_path).relative_to(file_output).parts[0]
    )
    file_parent.write_bytes(b"synthetic obstruction")

    file_report = _api("preflight_plan")(workspace, file_plan.plan_id)
    assert file_report.ok is False
    assert "TARGET_PARENT_NOT_DIRECTORY" in _issue_codes(file_report)


@pytest.mark.parametrize("category", ["CON.txt", "COM1.foo"])
def test_phase_f_remediation_category_device_stem_with_extension_is_sanitized(
    tmp_path: Path, category: str
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    source = tmp_path / "source"
    _seed_media(workspace, source, [{"name": "photo.jpg", "category": category}])

    plan = _build(workspace, tmp_path / "output")
    relative = Path(plan.entries[0].target_path).relative_to(tmp_path / "output")

    assert relative.parts[0].startswith("_")
    assert relative.parts[0].split(".", 1)[0].lstrip("_").upper() in {"CON", "COM1"}


def test_phase_f_remediation_trigger_name_with_weakened_sql_is_rejected(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    with closing(connect_database(workspace.database_path)) as connection:
        connection.execute("DROP TRIGGER organization_plan_no_update")
        connection.execute(
            """
            CREATE TRIGGER organization_plan_no_update
            BEFORE UPDATE ON organization_plan
            BEGIN
                SELECT 1;
            END
            """
        )
        connection.commit()

        with pytest.raises(MigrationError, match="trigger.*semantics"):
            validate_database(connection)


def test_phase_f_remediation_source_disappearing_after_safe_read_is_item_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    planner = _planner_module()
    workspace = initialize_workspace(tmp_path / "workspace")
    source = tmp_path / "source"
    _seed_media(workspace, source, [{"name": "photo.jpg"}])
    plan = _build(workspace, tmp_path / "output")
    _api("approve_plan")(workspace, plan.plan_id)
    real_open = planner._open_source_nofollow

    @contextmanager
    def disappearing_open(path: Path, source_root: Path, *, share_delete: bool = True):  # type: ignore[no-untyped-def]
        with real_open(path, source_root, share_delete=share_delete) as stream:
            yield stream
        path.unlink()

    monkeypatch.setattr(planner, "_open_source_nofollow", disappearing_open)
    report = _api("preflight_plan")(workspace, plan.plan_id)

    assert report.ok is False
    assert "SOURCE_MISSING" in _issue_codes(report)


def test_phase_f_remediation_future_plan_schema_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(_api("PlanPolicyError"), match="schema version"):
        _api("PlannerOptions")(output_root=tmp_path / "output", schema_version="future-v999")

    workspace = initialize_workspace(tmp_path / "workspace")
    source = tmp_path / "source"
    _seed_media(workspace, source, [{"name": "photo.jpg"}])
    plan = _build(workspace, tmp_path / "output")
    with closing(connect_database(workspace.database_path)) as connection:
        connection.execute("DROP TRIGGER organization_plan_no_update")
        connection.execute(
            "UPDATE organization_plan SET schema_version='future-v999' WHERE plan_id=?",
            (plan.plan_id,),
        )
        connection.commit()

    with pytest.raises(_planner_module().PlanStateError, match="schema version"):
        _api("inspect_plan")(workspace, plan.plan_id)


def test_phase_f_remediation_preflight_returns_revision_bound_approval_contract(
    tmp_path: Path,
) -> None:
    planner = _planner_module()
    workspace = initialize_workspace(tmp_path / "workspace")
    source = tmp_path / "source"
    _seed_media(workspace, source, [{"name": "photo.jpg"}])
    plan = _build(workspace, tmp_path / "output")
    approval = _api("approve_plan")(workspace, plan.plan_id)

    report = _api("preflight_plan")(workspace, plan.plan_id)

    assert report.ok is True
    assert report.approval_revision == approval.revision
    assert len(report.approval_contract) == 64
    assert planner.preflight_approval_is_current(workspace, report) is True

    _api("revoke_plan")(workspace, plan.plan_id)
    assert planner.preflight_approval_is_current(workspace, report) is False


@pytest.mark.parametrize("command", ["inspect", "preflight"])
def test_phase_f_remediation_read_only_cli_does_not_initialize_mistyped_workspace(
    tmp_path: Path, capsys: Any, command: str
) -> None:
    missing = tmp_path / "mistyped-workspace"

    assert cli_module.main(["plan", command, "plan-missing", "--workspace", str(missing)]) == 2

    assert not missing.exists()
    assert "workspace" in capsys.readouterr().err.lower()

    existing_empty = tmp_path / f"empty-{command}"
    existing_empty.mkdir()
    before = list(existing_empty.iterdir())
    assert (
        cli_module.main(["plan", command, "plan-missing", "--workspace", str(existing_empty)]) == 2
    )
    assert list(existing_empty.iterdir()) == before == []
