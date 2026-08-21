# Smart-Photo-Triage

Smart-Photo-Triage is a local-first CLI for safely indexing and organizing mixed photo and video
libraries. Phase H local evidence covers the synthetic release-candidate lifecycle. The product
provides read-only recursive scanning, incremental media-instance indexing,
auditable capture-time metadata, deterministic Live Photo/sidecar bundles, versioned previews,
exact-duplicate candidates, burst candidates, local quality/best-shot advice, and optional
provider-neutral Vision AI over privacy-reduced previews, plus a local Review UI with persistent
human decisions. It builds immutable deterministic organization plans, runs approval-gated
preflight, and provides journaled COPY or MOVE execution with doctor, resume, and rollback.

The supported runtime is Python 3.11 or newer. Pillow is the only base runtime dependency. Scanning,
preprocessing, and grouping never need network access.

## Clean install

### Windows PowerShell

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\spt.exe --help
.\.venv\Scripts\spt.exe init --workspace ".\demo workspace"
```

If Python 3.11 is not registered with the Python launcher, use any installed `python` executable
whose version is 3.11 or newer.

### Linux

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install .
.venv/bin/spt --help
.venv/bin/spt init --workspace "./demo workspace"
```

Workspace initialization is safe to repeat. It creates a local configuration whose default is
`allow_cloud = false`, a versioned SQLite database, and empty state directories. It does not scan or
modify photos. A `.spt-workspace` ownership marker prevents `init` from adopting an unrelated
existing `spt.sqlite3`. The atomically published JSON marker contains a random workspace UUID that
must match the identity stored inside SQLite on every initialization. Because Phase A has not been
released, an existing markerless database is conservatively rejected instead of being attributed to
SPT without proof.

## Local one-click GUI

Start the local control panel after installation. It binds only to `127.0.0.1` and opens a browser;
it never exposes a LAN service:

```powershell
spt gui --workspace "D:\SPT-Workspace"
```

Enter a source directory and a separate output directory, then follow the numbered controls:
scan, choose the offline Fake provider or explicitly configured Gemini, prepare local previews/groups,
open the human-review page, build a COPY plan, approve, preflight, and dry-run. The GUI starts with
COPY only. A real apply requires typing
`EXECUTE`; rollback requires the transaction ID and typing `ROLLBACK`. These confirmations do not
bypass the immutable plan, approval, preflight, journal, hash verification, or rollback checks.

For the complete Chinese installation, start/stop, uninstall, button-by-button, review, recovery,
and rollback guide, see [the local GUI user manual](docs/USER_MANUAL_zh-CN.md).

## v1.2.1 model routing

v1.2.1 adds provider registry, capability profiles, deterministic per-task routes, bounded fallback,
one-step low-confidence escalation, provider-result cache, route audit, and request/byte/cost budgets.
Cloud and LAN remain opt-in. See [the provider, privacy, configuration, and upgrade guide](docs/model-routing-v1.2.1.md).

## Read-only scan

```powershell
spt scan "D:\Pilot Photo Copy" --workspace ".\.spt"
```

Full SHA-256 is intentionally deferred by default. Request it when required:

```powershell
spt scan "D:\Pilot Photo Copy" --workspace ".\.spt" --full-hash
```

An output directory nested below the source can be declared and will be excluded together with a
nested workspace. A source nested inside the workspace or output root is rejected:

```powershell
spt scan ".\pilot" --workspace ".\.spt" --output ".\pilot\organized"
```

`--exclude GLOB` is repeatable. Directory symlinks and Windows junctions are not followed. Scan,
metadata, and bundle stages do not move, rename, delete, or rewrite source files. Per-item metadata
failures are recorded while the remaining items continue.

Scan progress is committed in bounded batches. If a run is interrupted, the run is recorded as
`INTERRUPTED`; a later scan with the same source and options reuses completed path/hash/metadata
state instead of repeating it. If a hard-killed owner's PID is provably absent, the next scan
atomically records the old run as `INTERRUPTED` and resumes from it. Unknown, inaccessible,
ownerless, or PID-reuse cases remain conservatively blocked. Missing markers are published only
after a complete walk. Walk or stat scope errors preserve prior presence and finish as
`COMPLETE_WITH_WARNINGS`.

