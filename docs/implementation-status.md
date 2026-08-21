# Implementation status

## v1.2.1 model-routing upgrade

In progress. The implementation adds an additive SQLite v12 migration for provider cache and route
audit, a Provider Registry with capability contract, Gemini/OpenAI/Anthropic/OpenAI-compatible
adapters, deterministic ITEM_ANALYSIS/BURST_REVIEW routing, privacy scope gates, bounded fallback,
low-confidence escalation, budget state, and zero-network CLI observability. Final status is
determined only by the current v1.2.1 full regression, coverage, synthetic multi-provider E2E, and
`docs/release-evidence-v1.2.1.md`; it is not inherited from the v1.2.0 evidence below.

## Current phase

Phase H. Local E2E, performance, and release-candidate evidence.

## Gate result

Phase H local synthetic gate is PASS on Windows. It adds traceable H1 fixtures, H2
copy/verify/doctor/rollback, a cache-only second pass, and H4's 1k read-only lifecycle benchmark.
The final release-candidate DoD is not declared. Local full-suite/coverage evidence and
`docs/release-evidence.md` exist, but hosted Windows/Linux CI, optional real-provider smoke, and a
strict fully isolated dependency-resolution check remain unproven. Exact local results are in
`docs/tdd-evidence/phase-h-green.md` and `docs/release-evidence.md`.

The latest recorded complete non-external suite is 483 passed and 7 skipped in 220.95s, with 90.09%
combined line-and-branch coverage. Executor focused coverage is 96.855% line and 95.248% branch;
planner/preflight focused coverage is 94.649% line and 90.244% branch after four synthetic policy
outcomes closed its recorded 87.805% RED. A fresh temporary installed-package CLI/API E2E now
exercises init through rollback and a cache-only second pass. `docs/release-evidence.md` records
NOT_READY because hosted Windows/Linux CI and optional real-provider smoke remain NOT RUN, and
because the no-network environment cannot yet prove a fully isolated dependency-resolution install.

Phase G Gate is PASS locally on Windows. The executor is dry-run by default and requires explicit
`--execute`. It runs Phase F preflight before the apply lock, then reruns the complete preflight under
the exclusive mutation lock while excluding only its own proven lease. This rechecks bundle
membership, sources, targets, capacity, writability, global incomplete transactions, immutable
payload, approval revision, and approval contract before mutation. An incomplete transaction for
the same plan is never duplicated and directs the operator to resume. The durable operation journal
commits every state boundary independently with SQLite `synchronous=FULL`.

COPY and MOVE use deterministic same-directory partials, file and directory fsync, streaming
SHA-256 verification, and hard-link no-overwrite finalization. Unsupported hard-link filesystems
fail closed with source and verified partial retained, no final target, and the owned lock released.
Final targets are reopened no-follow and checked for both hash and partial identity. MOVE commits a
deterministic quarantine path and source identity before mutation, then durably records prepared,
quarantined, unlinked, and removed states. It hashes the source before the final target identity/hash
recheck and source unlink. A same-hash pre-existing MOVE target remains unowned; rollback restores
that source without deleting the unowned target.

Resume covers PREPARED, PARTIAL_COPIED, PARTIAL_VERIFIED, FINALIZING, COPIED_VERIFIED,
SOURCE_DELETE_PREPARED, SOURCE_QUARANTINED, SOURCE_DELETE_UNLINKED, SOURCE_REMOVED, and recoverable
FAILED state without new names or journals. Doctor is read-only, uses immutable-plan-bound streaming
no-follow hashes, bounds database and directory iteration, rejects reparse output roots, skips
nested links, and reports lock, plan, journal, partial, target, source, bundle, and resumable-state
diagnoses. Rollback preflights a complete bundle before mutating any member and deletes only
output-contained targets whose committed identity and hash still prove transaction ownership.
Modified, unowned, outside-output, or otherwise unprovable paths are retained. Only provably dead
lock owners may be reclaimed; live and unknown owners remain untouched.

No schema migration was needed. Phase G uses the v11 transaction and journal tables, and the exact
published v1 through v11 migration statement hashes remain unchanged.

