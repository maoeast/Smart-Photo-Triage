# ADR 0005. Local review service and separate human decisions

- Status: Accepted
- Date: 2026-08-21
- Phase: E

## Context

Phase E needs a useful offline review UI without turning the project into a network service or
adding a front-end toolchain. It must persist category and disposition edits, keep HUMAN decisions
above AI and RULE advice, display stable duplicate and burst groups, and stay bounded on libraries
with thousands of items. A loopback listener still needs request-boundary protection because a
browser can be induced to send requests to local services from another site.

## Decision

Use Python's standard-library `ThreadingHTTPServer` and embedded Vanilla HTML, CSS, and JavaScript.
The server accepts only `127.0.0.1`, rejects other bind values, validates the exact Host header, and
serves no external asset or CDN URL. The injectable server factory enables short-lived HTTP tests;
the CLI wrapper may open the printed local URL.

Add immutable migration v10 with a `review_decision` table. Each media item has at most one HUMAN
row containing category, disposition, timestamps, and a monotonically increasing revision. AI rows
remain unchanged. Effective values are queried as:

```text
HUMAN > latest AI ordered by created_at DESC, id DESC > RULE defaults
```

Writes require a random per-process CSRF token, exact same Origin, JSON content type, a 4 KiB body
cap, an exact field allow-list, category/disposition allow-lists, and an expected revision. A stale
write returns HTTP 409. No group identity is copied into the decision row.

The list endpoint caps pages at 100 and applies all PRD filters in SQLite before `LIMIT/OFFSET`.
JavaScript replaces the current grid and creates cards only for that page. Database strings enter
the DOM through `textContent`, not HTML parsing.

Duplicate and burst membership, representative flags, and Best Shot flags are selected only from
the algorithm versions exported by the current grouping implementation. Historical algorithm rows
remain auditable but cannot participate in a current review view. Persisted rows are validated at
the Review boundary with strict integer, boolean, bounded numeric, allow-list, and JSON-shape
contracts. Malformed local state produces only the fixed `LOCAL_STATE_ERROR` HTTP 500 response.

Preview requests accept only `media_id`. The server resolves the database-owned READY preview below
the workspace `previews` root, uses the existing platform-specific no-follow opener, bounds the
response to 32 MiB, and verifies the stored artifact SHA-256 before returning bytes.

## Consequences

- Base installation gains no runtime dependency and the UI remains fully offline.
- Human overrides survive AI reruns and concurrent tabs cannot silently overwrite one another.
- Historical grouping versions cannot split or relabel current duplicate and burst views, and
  malformed persisted rows cannot terminate an HTTP handler with a conversion traceback.
- The UI is intentionally a review surface, not a full photo manager or remote service.
- IPv6 loopback is not exposed in MVP. The deliberately narrow bind contract reduces Host and
  browser-origin complexity while satisfying the required `127.0.0.1` default.
- Static dashboard export remains unimplemented because T-E-010 is optional and a second rendering
  path would add code without improving the editable local workflow.
