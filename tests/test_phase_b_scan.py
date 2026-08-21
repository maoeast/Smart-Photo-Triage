from __future__ import annotations

import json
import os
import sqlite3
import struct
import subprocess
import time
import tracemalloc
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import pytest
from PIL import Image

import smart_photo_triage.database as database_module
from smart_photo_triage.cli import main
from smart_photo_triage.database import apply_migrations, connect_database, read_workspace_id
from smart_photo_triage.metadata import CaptureMetadata, extract_metadata
from smart_photo_triage.scanner import ScanLayoutError, scan_library
from smart_photo_triage.workspace import Workspace, initialize_workspace


def make_jpeg(
    path: Path,
    *,
    datetime_original: str | None = None,
    create_date: str | None = None,
    offset_original: str | None = None,
    offset_create: str | None = None,
    subsec_original: str | bytes | None = None,
    subsec_create: str | bytes | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exif = Image.Exif()
    if datetime_original is not None:
        exif[36867] = datetime_original
    if create_date is not None:
        exif[36868] = create_date
    if offset_original is not None:
        exif[36881] = offset_original
    if offset_create is not None:
        exif[36882] = offset_create
    if subsec_original is not None:
        exif[37521] = subsec_original
    if subsec_create is not None:
        exif[37522] = subsec_create
    Image.new("RGB", (3, 2), color=(20, 40, 60)).save(path, exif=exif)


def make_quicktime_video(path: Path, captured_unix_seconds: int) -> None:
    quicktime_seconds = captured_unix_seconds + 2_082_844_800
    mvhd_payload = b"\x00\x00\x00\x00" + struct.pack(">I", quicktime_seconds) + b"\x00" * 12
    mvhd = struct.pack(">I4s", len(mvhd_payload) + 8, b"mvhd") + mvhd_payload
    moov = struct.pack(">I4s", len(mvhd) + 8, b"moov") + mvhd
    ftyp_payload = b"qt  \x00\x00\x00\x00qt  "
    ftyp = struct.pack(">I4s", len(ftyp_payload) + 8, b"ftyp") + ftyp_payload
    path.write_bytes(ftyp + moov)


def make_quicktime_v1_video(
    path: Path, captured_unix_seconds: int, *, timescale: int, duration: int
) -> None:
    quicktime_seconds = captured_unix_seconds + 2_082_844_800
    mvhd_payload = (
        b"\x01\x00\x00\x00"
        + struct.pack(">Q", quicktime_seconds)
        + struct.pack(">Q", quicktime_seconds)
        + struct.pack(">I", timescale)
        + struct.pack(">Q", duration)
    )
    mvhd = struct.pack(">I4s", len(mvhd_payload) + 8, b"mvhd") + mvhd_payload
    moov = struct.pack(">I4s", len(mvhd) + 8, b"moov") + mvhd
    path.write_bytes(moov)


def rows(workspace: Workspace, sql: str, parameters: tuple[object, ...] = ()) -> list[tuple]:
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        return connection.execute(sql, parameters).fetchall()


def media_rows(workspace: Workspace) -> list[tuple]:
    return rows(
        workspace,
        """
        SELECT id, original_path, media_type, extension, size_bytes, mtime_ns,
               source_present, content_sha256, captured_at, capture_source,
               capture_confidence, capture_timezone_status, last_seen_scan_id,
               metadata_error, preview_path
        FROM media_item
        ORDER BY original_path
        """,
    )


def source_snapshot(source: Path) -> dict[str, tuple[bytes, int]]:
    return {
        path.relative_to(source).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in source.rglob("*")
        if path.is_file()
    }


def bundle_snapshot(workspace: Workspace) -> list[tuple[str, str, str, str]]:
    return rows(
        workspace,
        """
        SELECT b.id, b.bundle_type, m.role, i.original_path
        FROM asset_bundle AS b
        JOIN bundle_member AS m ON m.bundle_id = b.id
        JOIN media_item AS i ON i.id = m.media_id
        ORDER BY b.id, m.role, i.original_path
        """,
    )


def test_t_b_001_same_content_at_two_paths_remains_two_media_instances(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    original = source / "a" / "photo.jpg"
    duplicate = source / "b" / "copy.jpg"
    make_jpeg(original)
    duplicate.parent.mkdir(parents=True)
    duplicate.write_bytes(original.read_bytes())
    before = source_snapshot(source)
    workspace = initialize_workspace(tmp_path / "workspace")

    result = scan_library(workspace, source, hash_content=True)
    indexed = media_rows(workspace)

    assert result.indexed_count == 2
    assert len(indexed) == 2
    assert indexed[0][0] != indexed[1][0]
    assert indexed[0][7] == indexed[1][7]
    assert indexed[0][7] is not None
    assert source_snapshot(source) == before


def test_t_b_002_same_path_rescan_updates_scan_identity_without_business_drift(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    make_jpeg(source / "photo.jpg")
    workspace = initialize_workspace(tmp_path / "workspace")
    first = scan_library(workspace, source, hash_content=True)
    first_row = media_rows(workspace)[0]
    with closing(sqlite3.connect(workspace.database_path)) as connection:
        connection.execute(
            "UPDATE media_item SET preview_path = ? WHERE id = ?",
            ("previews/keep.webp", first_row[0]),
        )
        connection.commit()

    second = scan_library(workspace, source)
    second_row = media_rows(workspace)[0]

    assert second.scan_id != first.scan_id
    assert second.unchanged_count == 1
    assert second_row[0] == first_row[0]
    assert second_row[7] == first_row[7]
    assert second_row[12] == second.scan_id
    assert second_row[14] == "previews/keep.webp"


def test_t_b_003_rescan_marks_missing_source_without_deleting_history(tmp_path: Path) -> None:
    source = tmp_path / "source"
    media_path = source / "photo.jpg"
    make_jpeg(media_path)
    workspace = initialize_workspace(tmp_path / "workspace")
    scan_library(workspace, source)
    first_row = media_rows(workspace)[0]
    media_id = first_row[0]
    assert first_row[7] is None
    media_path.unlink()

    result = scan_library(workspace, source)
    indexed = media_rows(workspace)

    assert result.missing_count == 1
    assert len(indexed) == 1
    assert indexed[0][0] == media_id
    assert indexed[0][6] == 0


def test_t_b_004_default_scan_does_not_follow_directory_symlink_or_junction(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    external = tmp_path / "external"
    source.mkdir()
    make_jpeg(source / "inside.jpg")
    make_jpeg(external / "outside.jpg")
    link = source / "outside-link"
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(external)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            pytest.fail(f"unable to create junction fixture: {result.stderr or result.stdout}")
    else:
        link.symlink_to(external, target_is_directory=True)
    workspace = initialize_workspace(tmp_path / "workspace")

    scan_library(workspace, source)

    indexed = media_rows(workspace)
    assert len(indexed) == 1
    assert Path(indexed[0][1]).name == "inside.jpg"


def test_t_b_005_workspace_and_output_nested_in_source_are_safely_excluded(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    make_jpeg(source / "keep.jpg")
    output = source / "organized-output"
    make_jpeg(output / "must-not-ingest.jpg")
    workspace = initialize_workspace(source / ".spt")

    result = scan_library(workspace, source, output=output)
    indexed = media_rows(workspace)

    assert result.indexed_count == 1
    assert [Path(row[1]).name for row in indexed] == ["keep.jpg"]


def test_scan_rejects_source_nested_inside_workspace_or_output(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    source_in_workspace = workspace.root / "unsafe-source"
    make_jpeg(source_in_workspace / "photo.jpg")
    with pytest.raises(ScanLayoutError, match="inside workspace"):
        scan_library(workspace, source_in_workspace)

    source_in_output = tmp_path / "output" / "unsafe-source"
    make_jpeg(source_in_output / "photo.jpg")
    with pytest.raises(ScanLayoutError, match="inside output"):
        scan_library(workspace, source_in_output, output=tmp_path / "output")


def test_t_b_006_capture_time_precedence_uses_exif_then_quicktime_before_filename(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    make_jpeg(
        source / "20200101_010101.jpg",
        datetime_original="2024:05:06 07:08:09",
        create_date="2023:04:03 02:01:00",
        offset_original="+08:00",
    )
    make_quicktime_video(source / "20210102_030405.mov", 1_704_164_645)
    workspace = initialize_workspace(tmp_path / "workspace")

    scan_library(workspace, source)
    captures = rows(
        workspace,
        "SELECT extension, captured_at, capture_source, capture_confidence "
        "FROM media_item ORDER BY extension",
    )

    assert captures[0] == (
        ".jpg",
        "2024-05-06T07:08:09+08:00",
        "EXIF_DATETIME_ORIGINAL",
        "HIGH",
    )
    assert captures[1] == (
        ".mov",
        "2024-01-02T03:04:05+00:00",
        "QUICKTIME_CREATION_TIME",
        "HIGH",
    )


def test_t_b_007_exif_without_offset_keeps_unknown_timezone_and_is_not_forced_to_utc(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    make_jpeg(source / "photo.jpg", datetime_original="2024:05:06 07:08:09")
    workspace = initialize_workspace(tmp_path / "workspace")

    scan_library(workspace, source)
    captured_at, timezone_status = rows(
        workspace,
        "SELECT captured_at, capture_timezone_status FROM media_item",
    )[0]

    assert captured_at == "2024-05-06T07:08:09"
    assert timezone_status == "UNKNOWN"
    assert not captured_at.endswith("Z")
    assert "+00:00" not in captured_at


def test_t_b_008_filename_fallback_accepts_only_supported_patterns_and_is_lower_confidence(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    make_jpeg(source / "IMG_20240203_040506.jpg")
    invalid = source / "vacation-2024-ish.jpg"
    make_jpeg(invalid)
    fixed_mtime = 1_600_000_000
    os.utime(invalid, (fixed_mtime, fixed_mtime))
    workspace = initialize_workspace(tmp_path / "workspace")

    scan_library(workspace, source)
    indexed = rows(
        workspace,
        "SELECT original_path, captured_at, capture_source, capture_confidence, "
        "capture_timezone_status FROM media_item ORDER BY original_path",
    )
    by_name = {Path(row[0]).name: row[1:] for row in indexed}

    assert by_name["IMG_20240203_040506.jpg"] == (
        "2024-02-03T04:05:06",
        "FILENAME",
        "MEDIUM",
        "UNKNOWN",
    )
    assert by_name["vacation-2024-ish.jpg"][1] == "FILESYSTEM_MTIME"
    assert by_name["vacation-2024-ish.jpg"][2] == "LOW"


def test_t_b_009_corrupt_metadata_is_isolated_and_other_items_continue(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "corrupt.jpg").write_bytes(b"not a jpeg")
    make_jpeg(source / "good.jpg", datetime_original="2024:01:02 03:04:05")
    workspace = initialize_workspace(tmp_path / "workspace")

    result = scan_library(workspace, source)
    indexed = media_rows(workspace)
    by_name = {Path(row[1]).name: row for row in indexed}

    assert result.indexed_count == 2
    assert result.error_count == 1
    assert by_name["corrupt.jpg"][13]
    assert by_name["corrupt.jpg"][6] == 1
    assert by_name["good.jpg"][13] is None
    assert by_name["good.jpg"][9] == "EXIF_DATETIME_ORIGINAL"


def test_t_b_010_matching_heic_and_mov_form_one_live_photo_bundle(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "IMG_0001.HEIC").write_bytes(b"synthetic heic")
    make_quicktime_video(source / "IMG_0001.MOV", 1_704_164_645)
    workspace = initialize_workspace(tmp_path / "workspace")

    scan_library(workspace, source)
    bundles = bundle_snapshot(workspace)

    assert {row[1] for row in bundles} == {"LIVE_PHOTO"}
    assert {row[2] for row in bundles} == {"PRIMARY", "LIVE_VIDEO"}
    assert len({row[0] for row in bundles}) == 1


def test_t_b_011_live_photo_aae_follows_the_same_logical_asset(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "IMG_0001.HEIC").write_bytes(b"synthetic heic")
    make_quicktime_video(source / "IMG_0001.MOV", 1_704_164_645)
    (source / "IMG_0001.AAE").write_text("<plist/>", encoding="utf-8")
    workspace = initialize_workspace(tmp_path / "workspace")

    scan_library(workspace, source)
    bundles = bundle_snapshot(workspace)

    assert len({row[0] for row in bundles}) == 1
    assert {row[1] for row in bundles} == {"LIVE_PHOTO"}
    assert {row[2] for row in bundles} == {"PRIMARY", "LIVE_VIDEO", "SIDECAR"}


def test_t_b_012_ambiguous_companions_warn_and_do_not_bind_randomly(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "IMG_0001.HEIC").write_bytes(b"one")
    (source / "IMG_0001.HEIF").write_bytes(b"two")
    make_quicktime_video(source / "IMG_0001.MOV", 1_704_164_645)
    workspace = initialize_workspace(tmp_path / "workspace")

    first = scan_library(workspace, source)
    first_bundles = bundle_snapshot(workspace)
    warnings = rows(
        workspace,
        "SELECT code, message FROM scan_warning WHERE scan_id = ? ORDER BY code, message",
        (first.scan_id,),
    )
    second = scan_library(workspace, source)
    second_bundles = bundle_snapshot(workspace)

    assert warnings
    assert {row[0] for row in warnings} == {"AMBIGUOUS_BUNDLE"}
    assert {row[1] for row in first_bundles} == {"SINGLE"}
    assert first_bundles == second_bundles
    assert second.warning_count >= 1


def test_t_b_013_missing_companion_keeps_primary_and_does_not_abort(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "IMG_0001.HEIC").write_bytes(b"synthetic heic without mov")
    make_jpeg(source / "ordinary.jpg")
    workspace = initialize_workspace(tmp_path / "workspace")

    result = scan_library(workspace, source)
    indexed = media_rows(workspace)
    bundles = bundle_snapshot(workspace)

    assert result.indexed_count == 2
    assert len(indexed) == 2
    assert all(row[6] == 1 for row in indexed)
    assert {row[1] for row in bundles} == {"SINGLE"}


def test_scan_cli_provides_minimal_operational_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "source"
    make_jpeg(source / "photo.jpg")
    workspace_path = tmp_path / "workspace"

    assert (
        main(
            [
                "scan",
                str(source),
                "--workspace",
                str(workspace_path),
                "--full-hash",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "Scan complete" in output
    workspace = initialize_workspace(workspace_path)
    assert len(media_rows(workspace)) == 1
    assert media_rows(workspace)[0][7] is not None


def test_phase_b_schema_migration_is_versioned_and_idempotent(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    first_schema = rows(
        workspace,
        "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
        "ORDER BY type, name",
    )
    second_workspace = initialize_workspace(workspace.root)
    second_schema = rows(
        second_workspace,
        "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
        "ORDER BY type, name",
    )
    version = rows(workspace, "PRAGMA user_version")[0][0]
    migration_count = rows(workspace, "SELECT COUNT(*) FROM schema_migration")[0][0]

    assert version >= 2
    assert migration_count == version
    assert second_schema == first_schema
    assert {row[1] for row in first_schema} >= {
        "media_item",
        "scan_run",
        "asset_bundle",
        "bundle_member",
        "scan_warning",
    }


def test_phase_b_migrates_v1_in_place_without_changing_workspace_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "upgrade.sqlite3"
    workspace_id = uuid4().hex
    all_migrations = database_module.MIGRATIONS
    with closing(connect_database(database_path)) as connection:
        monkeypatch.setattr(database_module, "MIGRATIONS", all_migrations[:1])
        assert apply_migrations(connection, workspace_id=workspace_id) == 1
        assert read_workspace_id(connection) == workspace_id

        monkeypatch.setattr(database_module, "MIGRATIONS", all_migrations)
        assert apply_migrations(connection, workspace_id=workspace_id) == all_migrations[-1].version
        assert read_workspace_id(connection) == workspace_id
        assert connection.execute(
            "SELECT version, name FROM schema_migration ORDER BY version"
        ).fetchall() == [(migration.version, migration.name) for migration in all_migrations]
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'media_item'"
        ).fetchone() == (1,)


def test_exif_invalid_original_falls_back_to_create_date_with_subseconds(
    tmp_path: Path,
) -> None:
    path = tmp_path / "photo.jpg"
    make_jpeg(
        path,
        datetime_original="invalid",
        create_date="2024:06:07 08:09:10",
        offset_create="Z",
        subsec_create=b"25",
    )

    metadata = extract_metadata(path, "IMAGE")

    assert metadata.captured_at == "2024-06-07T08:09:10.250000+00:00"
    assert metadata.capture_source == "EXIF_CREATE_DATE"
    assert metadata.capture_timezone_status == "KNOWN"
    assert metadata.error is not None
    assert "EXIF_DATETIME_ORIGINAL" in metadata.error


def test_invalid_exif_offset_falls_back_to_supported_alternate_filename(
    tmp_path: Path,
) -> None:
    path = tmp_path / "2024-02-03 04-05-06.jpg"
    make_jpeg(
        path,
        datetime_original="2023:01:02 03:04:05",
        offset_original="+99:00",
    )

    metadata = extract_metadata(path, "IMAGE")

    assert metadata.captured_at == "2024-02-03T04:05:06"
    assert metadata.capture_source == "FILENAME"
    assert metadata.capture_confidence == "MEDIUM"
    assert metadata.error is not None
    assert "unsupported EXIF UTC offset" in metadata.error


def test_quicktime_v1_creation_and_duration_are_read_without_decoding_video(
    tmp_path: Path,
) -> None:
    path = tmp_path / "clip.mov"
    make_quicktime_v1_video(path, 1_704_164_645, timescale=1_000, duration=2_500)

    metadata = extract_metadata(path, "VIDEO")

    assert metadata.captured_at == "2024-01-02T03:04:05+00:00"
    assert metadata.capture_source == "QUICKTIME_CREATION_TIME"
    assert metadata.duration_seconds == 2.5


def test_malformed_quicktime_metadata_falls_back_and_records_error(tmp_path: Path) -> None:
    path = tmp_path / "broken.mov"
    path.write_bytes(struct.pack(">I4s", 100, b"moov") + b"short")

    metadata = extract_metadata(path, "VIDEO")

    assert metadata.capture_source == "FILESYSTEM_MTIME"
    assert metadata.capture_confidence == "LOW"
    assert metadata.error is not None
    assert "invalid QuickTime atom size" in metadata.error


def test_sidecar_set_supports_media_extension_json_name(tmp_path: Path) -> None:
    source = tmp_path / "source"
    make_jpeg(source / "holiday.JPG")
    (source / "holiday.JPG.json").write_text("{}", encoding="utf-8")
    (source / "holiday.AAE").write_text("<plist/>", encoding="utf-8")
    (source / "holiday.XMP").write_text("<xmp/>", encoding="utf-8")
    workspace = initialize_workspace(tmp_path / "workspace")

    scan_library(workspace, source)
    bundles = bundle_snapshot(workspace)

    assert len({row[0] for row in bundles}) == 1
    assert {row[1] for row in bundles} == {"SIDECAR_SET"}
    assert {row[2] for row in bundles} == {"PRIMARY", "SIDECAR"}
    assert len(bundles) == 4


def test_changed_path_keeps_identity_and_invalidates_stale_content_hash(tmp_path: Path) -> None:
    source = tmp_path / "source"
    path = source / "photo.jpg"
    make_jpeg(path)
    workspace = initialize_workspace(tmp_path / "workspace")
    scan_library(workspace, source, hash_content=True)
    first = media_rows(workspace)[0]
    path.write_bytes(path.read_bytes() + b"changed")

    result = scan_library(workspace, source)
    second = media_rows(workspace)[0]

    assert result.unchanged_count == 0
    assert second[0] == first[0]
    assert first[7] is not None
    assert second[7] is None


def test_exclude_glob_and_injected_metadata_failure_remain_item_local(tmp_path: Path) -> None:
    source = tmp_path / "source"
    make_jpeg(source / "good.jpg")
    make_jpeg(source / "bad.jpg")
    make_jpeg(source / "drop-me.jpg")
    workspace = initialize_workspace(tmp_path / "workspace")

    def selective_metadata(path: Path, _media_type: str) -> CaptureMetadata:
        if path.name == "bad.jpg":
            raise RuntimeError("synthetic parser failure")
        return CaptureMetadata("2024-01-01T00:00:00", "SYNTHETIC", "HIGH", "UNKNOWN")

    result = scan_library(
        workspace,
        source,
        exclude_globs=("drop-*",),
        metadata_extractor=selective_metadata,
    )
    indexed = media_rows(workspace)
    by_name = {Path(row[1]).name: row for row in indexed}

    assert result.discovered_count == 2
    assert result.indexed_count == 2
    assert result.error_count == 1
    assert set(by_name) == {"bad.jpg", "good.jpg"}
    assert "synthetic parser failure" in by_name["bad.jpg"][13]
    assert by_name["good.jpg"][9] == "SYNTHETIC"


def test_scan_rejects_missing_source_and_regular_file_source(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    with pytest.raises(ScanLayoutError, match="unavailable"):
        scan_library(workspace, tmp_path / "missing")

    regular_file = tmp_path / "not-a-directory"
    regular_file.write_text("x", encoding="utf-8")
    with pytest.raises(ScanLayoutError, match="not a directory"):
        scan_library(workspace, regular_file)


def test_phase_b_10k_synthetic_metadata_scan_is_streaming_and_bounded(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    synthetic_payload = json.dumps({"synthetic": True}).encode()
    for index in range(10_000):
        (
            source
            / f"IMG_20240101_{index % 24:02d}{index % 60:02d}{index % 60:02d}_{index:05d}.json"
        ).write_bytes(synthetic_payload)
    workspace = initialize_workspace(tmp_path / "workspace")

    tracemalloc.start()
    started = time.perf_counter()
    result = scan_library(workspace, source)
    elapsed = time.perf_counter() - started
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert result.discovered_count == 10_000
    assert result.indexed_count == 10_000
    assert rows(workspace, "SELECT COUNT(*) FROM media_item")[0][0] == 10_000
    assert peak_bytes < 64 * 1024 * 1024
    assert elapsed > 0
    items_per_second = result.indexed_count / elapsed
    peak_mib = peak_bytes / 1024 / 1024
    print(
        "PHASE_B_10K_SMOKE "
        f"items={result.indexed_count} elapsed={elapsed:.3f}s "
        f"items_per_second={items_per_second:.1f} peak_mib={peak_mib:.2f}"
    )
