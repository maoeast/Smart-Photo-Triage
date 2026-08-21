# Phase F RED evidence

Date: 2026-08-21 (Asia/Singapore)

Scope: Phase F planner and preflight only. The tests use temporary workspaces and programmatically
written synthetic byte fixtures. No real photo library, credential, network service, or source-file
mutation by product code was used.

## Baseline before Phase F tests

```text
rtk python -m pytest -q
286 passed, 7 skipped in 121.96s

rtk python -m ruff check .
All checks passed!

rtk python -m ruff format --check .
55 files already formatted
```

## Meaningful RED

Command:

```text
rtk python -m pytest -q tests/test_phase_f_planner.py
```

Result:

```text
17 failed in 1.62s
```

The failures demonstrate missing Phase F behavior rather than syntax errors or broken test imports:

- the planner API and immutable deterministic plan builder do not exist;
- schema migration remains v10 instead of the required additive v11 planner contract;
- `spt plan` is not a recognized CLI command;
- approval, inspect, and preflight entry points do not exist;
- therefore filename safety, bundle naming, unreliable-time routing, stale/missing-source checks,
  overlap policy, output conflict checks, incomplete-transaction blocking, concurrency convergence,
  and deterministic injected-fault rollback are not implemented.

Representative failure text:

```text
AssertionError: Phase F planner API build_plan is unavailable
assert MIGRATIONS[-1].version == 11
E assert 10 == 11
spt: error: argument command: invalid choice: 'plan'
```

The tests deliberately access the future API through the already-importable CLI module, so pytest
collects and executes all behavior tests. No missing-module collection failure is used as RED.

## Independent Gate remediation RED

Command executed before remediation implementation:

```text
rtk python -m pytest -q tests/test_phase_f_planner.py -k remediation
```

Actual result:

```text
FFFFFFFFF                                                                [100%]
9 failed, 32 deselected in 1.06s
```

Observed failures:

```text
AssertionError: assert junction_report.ok is False
AssertionError: assert relative.parts[0].startswith("_")  # CON.txt
AssertionError: assert relative.parts[0].startswith("_")  # COM1.foo
Failed: DID NOT RAISE MigrationError matching "trigger.*semantics"
FileNotFoundError from planner.py final path.stat after the safe read
Failed: DID NOT RAISE PlanPolicyError matching "schema version"
AttributeError: 'PreflightReport' object has no attribute 'approval_revision'
AssertionError: assert not missing.exists()  # plan inspect
AssertionError: assert not missing.exists()  # plan preflight
```

This is meaningful behavioral RED. All tests collected and executed against valid synthetic
workspaces. It proves the confirmed intermediate target-parent, Windows device-stem, trigger
semantics, disappearing-source, schema-version, approval-contract, and read-only CLI workspace
gaps. The junction fixture was a real Windows directory junction created under pytest's temporary
root. No real media library or external target was touched.

## Coverage-discovered publication-lock remediation RED

The first fresh whole-suite coverage run after the Phase F remediation exposed an existing Phase C
race: a contender can observe the exclusive publication-lock file after creation but before the
owner JSON has been written. The operational path treated that transient empty file as permanently
malformed and deferred a healthy contender. A deterministic synthetic concurrency fixture was
added before changing the implementation.

Command:

```text
rtk python -m pytest -q tests/test_phase_c_ninth_review.py -k metadata_publication
```

Actual result:

```text
F                                                                        [100%]
FAILED tests/test_phase_c_ninth_review.py::test_contender_waits_for_new_lock_owner_metadata_publication
1 failed, 4 deselected in 0.45s
```

Representative failure:

```text
E assert second.is_alive()
WARNING smart_photo_triage.preprocess: preprocess deferred media_id=1
reason=publication lock owner metadata is unknown or malformed
```

The fixture pauses the first synthetic writer after its exclusive lock-file creation and before its
owner payload write. It proves the precise publication window without timing luck or real media.

## Final approval-integrity remediation RED

Command executed before changing `approve_plan`:

```text
rtk python -m pytest -q tests/test_phase_f_planner.py -k approve_rejects_corrupted
```

Actual result:

```text
F                                                                        [100%]
FAILED tests/test_phase_f_planner.py::test_approve_rejects_corrupted_canonical_plan_without_state_change
1 failed, 41 deselected in 0.64s
```

Representative failure:

```text
E Failed: DID NOT RAISE <class 'smart_photo_triage.planner.PlanStateError'>
```

The synthetic fixture temporarily removes the valid no-update trigger, corrupts only the stored
canonical document, restores the exact valid trigger SQL, and proves `validate_database` passes.
The failure therefore isolates approval behavior: the pre-fix implementation changed a corrupted
plan from `PENDING` to `APPROVED` without validating its canonical payload.
