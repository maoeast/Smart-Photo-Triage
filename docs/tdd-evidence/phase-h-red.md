# Phase H RED evidence

Date: 2026-08-21
Platform: Windows, Python 3.13
Scope: local synthetic Phase H only.

The pre-existing Phase H test passed first:

```text
rtk python -m pytest -q tests/test_phase_h_release.py
1 passed in 5.21s
```

The first expanded two-pass assertion was deliberately run before correction:

```text
rtk python -m pytest -q -x tests/test_phase_h_release.py
1 failed in 6.45s
AssertionError: second_scan.indexed_count == 0; actual indexed_count=31, unchanged_count=31
```

This was a test-modeling error, not a product defect. `indexed_count` is the scan's total indexed
media count; `unchanged_count` is the incremental no-change signal. The corrected contract asserts
the stable total and that all 31 items are unchanged on the second scan.

The next correction run exposed one further test-modeling error:

```text
rtk python -m pytest -q -x tests/test_phase_h_release.py
1 failed in 7.39s
AssertionError: output.exists() is false; rollback retained an empty output root
```

Rollback's contract is no owned target remains, not removal of the caller-selected output directory.
The corrected assertion checks the output snapshot is empty.

No real media, network, credentials, external provider, commit, or push was used.

## Focused coverage RED

The initial final-local planner/preflight coverage gate was red:

```text
coverage --branch, tests/test_phase_f_planner.py + tests/test_phase_g_executor.py
planner statements: 509/542 = 93.911%
planner branches: 144/164 = 87.805%  (required >=90%)
```

The missing reachable outcomes were fallback component sanitization, unreliable capture metadata,
pending-approval invalidation, and a regular file selected as output root. They became one small
synthetic behavior test, without exclusions or product changes.
