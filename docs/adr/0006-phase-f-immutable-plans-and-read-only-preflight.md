# ADR 0006. Immutable organization plans and read-only preflight

- Status: Accepted
- Date: 2026-08-21
- Phase: F

## Context

Phase F must translate effective `HUMAN > AI > RULE` decisions into deterministic COPY or MOVE
intent without giving the planner filesystem execution authority. Plans must remain previewable and
auditable after later review changes, must preserve bundle relationships, and must reject stale
sources or unsafe layouts before a future executor can start. Approval is mutable workflow state,
so storing it inside the immutable plan payload would either make the payload dishonest or require
rewriting it.

## Decision

Add migration v11 without changing any v1 through v10 migration statement bytes. It introduces:

- `organization_plan` for the canonical immutable header and payload digest;
- `plan_entry` for ordered source identity, target intent, decision provenance, bundle role, size,
  SHA-256, and source mtime;
- `plan_approval` for separate `PENDING`, `APPROVED`, or `REVOKED` state and revision;
- `operation_transaction` and `operation_journal` as the additive rollback-ready data contract
  needed by preflight to detect unfinished work. Phase F does not create transactions or execute
  entries;
- SQLite triggers that reject plan/header updates and deletes.

Schema validation compares the normalized SQL semantics of those immutable triggers, not merely
their names. The published v11 migration statements remain unchanged by later Gate remediation; no
v12 correction was necessary.

The planner reads present indexed media, securely reopens every source without following symlink or
reparse components, streams SHA-256, and rejects state that differs from the scan index. It groups
explicit bundle members under one directory and basename. An unbound sidecar is warned and omitted
instead of being guessed into a bundle. Effective human or AI policy is snapshotted into every
entry. `REJECT_CANDIDATE` routes only to `_待审废片`; it never becomes DELETE.

Target naming uses a reliable captured time only for EXIF, QuickTime, or supported filename time.
Filesystem mtime and invalid/low-confidence time route to `_时间待确认`. Every filename component is
NFC normalized, stripped of Windows control/illegal characters, protected against device names and
trailing dots/spaces, bounded by the configured path budget, and suffixed with a stable identity
token. Device-name protection applies to the stem before an extension, so `CON.txt` and `COM1.foo`
cannot bypass it. Target uniqueness uses NFC plus case-folded Windows semantics.

The payload digest excludes creation time, plan ID, and approval state. The deterministic plan ID is
derived from that digest. The first successful build stores a creation time; identical concurrent
or later builds reuse the same row and canonical document under `BEGIN IMMEDIATE` serialization.
Injected faults before commit roll back both header and entries.

Approval is explicit through `spt plan approve` and may be revoked separately. Preflight is
read-only. It checks current approval revision, source existence/size/SHA-256, source/output overlap,
output and workspace writability, intermediate target-parent reparse points and file obstructions,
target containment and content conflicts, bundle membership, capacity, unfinished transactions, and
apply-lock presence. A same-hash existing target is reported as `ALREADY_PRESENT`; different content
is a blocking `TARGET_CONFLICT`. Missing sources at either stable-open or final-stat time become
item-level `SOURCE_MISSING` issues. Unsupported plan schema versions are rejected rather than
optimistically interpreted.

Approval performs that persisted plan-integrity validation while holding `BEGIN IMMEDIATE`, before
changing the approval row. Validation and the revisioned state transition therefore observe one
consistent SQLite write transaction; an integrity failure rolls back without changing `PENDING` or
its revision. Revocation intentionally does not depend on plan integrity, because deauthorization
must remain possible for a damaged plan.

The preflight report binds the immutable payload digest to the observed approval state and revision
in an approval contract. Revocation makes that contract stale. Phase G must validate the same
contract again under its exclusive mutation lock immediately before filesystem mutation. That final
check belongs to the executor, so Phase F does not claim to make a read-only check itself atomic with
a future write. Non-build plan commands use a strict existing-workspace open and never initialize a
missing or mistyped path.

## Consequences

- Scan through Plan remains source-read-only and creates no output directory or media file.
- A HUMAN decision and its source are frozen into the plan even if review state later changes.
- Plan approval can change without weakening immutable payload auditability.
- A preflight report is previewable evidence, not a transferable authorization token; its approval
  contract must still be current at the executor's final mutation boundary.
- Phase G can consume explicit expected source/target hashes and journal tables without inventing a
  second plan representation, but no executor, resume, rollback, or filesystem transaction behavior
  is implemented in Phase F.
- The conservative 240-character default can reject a deeply nested output root instead of relying
  on platform-specific long-path configuration.
- Standalone sidecars and bundles already marked ambiguous require review rather than heuristic
  binding by the planner.
