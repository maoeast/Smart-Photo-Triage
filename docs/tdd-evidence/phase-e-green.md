# Phase E GREEN evidence

Recorded on 2026-08-21 on Windows with Python 3.13.2.

All Phase E tests use generated media state, temporary SQLite workspaces, and loopback-only HTTP.
No real photo library, credential, external host, or CDN is used.

## Review behavior and local HTTP integration

```powershell
rtk python -m pytest -q tests/test_phase_e_review.py
```

```text
42 passed in 9.69s
```

The expanded GREEN suite covers T-E-001 through T-E-009, latest-AI deterministic tie-breaking,
RULE fallback, immutable AI rows, v9 to v10 preservation, all PRD filters, duplicate and burst
membership, Best Shot, 1005-item page bounds, basename-only search, persisted repeated edits, stale revision 409, CSRF,
Origin, Host, content type/body/JSON allow-lists, digest changes, preview-root escape, missing preview
redaction, fixed-code database-failure responses, DOM text insertion, CLI injection, and clean
server shutdown, including browser-open failure. T-E-010 static export remains optional and
unimplemented.

Independent-review remediation additionally constrains duplicate and burst membership and marker
flags to the algorithm versions published by the grouping implementation. Strict persisted-row
validation converts malformed IDs, revisions, booleans, bounded numeric fields, and JSON into the
fixed `LOCAL_STATE_ERROR` contract. This applies to both list reads and decision POSTs without an
HTTP disconnect or traceback.

```powershell
rtk python -m pytest -q tests/test_phase_e_review.py -k "post_with_malformed_persisted_revision or old_algorithm or malformed_database"
```

```text
6 passed, 36 deselected in 3.31s
```

The initial full-coverage run found a real Gate failure despite green behavior:

```text
263 passed, 7 skipped in 171.37s
84.02% combined line and branch coverage
```

The coverage threshold was not lowered. Missing request/store branches were converted into
behavioral tests. The focused Phase E module subsequently reached 98% combined coverage.

Final remediation checks:

```powershell
rtk python -m pytest -q
rtk python -m coverage run --branch -m pytest -q
rtk python -m coverage report --fail-under=85
```

```text
286 passed, 7 skipped in 125.74s
286 passed, 7 skipped in 171.26s
87% combined line and branch coverage
review.py: 94%
```

The seven skips are retained Phase C platform/capability contracts. No Phase E test is skipped.

```powershell
rtk python -m ruff format --check .
rtk python -m ruff check .
```

```text
55 files already formatted
All checks passed
```
