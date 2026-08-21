# Phase F GREEN evidence

Recorded on 2026-08-21 on Windows with Python 3.13.2.

All Phase F tests use generated media bytes, Pillow-created images, temporary directories, and
temporary SQLite workspaces. No real photo library, credential, network service, copy, move, rename,
delete, or output-root creation by planner/preflight code is used.

## RED to GREEN

The preserved RED run is documented in `phase-f-red.md`:

```text
rtk python -m pytest -q tests/test_phase_f_planner.py
17 failed in 1.62s
```

The independent Gate remediation was also executed as behavioral RED before its implementation:

```text
rtk python -m pytest -q tests/test_phase_f_planner.py -k remediation
9 failed, 32 deselected in 1.06s
```

The same focused remediation selection then passed:

```text
rtk python -m pytest -q tests/test_phase_f_planner.py -k remediation
9 passed, 32 deselected in 0.83s
```

After the implementation, remediation, and safety-path additions:

```text
rtk python -m pytest -q tests/test_phase_f_planner.py tests/test_phase_f_e2e.py
43 passed in 3.39s
```

## Phase F behavior matrix

| Contract | Result | Evidence |
|---|---|---|
| T-F-001 deterministic canonical plan | PASS | Repeated build returns the same ID, payload digest, creation record, entries, and canonical JSON |
| T-F-002 ordering independent | PASS | Canonical entries are explicitly ordered; reverse-name seed order cannot drift target assignment |
| T-F-003 reserved Windows names | PASS | CON, PRN, AUX, NUL, COM1-9, and LPT1-9 stems are protected, including stems followed by extensions |
| T-F-004 illegal/control characters | PASS | Windows illegal and C0 control characters are replaced deterministically |
| T-F-005 trailing dot/space | PASS | Every target leaf is checked for a Windows-safe ending |
| T-F-006 same second/description | PASS | Stable per-media or bundle identity tokens prevent collision |
| T-F-007 case-insensitive collision | PASS | Target uniqueness uses NFC plus case-folded absolute keys |
| T-F-008 Unicode normalization collision | PASS | NFC-equivalent descriptions still receive unique stable target keys |
| T-F-009 unreliable capture time | PASS | Filesystem mtime routes to `_时间待确认` |
| T-F-010 bundle consistency | PASS | HEIC, MOV, and AAE retain one directory, basename, bundle ID, and roles |
| T-F-011 stale source | PASS | Changed source bytes fail with `STALE_SOURCE` |
| T-F-012 missing source | PASS | Missing source before or after the stable read fails as an item-level `SOURCE_MISSING` |
| T-F-013 output inside source | PASS | Plan build rejects source/output overlap |
| T-F-014 source inside output | PASS | Plan build rejects reverse overlap |
| T-F-015 unsafe/unwritable output | PASS | File output roots and injected permission/capacity failures block preflight |
| T-F-016 incomplete transaction | PASS | Non-terminal operation transaction blocks preflight |

Additional GREEN tests prove:

- plans and entries are immutable through SQLite triggers;
- v1 through v10 migration statement byte hashes are unchanged, and the published v11 statement
  digest remains `e184fa3c655777c5bc25aa5220a3fdeafb83515d993012bab95ae6a9edc2714b`;
- populated v10 upgrades to v11 without losing prior media and repeated migration is idempotent;
- missing or same-name/weakened immutable triggers are rejected by exact normalized SQL semantics;
- plan entries contain rollback-ready source path, target path, size, SHA-256, mtime, action,
  decision source, bundle ID/role, category, and disposition;
- a plan is initially `PENDING`; separate approval/revocation is idempotent and revisioned;
- approval validates the complete canonical plan inside the same `BEGIN IMMEDIATE` transaction as
  its state update; corrupt payloads leave `PENDING` and its revision unchanged, while revoke remains
  available for safe deauthorization;
- preflight returns a plan-payload and approval-revision-bound contract; revocation makes that
  contract stale, and a future executor must recheck it under its mutation lock;
- unsupported planner/plan schema versions are rejected at build and inspect boundaries;
- inspect and preflight strictly open an existing workspace and do not initialize or mutate an
  absent or mistyped path;
- different-content targets fail `TARGET_CONFLICT`; same-hash targets report `ALREADY_PRESENT`;
- bundle membership drift and pre-existing ambiguity are reported;
- apply-lock presence, insufficient capacity, unknown capacity, workspace/output permission probes,
  target directory conflicts, and unreadable sources are reported without partial execution;
- eight concurrent identical builds converge on one immutable plan and one entry set;
- deterministic fault injection after entry insert rolls back all plan rows and leaves sources
  unchanged;
- the planner never emits DELETE and routes `REJECT_CANDIDATE` only to `_待审废片`.
- intermediate target-parent reparse points and regular-file obstructions fail without escaping the
  declared output root.

## Planner/preflight coverage

