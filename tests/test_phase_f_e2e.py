from __future__ import annotations

from pathlib import Path

from PIL import Image

from smart_photo_triage.ai import AnalysisOptions, FakeVisionProvider, analyze_workspace
from smart_photo_triage.grouping import group_workspace
from smart_photo_triage.planner import (
    PlannerOptions,
    approve_plan,
    build_plan,
    preflight_plan,
)
from smart_photo_triage.preprocess import preprocess_workspace
from smart_photo_triage.review import ReviewStore
from smart_photo_triage.scanner import scan_library
from smart_photo_triage.workspace import initialize_workspace


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_phase_f_synthetic_pipeline_is_read_only_deterministic_and_approval_gated(
    tmp_path: Path,
) -> None:
    source = tmp_path / "synthetic-source"
    source.mkdir()
    for index, color in enumerate(((20, 80, 140), (20, 80, 140), (180, 40, 60))):
        Image.new("RGB", (32, 24), color).save(
            source / f"20240506_07080{index}_synthetic_{index}.png"
        )
    before = _snapshot(source)
    workspace = initialize_workspace(tmp_path / "workspace")

    scan = scan_library(workspace, source, hash_content=True)
    preview = preprocess_workspace(workspace)
    groups = group_workspace(workspace)
    analysis = analyze_workspace(
        workspace,
        provider=FakeVisionProvider(model="phase-f-e2e"),
        options=AnalysisOptions(batch_size=2),
    )
    review = ReviewStore(workspace)
    first_item = review.list_items(page_size=10).items[0]
    review.update_decision(
        int(first_item["id"]),
        category="01_家庭生活",
        disposition="KEEP",
        expected_revision=0,
    )
    output = tmp_path / "organized"
    first_plan = build_plan(workspace, PlannerOptions(output_root=output))
    second_plan = build_plan(workspace, PlannerOptions(output_root=output))
    pending = preflight_plan(workspace, first_plan.plan_id)
    approve_plan(workspace, first_plan.plan_id)
    approved = preflight_plan(workspace, first_plan.plan_id)

    assert scan.indexed_count == 3
    assert preview.processed_count + preview.cache_hit_count == 3
    assert preview.cache_hit_count == 1
    assert groups.duplicate_group_count == 1
    assert analysis.analyzed_count == 3
    assert first_plan.plan_id == second_plan.plan_id
    assert first_plan.canonical_json() == second_plan.canonical_json()
    assert "PLAN_NOT_APPROVED" in {issue.code for issue in pending.issues}
    assert approved.ok is True
    assert any(entry.decision_source == "HUMAN" for entry in first_plan.entries)
    assert _snapshot(source) == before
    assert not output.exists()
