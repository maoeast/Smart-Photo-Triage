# Phase E RED evidence

Recorded on 2026-08-21 on Windows with Python 3.13.2.

T-E-001 through T-E-009 and additional migration, request-boundary, pagination, and preview-root
contracts were written before the Phase E implementation. An importable public shell allowed all
tests to execute against missing behavior. Fixtures used only temporary workspaces, generated WebP
previews, direct synthetic SQLite state, and loopback HTTP. No user photo library or network service
was used.

```powershell
rtk python -m pytest -q tests/test_phase_e_review.py
```

```text
15 failed in 1.58s
```

The failures proved the absence of:

- a loopback-only injectable Review server and local assets;
- offline thumbnail service constrained by media identity;
- schema v10 and persistent HUMAN category/disposition rows;
- `HUMAN > AI > RULE` effective-decision queries and AI-rerun protection;
- stable duplicate/burst member views and Best Shot output;
- bounded API pagination for 1000+ items;
- CSRF and optimistic-concurrency request boundaries;
- PRD filters and the `spt review` CLI command.

All 15 failures reached `NotImplementedError`, the v9 migration ledger, or the absent CLI command.
There was no collection, syntax, dependency, or meaningless-assertion failure.

## Independent review remediation RED

The Phase E review found that historical and current grouping algorithm rows could coexist and
that SQLite type affinity allowed malformed persisted values to reach Python conversion code.
Meaningful synthetic regression tests reproduced both defects before remediation:

```powershell
rtk python -m pytest -q tests/test_phase_e_review.py -k "old_algorithm or malformed_database"
```

```text
5 failed, 36 deselected in 0.86s
```

The failures proved that cross-version `MIN(group_id)` could omit a member of the current group,
and that malformed revision, confidence, tags JSON, and quality JSON did not become a fixed local
state error. A separate POST regression then reproduced the remaining write-path boundary:

```powershell
rtk python -m pytest -q tests/test_phase_e_review.py -k "post_with_malformed_persisted_revision"
```

```text
1 failed, 41 deselected in 1.22s
```

That request ended with `RemoteDisconnected` because a malformed persisted revision raised an
uncaught `ValueError` in the server thread. Both RED runs used only temporary SQLite workspaces and
loopback HTTP.
