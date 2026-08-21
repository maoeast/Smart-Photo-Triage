# ADR 0002: Streaming Phase B scan, auditable metadata, and conservative bundles

- Status: Accepted, amended after independent review
- Date: 2026-08-21

## Context

Phase B must recursively index mixed media without modifying source files. File-instance identity
must remain separate from content hashes, rescans must be incremental, capture times must retain
their source and timezone certainty, and uncertain companions must not be silently bound. The
scanner must also stay usable on a large library without first materializing every path in memory.

## Decision

Extend the Phase A migration ledger with schema version 2. `media_item` has an integer instance ID
and a unique OS-normalized path key. SHA-256 is nullable and never acts as identity. A scan uses
path, size, and nanosecond mtime as its fast unchanged signature, preserves later-phase fields on an
unchanged upsert, invalidates a stale hash when the same path changes, and marks unseen historical
rows absent only after a successful full enumeration.

Treat the published v2 statements as immutable. Schema version 3 adds `config_fingerprint`,
`resume_of`, its self-reference, and the resume lookup index through additive statements. A real
v2 database upgrades in place without changing its workspace UUID or existing media rows.

Keep v3 immutable. Schema version 4 additively records `owner_pid`, a per-run `owner_token`, and
`terminal_reason`. The PID and token are audit evidence, not a lease and not a wall-clock timeout.
A real v3 database upgrades without changing workspace identity, run counters, or prior data.

Use an iterative `os.scandir` generator. It yields supported files one at a time, never follows a
symlink or Windows reparse-point directory, excludes a nested workspace/output subtree, and rejects
a source located inside the workspace or output root. Because the v2 media model has one source
membership per item, parent/child source roots are rejected in both directions. Disjoint roots may
share a workspace.

Perform the parent/child overlap check, same-source active-run check, and `RUNNING` insertion inside
one `BEGIN IMMEDIATE` transaction. This removes the registration time-of-check/time-of-use window.
A rejected concurrent scan writes no second run row.

During that transaction, inspect the active same-source owner's PID. If the operating system proves
the PID no longer exists, atomically finish the abandoned row as `INTERRUPTED`, increment its error
count, record `OWNER_PROCESS_NOT_FOUND`, and register the replacement with `resume_of` pointing to
the abandoned run. If the PID is live, inaccessible, unknown, reused by another process, or absent
from a legacy row, reject conservatively. Elapsed wall-clock time never authorizes takeover.
On Windows, `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` is followed by
`GetExitCodeProcess`; only `STILL_ACTIVE` is treated as alive, query failure is conservative, and
the locally opened handle is always closed. A valid retained handle alone does not imply liveness.

Persist `scan_run` as `RUNNING` before item work and commit media rows plus run counters in bounded
batches. KeyboardInterrupt becomes `INTERRUPTED`; another terminal exception becomes `FAILED`.
Committed items remain available. A later run with the same canonical source/config fingerprint
records `resume_of` and uses the existing path, size, mtime, hash, and clean metadata state to avoid
repeating completed expensive work. Bundle replacement and terminal status publish only after file
enumeration ends.

Missing markers are a separate finalization step. They publish only if every walk and file stat in
the requested scope completed. A scope error produces `COMPLETE_WITH_WARNINGS` and preserves every
unseen item's prior presence. Historical items below a newly protected workspace/output root or a
new exclude glob also retain their prior state. When a directory itself matches an exclude glob,
the traversal records its concrete prefix so every historical descendant remains protected.
Item-level hash or metadata failures do not make the directory scope incomplete and do not stop
other items. Metadata fallback uses the already captured stat timestamp. Presence is checked again
after metadata work so a file disappearing after initial stat is stored absent, not falsely
present, without rolling back the run.

Validate schema columns according to their actual version and the constraints used by scan SQL:
unique media path keys, unique bundle keys and members, required named indexes, and the
bundle/warning/run foreign keys. Partial UNIQUE indexes are rejected because they cannot satisfy
the scanner's unqualified `ON CONFLICT(path_key)` target. This prevents cascade and upsert behavior
from operating against a superficially compatible but unsafe schema.

Use Pillow for image EXIF and dimensions. Capture-time precedence is EXIF DateTimeOriginal, EXIF
CreateDate, QuickTime `mvhd`, an allow-listed filename pattern, then filesystem mtime. Naive EXIF or
filename values stay naive and carry `UNKNOWN` timezone status. The scanner never converts them to
UTC. Filesystem mtime is explicitly low confidence. QuickTime `mvhd` is parsed with a bounded,
streaming ISO-BMFF atom reader instead of decoding video.

Derive Phase B bundles from case-normalized directory plus filename stem. A unique HEIC/HEIF and
MOV pair becomes `LIVE_PHOTO`; AAE, XMP, and JSON sidecars join a unique primary; otherwise each
item remains `SINGLE`. Bundle IDs are deterministic hashes of the rule version, logical key, type,
and member path keys. Any multi-file stem with multiple plausible primaries or companions stays
separate and receives `AMBIGUOUS_BUNDLE` warning evidence.

## Consequences

- Scan path memory remains effectively constant with library size; bundle rows are consumed from
  an ordered SQLite cursor one logical stem at a time.
- Two equal byte streams at different paths remain two media instances but can share a deferred
  content hash.
- An interrupted scan preserves only committed batches. It never publishes missing markers or a
  partial bundle rebuild, and the next matching run deterministically continues.
- A transient metadata error is retried on an otherwise unchanged rescan and cleared after success.
- A hard-killed v4 scanner cannot permanently block its source once its PID is provably absent.
  Ambiguous or ownerless legacy state remains blocked. A future `spt doctor` may expose an explicit,
  audited recovery operation, but forced recovery is outside Phase B.
- CLI scan paths are validated before workspace creation and expected operational errors return a
  stable nonzero result without a traceback.
- Pillow is now a small runtime dependency. HEIC EXIF availability depends on the codecs available
  to the installed Pillow build. Unsupported HEIC metadata falls back safely and records an item
  error while filename-based bundling still works.
- Phase B reads QuickTime movie-header creation time only. Broader vendor-specific video metadata,
  content-identifier matching, previews, and duplicate grouping remain later work.