The current Phase B schema stores one source-root membership per media instance. To avoid ambiguous
missing semantics, a workspace rejects parent/child source-root overlap in either direction. Use
disjoint source roots or separate workspaces. A second active scan of the same source is also
rejected before it can register another run.

Phase B does not provide forced active-run takeover. A future `spt doctor` may provide an explicit
audited recovery path for legacy or ambiguous owner state. Automatic recovery never uses elapsed
wall-clock time.

## Preview and local quality

```powershell
spt preprocess --workspace ".\.spt"
```

Images are EXIF-transposed, bounded to a 1024-pixel longest edge by default, and atomically written
as WebP under the workspace. The configurable edge is capped at 4096 and decoded image/frame
headers above 64 Mi pixels are rejected before loading. Before every cache decision, preprocessing
opens the source without following symlinks/reparse points, copies the stable handle to a
workspace-owned snapshot while hashing, and sends only that snapshot to image/video decoders. It
reopens and verifies the original path and content again before reuse or publication. Preprocessing
does not overwrite scanner-owned size or mtime. Schema v7 stores each generated preview's SHA-256.
A second run reuses a READY preview only when its content-derived fingerprint and canonical path
match, a secure no-follow leaf open succeeds, and the streamed bytes match that stored artifact
digest. Failed, changed, linked, externally replaced, or missing previews are retried without
blocking other items.

Publication is verified from the canonical leaf after replace. The securely reopened regular file
must retain its identity and dimensions, and its streamed SHA-256 must equal the candidate prehash.
Only this post-publication digest can become READY. A mismatch removes the canonical artifact and
records the item as FAILED with no preview path or artifact digest.

Concurrent preprocessors serialize the full authoritative operation with one secure O_EXCL lock per
preview fingerprint. The lock owner records PID and token, waits are bounded, and only owners proven
dead are reclaimed. Equal-content media validate and share one canonical READY artifact, so the
generator runs once. Cleanup deletes a failed publication only while its captured digest and file
identity still match; an externally replaced artifact is not deleted as though it belonged to the
failed writer.

By default, a contender waits without a deadline for an owner whose PID metadata is valid and whose
process is still live. This prevents a healthy slow disk or legal FFmpeg operation from being
misclassified as deferred. `Ctrl+C` remains interruptible. A contender may briefly see a newly
created lock before its owner JSON is closed, so the same zero-byte lock inode receives at most a
250 ms metadata-publication grace, still capped by an explicit caller wait budget. Non-empty
malformed or unreadable owner metadata is conservatively deferred immediately and preserved for
diagnosis; it is never reclaimed. A persistently empty lock is also preserved and deferred after
the short grace.
Operators who prefer bounded waiting can explicitly set a positive finite budget:

```powershell
spt preprocess --workspace ".\.spt" --lock-wait-seconds 30
```

Only expiration of that explicit budget produces a timeout-based defer. Schema v8 persists the
exact `deferred_count` on the running and terminal `preprocess_run`; the API and CLI report the same
count. No READY row is overwritten and no FAILED media row is created solely because another
healthy writer remains busy.

POSIX source traversal is descriptor-relative from the trusted source root with `O_NOFOLLOW` on
every ancestor and the final file. Windows uses reparse-aware handles and final-path root checks.
Preview publication is likewise bound to the workspace root using directory FDs on POSIX and a
pinned, verified destination-directory handle on Windows, preventing preview-directory link swaps
from redirecting writes.

Full source snapshots and encoded candidates are run-scoped under `state/preprocess-runs`. Owner
PID/token metadata allows a later invocation to remove only remnants whose owner is provably dead;
active or uncertain owners are preserved. Normal exit and exceptions remove the current run state.

Video previews are contact sheets. FFprobe reads duration metadata, then FFmpeg extracts only 3, 6,
or 9 requested frames for videos up to 10 seconds, up to 60 seconds, or over 60 seconds. Sampling
uses interior positions. Stdout and stderr are continuously drained into separate hard-capped
buffers with a configurable timeout. Timeout or overflow kills, waits, and cleans up the process.
The final contact sheet also obeys the configured longest edge. The implementation never decodes a
whole video into Python memory. `ffmpeg` and `ffprobe` must be on `PATH`; a missing or corrupt
tool/video produces an item-level FAILED preview.

