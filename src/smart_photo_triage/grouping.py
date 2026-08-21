"""Deterministic, candidate-bounded exact-duplicate and burst grouping."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict, deque
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from smart_photo_triage.database import connect_database
from smart_photo_triage.preprocess import PERCEPTUAL_HASH_VERSION
from smart_photo_triage.workspace import Workspace

DUPLICATE_ALGORITHM_VERSION = "exact-sha256-v1"
BURST_ALGORITHM_VERSION = "time-dhash-ranked-capped-medoid-v3"
_HASH_CHUNK_SIZE = 1024 * 1024
_MEDOID_CANDIDATE_LIMIT = 12
_DEFAULT_COMPARISON_CAP = 32


@dataclass(frozen=True, slots=True)
class BurstCandidate:
    media_id: int
    captured_at: str
    perceptual_hash: str
    quality_score: float = 0.0
    path_key: str = ""


@dataclass(frozen=True, slots=True)
class BurstCluster:
    group_id: str
    representative_media_id: int
    member_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class BurstGroupingResult:
    clusters: tuple[BurstCluster, ...]
    comparison_count: int
    algorithm_version: str = BURST_ALGORITHM_VERSION
    warnings: tuple[str, ...] = ()

    @property
    def warning_count(self) -> int:
        return len(self.warnings)


@dataclass(frozen=True, slots=True)
class GroupResult:
    duplicate_group_count: int
    burst_group_count: int
    comparison_count: int
    warning_count: int = 0
    warnings: tuple[str, ...] = ()


@dataclass(slots=True)
class _WorkingGroup:
    anchor: BurstCandidate
    anchor_time: float
    members: list[BurstCandidate]
    band_keys: tuple[tuple[int, int], ...]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _candidate_key(candidate: BurstCandidate) -> tuple[float, str, int]:
    return (_captured_seconds(candidate.captured_at), candidate.path_key, candidate.media_id)


def _captured_seconds(value: str) -> float:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    captured = datetime.fromisoformat(normalized)
    if captured.tzinfo is None:
        captured = captured.replace(tzinfo=UTC)
    return captured.timestamp()


def _hash_value(value: str) -> int:
    if len(value) != 16:
        raise ValueError("perceptual hash must contain exactly 16 hexadecimal characters")
    try:
        return int(value, 16)
    except ValueError as error:
        raise ValueError("perceptual hash must be hexadecimal") from error


def hamming_distance(left: str, right: str) -> int:
    """Return the bit distance between canonical 64-bit perceptual hashes."""
    return (_hash_value(left) ^ _hash_value(right)).bit_count()


def _band_keys(value: str, band_count: int) -> tuple[tuple[int, int], ...]:
    bits = _hash_value(value)
    quotient, remainder = divmod(64, band_count)
    keys: list[tuple[int, int]] = []
    offset = 0
    for band in range(band_count):
        width = quotient + int(band < remainder)
        mask = (1 << width) - 1
        keys.append((band, (bits >> offset) & mask))
        offset += width
    return tuple(keys)


def _bounded_medoid(
    members: list[BurstCandidate],
) -> tuple[BurstCandidate, int]:
    ordered = sorted(members, key=_candidate_key)
    if len(ordered) <= _MEDOID_CANDIDATE_LIMIT:
        possible = ordered
    else:
        last = len(ordered) - 1
        indexes = {
            round(slot * last / (_MEDOID_CANDIDATE_LIMIT - 1))
            for slot in range(_MEDOID_CANDIDATE_LIMIT)
        }
        possible = [ordered[index] for index in sorted(indexes)]
    comparisons = 0
    scores: list[tuple[int, float, tuple[float, str, int], BurstCandidate]] = []
    for candidate in possible:
        total_distance = 0
        for member in ordered:
            total_distance += hamming_distance(candidate.perceptual_hash, member.perceptual_hash)
            comparisons += 1
        scores.append(
            (
                total_distance,
                -candidate.quality_score,
                _candidate_key(candidate),
                candidate,
            )
        )
    return min(scores, key=lambda score: score[:3])[3], comparisons


def _cluster_id(
    members: list[BurstCandidate],
    *,
    time_window_seconds: float,
    distance_threshold: int,
    comparison_cap: int,
) -> str:
    payload = "\0".join(
        (
            BURST_ALGORITHM_VERSION,
            f"{time_window_seconds:.6f}",
            str(distance_threshold),
            str(comparison_cap),
            *(str(candidate.media_id) for candidate in sorted(members, key=_candidate_key)),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def group_burst_candidates(
    candidates: list[BurstCandidate],
    *,
    time_window_seconds: float = 3.0,
    distance_threshold: int = 8,
    comparison_cap: int = _DEFAULT_COMPARISON_CAP,
) -> BurstGroupingResult:
    """Group against fixed anchors so similarity never expands transitively."""
    if time_window_seconds <= 0:
        raise ValueError("burst time_window_seconds must be positive")
    if not 0 <= distance_threshold < 64:
        raise ValueError("burst distance_threshold must be between 0 and 63")
    if comparison_cap <= _MEDOID_CANDIDATE_LIMIT:
        raise ValueError(f"burst comparison_cap must exceed {_MEDOID_CANDIDATE_LIMIT}")
    anchor_comparison_cap = comparison_cap - _MEDOID_CANDIDATE_LIMIT
    ordered = sorted(candidates, key=_candidate_key)
    band_count = max(16, distance_threshold + 1)
    band_index: dict[tuple[int, int], set[int]] = defaultdict(set)
    active_groups: deque[int] = deque()
    groups: list[_WorkingGroup] = []
    comparisons = 0
    warnings: list[str] = []

    for candidate in ordered:
        captured = _captured_seconds(candidate.captured_at)
        while active_groups:
            oldest_index = active_groups[0]
            oldest = groups[oldest_index]
            if captured - oldest.anchor_time <= time_window_seconds:
                break
            active_groups.popleft()
            for key in oldest.band_keys:
                indexes = band_index[key]
                indexes.discard(oldest_index)
                if not indexes:
                    del band_index[key]

        keys = _band_keys(candidate.perceptual_hash, band_count)
        match_counts: dict[int, int] = defaultdict(int)
        for key in keys:
            for group_index in band_index.get(key, ()):
                match_counts[group_index] += 1
        matches: list[tuple[int, tuple[float, str, int], int]] = []
        deterministic_nearby = sorted(
            match_counts,
            key=lambda index: (-match_counts[index], _candidate_key(groups[index].anchor)),
        )[:anchor_comparison_cap]
        if len(match_counts) > anchor_comparison_cap:
            warnings.append(
                f"candidate {candidate.media_id}: ranked {len(match_counts)} anchors, "
                f"evaluated {anchor_comparison_cap} under comparison_cap={comparison_cap}"
            )
        for group_index in deterministic_nearby:
            working = groups[group_index]
            distance = hamming_distance(working.anchor.perceptual_hash, candidate.perceptual_hash)
            comparisons += 1
            if distance <= distance_threshold:
                matches.append((distance, _candidate_key(working.anchor), group_index))
        if matches:
            selected = min(matches)[2]
            groups[selected].members.append(candidate)
            continue

        group_index = len(groups)
        groups.append(_WorkingGroup(candidate, captured, [candidate], keys))
        active_groups.append(group_index)
        for key in keys:
            band_index[key].add(group_index)

    clusters: list[BurstCluster] = []
    for working in groups:
        if len(working.members) < 2:
            continue
        representative, medoid_comparisons = _bounded_medoid(working.members)
        comparisons += medoid_comparisons
        member_ids = tuple(sorted(member.media_id for member in working.members))
        clusters.append(
            BurstCluster(
                group_id=_cluster_id(
                    working.members,
                    time_window_seconds=time_window_seconds,
                    distance_threshold=distance_threshold,
                    comparison_cap=comparison_cap,
                ),
                representative_media_id=representative.media_id,
                member_ids=member_ids,
            )
        )
    return BurstGroupingResult(
        clusters=tuple(sorted(clusters, key=lambda cluster: cluster.group_id)),
        comparison_count=comparisons,
        warnings=tuple(warnings),
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(_HASH_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _duplicate_id(content_sha256: str) -> str:
    payload = f"{DUPLICATE_ALGORITHM_VERSION}\0{content_sha256}"
    return hashlib.sha256(payload.encode("ascii")).hexdigest()[:32]


def group_exact_duplicates(workspace: Workspace) -> int:
    """Hash only same-size candidates and persist groups without file actions."""
    group_count = 0
    with closing(connect_database(workspace.database_path)) as connection:
        connection.execute(
            "DELETE FROM duplicate_group WHERE algorithm_version = ?",
            (DUPLICATE_ALGORITHM_VERSION,),
        )
        candidate_sizes = connection.execute(
            """
            SELECT size_bytes
            FROM media_item
            WHERE source_present = 1 AND media_type IN ('IMAGE', 'VIDEO')
            GROUP BY size_bytes HAVING COUNT(*) > 1
            ORDER BY size_bytes
            """
        ).fetchall()
        for (size_bytes,) in candidate_sizes:
            rows = connection.execute(
                """
                SELECT id, original_path, content_sha256
                FROM media_item
                WHERE source_present = 1 AND media_type IN ('IMAGE', 'VIDEO')
                  AND size_bytes = ?
                ORDER BY id
                """,
                (int(size_bytes),),
            )
            by_hash: dict[str, list[int]] = defaultdict(list)
            for media_id, original_path, existing_hash in rows:
                try:
                    content_hash = (
                        str(existing_hash)
                        if existing_hash is not None
                        else _sha256(Path(str(original_path)))
                    )
                except OSError:
                    continue
                if existing_hash is None:
                    connection.execute(
                        "UPDATE media_item SET content_sha256 = ? WHERE id = ?",
                        (content_hash, int(media_id)),
                    )
                by_hash[content_hash].append(int(media_id))
            for content_hash, media_ids in sorted(by_hash.items()):
                if len(media_ids) < 2:
                    continue
                group_id = _duplicate_id(content_hash)
                connection.execute(
                    """
                    INSERT INTO duplicate_group(
                        id, algorithm_version, content_sha256, size_bytes, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        group_id,
                        DUPLICATE_ALGORITHM_VERSION,
                        content_hash,
                        int(size_bytes),
                        _utc_now(),
                    ),
                )
                connection.executemany(
                    "INSERT INTO duplicate_member(group_id, media_id) VALUES (?, ?)",
                    ((group_id, media_id) for media_id in sorted(media_ids)),
                )
                group_count += 1
        connection.commit()
    return group_count


