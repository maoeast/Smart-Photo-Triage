from __future__ import annotations

import sqlite3
import time
import tracemalloc
from contextlib import closing
from pathlib import Path

from PIL import Image

from smart_photo_triage.cli import main
from smart_photo_triage.grouping import group_workspace
from smart_photo_triage.preprocess import preprocess_workspace
from smart_photo_triage.scanner import scan_library
from smart_photo_triage.workspace import initialize_workspace


def count_rows(database_path: Path, table: str) -> int:
    with closing(sqlite3.connect(database_path)) as connection:
        row = connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
    assert row is not None
    return int(row[0])


def test_phase_c_cli_preprocess_and_group_minimal_path(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    source = tmp_path / "source"
    source.mkdir()
    first = source / "IMG_20240101_000000_1.png"
    Image.new("RGB", (16, 16), "red").save(first)
    (source / "IMG_20240101_000000_2.png").write_bytes(first.read_bytes())
    workspace = tmp_path / "workspace"
    assert main(["scan", str(source), "--workspace", str(workspace)]) == 0

    assert main(["preprocess", "--workspace", str(workspace)]) == 0
    assert main(["group", "--workspace", str(workspace)]) == 0

    output = capsys.readouterr().out
    assert "Preprocess complete" in output
    assert "Grouping complete" in output


def test_phase_c_1k_generated_media_smoke_is_bounded_and_cacheable(tmp_path: Path) -> None:
    source = tmp_path / "generated-source"
    source.mkdir()
    for index in range(1000):
        color = (index % 256, (index * 7) % 256, (index * 13) % 256)
        Image.new("RGB", (16, 16), color).save(
            source / f"IMG_20240101_000000_{index:04d}.png",
            optimize=False,
        )
    workspace = initialize_workspace(tmp_path / "workspace")

    tracemalloc.start()
    started = time.perf_counter()
    scan = scan_library(workspace, source)
    first = preprocess_workspace(workspace)
    grouped = group_workspace(workspace)
    second = preprocess_workspace(workspace)
    elapsed = time.perf_counter() - started
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    sqlite_bytes = workspace.database_path.stat().st_size

    assert scan.indexed_count == 1000
    assert first.processed_count == 256
    assert first.cache_hit_count == 744
    assert first.failed_count == 0
    assert second.processed_count == 0
    assert second.cache_hit_count == 1000
    assert grouped.comparison_count < 32_000
    assert count_rows(workspace.database_path, "media_preprocess") == 1000
    assert peak_bytes < 96 * 1024 * 1024
    assert elapsed > 0
    print(
        "PHASE_C_1K_SMOKE "
        f"items=1000 elapsed={elapsed:.3f}s items_per_second={1000 / elapsed:.1f} "
        f"peak_mib={peak_bytes / 1024 / 1024:.2f} sqlite_mib={sqlite_bytes / 1024 / 1024:.2f} "
        f"cache_hits={second.cache_hit_count} duplicate_groups={grouped.duplicate_group_count} "
        f"burst_groups={grouped.burst_group_count} failed={first.failed_count} "
        f"comparisons={grouped.comparison_count}"
    )