The preserved pre-change Phase A-F baseline is 330 passed and 7 skipped in 132.80s. The expanded
pre-implementation RED is 30 failed and 1 passed in 1.20s, independently reproduced as 30 failed and
1 passed in 1.13s. Review remediation added true REDs for duplicate transactions, verified-partial
cleanup, pre-finalize rollback, reparse-output diagnosis, and MOVE target/source races. The
independent Gate audit then added `13 failed, 84 deselected`, followed by a four-case P2 RED for
prepared-state target revalidation, no-overwrite quarantine, hard-link failure, and bounded doctor
iteration. Exact output and failure meaning are in `docs/tdd-evidence/phase-g-red.md`.

Final Phase G evidence:

```text
rtk python -m coverage run --branch --source=smart_photo_triage.executor -m pytest -q tests/test_phase_g_executor.py
128 passed in 27.06s
executor line coverage 1149/1195 = 96.1506%; branch coverage 363/382 = 95.0262%

rtk python -m pytest -q tests/test_phase_f_planner.py tests/test_phase_g_executor.py
170 passed in 29.06s

rtk python -m pytest -q
458 passed, 7 skipped in 157.17s

rtk python -m pytest -q --cov=smart_photo_triage --cov-branch --cov-report=term
458 passed, 7 skipped in 198.13s; 89.56% combined line and branch coverage

rtk python -m ruff check .
All checks passed!
```

All Phase G tests use temporary directories and generated byte fixtures. No real photo library,
network, credential, external API, commit, push, or Phase H work was used.

Phase F Gate is PASS locally on Windows. T-F-001 through T-F-016 now have deterministic immutable
plans, query/order-independent stable targets, Windows and Unicode filename safety, explicit
`_时间待确认` and `_待审废片` policy, linked bundle naming, SHA-bound stale-plan detection, overlap and
output checks, unfinished-transaction blocking, and no source/output filesystem action. Plans
snapshot effective `HUMAN > AI > RULE` decisions and rollback-ready size/SHA/path/bundle data.
Approval state is separate, revisioned, and required before preflight. Preflight returns a
payload/revision-bound approval contract that the Phase G executor rechecks while holding its
mutation lock. Inspect and preflight strictly open an existing workspace without initializing a
mistyped path. Approval itself first validates the complete canonical plan inside the same
`BEGIN IMMEDIATE` transaction as the state update. A corrupt payload cannot become approved and
leaves its state/revision unchanged; revoke deliberately remains available for safe deauthorization.

Additive schema v11 preserves the exact v1 through v10 migration statement bytes and adds immutable
plan/header tables, separate approvals, and the operation transaction/journal data contract. Phase F
does not create operation transactions or implement an executor. Identical concurrent builds
converge on one plan. An injected failure after an entry insert rolls back all plan rows and leaves
synthetic sources untouched. The v11 migration itself was not rewritten during independent Gate
remediation, so no v12 was needed. Validation now checks the immutable trigger SQL semantics, not
only trigger names.

The initial Phase F RED was 17 failed in 1.62s. Independent Gate remediation added a second true RED
of 9 failed, 32 deselected in 1.06s, and final approval-integrity review added 1 failed, 41
deselected in 0.64s. The final focused planner and synthetic E2E suite is 43 passed in 3.39s with
92.49% planner/preflight combined line and branch coverage. The final plain suite is 330 passed,
7 skipped in 127.85s. The final explicit line-plus-branch coverage run is 330 passed, 7 skipped in
168.45s with 87.52% coverage. Ruff check and format are green. The synthetic E2E runs
scan, preprocess, exact/burst group, Fake AI, HUMAN override, deterministic two-build Plan, approval,
and preflight while proving the source snapshot is unchanged and the output root is not created.
No real photo library, credential, CDN, external network, copy, move, rename, or delete was used.

Phase E remains PASS with the dependency-free loopback Review UI, schema v10 HUMAN decisions,
optimistic revisions, current-algorithm group views, strict persisted-row parsing, and secure
preview serving. Optional static export T-E-010 remains intentionally unimplemented.

