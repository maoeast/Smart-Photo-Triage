# ADR 0004: Provider-neutral private Vision analysis and versioned cache

- Status: Accepted
- Date: 2026-08-21

## Context

Phase D must make optional Vision AI useful without giving AI access to source files or file
actions. Core behavior needs a complete offline path, while at least one real adapter must remain
replaceable and must perform zero network I/O unless cloud access is explicitly enabled. Provider
output can be shuffled, malformed, incomplete, overconfident, or temporarily unavailable. Cache
identity must cover every input and contract version that can change a result.

The published v1 through v8 migration statements are immutable.

## Decision

Add a `VisionProvider` protocol whose only business operation accepts a `VisionRequest` and returns
structured results. `FakeVisionProvider` is deterministic, offline, and drives the same
orchestration, validation, persistence, retry, splitting, and CLI paths used by real providers.
`GeminiVisionProvider` is a standard-library HTTP adapter. Its model is a constructor/CLI input,
not a core-business constant. API keys remain only in process memory and are never logged or placed
in the URL.

Build requests only from Phase C `READY` artifacts under the workspace `previews` tree. Reopen each
artifact without following links, bound it to 32 MiB, and require its streamed digest to match the
persisted preview digest. Serialize only `item_id`, media type, preview MIME/data, and allow-listed
anonymous quality fields. Source paths, original hashes, SQLite contents, EXIF dumps, and sidecar
files are not selected by the request builder.

Use an exact output schema. All eight fields are required and extra fields are rejected. Category
and disposition use closed sets, confidence and quality are finite values in `[0,1]`, and every
result must map exactly once to an expected positive `item_id`. Provider order is irrelevant. If a
`REJECT_CANDIDATE` confidence is below the configured threshold, store `REVIEW`. The result model
has no file-action or path field.

Check `allow_cloud` before an estimate, pending-item collection, preview read, or cloud-provider
invocation. The default remains false, including `--estimate-only`. Fake analysis remains available
regardless of cloud state. Authorization is an identity check against literal boolean `True`, not a
truthiness check.

Append migration v9. `ai_analysis` stores successful validated advice under a unique key containing
media ID, preview input fingerprint, preview fingerprint, preview version, provider, model, prompt
version, and schema version. The input fingerprint is a canonical SHA-256 over preview SHA-256,
preview fingerprint/version, media type, canonical allow-listed quality JSON, provider/model,
prompt/schema version, confidence threshold, and `ANALYSIS_POLICY_VERSION`. Thus it also captures
inputs not represented by dedicated v9 key columns without changing the published migration.
`ai_run` audits estimates, counts, status, and request count. Keep all v1-v8 statements unchanged and
validate the new tables, indexes, unique constraint, and foreign key according to schema version.

Retry only explicit transient errors, including HTTP 429, HTTP 5xx, and timeout/network failures,
for `max_retries` after the initial attempt. Exhaustion records each item in that batch without
splitting, so the retry budget cannot restart recursively. Treat other 4xx, invalid credentials,
configuration failures, and unexpected programming errors as global permanent failures that stop
the run immediately. Recursively split only exact-schema response failures or a provider-declared
`SplittableProviderError`; a singleton is recorded and unrelated items continue. Cap batch size at
100, retries at 10, and provider timeout at 300 seconds. Persist and return only stable error codes
plus exception types, never provider messages, credentials, or absolute paths.

Stream database rows into metadata-only descriptors. Estimate uses secure file metadata for upload
size and never reads preview bodies. Analysis validates a cache candidate by securely reopening the
artifact without following links and streaming its current SHA-256 before reuse. Missing or changed
artifacts are failures rather than hits. For non-cached work, load controlled bytes only for the
current bounded batch, then release the batch before loading the next. Gemini reads at most the
response limit plus one byte, which both detects overflow and forbids an unbounded `read()`.

Require exact integers for batch size and retry count; booleans and floats are rejected. Provider
timeouts require a finite non-boolean numeric value. On a global permanent failure after partial
success, preserve committed advice and audit every remaining non-cached item as failed so
`cache_hit_count + analyzed_count + failed_count == item_count`. Raise only the stable public code
`GLOBAL_PERMANENT_PROVIDER_ERROR`.

The descriptor pipeline uses cursor `fetchmany` and iterator batching, so total pending cardinality
does not create a retained descriptor list. Fake request recording is opt-in and retains only a
hard-bounded history with preview bytes removed. A mutable budget is created once per original batch
and shared by every recursive split child; its request ceiling is `2*n - 1 + max_retries`. Public
item errors are fixed codes such as `ITEM_SCHEMA_ERROR`, `ITEM_SPLITTABLE_PROVIDER_ERROR`,
`TRANSIENT_RETRY_EXHAUSTED`, and `PREVIEW_READ_ERROR`.

Treat summary as an estimate only. Execution has independent paged stats and failure collection,
revalidates every cache artifact, and writes final conserving audit counts. Persist one provider
batch transactionally and increment analyzed count only after commit; rollback exposes the fixed
`DB_WRITE_ERROR`. Parse quality only below a 16-KiB input bound and retain finite in-range numeric
allow-list fields. Fake metadata history has both count and 64-KiB serialized-size ceilings.

On exceptional exit, derive authoritative cache and pending counts from execution stats where
available, then compute the failed remainder so audit counts conserve. Guard the terminal FAILED
UPDATE and commit. If either is rejected by SQLite, rollback and replace all database text with the
fixed `DB_WRITE_ERROR`; the already committed RUNNING row is the documented unavoidable boundary.

Apply that SQLite normalization to every run-audit boundary, including the initial RUNNING
INSERT/commit and normal COMPLETE UPDATE/commit. A successful fallback FAILED audit does not cause
the original SQLite exception to be rethrown; it is still replaced by `DB_WRITE_ERROR`.

Enforce this through one outer database boundary shared by estimate and analysis, covering connect,
query iteration, cache lookup, mutation, commit, rollback, and close. Rollback is best-effort and
cannot replace the active error. Only SQLite exceptions are normalized; control-flow exceptions and
existing analysis/provider semantics pass through unchanged.

## Consequences

- Core tests and the operational CLI can run completely offline with Fake AI.
- Enabling cloud access is necessary but not sufficient. A provider, model, and runtime credential
  are also explicit.
- Provider output never directly creates file operations. Phase D only writes AI advice and run
  audit rows.
- The same preview and version contract is idempotent, while every required invalidation dimension
  produces a new cache entry.
- Batch splitting can increase request count around a bad item, but every retry and split is finite
  and auditable.
- The Gemini adapter is implemented and locally tested with an injected synthetic HTTP response.
  A live external smoke was not run because credentials were not inspected or assumed.