```text
rtk python -m pytest --cov=smart_photo_triage.planner --cov-branch \
  --cov-report=term -q tests/test_phase_f_planner.py tests/test_phase_f_e2e.py
43 passed in 3.99s
planner.py: 92.49% combined line and branch coverage
```

This exceeds the TDD planner/preflight safety-core target of 90%. The repository-wide 85% threshold
was not lowered.

## Synthetic E2E

```text
rtk python -m pytest -q tests/test_phase_f_e2e.py
1 passed in 0.53s
```

The E2E creates three Pillow PNGs and runs the real local pipeline:

```text
init -> full-hash scan -> preprocess -> group -> Fake AI -> HUMAN override
-> plan build twice -> pending preflight -> approve -> approved preflight
```

It proves one exact-duplicate group, preview cache sharing, Fake AI completion, HUMAN decision
snapshotting, deterministic two-build plan identity, approval gating, byte-for-byte unchanged source,
and an absent output root after preflight.

## Regressions and final gates

The first complete regression exposed two old version assumptions, not product failures. Phase C
expected no operation-named schema table, while v11 now legitimately provides an empty journal data
contract. Phase E used `MIGRATIONS[:-1]` as an implicit synonym for v9. The tests were corrected to
assert zero operation rows and explicit migration version bounds. Both targeted regressions passed:

```text
rtk python -m pytest -q \
  tests/test_phase_c_grouping.py::test_t_c_010_duplicate_grouping_never_moves_or_deletes_source \
  tests/test_phase_e_review.py::test_v9_upgrades_additively_to_v10_and_preserves_ai
2 passed in 0.36s
```

The independent Gate remediation and its affected stage regressions passed:

```text
rtk python -m pytest -q \
  tests/test_phase_a_database.py tests/test_phase_a_workspace.py \
  tests/test_phase_c_grouping.py tests/test_phase_e_review.py \
  tests/test_phase_f_planner.py tests/test_phase_f_e2e.py
119 passed in 14.94s
```

The coverage-discovered Phase C owner-publication race was fixed from the deterministic RED recorded
in `phase-f-red.md`:

```text
rtk python -m pytest -q tests/test_phase_c_ninth_review.py -k metadata_publication
1 passed, 4 deselected in 0.48s

rtk python -m pytest -q \
  tests/test_phase_c_eighth_review.py tests/test_phase_c_ninth_review.py \
  tests/test_phase_c_tenth_review.py
22 passed in 2.75s
```

Targeted concurrency stress command:

```text
rtk pwsh -NoProfile -Command \
  '$ErrorActionPreference = "Stop"; 1..20 | ForEach-Object {
    python -m pytest -q tests/test_phase_c_ninth_review.py \
      -k "metadata_publication or healthy_writer";
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
  }'
20/20 invocations passed; every invocation reported 2 passed, 3 deselected.
```

The final approval-integrity RED was then closed without changing revoke behavior:

```text
rtk python -m pytest -q tests/test_phase_f_planner.py -k approve_rejects_corrupted
1 passed, 41 deselected in 0.28s

rtk python -m pytest -q \
  tests/test_phase_a_database.py tests/test_phase_a_workspace.py \
  tests/test_phase_c_grouping.py tests/test_phase_c_eighth_review.py \
  tests/test_phase_c_ninth_review.py tests/test_phase_c_tenth_review.py \
  tests/test_phase_e_review.py tests/test_phase_f_planner.py tests/test_phase_f_e2e.py
142 passed in 17.55s
```

Final complete suite:

```text
rtk python -m pytest -q
330 passed, 7 skipped in 127.85s
```

Final repository coverage:

```text
rtk python -m pytest --cov=smart_photo_triage --cov-branch --cov-report=term-missing -q
330 passed, 7 skipped in 168.45s
87.52% combined line and branch coverage
planner.py: 92%
```

The seven skips are retained Phase C platform/capability contracts. No Phase F test is skipped.

Final static checks:

```text
rtk python -m ruff check .
All checks passed!

rtk python -m ruff format --check .
61 files already formatted
```

The first post-remediation coverage run exposed a pre-existing Phase C publication-lock metadata
race rather than a Phase F failure. A deterministic fixture paused a writer after exclusive lock
creation and before owner JSON publication. It failed before the fix, then passed after adding a
250 ms, caller-budget-bounded grace only for the same zero-byte lock inode. Non-empty malformed
metadata remains immediately deferred and is never reclaimed. Lock regressions were 22 passed, and
20 consecutive stress invocations of both healthy-writer concurrency cases passed before the fresh
full coverage run above.

## Limitations and boundary

- Phase F creates only immutable plan, approval, and journal-schema state inside the workspace. It
  does not create operation transactions or implement Apply, copy, move, resume, doctor, or rollback.
- Unbound sidecars are warned and omitted rather than heuristically attached.
- Writability and capacity results are point-in-time preflight evidence. Phase G must recheck them
  under its exclusive apply lock before any mutation.
- Hosted Windows/Linux CI was not run because this unpushed repository has no remote run. Local
  verification is Windows Python 3.13.2.