Phase D remains PASS. T-D-001 through T-D-018, provider-neutral Vision analysis, the complete
offline Fake Provider path, injected-HTTP Gemini adapter, privacy-reduced request building, exact
schema and item mapping, low-confidence protection, v9 cache/migration, cost estimate, bounded
retry, split isolation, CLI, Phase A/B/C regression, lint, and its original overall 85% coverage
Gate are GREEN. The independent Phase D remediation also proves complete canonical cache identity,
pre-preview cloud gating for estimates, global-permanent stop semantics, explicitly bounded
item-local splitting and transient retry, parameter caps, and public error redaction. The second
remediation additionally proves literal-boolean cloud authorization,
metadata-only estimate, batch-bounded preview loading, secure current-artifact cache validation,
bounded Gemini response reads, strict numeric types, and conserving partial-success failure audits.
The third remediation proves cursor-streamed descriptors, non-retaining Fake defaults, a shared
linear split-tree retry budget, and fixed enum-only public error codes.
The fourth remediation adds summary-to-execution artifact race auditing, commit-correct batch
counters with `DB_WRITE_ERROR`, and bounded numeric-only quality/Fake history contracts.
The fifth remediation makes exceptional audit counts execution-aware and guards both FAILED UPDATE
and commit so terminal database faults expose only `DB_WRITE_ERROR`.
The sixth remediation extends the same rule to initial RUNNING and normal COMPLETE INSERT/UPDATE
and commit boundaries, while preserving non-SQLite control-flow exceptions.
The seventh remediation consolidates every AI SQLite operation under one normalization boundary,
including summary/cache/estimate queries, connect, rollback, and close.

Latest Phase D evidence: 72 targeted tests passed. The initial coverage attempt exposed a real
Windows initialization-lock race on pressure invocation 13; after bounded contention handling, 20
consecutive stress invocations passed. The fresh full coverage run is 238 passed, 7 skipped, 85.16%.
The final identity-bound lock follow-up passes all 12 Phase A workspace tests and the fresh complete
coverage suite is 242 passed, 7 skipped, 85.20%.
The subsequent partial-owner-write remediation passes all 14 Phase A workspace tests. Its real
four-byte mid-write and close-failure injections leave no owner file or lock directory, the barrier
test passes 20/20 pressure invocations, and the latest full coverage suite is 244 passed, 7 skipped,
85.14%.

Evidence:

```text
rtk python -m pytest -q tests/test_phase_d_ai.py
29 passed in 1.60s

rtk python -m pytest --cov=smart_photo_triage --cov-report=term-missing -q
238 passed, 7 skipped in 164.38s, 85.16% combined line and branch coverage

rtk python -m pytest -q tests/test_phase_d_ai.py tests/test_phase_d_review_remediation.py
66 passed in 18.54s

rtk python -m pytest -q tests/test_phase_d_ai.py::test_fake_provider_runs_complete_offline_workspace_flow
1 passed in 0.26s

rtk python -m pytest -q
238 passed, 7 skipped in 124.74s

rtk python -m ruff format --check .
48 files already formatted

rtk python -m ruff check .
All checks passed

EXTERNAL_NOT_RUN

rtk python -m pytest -q tests/test_phase_c_tenth_review.py
12 passed in 1.03s

rtk python -m pytest -q -s tests/test_phase_c_smoke.py -k 1k
1000 items in 64.360s, 15.5 items/s, 6.32 MiB peak traced Python memory,
3.92 MiB SQLite, 1000 cache hits, 12999 burst comparisons

rtk python -m pytest -q
166 passed, 7 skipped in 88.40s

rtk python -m pytest --cov=smart_photo_triage --cov-report=term-missing -q
166 passed, 7 skipped in 119.18s, 87.29% combined line and branch coverage

rtk python -m ruff format --check .
43 files already formatted

rtk python -m ruff check .
All checks passed

isolated base install
Pillow-12.3.0 + smart-photo-triage-0.1.0, no pillow_heif module or HEIC registration

isolated .[heic] install
Pillow-12.3.0 + pillow-heif-1.5.0 + smart-photo-triage-0.1.0,
generated 24x16 HEIF -> WebP preview succeeded
```

Both isolated installs were rerun after the second-review remediation. Their verified system-temp
roots were removed after the checks completed.

The original Phase B RED was observed as 16 functional failures plus one separately executed 10k
failure. Independent review then produced 14 additional meaningful failures and one retained
scope regression. The third review produced seven additional failures before remediation. Exact
commands and failure meanings are in `docs/tdd-evidence/phase-b-red.md`. A final crash-recovery RED
then proved three owner-liveness and v4 migration gaps. A Windows retained-handle RED proved that
`OpenProcess` success alone was insufficient after process exit. Final results are in
`docs/tdd-evidence/phase-b-green.md`.

