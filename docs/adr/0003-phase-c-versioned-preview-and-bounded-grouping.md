# ADR 0003: Versioned previews and bounded candidate grouping

- Status: Accepted
- Date: 2026-08-21

## Context

Phase C must generate image and video previews without changing source media, isolate corrupt
items, resume safely, find exact duplicates without hashing the whole library, form deterministic
burst candidates without transitive false chains or all-library quadratic comparison, and expose
local quality/best-shot advice without creating file actions.

The published v1 through v4 migration statements are immutable. Standard Pillow in the Windows
baseline did not register HEIC/HEIF. FFmpeg and FFprobe were available on `PATH`.

## Decision

Keep published v1 through v5 statements immutable. Original v5 creates `preprocess_run`,
`media_preprocess`, `duplicate_group/member`, and `burst_group/member`. Add v6 for
`burst_group.comparison_cap` with default 32 and the new `grouping_run` audit table. Validator table
shapes are version-gated, including the pre-v6 burst columns. A real populated v5 fixture upgrades
to v6 while preserving workspace UUID, media, preprocess, burst-group, and burst-member rows.
Keep v6 immutable. Add v7 only to append `media_preprocess.preview_sha256 TEXT NOT NULL DEFAULT ''`.
A populated original-v6 fixture upgrades to v7 without changing its workspace, media, preprocess,
or grouping rows; legacy READY rows receive the explicit empty digest and are regenerated once.
Keep v7 immutable. Add v8 only to append
`preprocess_run.deferred_count INTEGER NOT NULL DEFAULT 0`. A populated original-v7 fixture upgrades
to v8 without changing workspace identity or existing run counters; historical runs receive zero.

Before every cache decision, open the current source without following links. POSIX opens the
trusted source-root directory, then walks every ancestor with `openat(O_DIRECTORY|O_NOFOLLOW)` and
opens the final file relative to the last trusted descriptor with `O_NOFOLLOW`; it never validates
with `lstat` and then reopens a whole path. Windows uses `CreateFileW` with
`FILE_FLAG_OPEN_REPARSE_POINT`, checks handle attributes, and verifies the handle's final path stays
under the scanned root. Stream that stable handle through SHA-256 into a workspace-owned temporary
snapshot, verifying handle size, mtime, device, and inode across the copy. Image and video decoders
receive only the snapshot. Reopen the original path with the same no-follow policy and hash it again
before cache reuse or publication. Publish from a per-attempt candidate only when both identities
match, so link swaps and mid-generation changes are FAILED without replacing a canonical artifact.
Preprocessing owns `content_sha256` and preview fields only; scanner-owned `size_bytes` and
`mtime_ns` remain unchanged until a later scan refreshes them. The preview fingerprint adds its own algorithm
identifier, preview version, maximum edge, and output format. A cache hit also requires READY status
and an existing artifact. Missing artifacts, failed items, source changes, and version/config
changes are retried. Runs and item rows are committed in bounded batches.

Apply Pillow's EXIF transpose before resizing, compute a versioned 64-bit dHash and local quality
metrics from the oriented preview, then publish through a unique `.partial` file and `os.replace`.
Preview code never writes the source. `max_edge` is capped at 4096, and image/video frame headers
whose decoded dimensions exceed 64 Mi pixels are rejected before pixel loading.

Snapshots and encoded candidates live only under `state/preprocess-runs/<run-id>`. Each run writes
an owner PID and token before media copying. Startup removes a run directory only when its owner PID
is provably absent; active or unprovable owners are preserved. Normal completion and every
`BaseException`, including `KeyboardInterrupt`, remove the current run directory in `finally`.

Bind preview publication to workspace directories. POSIX walks/creates preview directories from an
open workspace-root descriptor with no-follow flags and performs `os.replace` with source and
destination dir FDs. Windows rejects reparse components, pins the destination directory with a
no-delete-share handle, verifies its final path remains inside the workspace, and holds it through
replace. A directory swap therefore fails without an external canonical preview.

Use one secure workspace-tree abstraction for `state/preprocess-runs`, run directories, snapshots,
candidates, owner metadata, preview directories, publication, and cleanup. POSIX starts from an
open workspace-root FD, uses relative `mkdir/open` with `O_DIRECTORY|O_NOFOLLOW`, creates files with
`O_EXCL|O_NOFOLLOW`, and exposes snapshot/candidate paths through the retained directory FD;
FFmpeg receives the required FD through `pass_fds`. Windows opens and retains no-delete-share
handles for every ancestor from workspace root through the final directory, verifying every final
handle path. Cleanup traverses the same pinned tree and never follows an untrusted run link.

READY cache reuse requires the stored path to equal the fingerprint/format-derived canonical
workspace path. Open the final artifact through the pinned no-follow preview tree, require a regular
file, stream SHA-256 and compare it with the v7 READY row, decode it successfully, and verify
persisted dimensions. Windows opens the final leaf with `FILE_FLAG_OPEN_REPARSE_POINT`, rejects
reparse attributes, and verifies the handle's final path remains in the workspace. POSIX opens the
leaf relative to its pinned parent FD with `O_NOFOLLOW`. Absolute, external, noncanonical, linked,
missing, corrupt, digest-mismatched, or dimension-mismatched artifacts are not hits.

After publication, reopen the canonical leaf through the same pinned/no-follow workspace path,
require a stable regular-file identity, decode and verify its dimensions, and stream its SHA-256.
That post-publication digest is the only value written with READY. The candidate prehash is only a
comparison value. If the two differ, or canonical verification fails, securely remove the published
leaf and persist FAILED with cleared preview path and digest.

