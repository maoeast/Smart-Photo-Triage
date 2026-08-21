# Smart-Photo-Triage v1.2.1 Release Evidence

Status date: 2026-08-22. All automated development data was synthetic. No real photo, video,
family data, credential, or external provider request was used.

## Baseline and migration

- Full local regression: `525 passed, 7 skipped, 0 failed` in 224.965 seconds.
- Coverage run: `525 passed, 7 skipped, 0 failed` in 283.149 seconds, overall combined
  line-and-branch coverage 88%.
- SQLite migration v12 is additive. It creates `provider_analysis_cache`, `ai_route_run`, and
  `ai_route_attempt`; the v11 snapshot test preserved old preprocess/media data and migration is
  idempotent.
- Existing `ai_analysis`, `review_decision`, immutable plans, approvals, operation transactions,
  and operation journals are neither rewritten nor deleted by v12.

## Provider contract

| Driver | Mock contract status | Notes |
| --- | --- | --- |
| `fake` | PASS | Deterministic offline route and E2E provider. |
| `gemini` | PASS | Injected `generateContent` structured-response adapter test. |
| `openai` | PASS | Injected Chat Completions structured-response and HTTP classification tests. |
| `anthropic` | PASS | Injected Messages/Vision structured-response adapter test. |
| `openai_compatible` | PASS | Uses the OpenAI-compatible adapter rather than vendor-specific business logic. |

Capability profiles carry image, multi-image, structured JSON, JSON Schema, MIME, image count,
request byte, system prompt, streaming, and profile-version fields. The Router preflights these
requirements before driver invocation.

## Router, fallback, escalation, and budget

- `ITEM_ANALYSIS` and `BURST_REVIEW` use independent route policies.
- Primary/fallback order is deterministic. Disabled, missing, self-referential and duplicate routes
  are rejected.
- `RATE_LIMIT` retry is bounded by per-provider and total task limits before ordered fallback.
- Timeout, server, network, schema and capability failures may proceed only through configured
  fallbacks. Authentication, billing, configuration, privacy and budget failures do not.
- Low-confidence escalation is at most once; the synthetic E2E verifies a fast result at 0.50
  escalates to the strong provider at 0.95.
- Max requests, remote preview bytes and optional estimated-cost budget are reserved before a
  driver call, so fallback cannot bypass a budget ceiling.

## Privacy P0 matrix

| ID | Evidence | Result |
| --- | --- | --- |
| P0-01 | Existing scanner/preprocess/planner/executor/recovery regression | PASS |
| P0-02 | `test_t_d_network_policy_defaults_closed...` | PASS |
| P0-03 | Network scope gate and LAN classification tests | PASS |
| P0-04 | `test_t_e_local_failure_cannot_silently_use_remote...` | PASS |
| P0-05 | No-redirect transport and remote-only hostname classification | PASS |
| P0-06 | Controlled `VisionRequest` preview-only contract | PASS |
| P0-07 | Existing Phase D path/hash/sidecar privacy tests | PASS |
| P0-08 | Redacted provider summary, audit schema and injected-secret tests | PASS |
| P0-09 | `test_t_e_rate_limit_retries_once...` | PASS |
| P0-10 | `PRIVACY_BLOCKED` is non-fallbackable | PASS |
| P0-11 | Routed E2E retains `review_decision.decision_source=HUMAN` | PASS |
| P0-12 | v11 to v12 additive/idempotent migration test | PASS |
| P0-13 | Provider/model/endpoint/profile cache identity plus SQLite cache test | PASS |
| P0-14 | `test_t_f_remote_byte_and_cost_budget...` | PASS |

## Cache, audit, and E2E

`test_t_h_routed_workspace_e2e_cache_audit_and_human_decision_survive_rerun` uses a synthetic
workspace preview and verifies:

1. fast primary request then strong escalation;
2. Provider cache inserts are provider-specific;
3. second route run makes zero additional provider calls and records cache hits;
4. two route runs and four route attempts are recorded;
5. a HUMAN decision is retained unchanged.

The existing Phase H synthetic lifecycle continues to verify scan, preprocess, grouping, review,
plan, approval, preflight, dry-run, journaled COPY, doctor and rollback. It uses generated fixtures.

## CLI and documentation

`spt ai providers`, `doctor`, `route explain`, `estimate`, and `probe` are zero-network
observability commands. `spt ai run` runs the configured item route. Provider output redacts key
values and shows only whether a key is configured. See `docs/model-routing-v1.2.1.md`.

## External Provider Smoke

| Provider | Status |
| --- | --- |
| Gemini | NOT RUN - no credentials |
| OpenAI | NOT RUN - no credentials |
| Anthropic | NOT RUN - no credentials |
| OpenAI-compatible endpoint | NOT RUN - no credentials |

## Cross-platform CI and final verdict

Implementation commit `9e1d4a6bb497bbf51fa9fb25a8fd371b5d67b210` was pushed to
`codex/v1.2.1-model-routing` and passed hosted CI run
[`32505355983`](https://github.com/maoeast/Smart-Photo-Triage/actions/runs/32505355983).
All four matrix jobs passed: Windows and Ubuntu on Python 3.11 and 3.13. Each job completed
format, lint, full regression, coverage, distribution build, and installed-wheel synthetic E2E.
GitHub emitted only the existing Node.js 20 action deprecation annotation; no job failed.

External-provider smoke remains optional and correctly recorded as not run because no real API
credentials were supplied. The release candidate satisfies the non-external Definition of Done.

**READY_FOR_FINAL_ACCEPTANCE_1_2_1**