HEIC/HEIF decoding is supported through a registered Pillow codec. The tested extra is:

```powershell
python -m pip install ".[heic]"
```

The `pillow-heif` Python source declares BSD-3-Clause. Its prebuilt Windows wheel bundles libheif
and libde265 under LGPLv3 plus x265 under GPLv2, and the wheel's own bundled-license notice labels
the binary wheel GPLv2. This extra is therefore explicit rather than silently forced into the base
installation. Without a registered HEIC codec, HEIC items remain indexed and preprocessing records
an isolated failure. Source-built or system-codec alternatives remain the deployer's license
choice.

Local quality metrics record sharpness, exposure, clipping, resolution, a bounded score, and an
advisory. They have no file-action field and cannot move or delete source media.

## Duplicate and burst candidates

```powershell
spt group --workspace ".\.spt"
```

Exact duplicates first use same-size SQL buckets, then stream SHA-256 only for candidates. Equal
hashes form deterministic candidate groups. No source file action is created.

Burst grouping first orders items by capture time, limits membership to a configured time window,
and uses a versioned 64-bit dHash threshold. Locality-sensitive hash bands avoid all-library pair
comparison. Membership is checked against a fixed anchor, so `A near B` and `B near C` cannot pull
in `C` when `A` is not near `C`. A bounded deterministic medoid represents the group, while the
highest local quality score is stored separately as the advisory best shot. Input order does not
change normalized groups. `--burst-comparison-cap` defaults to 32 and strictly bounds total
reported candidate and medoid comparisons to a linear per-item budget. Candidate anchors are ranked
by shared-band similarity evidence before the cap. Any remaining truncation is returned explicitly
and persisted with algorithm metadata in `grouping_run`; it is never silent.

## Vision analysis

The default provider is deterministic and fully offline. It exercises the complete request,
validation, cache, mapping, and SQLite path without a credential or network connection:

```powershell
spt analyze --workspace ".\.spt" --provider fake
```

Every run prints an estimate before provider execution: eligible items, pending items, cache
candidates, preview upload bytes from secure file metadata, and expected initial batches. Estimate
does not read preview bodies. The cloud permission gate runs before estimate collection or preview
reads, including `--estimate-only`. To calculate only that estimate:

```powershell
spt analyze --workspace ".\.spt" --provider fake --estimate-only
```

AI input is limited to the workspace-owned image preview or video contact sheet plus allow-listed,
anonymous local quality metrics. Requests exclude source paths, original content SHA-256 values,
SQLite data, EXIF dumps, and sidecar contents. Provider results are exact-schema validated and
joined by `item_id`, never response order. Unknown categories, missing IDs, extra fields, invalid
confidence, and malformed output are rejected. A low-confidence `REJECT_CANDIDATE` is reduced to
`REVIEW`. AI records contain no copy, move, rename, overwrite, or delete action.

Cloud access remains disabled in a new workspace. Gemini requires all three explicit choices:

1. change the workspace `config.toml` to `allow_cloud = true`;
2. set `SPT_GEMINI_API_KEY` in the invoking process environment;
3. supply the model instead of relying on a core hard-coded model.

```powershell
spt analyze --workspace ".\.spt" --provider gemini --model "YOUR_CHOSEN_MODEL"
```

The standard-library Gemini adapter retries only bounded transient 429, 5xx, and timeout/network
failures. An exhausted transient batch is recorded once without resetting its retry budget through
splits. Global permanent failures such as invalid credentials or configuration stop the run after
one call. Only exact-schema item failures or a provider-declared splittable error are bisected to
isolate a bad item. Batch size is capped at 100, retries at 10, and provider timeout at 300 seconds.
Public failure records contain stable codes and exception types, not provider messages or paths.
Cloud authorization accepts only the literal boolean `true`; truthy strings, integers, and objects
are rejected before preview access or provider invocation.