Phase C started from 29 functional failures plus one separately executed 1k smoke failure. The
first 1k GREEN attempt then exposed an actual cache invalidation defect: exact grouping populated
the optional SHA field and changed the preview source signature, causing 1,000 second-pass misses.
An independent Phase C review then established 11 more meaningful RED failures: blocking or
unbounded media pipes, stale same-stat cache identity, stale publication after a source mutation,
an adversarial burst candidate fan-out, and small contact-sheet limits. The remediation uses
content-derived source identity, generation staging, dual-pipe capped readers with timeout and
cleanup, and a strict per-item burst budget. Exact RED and GREEN evidence is in
`docs/tdd-evidence/phase-c-red.md` and `phase-c-green.md`.

The second independent review added five executed RED failures and two capability-skipped real
leaf-symlink contracts. Remediation now preserves scanner stat ownership, decodes only from a
no-follow stable-handle snapshot, revalidates path/content before publication, caps preview size and
decoded pixels, ranks burst candidates by shared-band evidence, and persists every truncation
warning with algorithm metadata. A real Windows parent-junction swap is retained as GREEN coverage.

The third review added four executed RED failures and one POSIX-only collected race. V5 is restored
to its original published statements, with grouping audit additions moved to v6. Source traversal
and destination publication are descriptor/handle-bound, candidates are cleaned for
`BaseException`, and PID/token run directories reclaim only proven-dead remnants.

The fourth review added two real Windows RED failures. A unified secure workspace-tree abstraction
now protects state, run, snapshot, candidate, owner, preview, publication, and cleanup paths. Windows
pins every ancestor handle from workspace root; POSIX uses root-relative no-follow directory FDs and
passes retained snapshot FDs to FFmpeg.

The fifth review added one real cache-junction RED. READY reuse now securely opens only the
canonical derived artifact and validates regular-file decoding/dimensions. ExitStack acquisition is
exception-safe, and all run cleanup uses explicit no-follow recursion instead of `shutil.rmtree`.

The sixth review added three executed RED failures and two cross-platform capability contracts.
Immutable v6 now upgrades through additive v7, which persists the SHA-256 of each READY preview.
Cache reuse securely opens the canonical leaf and streams its bytes for comparison with that stored
digest, so a valid same-size and same-dimension WebP replacement is regenerated. Item cleanup closes
its security context even when best-effort unlink fails. POSIX deletion verifies the opened child
device/inode against its no-follow stat; Windows first atomically binds each child to an unpredictable
quarantine name before reparse-aware deletion.

The seventh review added one executed publication-race RED and two POSIX-only collected cleanup
races. READY now derives its artifact digest only by securely reopening the published canonical leaf,
checking regular-file identity and decoded dimensions, and comparing it to the candidate prehash.
A mismatch securely removes the canonical artifact and persists FAILED with no preview path/hash.
POSIX cleanup now first uses `renameat` under the pinned parent FD to bind every leaf or directory to
a random quarantine name before identity validation and deletion.

The eighth review introduced cross-process per-fingerprint publication serialization. No preserved
pre-implementation RED execution exists for this review; the five added concurrency/owner contracts
were first run after implementation and are recorded only as GREEN. Locks are O_EXCL/no-follow files
inside the secure workspace tree with PID/token audit. Live or unprovable owners receive bounded
polling and stable busy behavior; only proven-dead owners are identity-bound and reclaimed. The lock
covers authoritative snapshot, cache validation, generation, publication, post-publication checks,
and per-item READY/FAILED commit. Equal-content media share one READY artifact and one generation.

The ninth review replaced the fixed five-second publication wait with a budget derived from the
maximum legal FFmpeg operation: FFprobe plus nine frame extractions, each allowed the configured
media timeout, followed by cleanup and scheduling margin. Tests may inject a shorter positive finite
budget. A healthy owner that outlives the former threshold is waited for and then reused as a cache
hit. Budget exhaustion with a still-live or unknown owner increments `deferred_count`, not
`failed_count`, does not write or overwrite `media_preprocess`, appears in CLI output, and persists as
`COMPLETE_WITH_DEFERRED` or `COMPLETE_WITH_FAILURES_AND_DEFERRED` on the run.

