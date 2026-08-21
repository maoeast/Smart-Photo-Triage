# Phase C GREEN evidence

Recorded on 2026-08-21 on Windows with Python 3.13.2.

All fixtures are Pillow-generated, FFmpeg-generated, synthetic byte streams, injected backends, or
temporary SQLite/filesystem state. No real photo library was used.

## Independent-review remediation final gate

The earlier results below are retained as development history. The final post-review results are:

```powershell
rtk python -m pytest -q tests/test_phase_c_review_remediation.py
rtk python -m pytest -q tests/test_phase_c_preview.py tests/test_phase_c_grouping.py tests/test_phase_c_review_remediation.py
```

```text
13 passed in 1.56s
42 passed in 2.99s
```

The 13 remediation contracts cover nonblocking dual-pipe subprocess draining, independent stdout
and stderr hard caps, timeout and kill/wait/close cleanup, actual SHA-256 cache identity despite
unchanged path/size/mtime, pre/post-generation identity consistency and staged publication, a
1,000-item shared-band burst adversary under a configurable strict linear budget, final
contact-sheet bounds down to one pixel, and stable CLI failures without traceback.

```text
PHASE_C_1K_SMOKE items=1000 elapsed=9.433s items_per_second=106.0
peak_mib=3.17 sqlite_mib=3.82 cache_hits=1000 duplicate_groups=256
burst_groups=1 failed=0 comparisons=12999
2 passed in 10.54s

124 passed in 32.66s
124 passed in 46.30s
89.57% combined line and branch coverage
34 files already formatted
All checks passed
```

The generated FFmpeg and registered-codec HEIC integrations were also rerun together: `2 passed in
0.50s`.

## Second independent-review remediation final gate

```powershell
rtk python -m pytest -q tests/test_phase_c_second_review.py
rtk python -m pytest -q
rtk python -m pytest --cov=smart_photo_triage --cov-report=term-missing --cov-fail-under=85 -q
```

```text
6 passed, 2 skipped in 0.85s
130 passed, 2 skipped in 40.70s
130 passed, 2 skipped in 57.70s
89.01% combined line and branch coverage
```

The two skips are real leaf-symlink fixtures that this Windows account lacks permission to create.
The real Windows parent-junction swap executed and passed. The retained POSIX path runs both leaf
symlink cases. The new GREEN contracts also cover scanner-owned stat preservation followed by a
dimension-refreshing rescan, stable-handle workspace snapshots for both media types, path/content
revalidation, 4096 edge and 64 Mi-pixel decode limits, similarity-ranked burst candidates, and
returned plus persisted truncation warnings.

```text
PHASE_C_1K_SMOKE items=1000 elapsed=15.329s items_per_second=65.2
peak_mib=3.43 sqlite_mib=3.83 cache_hits=1000 duplicate_groups=256
burst_groups=1 failed=0 comparisons=12999
2 passed in 16.31s

generated FFmpeg + registered HEIC: 2 passed in 0.50s
isolated base: Pillow-12.3.0, no pillow_heif and no .heic registration
isolated [heic]: generated 24x16 HEIF -> WebP succeeded
35 files already formatted
All checks passed
```

## Third independent-review remediation final gate

```text
third-review targeted: 4 passed, 1 skipped in 0.45s
all Phase C targeted: 52 passed, 3 skipped in 4.77s
full: 134 passed, 3 skipped in 43.47s
coverage: 134 passed, 3 skipped in 61.64s, 86.04%
ruff: 36 files already formatted, All checks passed

PHASE_C_1K_SMOKE items=1000 elapsed=17.761s items_per_second=56.3
peak_mib=3.85 sqlite_mib=3.82 cache_hits=1000 duplicate_groups=256
burst_groups=1 failed=0 comparisons=12999
2 passed in 18.78s

generated FFmpeg + registered HEIC: 2 passed in 0.50s
```

The populated original-v5 fixture upgraded to v6 with UUID and all Phase C rows intact. Windows
executed the real destination-junction swap and proved no canonical preview escaped the workspace.
The POSIX ancestor openat race remains collected for POSIX CI. KeyboardInterrupt cleanup and
dead-owner versus active-owner run-directory retention are also GREEN.

