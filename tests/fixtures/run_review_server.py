"""Start a Phase E browser-smoke server over a synthetic local library.

Example:
    python tests/fixtures/run_review_server.py --root C:/Temp/spt-review-smoke

The root must be absent or empty. The script never reads a user photo library.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from PIL import Image

from smart_photo_triage.ai import FakeVisionProvider, analyze_workspace
from smart_photo_triage.grouping import group_workspace
from smart_photo_triage.preprocess import preprocess_workspace
from smart_photo_triage.review import ReviewStore, serve_review
from smart_photo_triage.scanner import scan_library
from smart_photo_triage.workspace import initialize_workspace


def build_fixture(root: Path) -> None:
    if root.exists() and any(root.iterdir()):
        raise ValueError("fixture root must be absent or empty")
    source = root / "synthetic-source"
    source.mkdir(parents=True, exist_ok=True)
    for index in range(24):
        path = source / f"20240101_1200{index:02d}_synthetic-{index:02d}.jpg"
        Image.new("RGB", (96, 64), ((index * 31) % 255, 70, 140)).save(path, quality=95)
    duplicate = source / "20240101_120059_synthetic-duplicate.jpg"
    shutil.copyfile(source / "20240101_120000_synthetic-00.jpg", duplicate)

    workspace = initialize_workspace(root / "workspace")
    scan_library(workspace, source)
    preprocess_workspace(workspace)
    group_workspace(workspace)
    analyze_workspace(workspace, provider=FakeVisionProvider())
    first = ReviewStore(workspace).list_items(page_size=1).items[0]
    ReviewStore(workspace).update_decision(
        int(first["id"]),
        category="01_家庭生活",
        disposition="KEEP",
        expected_revision=0,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    build_fixture(args.root)
    workspace = initialize_workspace(args.root / "workspace")
    serve_review(workspace, port=args.port, open_browser=False)


if __name__ == "__main__":
    main()
