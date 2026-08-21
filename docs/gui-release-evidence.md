# Local GUI release evidence

Date: 2026-08-21
Status: **READY_FOR_FINAL_ACCEPTANCE**

## Scope

`spt gui --workspace <path>` starts a local browser control panel on numeric IPv4 loopback only.
It reuses the production workspace, scanner, preprocessing, grouping, offline Fake AI, review,
planner, preflight, executor, doctor, and rollback modules. It introduces no cloud dependency.

## Safety contract

- The GUI server accepts same-origin, CSRF-token-protected JSON requests only.
- The server binds only to `127.0.0.1`; it does not expose a LAN listener.
- Scan, preparation, plan build, approval, preflight, and dry-run retain the original core behavior.
- The GUI creates COPY plans only. Real apply additionally requires the literal `EXECUTE`.
- Rollback additionally requires the transaction ID and the literal `ROLLBACK`.
- The confirmations do not bypass executor preflight, immutable approvals, journaling, hashes, locks,
  or rollback ownership checks.

## Local evidence

`rtk python -m pytest -q tests/test_gui.py tests/test_phase_a_cli.py tests/test_phase_e_review.py`
passed: **49 passed**.

`tests/test_gui.py` creates generated media only, starts the real loopback GUI server, and runs scan,
offline preparation, plan build, approve, preflight, dry-run, rejected unconfirmed execute, confirmed
COPY, and confirmed rollback through actual HTTP requests. It verifies that copied output is removed
by rollback.

`rtk python -m ruff format --check .; rtk python -m ruff check .` passed.

## Hosted qualification

[GitHub Actions run 32492969931](https://github.com/maoeast/Smart-Photo-Triage/actions/runs/32492969931)
passed on Windows/Linux and Python 3.11/3.13. Each matrix entry completed format, lint, full tests
and coverage, distribution build, fresh wheel install, installed-package synthetic E2E, and artifact
upload successfully.
