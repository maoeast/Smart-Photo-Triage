# ADR 0008: Phase H local release evidence

Date: 2026-08-21

## Decision

Phase H uses only pytest temporary directories, generated image/video bytes, a deterministic fake
video backend, and `FakeVisionProvider`. The release-candidate local path is exercised twice with
source snapshots, immutable canonical plans, approval/preflight, dry-run, COPY hash verification,
doctor, rollback, and a cache-only second pass. This avoids network, credentials, codecs, ffmpeg,
and personal media while still exercising the operational paths.

The 1k smoke deliberately uses the full read-only lifecycle through dry-run. H2 retains real COPY,
verification, and rollback on a small fixture. This divides performance measurement from mutation
cost without claiming that a 1k apply benchmark ran.

## Consequences

This local evidence does not substitute for the full non-external suite, coverage gates,
Windows/Linux CI, or `docs/release-evidence.md`. No external-provider smoke runs without explicit
user configuration.