Serialize publication by preview fingerprint with an O_EXCL/no-follow owner file under the secure
`state/publication-locks` tree. Owner metadata contains PID and an unpredictable token. A contender
polls for a bounded interval and rechecks cache after acquisition. It reclaims only an owner proven
dead, using identity-bound quarantine; live, malformed, or unprovable owners remain untouched and
produce stable busy behavior. The critical section starts before the authoritative source snapshot
and covers cache validation, generation, publish, canonical verification, READY/FAILED persistence,
and commit. Equal-content media may copy advisory/preview metadata from an already validated READY
row and share its canonical artifact without invoking the generator again.

Before cleanup after a publication failure, securely reverify both the untrusted artifact digest and
file identity captured by this attempt. Delete only when both still match. If an external actor has
replaced the leaf, retain that other object, record FAILED, and clear this media row's preview path and
digest. A second SPT writer cannot reach publish or verification while the fingerprint owner holds
the lock, including the post-verify to database-commit interval.

Use no deadline by default when valid owner metadata identifies a process that is still live. The
wait loop remains naturally interruptible by `KeyboardInterrupt`. Malformed, unreadable, or
otherwise unknown owner metadata is deferred immediately, logged with its reason, preserved, and
never reclaimed. Tests, embedding callers, and CLI users may explicitly supply a positive finite
budget; only expiration of that explicit budget produces a timeout-based defer. Do not modify the
item's READY row or create a FAILED row. Persist `deferred_count` immediately while the run is still
`RUNNING`, then persist the exact same count with `COMPLETE_WITH_DEFERRED` or the combined
failures-and-deferred terminal status. Expose the count through the API and CLI.

Acquire run and item security resources only inside `with ExitStack()` blocks, including owner
metadata creation, so every partial setup failure closes all acquired descriptors/handles. Replace
`shutil.rmtree` with explicit no-follow deletion: POSIX atomically renames every selected child to an
unpredictable quarantine name under its pinned parent FD, then compares no-follow stat and opened
directory device/inode before recursive dir-FD unlink/rmdir while the directory FD remains held.
Windows atomically renames each child under its pinned parent to an unpredictable
quarantine name before reparse-aware deletion. Link objects are deleted without traversing their
targets. Dead-owner and normal cleanup share this implementation. Best-effort candidate/snapshot
unlink is nested inside a final context-exit guarantee, so cleanup faults cannot retain handles.

Use FFprobe only for duration and FFmpeg only for individually requested frames. Short, medium, and
long videos receive 3, 6, and 9 evenly spaced interior samples. A pair of reader threads continuously
drains stdout and stderr into separate hard-capped buffers. Timeout or either overflow kills and
waits for the process, closes both pipes, and remains item-local. Each FFmpeg command emits one PNG
frame. Frames are consumed sequentially into a contact sheet, so Python never receives a full
decoded video. The final sheet is resized again so its longest edge never exceeds `max_edge`,
including very small configured limits.

Use same-size SQL buckets before SHA-256. Hash only candidate images/videos, retain media-instance
identity, and persist a group only for equal SHA values. Grouping creates no plan, operation, move,
delete, or reject action.

For bursts, sort candidates canonically by capture time, path key, and media ID. Split the 64-bit
dHash into `threshold + 1` bands. Two hashes within the threshold must share at least one band, so
active time-window candidate lookup does not require comparing every library pair. Expire band
entries when their fixed anchor leaves the time window. A member must be within both the anchor time
window and the anchor hash threshold. This blocks unbounded `A near B`, `B near C`, `A not near C`
chain expansion.

Apply a configurable per-item comparison budget, default 32, across anchor lookup and medoid work.
Use at least 16 dHash bands and rank candidate anchors by shared-band count before canonical
tie-breaking and capped selection. This prioritizes actual similarity evidence rather than early
anchors. If ranking must truncate candidates, return a deterministic warning and persist the
algorithm version, cap, comparison count, warning count, and warning JSON in `grouping_run`; loss is
never silent. After membership is fixed,
select a deterministic medoid from at most 12 evenly spaced candidates. Total reported similarity
work is therefore at most `item_count * comparison_cap`, including an adversarial shared hash band.
Ties use quality score, capture/path order, and media ID. The algorithm version is
`time-dhash-ranked-capped-medoid-v3`, and v5 persists the selected cap. Store the separate
highest-quality member as `is_best_shot`; both flags are advice only.

Register `pillow-heif` when installed. Keep it in the explicit `heic` extra rather than the base
dependencies. The installed 1.5.0 Windows wheel's own `LICENSES_bundled.txt` states that binary
wheels are GPLv2 because they include x265; it also lists libheif and libde265 as LGPLv3. Making the
extra explicit preserves HEIC support while disclosing the deployment/license choice. Core tests
use an injected decoder, and real generated-HEIC integration runs when a codec is registered.

## Consequences

- Image orientation, preview cache invalidation, missing-artifact resume, and item isolation are
  auditable in SQLite.
- Video memory is bounded by one encoded frame plus at most nine small decoded preview frames and
  the contact sheet, not by video duration.
- Exact duplicate work is limited to same-size candidates.
- Burst normalized output is independent of query/input order, fixed anchors prevent false chains,
  and comparison counts include bounded-medoid work.
- Quality and best-shot results remain advisory. Phase C has no code path that performs a source
  file action.
- Base install indexes HEIC but needs a separately chosen codec for HEIC preview. The tested extra
  is operational but has an explicit GPLv2 binary-wheel boundary.