The tenth review removed the derived default deadline because even a media-sized estimate can
misclassify a healthy slow disk. A valid live owner is now waited for without a deadline and remains
interruptible with `Ctrl+C`. Malformed or unreadable owner metadata defers immediately, logs the
reason, remains untouched, and is never reclaimed. Only an explicit positive finite API/CLI budget
can cause timeout-based defer. Immutable v7 upgrades additively through v8, which persists the exact
`deferred_count` during `RUNNING` and at every terminal status; CLI and result counts are identical.

The Phase F fresh coverage gate exposed a narrower owner-publication race in that Phase C contract:
an O_EXCL lock name becomes visible just before its buffered owner JSON is closed. A deterministic
RED pauses precisely in that window. Contenders now give only the same zero-byte lock inode a 250 ms
metadata-publication grace, capped by an explicit caller wait budget. Persistent empty locks still
defer boundedly, non-empty malformed metadata still defers immediately, and neither is reclaimed.
All 22 lock-focused regressions and 20 repeated two-writer stress invocations passed before the
fresh whole-suite coverage run.

The final Phase C type-contract review found two inverse return annotations in preprocessing:
`preview_fingerprint` returned `str` while declaring `_ArtifactVerification`, and
`_published_artifact_sha256` returned `_ArtifactVerification` while declaring `str`. Runtime plus
`typing.get_type_hints` tests first failed on both mismatches. The annotations now match actual
values. A focused audit of the remaining explicit return constructions in `preprocess.py` found no
other same-class mismatch.

## Tests added

- T-E-001 through T-E-003 cover strict 127.0.0.1 binding, local-only assets, offline page loading,
  and digest-verified thumbnail delivery.
- T-E-004 through T-E-007 cover `HUMAN > AI > RULE`, deterministic latest-AI selection, RULE
  fallback, AI-rerun protection, repeated category/disposition edits, immutable AI rows, and
  optimistic conflict rejection.
- T-E-008 and T-E-009 cover stable exact/burst membership, Best Shot, 1005-item API pagination, and
  current-page-only DOM construction. T-E-010 static export is optional and unimplemented.
- Additional Phase E tests cover all PRD filters, v9-to-v10 preservation, CSRF/Origin/Host and JSON
  boundaries, invalid query limits, preview root/digest/missing-file rejection, DOM text insertion,
  CLI injection, server shutdown, current-algorithm-only group views, strict malformed persisted-row
  rejection, and fixed JSON 500 behavior for malformed revisions on decision POST.
- T-B-001 keeps equal content at two paths as two media instances with equal optional SHA-256.
- T-B-002 retains identity, deferred hash, and later-phase state on unchanged rescans.
- T-B-003 preserves history and marks externally missing source files absent.
- T-B-004 exercises a real Windows junction and proves it is not followed.
- T-B-005 excludes nested workspace/output trees and rejects the reverse unsafe layout.
- T-B-006 through T-B-009 cover EXIF/QuickTime/filename/mtime precedence, confidence, unknown
  timezone preservation, allow-listed filename patterns, malformed metadata, and item isolation.
- T-B-010 through T-B-013 cover Live Photo, AAE, XMP, JSON, deterministic ambiguity warnings,
  conservative non-binding, and missing companions.
- Additional tests cover QuickTime version 1 duration, EXIF CreateDate/SubSec fallback, changed-file
  hash invalidation, repeatable glob exclusion, injected parser failure, CLI scan, migration v2, and
  an in-place v1-through-v4 upgrade that preserves the Phase A workspace UUID.
- Independent-review tests cover incomplete walk/stat missing suppression, protected-history scope,
  post-stat disappearance, base-schema and resume-state UNIQUE/FK/index validation, persistent
  RUNNING state, bounded batch interruption/failure, metadata/hash resume reuse, transient metadata
  retry, stable CLI failure, overlapping-root rejection, and disjoint-root isolation.
- Third-review tests cover skipped-directory prefix preservation, post-metadata disappearance,
  partial-UNIQUE rejection, atomic same-source and parent/child registration, immutable old-v2 to
  v3 upgrade with UUID/data preservation, and stable SQLite CLI failures without swallowing
  KeyboardInterrupt.
