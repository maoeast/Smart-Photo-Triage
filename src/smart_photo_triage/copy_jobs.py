"""In-process, observable copy jobs for the local GUI.

The executor remains the single authority for mutation.  This module only starts it
on a worker thread and aggregates throttled read-only progress snapshots.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from smart_photo_triage.executor import ExecutionResult, apply_plan
from smart_photo_triage.planner import PlanEntry, PreflightReport, inspect_plan
from smart_photo_triage.workspace import Workspace


@dataclass
class CopyJob:
    workspace: Workspace
    report: PreflightReport

    def __post_init__(self) -> None:
        plan = inspect_plan(self.workspace, self.report.plan_id)
        self._total_files = len(plan.entries)
        self._total_bytes = sum(entry.expected_size for entry in plan.entries)
        self._done_files = 0
        self._failed_files = 0
        self._copied_bytes = 0
        self._current_path: str | None = None
        self._state = "QUEUED"
        self._result: ExecutionResult | None = None
        self._error: str | None = None
        self._started_at: float | None = None
        self._updated_at = time.monotonic()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="spt-copy-job", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _on_progress(self, event: str, entry: PlanEntry, byte_count: int) -> None:
        source_path = entry.source_path
        with self._lock:
            if event == "started":
                self._current_path = source_path
            elif event == "bytes":
                self._copied_bytes += byte_count
            elif event == "completed":
                self._done_files += 1
            elif event == "failed":
                self._failed_files += 1
            self._updated_at = time.monotonic()

    def _run(self) -> None:
        with self._lock:
            self._state = "RUNNING"
            self._started_at = time.monotonic()
        try:
            result = apply_plan(
                self.workspace, self.report, dry_run=False, progress_callback=self._on_progress
            )
            with self._lock:
                self._result = result
                self._state = "DONE" if result.state == "DONE" else "RECOVERABLE"
                self._failed_files = result.failed_count
                self._done_files = result.done_count + result.already_present_count
                self._current_path = None
                self._updated_at = time.monotonic()
        except Exception as error:  # noqa: BLE001
            with self._lock:
                self._state = "RECOVERABLE"
                self._error = type(error).__name__
                self._current_path = None
                self._updated_at = time.monotonic()

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            now = time.monotonic()
            elapsed = max(0.001, now - self._started_at) if self._started_at else 0.0
            speed = self._copied_bytes / elapsed if elapsed else 0.0
            remaining = max(0, self._total_bytes - self._copied_bytes)
            eta = int(remaining / speed) if speed > 0 else None
            return {
                "plan_id": self.report.plan_id,
                "state": self._state,
                "current_file": self._current_path,
                "completed_files": self._done_files,
                "total_files": self._total_files,
                "copied_bytes": self._copied_bytes,
                "total_bytes": self._total_bytes,
                "bytes_per_second": round(speed, 2),
                "remaining_seconds": eta,
                "failed_files": self._failed_files,
                "recoverable": self._state == "RECOVERABLE",
                "transaction_id": self._result.transaction_id if self._result else None,
                "error": self._error,
            }
