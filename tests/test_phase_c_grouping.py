from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

from PIL import Image

import smart_photo_triage.database as database_module
from smart_photo_triage.database import apply_migrations, connect_database, read_workspace_id
from smart_photo_triage.grouping import (
    BURST_ALGORITHM_VERSION,
    DUPLICATE_ALGORITHM_VERSION,
    BurstCandidate,
    group_burst_candidates,
    group_bursts,
    group_exact_duplicates,
)
from smart_photo_triage.preprocess import PERCEPTUAL_HASH_VERSION, measure_quality
from smart_photo_triage.scanner import scan_library
from smart_photo_triage.workspace import Workspace, initialize_workspace


def query(workspace: Workspace, sql: str, parameters: tuple[object, ...] = ()) -> list[tuple]:
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        return connection.execute(sql, parameters).fetchall()


def source_snapshot(source: Path) -> dict[str, bytes]:
    return {
        path.relative_to(source).as_posix(): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file()
    }


def make_jpeg(path: Path, color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (24, 16), color).save(path, quality=95)


def candidates(*hashes: str, step_seconds: float = 0.5) -> list[BurstCandidate]:
    origin = datetime.fromisoformat("2024-01-01T00:00:00")
    return [
        BurstCandidate(
            media_id=index + 1,
            captured_at=(origin + timedelta(seconds=index * step_seconds)).isoformat(),
            perceptual_hash=value,
            quality_score=float(index),
            path_key=f"item-{index + 1:04d}",
        )
        for index, value in enumerate(hashes)
    ]


def test_t_c_008_exact_duplicate_same_size_and_sha_forms_group(tmp_path: Path) -> None:
    source = tmp_path / "source"
    original = source / "one.jpg"
    make_jpeg(original, (20, 60, 100))
    duplicate = source / "two.jpg"
    duplicate.write_bytes(original.read_bytes())
    make_jpeg(source / "different-size.jpg", (200, 20, 80))
    workspace = initialize_workspace(tmp_path / "workspace")
    scan_library(workspace, source)

    group_count = group_exact_duplicates(workspace)
    grouped = query(
        workspace,
        """
        SELECT g.algorithm_version, g.content_sha256, m.media_id
        FROM duplicate_group AS g
        JOIN duplicate_member AS m ON m.group_id = g.id
        ORDER BY g.id, m.media_id
        """,
    )

    assert group_count == 1
    assert len(grouped) == 2
    assert {row[0] for row in grouped} == {DUPLICATE_ALGORITHM_VERSION}
    assert len({row[1] for row in grouped}) == 1


def test_t_c_009_same_size_different_bytes_is_not_duplicate(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "one.jpg").write_bytes(b"a" * 128)
    (source / "two.jpg").write_bytes(b"b" * 128)
    workspace = initialize_workspace(tmp_path / "workspace")
    scan_library(workspace, source)

    group_count = group_exact_duplicates(workspace)

    assert group_count == 0
    assert query(workspace, "SELECT COUNT(*) FROM duplicate_group") == [(0,)]
    hashes = query(workspace, "SELECT content_sha256 FROM media_item ORDER BY original_path")
    assert hashes[0][0] != hashes[1][0]


def test_t_c_010_duplicate_grouping_never_moves_or_deletes_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    original = source / "one.jpg"
    make_jpeg(original, (40, 90, 120))
    (source / "two.jpg").write_bytes(original.read_bytes())
    before = source_snapshot(source)
    workspace = initialize_workspace(tmp_path / "workspace")
    scan_library(workspace, source)

    group_exact_duplicates(workspace)

    assert source_snapshot(source) == before
    assert query(workspace, "SELECT COUNT(*) FROM operation_transaction") == [(0,)]
    assert query(workspace, "SELECT COUNT(*) FROM operation_journal") == [(0,)]


def test_t_c_011_time_close_and_visually_similar_items_form_burst() -> None:
    result = group_burst_candidates(
        candidates("0000000000000000", "0000000000000001", "0000000000000003"),
        distance_threshold=2,
    )

    assert len(result.clusters) == 1
    assert result.clusters[0].member_ids == (1, 2, 3)
    assert result.clusters[0].representative_media_id in {1, 2, 3}


def test_t_c_012_time_close_but_visually_different_items_stay_separate() -> None:
    result = group_burst_candidates(
        candidates("0000000000000000", "ffffffffffffffff"),
        distance_threshold=8,
    )

    assert result.clusters == ()


def test_t_c_013_visually_similar_but_time_far_items_stay_separate() -> None:
    result = group_burst_candidates(
        candidates("0000000000000000", "0000000000000001", step_seconds=60.0),
        time_window_seconds=3.0,
        distance_threshold=8,
    )

    assert result.clusters == ()


def test_t_c_014_false_chain_does_not_expand_transitively() -> None:
    result = group_burst_candidates(
        candidates("0000000000000000", "0000000000000001", "0000000000000003"),
        distance_threshold=1,
    )

    assert len(result.clusters) == 1
    assert result.clusters[0].member_ids == (1, 2)
    assert 3 not in result.clusters[0].member_ids


def test_t_c_015_burst_grouping_is_input_order_independent() -> None:
    ordered = candidates(
        "0000000000000000",
        "0000000000000001",
        "0000000000000003",
        "ffffffffffffffff",
        "fffffffffffffffe",
    )

    first = group_burst_candidates(ordered, distance_threshold=2)
    second = group_burst_candidates(list(reversed(ordered)), distance_threshold=2)

    assert first == second
    assert all(cluster.group_id for cluster in first.clusters)


