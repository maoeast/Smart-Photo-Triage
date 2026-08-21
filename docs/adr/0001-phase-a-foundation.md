# ADR 0001: Dependency-free Phase A runtime and SQLite migration ledger

- Status: Accepted
- Date: 2026-08-20
- Amended: 2026-08-21 after independent Phase A lock reviews

## Context

Phase A must provide an installable CLI, privacy-safe configuration, idempotent workspace creation,
and versioned SQLite migrations without implementing photo-domain behavior.

## Decision

Use the Python 3.11 standard library for the Phase A runtime: `argparse`, `pathlib`, `tomllib`, and
`sqlite3`. Store the schema level in `PRAGMA user_version` and keep an auditable
`schema_migration` ledger.

Run validation and all pending migrations inside one explicit `BEGIN IMMEDIATE` transaction. This
serializes concurrent initialization and makes migration DDL, ledger rows, and `user_version`
atomic. Validate every expected `(version, name)` ledger pair plus the exact critical table columns,
primary keys, migration-name uniqueness constraint, and singleton workspace UUID before commit.
The `commit()` call remains inside the rollback-protected region, so a commit exception rolls the
connection back and leaves it reusable for a deterministic retry.

Each new workspace receives a random UUID. Migration v1 stores it as the singleton
`workspace_metadata.workspace_id`, while `.spt-workspace` stores the same value in canonical ASCII
JSON. Every initialization validates both identities inside the migration transaction. A retained
marker therefore cannot authorize a different otherwise-valid SPT database.

Publish the marker with a unique same-directory temporary file, `flush`, `fsync`, and `os.replace`,
then sync the parent directory where supported. An atomic short-lived initialization-lock directory
selects the UUID writer. SQLite's `BEGIN IMMEDIATE` serializes concurrent database initialization
after marker publication. Existing markerless databases are rejected without modification. Phase A
has not shipped, so this avoids making an unprovable legacy-origin claim.

The initialization lock records ownership in a unique token-derived marker and captures the lock
directory's filesystem identity. The expected marker path is established before opening it. If
owner-marker creation, write, or close fails, cleanup may remove that owner's partial unique marker
only while the directory identity still matches; ordinary release additionally validates the full
token content. An old owner therefore cannot delete a replacement lock directory.

When a redirected output encoding cannot represent a workspace path, render unrepresentable
characters as standard backslash escapes instead of failing a successful initialization.

Use Ruff, pytest, and coverage as development-only dependencies. Build the package with setuptools
and expose `spt` as a console script.

## Consequences

- A clean runtime install has no application dependency tree.
- Migration order is explicit, atomic, retryable, and repeatable on Windows and Linux.
- Existing workspace files are preserved during repeated initialization.
- Unknown databases named `spt.sqlite3` are rejected rather than implicitly altered.
- A standard-library marker-selection lock plus SQLite's write lock safely serializes concurrent
  `init` calls without an additional package.
- Marker contents are never published partially, and database replacement is detected by identity.
- Markerless databases, including schema-compatible ones, require manual inspection rather than
  automatic adoption.
- Later phases may add light dependencies when their behavior requires them. Such additions must be
  justified against the PRD dependency boundary.