## Fourth independent-review remediation final gate

```text
fourth-review targeted: 3 passed in 0.37s
all Phase C targeted: 55 passed, 3 skipped in 5.37s
full: 137 passed, 3 skipped in 55.87s
coverage: 137 passed, 3 skipped in 84.84s, 85.12%
ruff: 37 files already formatted, All checks passed

PHASE_C_1K_SMOKE items=1000 elapsed=33.827s items_per_second=29.6
peak_mib=4.32 sqlite_mib=3.82 cache_hits=1000 duplicate_groups=256
burst_groups=1 failed=0 comparisons=12999
2 passed in 35.08s

generated FFmpeg + registered HEIC: 2 passed in 0.51s
```

Both real Windows state-ancestor junction cases now fail before any external snapshot/candidate is
created. The previews ancestor is explicitly pinned from workspace root through the hash directory.
POSIX retains root-relative directory FDs for snapshot decoding and publication; its conditional
ancestor-swap tests remain collected for POSIX CI.

## Fifth independent-review remediation final gate

```text
fifth-review targeted: 4 passed in 0.46s
all Phase C targeted: 59 passed, 3 skipped in 5.50s
full: 141 passed, 3 skipped in 59.70s
coverage: 141 passed, 3 skipped in 88.98s, 87.20%
ruff: 38 files already formatted, All checks passed

PHASE_C_1K_SMOKE items=1000 elapsed=35.658s items_per_second=28.0
peak_mib=4.40 sqlite_mib=3.83 cache_hits=1000 duplicate_groups=256
burst_groups=1 failed=0 comparisons=12999
2 passed in 37.00s

generated FFmpeg + registered HEIC: 2 passed in 0.53s
```

The real Windows external-artifact junction is no longer a cache hit and cannot be republished
through the hostile preview tree. Partial acquisition and owner-write faults release all handles,
and a hostile child junction is removed without touching its external sentinel.

## Sixth independent-review remediation final gate

```text
Phase C targeted: 62 passed, 5 skipped in 6.23s
full regression: 144 passed, 5 skipped in 68.43s
coverage: 87.26%, 144 passed, 5 skipped in 91.83s
PHASE_C_1K_SMOKE items=1000 elapsed=39.219s items_per_second=25.5
peak_mib=6.04 sqlite_mib=3.91 cache_hits=1000 failed=0 comparisons=12999
generated FFmpeg + registered HEIC: 2 passed in 0.52s
39 files already formatted
All checks passed
```

The original v6 fixture upgrades additively to v7 with all populated rows intact. READY now stores
the generated artifact SHA-256 in the same database write as its status and recomputes that digest
through a secure no-follow leaf handle before reuse. Same-path, same-dimension valid WebP replacement
therefore misses cache. Candidate unlink faults no longer prevent item-context exit. POSIX deletion
binds stat to opened directory identity; Windows deletion binds the selected child by atomic rename
before recursively inspecting it. The Windows leaf-symlink case is collected but capability-skipped
on this host because creating a file symlink returned WinError 1314.

## Seventh independent-review remediation final gate

```text
Phase C targeted: 63 passed, 7 skipped in 6.57s
full regression: 145 passed, 7 skipped in 69.78s
coverage: 87.17%, 145 passed, 7 skipped in 98.07s
PHASE_C_1K_SMOKE items=1000 elapsed=43.117s items_per_second=23.2
peak_mib=6.14 sqlite_mib=3.92 cache_hits=1000 failed=0 comparisons=12999
generated FFmpeg + registered HEIC: 2 passed in 0.50s
40 files already formatted
All checks passed
```

The publication hook now proves a post-hash candidate replacement cannot become READY. Canonical
verification securely reopens the published leaf, pins Windows deletion sharing, verifies stable
regular-file identity and dimensions, and computes the authoritative READY digest. Any mismatch
removes the canonical object and records FAILED with empty path/hash. POSIX cleanup now performs
same-parent `renameat` quarantine before identity checks for files and directories; its deterministic
swap fixtures remain collected for POSIX CI.