def _group_bursts_detailed(
    workspace: Workspace,
    *,
    time_window_seconds: float = 3.0,
    distance_threshold: int = 8,
    comparison_cap: int = _DEFAULT_COMPARISON_CAP,
) -> tuple[int, BurstGroupingResult]:
    """Persist deterministic burst groups, medoids, and advisory best-shot flags."""
    with closing(connect_database(workspace.database_path)) as connection:
        rows = connection.execute(
            """
            SELECT m.id, m.captured_at, p.perceptual_hash,
                   COALESCE(p.quality_score, 0.0), m.path_key
            FROM media_item AS m
            JOIN media_preprocess AS p ON p.media_id = m.id
            WHERE m.source_present = 1 AND m.media_type = 'IMAGE'
              AND m.captured_at IS NOT NULL
              AND p.preview_status = 'READY'
              AND p.perceptual_hash_version = ?
              AND p.perceptual_hash IS NOT NULL
            ORDER BY m.captured_at, m.path_key, m.id
            """,
            (PERCEPTUAL_HASH_VERSION,),
        )
        candidate_by_id = {
            int(row[0]): BurstCandidate(
                media_id=int(row[0]),
                captured_at=str(row[1]),
                perceptual_hash=str(row[2]),
                quality_score=float(row[3]),
                path_key=str(row[4]),
            )
            for row in rows
        }
        result = group_burst_candidates(
            list(candidate_by_id.values()),
            time_window_seconds=time_window_seconds,
            distance_threshold=distance_threshold,
            comparison_cap=comparison_cap,
        )
        connection.execute(
            """
            INSERT INTO grouping_run(
                id, algorithm_version, time_window_seconds, distance_threshold,
                comparison_cap, comparison_count, warning_count, warning_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid4().hex,
                result.algorithm_version,
                time_window_seconds,
                distance_threshold,
                comparison_cap,
                result.comparison_count,
                result.warning_count,
                json.dumps(result.warnings, separators=(",", ":")),
                _utc_now(),
            ),
        )
        connection.execute(
            "DELETE FROM burst_group WHERE algorithm_version = ?",
            (BURST_ALGORITHM_VERSION,),
        )
        for cluster in result.clusters:
            representative = candidate_by_id[cluster.representative_media_id]
            members = [candidate_by_id[media_id] for media_id in cluster.member_ids]
            best_shot = min(
                members,
                key=lambda member: (
                    -member.quality_score,
                    hamming_distance(member.perceptual_hash, representative.perceptual_hash),
                    _candidate_key(member),
                ),
            )
            connection.execute(
                """
                INSERT INTO burst_group(
                    id, algorithm_version, representative_media_id,
                    time_window_seconds, distance_threshold, comparison_cap, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cluster.group_id,
                    BURST_ALGORITHM_VERSION,
                    representative.media_id,
                    time_window_seconds,
                    distance_threshold,
                    comparison_cap,
                    _utc_now(),
                ),
            )
            connection.executemany(
                """
                INSERT INTO burst_member(
                    group_id, media_id, distance, quality_score,
                    is_representative, is_best_shot
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        cluster.group_id,
                        member.media_id,
                        hamming_distance(member.perceptual_hash, representative.perceptual_hash),
                        member.quality_score,
                        int(member.media_id == representative.media_id),
                        int(member.media_id == best_shot.media_id),
                    )
                    for member in members
                ),
            )
        connection.commit()
    return len(result.clusters), result


def group_bursts(
    workspace: Workspace,
    *,
    time_window_seconds: float = 3.0,
    distance_threshold: int = 8,
    comparison_cap: int = _DEFAULT_COMPARISON_CAP,
) -> tuple[int, int]:
    """Persist bursts and return the retained two-field public summary."""
    group_count, result = _group_bursts_detailed(
        workspace,
        time_window_seconds=time_window_seconds,
        distance_threshold=distance_threshold,
        comparison_cap=comparison_cap,
    )
    return group_count, result.comparison_count


def group_workspace(
    workspace: Workspace,
    *,
    time_window_seconds: float = 3.0,
    distance_threshold: int = 8,
    comparison_cap: int = _DEFAULT_COMPARISON_CAP,
) -> GroupResult:
    """Run both Phase C grouping stages through their only operational path."""
    duplicate_group_count = group_exact_duplicates(workspace)
    burst_group_count, burst_result = _group_bursts_detailed(
        workspace,
        time_window_seconds=time_window_seconds,
        distance_threshold=distance_threshold,
        comparison_cap=comparison_cap,
    )
    return GroupResult(
        duplicate_group_count,
        burst_group_count,
        burst_result.comparison_count,
        burst_result.warning_count,
        burst_result.warnings,
    )