- Crash-recovery tests inject live/dead process results and prove live owners reject without a new
  run, dead owners atomically become `INTERRUPTED` without losing counters, the replacement links
  through `resume_of`, and a real v3 fixture upgrades to immutable v4 without identity/data loss.
  A Windows-only real child test verifies that a terminated process is dead even while `Popen`
  retains a valid process handle.
- The 10k smoke uses only generated JSON sidecars in `tmp_path` and asserts all instances reach
  SQLite with peak traced Python allocations below 64 MiB.
- T-C-001 through T-C-007 cover oriented image previews, injected and real HEIC decoding,
  deterministic version/config fingerprints, corrupt-item isolation, 3/6/9 interior video
  sampling, bounded FFmpeg output, and a real contact sheet from a generated video.
- Preview remediation tests cover READY cache reuse, missing-artifact regeneration, failed-item
  retry, preview-version/size/format invalidation, same-size/same-mtime byte replacement, and source
  mutation between generation identities without stale publication.
- Runner remediation tests cover blocking stdout, continuous stderr, timeout, stdout overflow, and
  kill/wait/dual-pipe cleanup. Contact-sheet tests include 1, 8, 16, and 31-pixel edge limits, and
  Phase C CLI expected errors return exit code 2 without a traceback.
- Second-review tests prove scanner size/mtime remain unchanged until rescan, rescan refreshes
  dimensions after replacement, Windows junction and POSIX/file-symlink swaps cannot escape the
  scanned root, max-edge/decode-pixel gates reject excessive input, and a late true near anchor is
  similarity-ranked ahead of far shared-band candidates. Truncation warnings are returned and
  persisted in `grouping_run` with algorithm/cap metadata.
- T-C-008 through T-C-010 prove same-size candidate hashing, equal SHA grouping, same-size negative
  cases, retained media-instance identity, and zero source actions.
- T-C-011 through T-C-016 prove time/visual positive and negative burst behavior, fixed-anchor
  false-chain prevention, input-order determinism, bounded-medoid representation, best-shot flags,
  algorithm versions, and a 1,000-item shared-band adversary under the strict per-item comparison cap.
- T-C-017 proves local quality output contains only metrics and advice, with no action field.
- Phase C migration tests build a real v4 database through v5, populated original-v5/v6 fixtures
  through v8, and a populated original-v7 fixture through additive v8, preserving UUID, media,
  preprocess, burst-group, member, and prior run-counter data.
- The 1k smoke generates 1,000 PNG files with Pillow, runs scan/preprocess/group/preprocess, records
  throughput, peak traced memory, SQLite size, duplicate/burst counts, failures, comparisons, and a
  complete second-pass cache hit.
- Final type-contract tests pair runtime `isinstance` assertions with resolved type hints for the
  preview fingerprint string and post-publication artifact verification object.
- T-D-001 through T-D-006 cover strict confidence/category/ID/field validation, response-order
  independent mapping, low-confidence reject reduction to REVIEW, and the absence of file actions.
- T-D-007 through T-D-010 inspect serialized requests for source-path, original-hash, preview-hash,
  and sidecar exclusions, and prove default cloud denial occurs before provider invocation.
- T-D-011 through T-D-015 cover exact cache hits plus prompt, schema, provider, model, preview
  version, and preview fingerprint invalidation.
- T-D-016 through T-D-018 cover bounded transient retry, permanent no-retry, and recursive batch
  splitting that isolates one persistent bad item while good items are stored.
- Supporting Phase D tests prove Fake Provider two-pass E2E, cost estimate counts, injected Gemini
  structured response parsing, HTTP 429/503/timeout versus 401 classification, additive v8-to-v9
  migration with prior-row preservation, and the minimal `spt analyze --provider fake` CLI.

## Known limitations

- Execution is local and sequential. A live or unknown mutation-lock owner is never force-reclaimed;
  the operator must wait for the owner or resolve unprovable state outside the executor.
- Rollback deliberately retains modified or unprovable source, partial, and target paths and reports
  partial rollback instead of forcing cleanup.
- Unbound sidecars are recorded as plan warnings and omitted rather than attached heuristically.
  Explicit bundle members remain linked.
- Phase G repeats all preflight checks under its workspace apply lock immediately before mutation.
  Capacity and permissions can still change after any point-in-time operating-system observation;
  later filesystem failures remain journaled and recoverable.
