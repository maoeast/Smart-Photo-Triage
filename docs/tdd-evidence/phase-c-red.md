# Phase C RED evidence

Recorded on 2026-08-21 on Windows with Python 3.13.2.

The Phase C contract shells were intentionally collectable and raised explicit
`NotImplementedError` messages. Tests used only Pillow-generated images, synthetic byte streams,
temporary directories, an injected video backend, and one FFmpeg-generated video. No real photo
library was read or modified.

## T-C-001 through T-C-017 and supporting contracts

```powershell
rtk python -m pytest -q tests/test_phase_c_preview.py tests/test_phase_c_grouping.py tests/test_phase_c_smoke.py -k "not 1k"
```

```text
29 failed, 1 skipped, 1 deselected in 1.40s
```

The failures reached these missing behaviors:

- EXIF-transposed image preview and injected HEIC decoding;
- deterministic, version-sensitive preview fingerprints;
- per-item corrupt image and video isolation;
- missing-preview resume, cache hit, failed-item retry, and version invalidation;
- short, medium, and long video sampling with 3, 6, and 9 interior timestamps;
- bounded single-frame FFmpeg subprocess output and generated-video contact sheet integration;
- same-size candidate SHA-256 exact duplicate grouping without source actions;
- time-window plus perceptual-hash burst grouping, visual/time negative cases, false-chain
  prevention, input-order determinism, and a comparison-count complexity guard;
- persisted algorithm versions and deterministic medoid representation;
- local quality metrics that expose advice but no file action;
- additive v5-or-newer migration from an immutable v4 database;
- minimal `spt preprocess` and `spt group` commands.

The registered Pillow extensions did not include HEIC/HEIF before Phase C dependencies were
installed, so the real codec integration was explicitly skipped. The injected-decoder HEIC core
contract failed at the missing implementation and remained executable without that environment
capability.

## 1k generated-media smoke RED

```powershell
rtk python -m pytest -q tests/test_phase_c_smoke.py -k 1k
```

```text
1 failed, 1 deselected in 2.12s
```

The test generated 1,000 small PNG files under `tmp_path`, completed the existing streaming scan,
then reached the explicit missing preprocess contract. Its GREEN contract requires 1,000 previews,
zero item failures, a full second-pass cache hit, bounded traced Python memory, a non-quadratic
comparison count, SQLite/group counts, and a printed throughput report.

## Independent-review remediation RED

Recorded on 2026-08-21 after the first Phase C GREEN and before remediation.

```powershell
rtk python -m pytest -q tests/test_phase_c_review_remediation.py
```

```text
11 failed, 2 passed in 0.67s
```

The failures proved five separate gaps:

- the media runner had no configurable timeout or stderr cap and synchronously read stdout before
  waiting, so a blocked stdout reader could hang permanently;
- the runner did not prove kill, wait, and two-pipe cleanup on stdout/stderr overflow or timeout;
- preview cache identity trusted the scan-time path/size/mtime record, so a same-path, same-size,
  same-mtime byte replacement incorrectly returned a cache hit;
- source mutation during decode published a READY artifact based on stale bytes instead of failing
  and removing it;
- burst locality bands could expose an unbounded number of candidate groups per item and had no
  configurable strict comparison cap;
- video contact sheets did not apply `max_edge` to the final assembled sheet, producing a 96-pixel
  edge even when configured for 1, 8, 16, or 31 pixels.

The two passing tests proved that the existing Phase C CLI already returned stable exit code 2 and
no traceback for injected preprocess SQLite errors and group validation errors. They remain as
retained regression coverage.

## Second independent-review RED

Recorded on 2026-08-21 after the first remediation and before the second remediation.

```powershell
rtk python -m pytest -q tests/test_phase_c_second_review.py
```

```text
5 failed, 2 skipped in 0.64s
```

The failures proved that preprocessing overwrote scanner-owned size/mtime, followed a real Windows
parent junction swapped in after scan, accepted `max_edge=4097`, attempted to load a synthetic
72-million-pixel BMP header instead of rejecting it at the pixel gate, and truncated a true near
burst anchor behind 20 shared-band far candidates. The two leaf-symlink variants were collected but
skipped because this Windows account cannot create file symlinks; they remain executable on hosts
with symlink capability and on POSIX.

## Third independent-review RED

Recorded on 2026-08-21 before restoring the published v5 contract and hardening directory races.

```powershell
rtk python -m pytest -q tests/test_phase_c_third_review.py
```

```text
4 failed, 1 skipped in 0.52s
```

The failures proved that post-publication grouping audit fields had been incorrectly folded into
v5, a real Windows preview-directory junction swap could publish a canonical preview outside the
workspace, `KeyboardInterrupt` could leave a complete candidate behind, and dead-owner run state
was never reclaimed. The skipped test is a POSIX-only deterministic ancestor-directory symlink
race, retained for Linux/macOS execution.

