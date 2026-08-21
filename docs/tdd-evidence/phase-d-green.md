# Phase D GREEN evidence

Recorded on 2026-08-21 on Windows with Python 3.13.2.

All Phase D fixtures use generated previews, temporary files and SQLite databases, Fake AI,
recording providers, or injected synthetic HTTP responses. No real photo library or live network
request was used.

## T-D-001 through T-D-018 and supporting contracts

```powershell
rtk python -m pytest -q tests/test_phase_d_ai.py
```

```text
29 passed in 1.60s
```

The original 25 contracts are GREEN. Four parameterized adapter cases additionally prove HTTP 429,
HTTP 503, and timeout classification as transient, plus HTTP 401 classification as permanent.
The Fake Provider completes a two-item run and a second all-cache-hit run through the same workspace
orchestrator. Privacy assertions inspect the serialized provider request. v8 upgrades additively to
v9 while preserving prior rows and workspace identity. The minimal Fake CLI writes validated advice.

## Migration regression

Phase C fixtures that previously hard-coded latest schema `8` now compare against
`MIGRATIONS[-1].version`, as earlier phase tests already do. Their Phase C data-preservation
assertions are unchanged.

```powershell
rtk python -m pytest -q tests/test_phase_d_ai.py tests/test_phase_c_sixth_review.py::test_populated_original_v6_upgrades_to_latest_preserving_preview tests/test_phase_c_tenth_review.py::test_populated_v7_upgrades_to_v8_with_deferred_count tests/test_phase_c_third_review.py::test_real_original_v5_upgrades_additively_to_latest_and_preserves_phase_c_data
```

```text
32 passed in 1.77s
```

## Full regression and coverage

```powershell
rtk python -m pytest --cov=smart_photo_triage --cov-report=term-missing -q
```

```text
195 passed, 7 skipped in 122.40s
85.02% combined line and branch coverage
```

The seven skips are retained platform/capability tests from Phase C. No Phase D safety contract is
skipped.

The final plain regression after formatting and documentation updates was:

```powershell
rtk python -m pytest -q
```

```text
195 passed, 7 skipped in 90.34s
```

## Static checks

```powershell
rtk python -m ruff format --check .
rtk python -m ruff check .
```

```text
48 files already formatted
All checks passed
```

## External AI status

```text
EXTERNAL_NOT_RUN
```

No credential was searched, inspected, printed, or assumed. The real Gemini adapter is covered only
with an injected synthetic HTTP transport. This does not block the Fake-AI Phase D Gate.

## Independent-review remediation GREEN

The 10 observed remediation failures are GREEN. Cache identity now covers every request and final
disposition input, with canonical quality ordering. Cloud permission is checked before estimate or
preview collection. Global permanent failures stop immediately, explicit item-local failures alone
split, and transient exhaustion cannot restart its retry budget. Batch, retry, and timeout inputs
have hard caps. Public failure values omit provider messages, secrets, and absolute paths.

```powershell
rtk python -m pytest -q tests/test_phase_d_ai.py tests/test_phase_d_review_remediation.py
```

```text
40 passed in 2.32s
```

The focused offline Fake two-pass smoke also passed, proving two analyzed items followed by two
cache hits and no provider call on the second pass.

```text
rtk python -m pytest -q tests/test_phase_d_ai.py::test_fake_provider_runs_complete_offline_workspace_flow
1 passed in 0.26s
```

The post-remediation full coverage run was:

```text
rtk python -m pytest --cov=smart_photo_triage --cov-report=term-missing -q
206 passed, 7 skipped in 122.69s
85.11% combined line and branch coverage
```

The final plain full regression after formatting and documentation updates was:

```text
rtk python -m pytest -q
206 passed, 7 skipped in 92.16s
```

Final static checks reported 49 files formatted and all checks passed. Ruff emitted a non-fatal
Windows cache-write warning during the format check; it did not affect source checking or results.

## Second independent-review remediation GREEN

All 12 executed RED contracts are GREEN. Literal-boolean cloud authorization rejects truthy
non-booleans before reads or calls. Estimate derives upload bytes from secure file metadata without
reading bodies. Analysis holds only metadata descriptors across the full set, securely validates
cache artifacts before reuse, and loads preview bytes one bounded batch at a time. Gemini performs a
bounded `MAX_RESPONSE_BYTES + 1` read. Integer controls and numeric timeouts are strict. Partial
success survives a later global permanent failure, whose audit counts every remaining item under a
stable redacted error code.

```text
rtk python -m pytest -q tests/test_phase_d_ai.py tests/test_phase_d_review_remediation.py
52 passed in 3.12s

rtk python -m pytest --cov=smart_photo_triage --cov-report=term-missing -q
218 passed, 7 skipped in 123.15s
85.17% combined line and branch coverage

rtk python -m pytest -q tests/test_phase_d_ai.py::test_fake_provider_runs_complete_offline_workspace_flow
1 passed in 0.26s

rtk python -m ruff format --check .
49 files already formatted

rtk python -m ruff check .
All checks passed

rtk python -m pytest -q
218 passed, 7 skipped in 91.66s
```

Migration v9 remains unchanged and no v10 was needed. All new fixtures are synthetic and temporary;
no credential or live external provider was used.

## Third independent-review remediation GREEN