## Eighth independent-review remediation final gate

```text
Phase C targeted: 68 passed, 7 skipped in 7.84s
full regression: 150 passed, 7 skipped in 88.10s
coverage: 87.41%, 150 passed, 7 skipped in 119.40s
PHASE_C_1K_SMOKE items=1000 elapsed=58.207s items_per_second=17.2
peak_mib=6.32 sqlite_mib=3.92 cache_hits=1000 failed=0 comparisons=12999
generated FFmpeg + registered HEIC: 2 passed in 0.51s
41 files already formatted
All checks passed
```

Five new GREEN contracts cover one generation for two equal-content media rows, two concurrent SPT
writers across the post-verify/commit hook, proven-dead owner recovery, conservative bounded busy for
invalid/unknown owner metadata, and external leaf replacement without deleting the other artifact.
The 1k fixture contains 256 unique byte streams, so its first pass now records 256 generated previews
plus 744 same-fingerprint shared READY hits; its second pass remains 1000 cache hits. No
pre-implementation RED execution was captured for this review, as recorded in the RED evidence file.

## Ninth independent-review remediation final gate

```text
Phase C plus CLI targeted: 78 passed, 7 skipped in 9.20s
full regression: 154 passed, 7 skipped in 87.75s
coverage: 87.38%, 154 passed, 7 skipped in 118.54s
PHASE_C_1K_SMOKE items=1000 elapsed=58.497s items_per_second=17.1
peak_mib=6.32 sqlite_mib=3.92 cache_hits=1000 failed=0 comparisons=12999
generated FFmpeg + registered HEIC: 2 passed in 0.52s
42 files already formatted
All checks passed
```

The three executed RED contracts and one CLI reporting contract are GREEN. A short injected test
budget simulates an operation longer than the former threshold without sleeping for more than one
second: the second writer waits and then cache-hits. Explicit budget exhaustion remains diagnostic
and non-destructive: `deferred_count=1`, `failed_count=0`, no media preprocess row is written, and the
run status persists `COMPLETE_WITH_DEFERRED`.

## Tenth independent-review remediation final gate

```text
Tenth-review targeted: 12 passed in 1.03s
full regression: 166 passed, 7 skipped in 88.40s
coverage: 87.29%, 166 passed, 7 skipped in 119.18s
PHASE_C_1K_SMOKE items=1000 elapsed=64.360s items_per_second=15.5
peak_mib=6.32 sqlite_mib=3.92 cache_hits=1000 failed=0 comparisons=12999
generated FFmpeg + registered HEIC: 2 passed in 0.54s
43 files already formatted
All checks passed
```

The default API path now supplies no deadline to the publication lock. A valid live owner outlives
the former injected budget, completes, and the contender cache-hits. Malformed owner metadata
returns immediately as a recorded defer without reclaiming the lock. Explicit positive finite
timeouts remain available through `--lock-wait-seconds`; zero, negative, NaN, and infinity are
rejected. Schema v8 appends only `preprocess_run.deferred_count`, upgrades a populated original-v7
database with prior counters intact, persists each defer while the run is still active, and stores
the exact count under deferred-only and combined terminal states.

The final two type-contract tests are GREEN after correcting annotations only. Runtime behavior did
not change. A focused review of `preprocess.py` found the remaining explicit bytes, tuple,
`QualityMetrics`, `PreviewArtifact`, `_SourceIdentity`, string, and `PreprocessResult` return sites
consistent with their annotations.

## T-C-001 through T-C-017 and supporting contracts

```powershell
rtk python -m pytest -q tests/test_phase_c_preview.py tests/test_phase_c_grouping.py tests/test_phase_c_smoke.py -k "not 1k"
```

```text
30 passed, 1 deselected in 1.55s
```