Successful results use the v9 unique columns with a canonical `input_fingerprint` that hashes every
result-affecting input: preview SHA-256, preview fingerprint and version, media type, allow-listed
canonical quality JSON, provider, model, prompt and schema versions, confidence threshold, and the
analysis-policy version. Changing any of these inputs misses cache. JSON key ordering alone does not.
Before reusing a cache candidate, analysis securely reopens the current artifact without following
links and recomputes its SHA-256. A missing or changed preview is a per-item input failure, never a
stale advice hit. Non-cached preview bodies are loaded only for the current bounded batch and are
released before the next batch. Gemini response reads are capped at the response limit plus one byte.
Descriptor pagination and iterator batching keep pending metadata memory proportional to batch size.
Fake request recording is off by default and explicit test recording removes preview bytes and has a
bounded history. Retry allowance is shared by the full split tree, with a linear per-root request cap.
Execution rechecks artifacts independently of its estimate and writes conserving final run counts.
Advice persistence is batch-transactional; counters advance only after commit. Quality JSON over
16 KiB is discarded, and only finite in-range numeric allow-list metrics reach providers or cache.

## Local review UI and human decisions

Start the review service after previews, groups, and optional analysis exist:

```powershell
spt review --workspace ".\.spt"
```

The server binds only to the numeric IPv4 loopback address `127.0.0.1`. Port `0` selects an
available local port. Use `--port 8765` for a stable local port or `--no-open` when the caller will
open the printed URL itself. It does not use a CDN, fetch remote assets, or expose a LAN listener.

The UI pages at most 100 items into the API and DOM at once. It supports year/month, effective
category, effective disposition, filename/tag/short-description search, exact-duplicate group and
burst-group views, Best Shot markers, a lightbox, AI reason/confidence, local quality metrics,
category edits, disposition edits, and quick KEEP or REJECT_CANDIDATE actions.
Group membership and burst marker flags are read only from the current exact-duplicate and burst
algorithm versions, so historical algorithm rows cannot change a current group view.

Human decisions are stored separately from immutable AI analysis in schema v10. Effective advice
uses `HUMAN > latest AI > RULE`, where latest AI is ordered deterministically by creation time and
row identity. Rerunning AI cannot overwrite a HUMAN row. Writes use a per-server CSRF token,
same-origin and Host checks, an exact JSON allow-list, and optimistic revisions that return HTTP 409
instead of silently losing another tab's edit. Preview URLs accept only a media ID. The server then
securely opens a digest-verified regular file below the workspace `previews` root without following
links or accepting a caller-supplied path.
Malformed persisted review inputs, including SQLite values that violate the expected integer,
boolean, bounded-number, or JSON shape, return a fixed `LOCAL_STATE_ERROR` response rather than
leaking a conversion traceback or disconnecting the request.

For automated browser work, `tests/fixtures/run_review_server.py` builds a new synthetic library
under an absent or empty explicit root, runs the real scan/preprocess/group/Fake-AI pipeline, injects
one HUMAN decision, and starts the same loopback server. It never reads a user photo library.

```powershell
python tests/fixtures/run_review_server.py --root "$env:TEMP\spt-review-browser-smoke"
```

## Immutable organization plans and preflight

Build a COPY-intent plan after review. This command securely hashes source files and writes only
immutable plan rows inside the workspace SQLite database. It does not create the output directory,
copy, move, rename, or delete any media:

```powershell
spt plan build --workspace ".\.spt" --output ".\organized" --mode copy
```

The command prints JSON containing a deterministic `plan_id`, payload SHA-256, entry count, and the
initial `PENDING` approval state. An identical DB/config/source state reuses the same plan and target
paths. Only `plan build` initializes a workspace. Inspect, approve, revoke, and preflight require a
valid existing ownership marker and database, and do not create a mistyped workspace path. Inspect
the canonical plan before approval:

```powershell
spt plan inspect "plan-..." --workspace ".\.spt"
```

Target names use a reliable EXIF, QuickTime, or supported filename time. Files with only filesystem
mtime are routed to `_时间待确认`. Final `REJECT_CANDIDATE` decisions route to `_待审废片`; the planner
never emits DELETE. Windows illegal/control characters, reserved device names, trailing dots/spaces,
case-folded collisions, Unicode normalization collisions, and the default 240-character path budget
are handled deterministically. Reserved device stems remain protected when followed by an extension,
including names such as `CON.txt` or `COM1.foo`. Explicit Live Photo and sidecar bundles retain one
directory and linked basename.

Approval is stored separately so the canonical plan remains immutable:

```powershell
spt plan approve "plan-..." --workspace ".\.spt"
spt plan revoke "plan-..." --workspace ".\.spt"
```

