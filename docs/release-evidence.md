# Release evidence

Date: 2026-08-21
Verdict: **NOT_READY**

All executed lifecycle work used temporary directories, generated image bytes, the offline `FakeVisionProvider`, and no real photo library, credential, network, commit, or push.

## 11.1 Build Summary

| Field | Value |
| --- | --- |
| Implementation | `smart-photo-triage` 0.1.0 |
| Python / platform | Python 3.13.2, Windows 11 |
| Runtime dependency | `Pillow>=10.4,<13` (observed 11.2.1 in installed-run base) |
| Development tools | pytest, coverage, Ruff |
| External tools | None. Fake video backend and Fake AI only. |

## 11.2 Test Summary

| Measure | Result | Evidence command |
| --- | --- | --- |
| Latest non-external full suite | 483 passed, 7 skipped, 220.95s | `rtk python -m pytest -q` |
| Latest overall coverage | 90.09%, 483 passed, 7 skipped, 263.39s | `rtk python -m pytest -q --cov=smart_photo_triage --cov-branch --cov-report=term` |
| Planner/preflight focused coverage | 513/542 lines (94.649%); 148/164 branches (90.244%) | focused run in `docs/tdd-evidence/phase-h-green.md` |
| Executor focused coverage | 1386/1431 lines (96.855%); 461/484 branches (95.248%) | `rtk python -m coverage run --branch --source=smart_photo_triage.executor -m pytest -q tests/test_phase_g_executor.py` |
| Current installed-package synthetic E2E | PASS in a fresh temporary inherited-runtime venv. `Pillow=11.2.1`. | `python -m venv --system-site-packages`; `pip install --no-index --no-build-isolation --no-cache-dir .`; `python tests/fixtures/phase_h_installed_e2e.py <temp-root> <venv>/Scripts/spt.exe` |
| Current Phase H focused sanity | 1 passed, 1 deselected, 4.28s | `rtk python -m pytest -q --basetemp .phase-h-pytest-temp-focused -p no:cacheprovider tests/test_phase_h_release.py -k isolated` |
| Current Ruff | PASS. 72 files already formatted; all checks passed. | `rtk python -m ruff format --check .`; `rtk python -m ruff check .` |
| External AI | **NOT RUN - no credentials** | No provider was selected and no network was used. |
| Windows hosted CI | NOT RUN | CI is prepared to build sdist/wheel and run the built-wheel synthetic E2E. No remote run was authorized. |
| Linux hosted CI | NOT RUN | CI is prepared to build sdist/wheel and run the built-wheel synthetic E2E. No remote run was authorized. |

The full-suite and coverage rows are retained historical local evidence. The installed-package E2E was rerun for this release-evidence closure. CI is prepared to build and upload distributions and to run the built-wheel E2E on both hosted platforms, but hosted Windows and Linux CI remain the release blocker until their results are recorded.

## 11.3 P0 Safety Matrix

| Test ID | Risk | Result | Evidence command |
| --- | --- | --- | --- |
| T-G-001 | dry-run mutates source, output, or journal | PASS | `rtk python -m pytest -q tests/test_phase_g_executor.py -k t_g_001` |
| T-G-002/T-G-003 | overwrite or claim an unowned target | PASS | `rtk python -m pytest -q tests/test_phase_g_executor.py -k "t_g_002 or t_g_003"` |
| T-F stale plan | stale source or approval is applied | PASS | `rtk python -m pytest -q tests/test_phase_f_planner.py -k stale` |
| T-G COPY verify | copied target hash is not verified | PASS | installed E2E `spt apply --execute`, plus `rtk python -m pytest -q tests/test_phase_g_executor.py -k copy` |
| T-G MOVE verify | source deleted before verified target | PASS | `rtk python -m pytest -q tests/test_phase_g_executor.py -k move` |
| T-G-004..011 | crash boundary cannot resume safely | PASS | `rtk python -m pytest -q tests/test_phase_g_executor.py -k "t_g_004_to_006_and_009 or t_g_007_to_011"` |
| T-G-012 | partial bundle is treated as complete | PASS | `rtk python -m pytest -q tests/test_phase_g_executor.py -k t_g_012` |
| T-G-013/T-G-014 | concurrent apply lock is unsafe | PASS | `rtk python -m pytest -q tests/test_phase_g_executor.py -k "t_g_013_and_014"` |
| T-G-015 | rerun creates side effects or name drift | PASS | `rtk python -m pytest -q tests/test_phase_g_executor.py -k t_g_015` |
| rollback modified target | rollback deletes changed or unowned output | PASS | `rtk python -m pytest -q tests/test_phase_g_executor.py -k rollback` |
| T-F HUMAN > AI | AI overrides a human decision | PASS | installed API `ReviewStore.update_decision`, then `spt plan build`; `rtk python -m pytest -q tests/test_phase_f_e2e.py` |
| AI delete guard | AI can authorize destructive disposition | PASS | `rtk python -m pytest -q tests/test_phase_d_ai.py -k delete` |
| cloud disabled | cloud access happens by default | PASS | `rtk python -m pytest -q tests/test_phase_d_ai.py -k cloud` |
| cache invalidation | stale versioned AI/preview result is reused | PASS | `rtk python -m pytest -q tests/test_phase_d_ai.py tests/test_phase_c_preview.py -k cache` |
| false-chain burst | transitive false burst grouping | PASS | `rtk python -m pytest -q tests/test_phase_h_release.py -k isolated_idempotent` |
| deterministic plan | equivalent inputs produce different plans | PASS | `rtk python -m pytest -q tests/test_phase_h_release.py -k isolated_idempotent` |
| Windows name collision | unsafe/reserved target name | PASS | `rtk python -m pytest -q tests/test_phase_f_planner.py -k windows` |

