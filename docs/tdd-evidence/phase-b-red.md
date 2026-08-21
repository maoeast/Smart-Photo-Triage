# Phase B RED evidence

Recorded on 2026-08-21 on Windows with Python 3.13.2.

The complete T-B-001 through T-B-013 suite, the minimal CLI path, the versioned-schema check, and
the 10k synthetic metadata smoke were written before the Phase B implementation. Small public
contract shells were added so collection and imports succeeded and every scan contract reached an
explicit `NotImplementedError` instead of failing through syntax, dependency, or import errors.

## Functional RED

Command:

```powershell
rtk python -m pytest -q tests/test_phase_b_scan.py -k "not 10k"
```

Observed result:

```text
16 failed, 1 deselected in 1.50s
```

The failures established the missing behaviors:

- T-B-001 through T-B-013 each reached the unimplemented scanner contract;
- the unsafe reverse-overlap layout check reached the same missing scanner behavior;
- `spt scan` was absent from the CLI parser and exited 2;
- the database remained at schema version 1 and lacked the Phase B tables.

The junction fixture was successfully created before T-B-004 reached the scanner contract. This
proved that the Windows reparse-point path was exercised rather than silently skipped.

## 10k streaming smoke RED

Command:

```powershell
rtk python -m pytest -q tests/test_phase_b_scan.py -k 10k
```

Observed result:

```text
1 failed, 16 deselected in 6.11s
```

The test created 10,000 synthetic JSON sidecars under `tmp_path`, started `tracemalloc`, and reached
the explicit scanner contract before any implementation existed. Its GREEN contract requires all
10,000 instances in SQLite with peak traced Python memory below 64 MiB and reports elapsed time,
throughput, and peak memory. No real photo library or hard-coded user media directory is used.

## Independent-review remediation RED

Recorded on 2026-08-21 before the Phase B independent-review fixes.

Command:

```powershell
rtk python -m pytest -q tests/test_phase_b_review_remediation.py
```

Observed result:

```text
14 failed, 1 passed in 1.08s
```

The failures were distinct observable contract violations:

- an incomplete directory walk marked two still-present historical items absent;
- a per-path stat denial marked that historical item absent;
- newly protected output/glob prefixes marked two historical items absent;
- a file disappearing after initial stat caused fallback stat to abort and roll back the run;
- missing `media_item.path_key` uniqueness, missing warning foreign keys, and a missing scan index
  were all accepted by schema validation;
- `scan_run` was invisible to another connection during item work because the whole run was one
  transaction;
- KeyboardInterrupt and terminal failure removed the run row and every already-processed item;
- unchanged items with transient metadata errors were not retried;
- the CLI created a workspace before discovering a missing source and then leaked an exception;
- parent/child source overlap was not rejected and instead reached a bundle uniqueness failure.

The one passing test proved that rescanning one of two disjoint source roots did not mark the other
root absent. This retained regression stays in the remediation suite. All fixtures use `tmp_path`
and synthetic JSON or injected failures.

## Third independent-review remediation RED

Recorded on 2026-08-21 before changing the Phase B implementation.

Command:

```powershell
rtk python -m pytest -q tests/test_phase_b_third_remediation.py
```

Observed result:

```text
7 failed in 0.70s
```

The seven failures established separate observable defects:

- excluding the directory name `hidden` marked an existing historical descendant absent;
- a file deleted after its initial stat was persisted with `source_present = 1`;
- the schema validator did not reject a partial unique index as unsuitable for the scanner's
  unqualified `ON CONFLICT(path_key)` contract;
- a concurrent scan of the same source was accepted instead of being rejected as active;
- concurrent parent/child source registration passed the overlap check in both threads and later
  collided on bundle uniqueness;
- the published v2 migration already contained resume columns, so a real old-v2 fixture could not
  demonstrate a v2-to-v3 upgrade;
- the CLI leaked `sqlite3.OperationalError` instead of returning stable exit code 2.

All fixtures use `tmp_path`, synthetic JSON sidecars, and injected synchronization or failures.

## Crash-owner recovery RED

Recorded on 2026-08-21 before adding schema v4 or owner-aware registration.

Command:

```powershell
rtk python -m pytest -q tests/test_phase_b_crash_recovery.py
```

Observed result:

```text
3 failed in 0.25s
```

The failures proved that `scan_run` had no owner PID/token audit fields, a dead owner could not be
distinguished from a live owner, and the immutable migration ledger ended at v3. Therefore a
process killed without Python exception handling could leave `RUNNING` permanently blocking its
source. The final contracts require conservative live-owner rejection, explicit dead-PID recovery
with preserved counters and `resume_of`, and a data-preserving v3-to-v4 migration.

## Windows retained-handle liveness RED

Recorded on 2026-08-21 before correcting the Windows process query.

Command:

```powershell
rtk python -m pytest -q tests/test_phase_b_crash_recovery.py::test_windows_process_alive_checks_exit_code_while_popen_handle_remains_open
```

Observed result:

```text
1 failed in 0.18s
```

The real child process was reported alive while running. After `terminate()` and `wait()`, the
`Popen` object deliberately remained in scope with its process handle open, and the same PID was
incorrectly reported alive. `OpenProcess` success alone therefore cannot prove `STILL_ACTIVE`.