Approval is not a blind workflow-state update. It validates the persisted schema/planner versions,
entry count, payload digest, and canonical document inside the same SQLite `BEGIN IMMEDIATE`
transaction as the approval revision update. Corrupt plan content therefore remains unapproved with
the prior revision. Revocation intentionally remains available even when plan inspection fails, so
an operator can always remove authorization safely.

Run the read-only apply preflight after approval:

```powershell
spt plan preflight "plan-..." --workspace ".\.spt"
```

Preflight rehashes every source and checks source/output overlap, output and workspace writability,
target conflicts, every intermediate target parent, bundle drift, available capacity, unfinished
transaction rows, and apply-lock presence. Missing or changed sources, target-parent reparse points
or regular-file obstructions, different-content targets, unsafe layout, insufficient space, or
missing approval make the report fail. A same-hash existing target is reported as `ALREADY_PRESENT`.
The report includes a payload/revision-bound approval contract. The executor rechecks that contract
while holding its exclusive mutation lock immediately before acting.

## Apply, doctor, resume, and rollback

Apply is a dry run unless `--execute` is explicit:

```powershell
spt apply "plan-..." --workspace ".\.spt"
spt apply "plan-..." --workspace ".\.spt" --execute
```

The command reruns read-only preflight before acquiring the apply lock, then reruns the complete
preflight under that lock while excluding only its own proven lease. Bundle membership, sources,
targets, capacity, writability, global incomplete transactions, payload, approval revision, and
approval contract must all remain valid. It refuses a second transaction when the same plan already
has incomplete journal state. Final targets are published with hard-link no-overwrite semantics.
On a filesystem without same-directory hard-link support, apply fails closed, retains the source and
verified partial, creates no final target, and releases its owned lock. Existing different-content
targets fail; an existing same-content COPY target is recorded as unowned `ALREADY_PRESENT`.

COPY and MOVE first stream into a same-directory partial, fsync it, and independently verify its
SHA-256 before finalization. MOVE commits a deterministic source-quarantine path and identity before
source-path mutation. It binds the quarantine without overwrite, hashes the source, rechecks the
final target after that hash and immediately before source unlink, then durably records quarantined,
unlinked, and removed states. A same-content pre-existing MOVE target remains unowned but still
requires successful source removal before DONE.

Inspect the workspace without mutation:

```powershell
spt doctor --workspace ".\.spt"
```

Doctor reports live, stale, or unknown apply locks; resumable journal states; missing, changed, or
orphan partials; stale sources; target mismatches; invalid plans or journals; and partially completed
bundles. It does not reclaim a live or unknown lock and does not traverse a symlink or Windows
junction output root. Database and directory iteration is bounded; exceeding the limit reports
`DOCTOR_SCAN_LIMIT` rather than scanning indefinitely.

Continue the exact durable transaction after resolving any reported external conflict:

```powershell
spt resume "tx-..." --workspace ".\.spt"
```

Resume never creates a replacement transaction or regenerates target names. Modified or unprovable
partials and targets are retained for diagnosis.

Rollback reverses only effects still proven to be owned by that transaction:

```powershell
spt rollback "tx-..." --workspace ".\.spt"
```

Rollback never deletes an unowned same-content target, a modified target, or anything outside the
plan output root. MOVE rollback restores and verifies a missing source before removing an owned
output. Before changing a bundle, rollback proves every member is safe. One conflict retains every
member unchanged and reports a partial rollback.

## Development checks

```powershell
python -m pip install -e ".[dev]"
python -m ruff format --check .
python -m ruff check .
python -m pytest --cov=smart_photo_triage --cov-report=term-missing
```

All automated tests use temporary directories and synthetic state. They must not point at a real
photo library.

Local Phase H evidence, including the 1k synthetic benchmark and clean base-install check, is in
`docs/tdd-evidence/phase-h-green.md`. It is not a final release declaration; see
`docs/adr/0008-phase-h-local-release-evidence.md` for its boundaries.

GitHub Actions is prepared to run this same quality matrix on Windows and Linux for Python 3.11
and 3.13, build both sdist and wheel, then install the built wheel in a fresh virtual environment
before the synthetic lifecycle E2E. Hosted CI evidence remains **NOT RUN** until a remote workflow
run is recorded.