- The MVP Review listener intentionally supports only `127.0.0.1`, not IPv6 loopback. Static
  dashboard export is not implemented because T-E-010 is optional.
- Automated real-browser Phase E verification passed over the startable synthetic fixture. Hosted
  browser/OS coverage beyond that local acceptance run is not yet available.
- Standard Pillow builds may not decode HEIC without a compatible codec/plugin. The explicit
  `heic` extra installs the tested `pillow-heif` codec. Its Windows binary wheel is labeled GPLv2 by
  its bundled license notice because it includes x265; this is not silently forced into base install.
- Phase B's bounded video parser reads ISO-BMFF `mvhd` creation time. Vendor-specific metadata atoms,
  ExifTool integration, and content-identifier Live Photo matching are not yet implemented.
- Filename bundle matching is deliberately conservative. Ambiguous same-stem sets remain separate
  and require later review.
- Source-root membership is currently stored on each media instance. Parent/child source roots are
  therefore rejected in both directions within one workspace; many-to-many membership is not yet
  implemented.
- Resume re-enumerates paths and performs the cheap stat comparison. It skips already committed
  metadata extraction and hashing, but it is not a persisted directory-cursor continuation.
- Ownerless legacy RUNNING rows and PIDs whose state cannot be proven are conservatively blocked.
  Phase B has no forced takeover command. A future `spt doctor` may provide an explicit audited
  recovery workflow; PID reuse must never be resolved by an automatic wall-clock timeout.
- The performance memory number is `tracemalloc` Python allocation peak, not whole-process RSS.
- Video preview requires `ffmpeg` and `ffprobe` on `PATH`. Missing tools and corrupt videos are
  isolated per item. The automated generated-video integration was verified with the local
  2023-05-11 Gyan FFmpeg build.
- Burst dHash and local metrics do not understand eyes, expression, pose, or subject completeness.
  Best-shot is local advice only; higher-level judgment remains for Phase D or human review.
- CI is prepared for Python 3.11 and 3.13 on Windows and Linux. It builds sdist/wheel, installs the
  built wheel in a fresh venv, and runs the synthetic lifecycle E2E before publishing distribution
  artifacts. Hosted CI evidence is still NOT RUN because no remote run has been authorized.
- A live Gemini smoke was not run. No credential was searched, inspected, printed, or assumed.
  External status is `EXTERNAL_NOT_RUN`; the adapter is covered with injected synthetic HTTP only.

## ADR changes

- ADR 0002 records the immutable SQLite v2 identity model, additive v3 resume state, additive v4
  owner audit, dead-owner recovery, atomic active scan registration, streaming traversal, overlap
  policy, batch persistence/resume, complete-scope missing semantics, metadata precedence,
  deterministic bundle rules, and conservative ambiguity behavior.
- ADR 0001 remains the authority for workspace identity and atomic migration behavior.
- ADR 0003 records immutable v5/v6/v7 plus additive v8 state, stable/versioned preview fingerprints,
  secure publication locking and persisted deferral counts, bounded FFmpeg frame extraction,
  same-size/SHA exact groups, fixed-anchor LSH burst grouping, bounded medoid and best-shot advice,
  and the explicit HEIC codec license boundary.
- ADR 0004 records the provider protocol, full offline Fake path, standard-library Gemini adapter,
  preview-only privacy contract, exact schema/item mapping, default cloud gate, additive v9 cache,
  bounded transient retry, and recursive failed-item isolation.
- ADR 0005 records the loopback-only standard-library Review server, separate schema v10 HUMAN
  decisions, optimistic request boundary, SQL/API/DOM pagination, and secure preview serving.
- ADR 0006 records immutable deterministic plans, additive schema v11, separate approval revisions,
  Windows/Unicode target policy, secure source hashing, bundle preservation, and read-only preflight.
- ADR 0007 records approval revalidation under the mutation lock, durable operation-journal states,
  no-overwrite verified finalization, identity-bound MOVE deletion, doctor/resume convergence, and
  ownership-proven rollback.

## Next phase

Phase G is complete. Phase H E2E, Performance, and Release Candidate is the next planned phase, but
it was not started because this work package is explicitly limited to Phase G.

## Blockers

None.
