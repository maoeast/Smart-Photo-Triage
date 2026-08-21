# Phase B GREEN evidence

Recorded on 2026-08-21 on Windows with Python 3.13.2.

## Targeted functional suite

```powershell
rtk python -m pytest -q tests/test_phase_b_scan.py -k "not 10k"
```

```text
25 passed, 1 deselected in 1.52s
```

This includes T-B-001 through T-B-013 plus safe reverse-overlap rejection, CLI operation, schema
migration idempotency, in-place v1-through-v4 identity preservation, EXIF fallback branches, QuickTime
version 1, all three sidecar extensions, changed-file hash invalidation, glob exclusion, and
injected per-item metadata failure isolation.

## Phase A regression

```powershell
rtk python -m pytest -q tests/test_phase_a_database.py tests/test_phase_a_workspace.py tests/test_phase_a_config.py tests/test_phase_a_cli.py
```

```text
28 passed in 2.31s
```

The three Phase A assertions that hard-coded schema version 1 now derive the latest migration
version. Their original atomic rollback, ledger-drift, commit-retry, and concurrent-init behaviors
remain unchanged.

## Independent-review remediation

```powershell
rtk python -m pytest -q tests/test_phase_b_review_remediation.py
```

```text
15 passed in 1.27s
```

This proves incomplete walk/stat scope never publishes missing markers, newly protected paths keep
their historical presence, post-stat disappearance is item-local, critical base and resume-state
UNIQUE/FK/index constraints are validated, `RUNNING` is externally visible, committed batches survive interruption
or failure, matching resumes skip completed metadata and hashes, transient metadata errors retry,
CLI errors are stable and pre-creation, and parent/child roots are rejected both ways.

## Third independent-review remediation

```powershell
rtk python -m pytest -q tests/test_phase_b_third_remediation.py tests/test_phase_b_review_remediation.py tests/test_phase_b_scan.py -k "not 10k"
```

```text
47 passed, 1 deselected in 3.59s
```

This proves a skipped directory records an explicit protected prefix, a post-metadata disappearance
cannot be stored present, partial UNIQUE indexes cannot satisfy the scanner upsert contract, and
same-source plus parent/child registrations are serialized under one `BEGIN IMMEDIATE`. It also
proves the published v2 schema upgrades through an additive v3 migration while preserving workspace
UUID and media data, and SQLite operational errors have stable CLI handling while KeyboardInterrupt
still propagates.

## Crash-owner recovery remediation

```powershell
rtk python -m pytest -q tests/test_phase_b_crash_recovery.py
```

```text
4 passed in 0.22s
```

This proves an injected live owner rejects a second scan without writing another run, while an
owner PID proven absent is atomically changed from `RUNNING` to `INTERRUPTED`. Its committed
counters remain, its error and terminal reason are audited, and the replacement run records it in
`resume_of`. A real v3 database fixture upgrades through additive v4 statements without changing
the workspace UUID or existing run data. On Windows, a real spawned child is detected alive while
running and dead after `terminate()` plus `wait()`, even while the `Popen` process handle remains
open.

## Full regression and coverage

```powershell
rtk python -m pytest -q
```

```text
80 passed in 20.61s
```

```powershell
rtk python -m pytest --cov=smart_photo_triage --cov-report=term-missing -q
```

```text
80 passed in 27.88s
90.00% combined line and branch coverage
```

## 10k streaming smoke

```powershell
rtk python -m pytest -q -s tests/test_phase_b_scan.py -k 10k
```

```text
PHASE_B_10K_SMOKE items=10000 elapsed=8.726s items_per_second=1145.9 peak_mib=0.04
1 passed, 25 deselected in 14.58s
```

The fixture consists only of 10,000 generated JSON sidecars under `tmp_path`. The scan inserts one
media instance and one SINGLE bundle per item from streaming iterators and an ordered SQLite cursor.
The reported memory value is Python allocation peak from `tracemalloc`; it excludes SQLite's native
page cache and is not total process RSS.

## Static checks

```powershell
rtk python -m ruff format --check .
rtk python -m ruff check .
```

```text
25 files already formatted
All checks passed!
```
