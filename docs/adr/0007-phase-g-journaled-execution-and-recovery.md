# ADR 0007. Journaled execution, recovery, and rollback

- Status: Accepted
- Date: 2026-08-21
- Phase: G

## Context

Phase F produces an immutable, approved plan and a read-only preflight report. Filesystem mutation
still needs one authority boundary that prevents concurrent apply operations, revalidates the exact
payload and approval revision, never overwrites an existing target, survives interruption at every
durable state, and can reverse only effects whose ownership remains provable.

A plan may contain COPY or MOVE entries and linked bundle members. A process can stop after a
partial copy, target verification, no-overwrite finalization, target ownership commit, or MOVE
source removal. Recovery must converge the existing transaction rather than create a second journal
or regenerate a target name.

## Decision

Use the existing additive v11 `operation_transaction` and `operation_journal` tables. Do not change
the published v1 through v11 migration statement bytes and do not add v12. The journal `error`
column stores a canonical JSON recovery envelope containing the approval contract, revision,
payload digest, entry position, optional error code, and identity-bound source, partial, and target
metadata. Each journal and transaction state transition commits independently with SQLite
`synchronous=FULL`.

`spt apply PLAN_ID` is dry-run by default. Explicit `--execute` first runs Phase F preflight before
the apply lock because preflight reports lock presence. The executor then acquires the exclusive
workspace mutation lock and reruns the complete Phase F preflight, excluding only the exact lock
lease held by that process. Bundle membership, sources, targets, capacity, writability, every global
incomplete transaction, the plan payload, approval revision, and approval contract are therefore
rechecked before creating a transaction or touching an output path. An existing incomplete
transaction for the same plan is never duplicated and directs the operator to resume.

The apply lock is an O_EXCL, no-follow owner file containing PID, token, and workspace root. Live
and unknown owners remain untouched. Only a process owner proven dead may be reclaimed, and lock
release or failed owner publication removes the file only while its captured identity still
matches. Doctor reports `LIVE`, `STALE`, or `UNKNOWN` lock state without mutation.

All output parents are contained below the immutable plan output root and opened through the
reparse-aware secure-directory binding. Data is copied to a deterministic same-directory partial,
fsynced, streamed and hashed again, and committed as verified before finalization. Finalization uses
a hard link from the verified partial to the absent final name, which has no-overwrite semantics on
filesystems that support same-directory hard links. The parent directory is fsynced and the final
file is reopened no-follow and checked for both expected SHA-256 and partial identity before target
ownership is committed. An unsupported hard-link filesystem fails closed with
`NO_OVERWRITE_LINK_UNSUPPORTED`: the source and verified partial are retained, no final target is
created, and the process-owned apply lock is still identity-bound released. Existing
different-content targets fail. Existing same-content COPY targets are `ALREADY_PRESENT` and remain
unowned. Windows verification used NTFS. No broader filesystem portability is claimed.

MOVE uses the same copy, fsync, verify, and no-overwrite sequence. Before path mutation it commits a
deterministic quarantine path, source identity, and `SOURCE_DELETE_PREPARED` state. A no-overwrite
hard link binds that source to the quarantine name. The executor then streams and hashes the source,
reopens and rehashes the final target after that source verification, rechecks the source identity,
and only then unlinks the original source name. It commits `SOURCE_QUARANTINED`, verifies and removes
the quarantine, commits `SOURCE_DELETE_UNLINKED`, and finally commits `SOURCE_REMOVED`. Faults after
prepare, link/source unlink, quarantine unlink, or a later SQLite failure leave a journal envelope
that resume, doctor, and rollback can converge. A same-content pre-existing MOVE target is marked
`target_preexisting=true`, remains unowned, and still requires verified source removal before the
journal can reach `DONE`.

Resume consumes the original transaction ID, whose revision and approval contract are encoded and
checked for incomplete transactions. It continues PREPARED, PARTIAL_COPIED, PARTIAL_VERIFIED,
FINALIZING, COPIED_VERIFIED, SOURCE_REMOVED, or recoverable FAILED state without a new journal or
name. DONE is an idempotent read-only result. ROLLED_BACK is rejected before approval checking
because it cannot mutate again. Known partials are removed only when path containment, recorded
identity, and hash all still match. Modified or unprovable partials are retained and diagnosed.

Rollback first preflights every member of each bundle without mutation. If one member is modified or
unprovable, no filesystem path in that bundle is changed and the transaction becomes
`ROLLBACK_PARTIAL`. It then walks safe journal rows in reverse. It deletes an owned output only when
the target is
inside the plan output root and its no-follow identity and SHA-256 still match the committed
ownership envelope. COPY `ALREADY_PRESENT` targets and MOVE pre-existing targets are never deleted.
MOVE rollback restores a missing source with copy, fsync, hash verification, and no-overwrite
publication before removing an owned output. Recreated or modified source/target conflicts are
retained and produce `ROLLBACK_PARTIAL`. PREPARED or pre-finalize partial states can converge to
`ROLLED_BACK` when no final target exists; an unchanged owned partial may be identity-bound removed,
while a changed partial is retained.

Doctor is read-only. It integrity-checks immutable plans, uses plan-bound streaming no-follow hashes
for sources, targets, and partials, refuses a symlink or reparse output root, avoids traversing
nested links, and reports resumable states, missing or changed owned files, journal-plan mismatch,
orphan partials, bundle partials, stale plans, target mismatch, and lock status. Database result sets
and directory entry traversal are bounded by a positive record limit. Exceeding it reports
`DOCTOR_SCAN_LIMIT` and makes the report non-OK instead of continuing unbounded.

## Consequences

- Explicit approval is necessary but not sufficient. The exact contract is checked again under the
  mutation lock.
- No final target is overwritten. A collision introduced after temporary verification remains
  external and causes a recoverable failure.
- A crash can leave a durable partial or final target, but resume converges the same journal and
  rollback acts only on identity-bound owned effects.
- MOVE never deletes its source until the final target is independently verified immediately before
  deletion.
- Unknown ownership favors retention and diagnosis over cleanup.
- Bundles share one transaction but may be partially complete after interruption. Apply cannot
  report DONE until every member is DONE or safely ALREADY_PRESENT, and rollback mutates no member
  of a bundle whose complete preflight fails.
- The implementation is local and sequential. Phase H release-candidate, performance, and remote CI
  work remains outside this decision.