Descriptor rows now flow through a paged cursor and `islice` batches without an all-pending list.
Fake stores no requests by default; explicit recording keeps only bounded redacted metadata. Each
original batch shares one retry/request budget across its complete split tree. Public failures use
only fixed enum codes and never exception names or messages.

```text
Focused remediation: 4 passed, 23 deselected in 13.71s
Phase D targeted: 56 passed in 18.97s
Full coverage: 222 passed, 7 skipped in 164.52s, 85.13%
Plain full: 222 passed, 7 skipped in 115.11s
Fake default and explicit-record paths: 2 passed in 0.40s
Ruff format: 49 files already formatted
Ruff check: All checks passed
```

## Seventh independent-review remediation GREEN

All AI database access now runs inside one outer normalization boundary. Only `sqlite3.Error` is
converted to fixed `DB_WRITE_ERROR`; best-effort rollback suppresses its own SQLite faults, and close
cannot replace an active `AnalysisError`, `KeyboardInterrupt`, or `SystemExit`.

```text
Focused remediation: 6 passed, 37 deselected in 0.57s
Phase D targeted: 72 passed in 19.59s
Coverage run: 237 passed, 7 skipped, 1 unrelated Phase A Windows lock failure, 85.16%
Isolated Phase A rerun: 1 passed in 0.72s
Final plain full: 238 passed, 7 skipped in 124.74s
Ruff format: 49 files already formatted
Ruff check: All checks passed
```

The first coverage-run failure was initially isolated as a Windows `PermissionError` in concurrent
workspace initialization. A requested 20-invocation pressure run reproduced it on invocation 13,
internal barrier round 5, so it was treated as a real RED rather than dismissed as flaky. Windows
can report access denied while the prior owner removes `.spt-init-lock`; the bounded acquisition
loop now treats that window as contention. After the fix all 20 pressure invocations passed, then a
fresh full coverage run passed 238 tests with 7 skips at 85.16%.

The follow-up init-lock remediation uses a unique owner marker plus directory identity for cleanup,
preserves unproven persistent `PermissionError`, cleans partial owner-write acquisition, and keeps the
original bounded wait. Phase A targeted passed 12 tests; the final full coverage run passed 242
tests with 7 skips at 85.20%.

A second, narrower init-lock remediation covers failures after the unique owner file has actually
been created. Real file-backed tests write four bytes before raising and separately raise from
close. Cleanup now knows the token-derived owner path before open, removes its own partial marker
only when the captured lock-directory identity still matches, and preserves the original error.
Phase A targeted passed 14 tests, the barrier test passed 20/20 pressure invocations, and the fresh
full coverage run passed 244 tests with 7 skips at 85.14%.

## Sixth independent-review remediation GREEN

Initial RUNNING INSERT/commit and normal terminal UPDATE/commit are now inside SQLite guards. Every
SQLite local-state failure exposed by the core API is normalized to fixed `DB_WRITE_ERROR`. Normal
terminal faults first make a best-effort conserving FAILED audit. Initial faults may leave no run;
an unwriteable terminal audit may leave RUNNING. Non-SQLite control-flow exceptions remain unchanged.

```text
Focused remediation: 4 passed, 33 deselected in 0.48s
Phase D targeted: 66 passed in 18.54s
Full coverage: 232 passed, 7 skipped in 159.48s, 85.05%
Plain full: 232 passed, 7 skipped in 115.45s
Ruff format: 49 files already formatted
Ruff check: All checks passed
```

## Fifth independent-review remediation GREEN

FAILED audits now use execution-observed cache/pending stats and enforce
`item_count = cache_hit_count + analyzed_count + failed_count`. Terminal audit UPDATE and commit are
both guarded; if terminal persistence itself is impossible, the transaction is rolled back and only
fixed `DB_WRITE_ERROR` is exposed. The explicit persistence boundary is that the prior `RUNNING`
row may remain when the database rejects every terminal write.

```text
Focused remediation: 3 passed, 30 deselected in 0.47s
Phase D targeted: 62 passed in 18.78s
Full coverage: 228 passed, 7 skipped in 157.19s, 85.01%
Plain full: 228 passed, 7 skipped in 115.53s
Ruff format: 49 files already formatted
Ruff check: All checks passed
```

The 10,000-row structural memory contract is GREEN under the six-MiB traced peak bound. Migration
v9 remains unchanged. No external provider or credential was used.

## Fourth independent-review remediation GREEN

Execution now owns independent descriptor stats and a failure sink, revalidates artifacts, and
persists final conserving counts. Batch advice counts advance only after every INSERT and commit;
database faults roll back the entire batch and expose only `DB_WRITE_ERROR`. Anonymous quality JSON
is capped at 16 KiB and accepts only finite in-range numeric allow-list fields. Fake history is
redacted, count-bounded, and capped at 64 KiB serialized metadata.

```text
Focused remediation: 3 passed, 27 deselected in 0.59s
Phase D targeted: 59 passed in 17.37s
Full coverage: 225 passed, 7 skipped in 152.74s, 85.04%
Plain full: 225 passed, 7 skipped in 112.90s
Fake/cache/quality focused: 3 passed in 0.59s
Ruff format: 49 files already formatted
Ruff check: All checks passed
```
