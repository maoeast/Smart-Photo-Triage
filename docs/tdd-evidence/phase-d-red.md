# Phase D RED evidence

Recorded on 2026-08-21 on Windows with Python 3.13.2.

T-D-001 through T-D-018 and supporting Fake E2E, cost estimate, Gemini adapter, v9 migration, and
CLI contracts were written before the Phase D implementation. A small collectable public contract
shell was present so every test reached a missing Phase D behavior instead of failing through an
import, dependency, syntax, or collection error. Fixtures used only generated previews, temporary
SQLite/filesystem state, injected providers, and synthetic HTTP responses.

```powershell
rtk python -m pytest -q tests/test_phase_d_ai.py
```

```text
25 failed in 1.52s
```

The failures established:

- missing exact structured-output validation for confidence, category, required ID, extra action
  fields, and low-confidence protection;
- missing response-order-independent `item_id` mapping;
- missing privacy-reduced request building and source path/hash/sidecar exclusions;
- missing default no-network gate for a cloud provider;
- missing cache hits and prompt/schema/provider/model/preview invalidation;
- missing bounded transient retry, permanent no-retry, and batch split isolation;
- missing complete offline Fake Provider orchestration and cost estimate;
- missing real Gemini HTTP adapter with injected transport;
- migration ledger ending at v8 without Phase D tables;
- no `spt analyze` CLI command.

This RED is meaningful. The suite collected and executed all 25 cases, and failures reached explicit
Phase D contract shells or the observably absent v9/CLI behavior.

## Independent-review remediation RED

Recorded on 2026-08-21 after the first Phase D GREEN and before remediation.

```powershell
rtk python -m pytest -q tests/test_phase_d_review_remediation.py tests/test_phase_d_ai.py::test_t_d_017_permanent_failure_is_not_retried tests/test_phase_d_ai.py::test_t_d_018_bad_item_is_split_and_isolated_from_batch
```

```text
10 failed, 3 passed in 1.35s
```

The failures proved that the cache reused results across confidence-threshold, quality, media-type,
and policy changes; estimate-only cloud analysis read previews before rejecting disabled cloud use;
global invalid-credential errors were recursively split; transient retry budgets restarted during
splits; batch, retry, and timeout values lacked hard upper bounds; and provider messages could reach
public errors. The three passing contracts confirmed that explicit item-local splitting was already
finite and that the exercised preview-read failure did not expose its absolute path. This was an
executed behavioral RED, not a collection or syntax failure.

## Second independent-review remediation RED

Recorded on 2026-08-21 before the second remediation implementation.

```powershell
rtk python -m pytest -q tests/test_phase_d_review_remediation.py -k "literal_true or one_batch_ahead or revalidates_current or explicitly_bounded or integer_controls or timeout_rejects or audit_conservation"
```

```text
12 failed, 11 deselected in 1.23s
```

The executed failures proved that truthy non-boolean cloud values bypassed authorization; estimate
read all 24 one-MiB preview bodies; analysis loaded the full set before its first batch request;
changed or missing preview artifacts reused cached advice without reopening; Gemini used unbounded
`read()`; float batch/retry controls and boolean timeout were accepted while a string timeout leaked
a `TypeError`; and a permanent failure after one successful batch did not persist a conserving
failed count or stable global error code.

## Third independent-review remediation RED

Recorded on 2026-08-21 before the third remediation implementation.

```text
rtk python -m pytest -q tests/test_phase_d_review_remediation.py -k "descriptor_pipeline or fake_provider_default or split_tree_shares or fixed_enums"
4 failed, 23 deselected in 13.99s
```

The failures measured a 10,501,742-byte traced peak for 10,000 metadata descriptors, three retained
Fake requests containing preview bytes, 398 requests for an adversarial 100-item split tree, and a
public failure containing the synthetic exception class name `LEAKED_SECRET`.

## Fourth independent-review remediation RED

```text
Focused run: 3 failed, 27 deselected in 0.90s
```

The failures proved that an artifact deleted after summary vanished silently from execution, a
second-insert trigger fault leaked its SQLite error after incrementing analyzed count, and a four-MiB
advisory string entered Fake request history.

## Fifth independent-review remediation RED

```text
Focused run: 3 failed, 30 deselected in 0.83s
```

The failures reproduced non-conserving FAILED audit counts after a cache race plus DB write fault,
and raw SQLite secret leakage when either the terminal FAILED UPDATE or its commit failed.

## Sixth independent-review remediation RED

```text
Focused run: 4 failed, 33 deselected in 0.81s
```

All four SQLite audit boundaries leaked raw local messages: COMPLETE UPDATE, COMPLETE commit,
initial RUNNING INSERT, and initial RUNNING commit.

## Seventh independent-review remediation RED

```text
Focused run: 6 failed, 37 deselected in 0.97s
```

Summary SELECT, cache lookup, estimate SELECT, connect, close, and a rollback fault all leaked raw
SQLite messages outside the prior point fixes.
