# Phase A RED evidence

## Windows lock-removal contention RED and GREEN discovered during Phase D

On 2026-08-21 the barrier concurrency test failed in a coverage run, passed once in isolation, then
reproduced on invocation 13 of a 20-invocation pressure run. `Path.mkdir()` raised WinError 5 while
the prior owner was removing `.spt-init-lock`. This was retained as a real RED. The bounded
lock-acquisition loop now retries both existing-directory and Windows access-denied contention until
the original deadline. Twenty consecutive invocations of the five-round, 12-worker barrier test
then passed; a fresh full coverage run passed 238 tests, skipped 7, and reached 85.16%.

An ownership follow-up added three deterministic RED failures: the lock API lacked injected timing
for `FileExistsError -> PermissionError -> success`, persistent permission denial was not preserved,
and an old owner attempted path-based cleanup after its lock directory had been replaced.

## Partial owner-marker write remediation

The next independent review identified a narrower acquisition failure. If creation of the unique
owner file succeeded but its stream raised after writing four bytes, acquisition cleanup had no
owner path and left both the partial owner file and `.spt-init-lock` behind. A real file-backed
faulting stream also proved the same residual-lock outcome when close raised.

Meaningful RED, run before the fix:

```text
rtk python -m pytest -q tests/test_phase_a_workspace.py -k partial_owner
2 failed, 12 deselected
```

The fix determines the token-derived owner path before opening it. Acquisition-failure cleanup may
remove that unique partial marker only while the lock directory still has the identity captured by
this owner. Normal cleanup continues to require the complete token content. Both injected errors
remain the original `OSError`; cleanup does not replace them.

```text
Phase A targeted: 14 passed in 1.58s
Barrier pressure: 20/20 invocations passed
Full coverage: 244 passed, 7 skipped in 158.87s, 85.14%
```

Recorded on 2026-08-20 on Windows with Python 3.13.2.

Command:

```powershell
rtk python -m pytest -q
```

Observed result:

```text
8 failed in 0.48s
```

The suite collected successfully. Each failure reached an explicit Phase A contract stub and
reported that the behavior was not implemented:

- T-A-001 CLI help and the Windows Unicode/space path smoke;
- T-A-002 default and explicit configuration loading, including invalid input rejection;
- T-A-003 idempotent workspace initialization and preservation of existing state;
- T-A-004 versioned, idempotent SQLite migration.

This is meaningful RED evidence. It is not caused by a syntax error, missing test dependency, or
failed test collection.

## Independent-review remediation RED

Recorded on 2026-08-20 before the remediation implementation.

Command:

```powershell
rtk python -m pytest -q tests/test_phase_a_database.py tests/test_phase_a_workspace.py tests/test_phase_a_cli.py
```

Observed result:

```text
7 failed, 9 passed in 1.31s
```

The seven behavior failures demonstrated the review findings directly:

- failed migration left its `atomic_probe` DDL behind even though ledger and `user_version` stayed
  at version 1;
- a renamed migration ledger entry was accepted;
- a missing critical `workspace_metadata` table was accepted;
- an unrelated existing `spt.sqlite3` was modified instead of rejected;
- a recognizable pre-marker SPT workspace was not given an ownership marker;
- concurrent initialization raced and failed with `table workspace_metadata already exists`;
- redirected strict-ASCII output raised `UnicodeEncodeError` for a Unicode workspace path.

The suite collected and executed normally. The failures were observable contract violations, not
test collection, dependency, import, or syntax failures.

## Third remediation RED

Recorded on 2026-08-21 before the third remediation implementation.

Command:

```powershell
rtk python -m pytest -q tests/test_phase_a_database.py tests/test_phase_a_workspace.py
```

Observed result:

```text
4 failed, 13 passed in 1.75s
```

The failures established four observable defects:

- an injected `commit()` failure escaped the rollback region and left the real SQLite connection
  inside a transaction;
- a second initializer observed the ownership marker after file creation but before content write
  and rejected the empty marker;
- the marker was not structured and did not carry the database workspace identity;
- replacing one initialized workspace database with another valid SPT database was accepted because
  both markers contained the same public constant.

The same suite also included a 12-worker barrier stress over five independent workspaces. The
deterministic paused-writer test, rather than scheduling luck, supplied the marker atomicity RED.
The third review superseded the second review's markerless legacy-adoption expectation. Because
Phase A has not shipped and origin cannot be proven from public schema alone, the final contract
conservatively rejects every markerless existing database.

## Atomic-marker test self-check RED

Recorded on 2026-08-21 during the final Phase A test correction.

Command:

```powershell
rtk python -m pytest -q tests/test_phase_a_workspace.py::test_marker_is_never_observable_before_its_complete_content
```

Observed result:

```text
1 failed in 1.20s
AssertionError: marker writer pause hook did not intercept a write
```

The old hook matched a direct `open("x")` of final `.spt-workspace`, while the implementation writes
`..spt-workspace.<unique>.tmp` and publishes it with `os.replace`. The test therefore waited one
second and took its fallback path without pausing a writer. Adding an explicit hook-trigger assertion
turned that false-positive path into meaningful RED before correcting the matcher and timing.
