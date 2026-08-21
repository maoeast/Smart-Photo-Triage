"""Run an installed-package Phase H synthetic lifecycle without pytest or network."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

from PIL import Image

from smart_photo_triage.executor import apply_plan, rollback_transaction
from smart_photo_triage.planner import PlannerOptions, approve_plan, build_plan, preflight_plan
from smart_photo_triage.review import ReviewStore
from smart_photo_triage.workspace import open_workspace


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _run(spt: Path, *arguments: str) -> str:
    completed = subprocess.run([str(spt), *arguments], check=True, text=True, capture_output=True)
    output = completed.stdout.strip()
    print(output)
    return output


def main(root: Path, spt: Path) -> None:
    source = root / "source"
    workspace_path = root / "workspace"
    output = root / "organized"
    source.mkdir(parents=True)
    Image.new("RGB", (16, 16), "red").save(source / "IMG_20250101_000001.png")
    (source / "IMG_20250101_000002.png").write_bytes(
        (source / "IMG_20250101_000001.png").read_bytes()
    )
    before = _snapshot(source)

    _run(spt, "init", "--workspace", str(workspace_path))
    _run(spt, "scan", str(source), "--workspace", str(workspace_path), "--full-hash")
    _run(spt, "preprocess", "--workspace", str(workspace_path))
    _run(spt, "group", "--workspace", str(workspace_path))
    first_analysis = _run(spt, "analyze", "--workspace", str(workspace_path), "--provider", "fake")
    assert "requests=1" in first_analysis

    workspace = open_workspace(workspace_path)
    review = ReviewStore(workspace)
    first_item = review.list_items(page_size=1).items[0]
    review.update_decision(
        int(first_item["id"]),
        category="01_家庭生活",
        disposition="KEEP",
        expected_revision=int(first_item["revision"]),
    )
    print("HUMAN_OVERRIDE=PASS")
    plan = build_plan(workspace, PlannerOptions(output_root=output))
    assert any(entry.decision_source == "HUMAN" for entry in plan.entries)
    approve_plan(workspace, plan.plan_id)
    report = preflight_plan(workspace, plan.plan_id)
    assert report.ok
    assert apply_plan(workspace, report, dry_run=True).state == "DRY_RUN"
    applied = apply_plan(workspace, report, dry_run=False)
    assert applied.state == "DONE"
    for entry in plan.entries:
        assert (
            hashlib.sha256(Path(entry.target_path).read_bytes()).hexdigest()
            == entry.expected_sha256
        )
    _run(spt, "doctor", "--workspace", str(workspace_path))
    assert rollback_transaction(workspace, applied.transaction_id).state == "ROLLED_BACK"
    assert _snapshot(source) == before
    assert _snapshot(output) == {}

    second_scan = _run(
        spt,
        "scan",
        str(source),
        "--workspace",
        str(workspace_path),
        "--full-hash",
    )
    second_preprocess = _run(spt, "preprocess", "--workspace", str(workspace_path))
    _run(spt, "group", "--workspace", str(workspace_path))
    second_analysis = _run(spt, "analyze", "--workspace", str(workspace_path), "--provider", "fake")
    second_plan = build_plan(workspace, PlannerOptions(output_root=output))

    assert "indexed=2" in second_scan
    assert "cache_hits=2" in second_preprocess
    assert "requests=0" in second_analysis
    assert second_plan.plan_id == plan.plan_id
    assert second_plan.canonical_json() == plan.canonical_json()
    assert _snapshot(output) == {}
    print("INSTALLED_SYNTHETIC_E2E_IDEMPOTENT PASS")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