## Fourth independent-review RED

Recorded on 2026-08-21 before workspace-tree unification.

```text
tests/test_phase_c_fourth_review.py: 2 failed, 1 passed in 0.62s
```

Real Windows junction fixtures proved that `state` and `state/preprocess-runs` could redirect full
snapshots/candidates outside the workspace. The previews-ancestor race was already indirectly
blocked by the pinned leaf handle, but remained as a structural regression requiring explicit
root-to-leaf ancestor pinning.

## Fifth independent-review RED

Recorded on 2026-08-21 before secure cache validation and no-follow deletion unification.

```text
tests/test_phase_c_fifth_review.py: 1 failed, 3 passed in 0.60s
```

The failure proved that READY cache reuse followed a `previews` junction and accepted an external
same-named artifact. The retained passing contracts covered partial setup faults and the current
platform's junction cleanup behavior; implementation was still restructured to guarantee those
properties rather than rely on object finalization or `shutil` behavior.

## Sixth independent-review RED

Recorded on 2026-08-21 before artifact identity persistence and deletion identity binding.

```text
tests/test_phase_c_sixth_review.py: 3 failed, 1 skipped in 0.49s
```

The failures proved that schema v7 did not exist, a valid same-dimension WebP replacement could not
be distinguished from the originally published preview, and a candidate unlink error escaped before
the item security context closed. The skipped contract deterministically swaps a POSIX directory
between no-follow stat and open; Windows cannot execute that dir-FD fixture.

## Seventh independent-review RED

Recorded on 2026-08-21 before post-publication canonical verification and POSIX quarantine binding.

```text
tests/test_phase_c_seventh_review.py: 1 failed, 2 skipped in 0.49s
```

The executed failure replaced a generated candidate with another valid same-dimension WebP inside
the existing publication hook, after candidate hashing but before `os.replace`. The old path still
reported one processed READY item. The two skipped tests collect POSIX-only leaf and directory swaps
at `renameat`; this Windows host cannot exercise dir-FD cleanup.

## Eighth independent-review gap record

No pre-implementation RED command output was preserved for the eighth review. Implementation began
before the new concurrency tests were executed, and their first recorded run was GREEN. This is an
evidence gap and is intentionally not represented as an observed RED count. The review gap concerned
missing cross-process per-fingerprint serialization, owner-liveness rules, and the post-verify to
READY-commit race.

## Ninth independent-review RED

Recorded on 2026-08-21 before operation-budget-aware waiting and deferred reporting.

```text
tests/test_phase_c_ninth_review.py: 3 failed in 1.37s
```

The failures proved there was no computed FFmpeg-sized wait budget, no injectable
`lock_wait_timeout`, and therefore no way for the simulated healthy long-running writer contract to
start. The old fixed five-second timeout also classified budget exhaustion as a failed media item and
did not expose a deferred count.

## Tenth independent-review RED

Recorded on 2026-08-21 before removing the derived default deadline and adding schema v8.

```powershell
rtk python -m pytest -q tests/test_phase_c_tenth_review.py::test_default_known_live_owner_has_no_derived_deadline tests/test_phase_c_tenth_review.py::test_populated_v7_upgrades_to_v8_with_deferred_count tests/test_phase_c_tenth_review.py::test_explicit_timeout_persists_exact_deferred_count_for_two_media
```

```text
3 failed in 0.79s
```

The failures proved that a healthy live writer still became deferred when the old derived budget
expired, migrations stopped at v7, and `preprocess_run` had no persisted `deferred_count` column.

The combined tenth-review file was also run before implementation. It produced the first failure,
then remained blocked for more than 60 seconds on malformed owner metadata because the old default
wait was approximately 307 seconds; the command was interrupted with `Ctrl+C`. This is direct
evidence that an unknown owner could hang a default run rather than defer immediately.

```powershell
rtk python -m pytest -q tests/test_phase_c_tenth_review.py::test_cli_passes_explicit_lock_wait_seconds
```

```text
1 failed in 0.34s
```

The CLI rejected the new positive option as unrecognized, proving there was no operator-controlled
timeout path.

## Final Phase C type-contract RED

Recorded on 2026-08-21 before correcting the two inverse return annotations.

```powershell
rtk python -m pytest -q tests/test_phase_c_tenth_review.py::test_preview_fingerprint_return_annotation_matches_runtime tests/test_phase_c_tenth_review.py::test_published_artifact_return_annotation_matches_runtime
```

```text
2 failed in 0.31s
```

Both runtime values were already correct. `typing.get_type_hints` proved that
`preview_fingerprint` incorrectly declared `_ArtifactVerification` instead of `str`, while
`_published_artifact_sha256` incorrectly declared `str` instead of `_ArtifactVerification`.