## 11.4 E2E, idempotency, recovery, and rollback

### Initial installed-package E2E

Fresh temporary workspace and source. The installed `spt` completed `init`, `scan --full-hash`, `preprocess`, `group`, and `analyze --provider fake`. The installed API then injected a HUMAN decision, built/approved/preflighted the immutable plan, ran dry-run and real COPY/apply with SHA-256 target assertions, ran `spt doctor`, and rolled back. The installed fixture is `tests/fixtures/phase_h_installed_e2e.py`.

```text
Scan complete: indexed=2 missing=0 errors=0 warnings=0 bundles=2
Preprocess complete: processed=1 cache_hits=1 failed=0 deferred=0
Grouping complete: duplicates=1 bursts=1 comparisons=5 warnings=0
INSTALLED_PILLOW=11.2.1
Analysis complete: analyzed=2 cache_hits=0 failed=0 requests=1
HUMAN_OVERRIDE=PASS
{"diagnoses": [], "lock_status": "NONE", "ok": true}
```

### Second E2E and idempotency

The installed fixture rescanned, preprocessed, grouped, analyzed, and planned after rollback. It asserts two indexed items, preprocessing `cache_hits=2`, `requests=0`, the same canonical plan and plan ID, and no remaining output files. The exact transcript ended `INSTALLED_SYNTHETIC_E2E_IDEMPOTENT PASS`, `INHERITED_INSTALLED_E2E=PASS`, and `INHERITED_E2E_TEMP_ROOT_REMOVED=PASS`.

### Crash recovery and rollback

Fault-boundary and recovery coverage is the Phase G executor suite, including T-G-004 through T-G-012. The installed E2E additionally performed real COPY, `doctor` with `ok: true`, and CLI rollback of both copied files. The 1k smoke stays read-only through dry-run by ADR 0008.

### Clean-install qualification

The strict fully isolated venv attempt failed before installation because the offline environment has no local `setuptools` or Pillow wheel. It was not misreported as a pass. The successful installed E2E therefore used a newly created `--system-site-packages` venv, inherited only the already-installed declared Pillow runtime, and ran `pip install --no-index --no-build-isolation --no-cache-dir .`. This proves the installed CLI/API path, but does **not** replace a fully isolated dependency-resolution check. It remains a limitation until an offline wheelhouse or authorized dependency access is supplied.

## 11.5 Performance Smoke

The recorded Phase H 1k generated-media full read-only lifecycle completed in 51.518s: scan 775.8 items/s, preview 41.6 items/s, peak RSS 69.855 MiB, SQLite 6.77 MiB, 1000 second-pass preview cache hits, 1 duplicate group, 1 burst group, 0 failures, 12,986 comparisons, and 10 Fake AI requests. Command: `rtk python -m pytest -q -s tests/test_phase_h_release.py`.

## 11.6 Known Limitations

- Hosted Windows and Linux CI are not run.
- A strict fresh venv `pip install .` attempted normal index resolution and failed with exit code 1: connection attempts to `/simple/setuptools/` were blocked with Windows error 10013, then pip reported no matching `setuptools>=68`. The temporary root was removed. The successful installed-package proof therefore inherits the declared Pillow runtime and is not a fully isolated dependency-resolution pass.
- No real user photo library or real video codec/backend was used.

## 11.7 External AI Status

NOT RUN - no credentials

## 11.8 Final Verdict

**NOT_READY**. Local synthetic operational evidence is green, but hosted Windows and Linux CI have not passed, external AI remains unrun, and strict fully isolated dependency resolution has not been proven. This file does not claim final acceptance.

## Appendix A. Dependency and license status

Only Pillow is declared at runtime and it is used by metadata/preprocess. The fresh installed E2E observed `Pillow=11.2.1`; its license expression is `MIT-CMU`. `pillow-heif` is optional and not part of the base install. No network service or large native runtime is declared by this project.