def test_t_c_016_burst_candidate_comparisons_are_not_full_library_quadratic() -> None:
    origin = datetime.fromisoformat("2024-01-01T00:00:00")
    generated = [
        BurstCandidate(
            media_id=index + 1,
            captured_at=(origin + timedelta(milliseconds=index * 10)).isoformat(),
            perceptual_hash=f"{index % 16:016x}",
            path_key=f"item-{index:05d}",
        )
        for index in range(2000)
    ]

    result = group_burst_candidates(
        generated,
        time_window_seconds=2.0,
        distance_threshold=2,
    )

    assert result.comparison_count < len(generated) * 32


def test_persisted_burst_records_algorithm_version_and_deterministic_medoid(
    tmp_path: Path,
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    with closing(connect_database(workspace.database_path)) as connection:
        for media_id, candidate in enumerate(
            candidates("0000000000000000", "0000000000000001", "0000000000000003"),
            start=1,
        ):
            path = tmp_path / f"item-{media_id}.jpg"
            path.write_bytes(bytes([media_id]))
            connection.execute(
                """
                INSERT INTO media_item(
                    id, original_path, path_key, source_root, source_root_key, parent_key,
                    bundle_stem, media_type, extension, size_bytes, mtime_ns, source_present,
                    captured_at, capture_source, capture_confidence, capture_timezone_status,
                    last_seen_at, last_seen_scan_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'IMAGE', '.jpg', 1, 1, 1, ?, 'SYNTHETIC',
                          'HIGH', 'UNKNOWN', '2024-01-01T00:00:00', 'synthetic')
                """,
                (
                    media_id,
                    str(path),
                    candidate.path_key,
                    str(tmp_path),
                    str(tmp_path),
                    str(tmp_path),
                    f"item-{media_id}",
                    candidate.captured_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO media_preprocess(
                    media_id, source_fingerprint, preview_fingerprint, preview_path,
                    preview_version, preview_status, perceptual_hash, perceptual_hash_version,
                    quality_json, quality_score, updated_at
                ) VALUES (?, ?, ?, ?, 'preview-v1', 'READY', ?, ?, '{}', ?,
                          '2024-01-01T00:00:00')
                """,
                (
                    media_id,
                    f"source-{media_id}",
                    f"preview-{media_id}",
                    f"previews/{media_id}.webp",
                    candidate.perceptual_hash,
                    PERCEPTUAL_HASH_VERSION,
                    candidate.quality_score,
                ),
            )
        connection.commit()

    first = group_bursts(workspace, distance_threshold=2)
    first_rows = query(
        workspace,
        """
        SELECT g.id, g.algorithm_version, g.representative_media_id,
               m.media_id, m.is_representative, m.is_best_shot
        FROM burst_group AS g JOIN burst_member AS m ON m.group_id = g.id
        ORDER BY g.id, m.media_id
        """,
    )
    second = group_bursts(workspace, distance_threshold=2)
    second_rows = query(
        workspace,
        """
        SELECT g.id, g.algorithm_version, g.representative_media_id,
               m.media_id, m.is_representative, m.is_best_shot
        FROM burst_group AS g JOIN burst_member AS m ON m.group_id = g.id
        ORDER BY g.id, m.media_id
        """,
    )

    assert first == second
    assert first[0] == 1
    assert 0 < first[1] < 32
    assert first_rows == second_rows
    assert {row[1] for row in first_rows} == {BURST_ALGORITHM_VERSION}
    assert sum(row[4] for row in first_rows) == 1
    assert sum(row[5] for row in first_rows) == 1


def test_t_c_017_local_quality_score_is_advisory_and_non_destructive() -> None:
    black = Image.new("RGB", (2, 2), "black")

    metrics = measure_quality(black)

    assert 0.0 <= metrics.score <= 1.0
    assert metrics.advisory in {"BEST_SHOT_CANDIDATE", "REVIEW"}
    assert not hasattr(metrics, "action")
    assert not hasattr(metrics, "file_action")


def test_phase_c_schema_is_additive_v5_or_newer_and_preserves_v4_data(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    database_path = tmp_path / "phase-c-upgrade.sqlite3"
    workspace_id = uuid4().hex
    all_migrations = database_module.MIGRATIONS
    assert [migration.version for migration in all_migrations[:4]] == [1, 2, 3, 4]
    monkeypatch.setattr(database_module, "MIGRATIONS", all_migrations[:4])
    with closing(connect_database(database_path)) as connection:
        assert apply_migrations(connection, workspace_id=workspace_id) == 4
        connection.execute(
            "INSERT INTO scan_run(id, source_root, source_root_key, started_at, status) "
            "VALUES ('preserved', 'C:/source', 'c:/source', '2024-01-01', 'COMPLETE')"
        )
        connection.commit()

        monkeypatch.setattr(database_module, "MIGRATIONS", all_migrations)
        assert apply_migrations(connection, workspace_id=workspace_id) >= 5
        assert read_workspace_id(connection) == workspace_id
        assert connection.execute("SELECT id FROM scan_run").fetchall() == [("preserved",)]
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }

    assert {
        "preprocess_run",
        "media_preprocess",
        "duplicate_group",
        "duplicate_member",
        "burst_group",
        "burst_member",
    } <= tables