This covers EXIF orientation, injected and registered-codec HEIC, deterministic version/config
fingerprints, image/video corrupt isolation, cache/resume/retry, 3/6/9 interior video sampling,
bounded FFmpeg output, generated-video contact sheets, same-size/SHA duplicate groups, zero file
actions, burst positive/negative cases, false-chain prevention, ordering determinism, complexity,
algorithm versions, deterministic bounded medoids, best-shot advice, additive v5 upgrade, and the
minimal preprocess/group CLI.

## Real HEIC codec integration

The base Pillow 11.2.1 environment initially had no `.heic` registration. After explicitly
installing the project `heic` extra, `pillow-heif` 1.5.0 registered `.heic` as `HEIF`.

```powershell
rtk python -m pytest -q tests/test_phase_c_preview.py -k heic
```

```text
2 passed, 15 deselected in 0.21s
```

One test exercises the decoder-independent injected HEIC contract. The other encodes a generated
HEIF image and decodes it through the registered codec before writing the preview.

The installed binary wheel's `LICENSES_bundled.txt` says the wheel is GPLv2 because it includes
x265, and lists libheif/libde265 as LGPLv3. The dependency therefore remains an explicit `heic`
extra, not a silent base dependency.

## Isolated clean-install smokes

Two fresh Python 3.13 virtual environments were created outside the repository and removed after
verification.

Base install:

```powershell
python -m pip install .
python -m smart_photo_triage --help
python -m smart_photo_triage init --workspace <temp-workspace>
python -m smart_photo_triage preprocess --workspace <temp-workspace>
python -m smart_photo_triage group --workspace <temp-workspace>
```

```text
Successfully installed Pillow-12.3.0 smart-photo-triage-0.1.0
pillow_heif_spec None
heic_registration None
Preprocess complete: processed=0 cache_hits=0 failed=0
Grouping complete: duplicates=0 bursts=0 comparisons=0
```

This proves the base wheel does not silently install the GPLv2-labeled HEIC binary wheel.

HEIC extra install:

```powershell
python -m pip install ".[heic]"
```

```text
Successfully installed Pillow-12.3.0 pillow-heif-1.5.0 smart-photo-triage-0.1.0
heic_format HEIF
preview True 24 16 0000000000000000
```

The smoke encoded a 24 by 16 synthetic HEIF and generated a real WebP preview. All temporary
virtual environments, workspaces, and synthetic artifacts were deleted after their resolved paths
were verified to remain under the explicit Windows temp root.

## Historical cache defect found by the first smoke

The first implemented 1k run generated all previews successfully but the second preprocess pass
reported 1,000 misses. Exact grouping had filled `media_item.content_sha256`; because the first
source signature included this optional field, a metadata enrichment looked like a source change.

That first fix was superseded by independent review. The final implementation streams actual
SHA-256 before every cache decision and repeats source stat/hash identity after generation.

## 1k generated-media smoke

```powershell
rtk python -m pytest -q -s tests/test_phase_c_smoke.py -k 1k
```

```text
PHASE_C_1K_SMOKE items=1000 elapsed=6.660s items_per_second=150.2
peak_mib=4.70 sqlite_mib=3.84 cache_hits=1000 duplicate_groups=256
burst_groups=1 failed=0 comparisons=17999
1 passed, 1 deselected in 7.46s
```

The comparison count includes fixed-anchor candidate checks and bounded-medoid work. It remains
below the test's 32,000 ceiling and far below an all-library pair count. Peak memory is Python
allocation peak from `tracemalloc`, not whole-process RSS.

## Full regression

```powershell
rtk proxy python -m pytest -q
```

```text
111 passed in 28.31s
```

This includes the retained Phase B 10k scan smoke and the Phase C 1k smoke.

## Coverage

```powershell
rtk proxy python -m pytest --cov=smart_photo_triage --cov-report=term-missing -q
```

```text
111 passed in 38.95s
90.12% combined line and branch coverage
```

The configured overall threshold is 85%. Phase C grouping reached 95% and preprocessing reached
86% in the recorded report.

## Static checks

```powershell
rtk python -m ruff format --check .
rtk python -m ruff check .
```

```text
31 files already formatted
All checks passed
```
