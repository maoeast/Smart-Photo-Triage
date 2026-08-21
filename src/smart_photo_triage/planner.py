"""Deterministic immutable organization plans and read-only apply preflight."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import unicodedata
from collections import defaultdict
from collections.abc import Callable, Iterable
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from smart_photo_triage.database import connect_database
from smart_photo_triage.preprocess import PreviewError, _open_source_nofollow
from smart_photo_triage.workspace import Workspace

PLAN_SCHEMA_VERSION = "plan-v1"
PLANNER_VERSION = "planner-v1"
DEFAULT_MAX_PATH_CHARS = 240
_HASH_CHUNK_SIZE = 1024 * 1024
_ALLOWED_MODES = frozenset({"COPY", "MOVE"})
_TERMINAL_TRANSACTION_STATES = frozenset({"DONE", "ROLLED_BACK"})
_RESERVED_WINDOWS_STEMS = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)
_ILLEGAL_WINDOWS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class PlannerError(RuntimeError):
    """Base error for planner state or safe source inspection failures."""


class PlanPolicyError(PlannerError, ValueError):
    """Raised when requested output policy is unsafe or unsupported."""


class PlanSourceError(PlannerError):
    """Raised when indexed source state cannot safely produce a plan."""


class PlanSourceMissingError(PlanSourceError):
    """Raised when a source disappears before or during safe inspection."""


class PlanStateError(PlannerError):
    """Raised when persisted immutable plan state is missing or inconsistent."""


@dataclass(frozen=True, slots=True)
class PlannerOptions:
    output_root: Path
    mode: str = "COPY"
    max_path_chars: int = DEFAULT_MAX_PATH_CHARS
    schema_version: str = PLAN_SCHEMA_VERSION
    planner_version: str = PLANNER_VERSION

    def __post_init__(self) -> None:
        normalized_mode = self.mode.upper() if isinstance(self.mode, str) else ""
        if normalized_mode not in _ALLOWED_MODES:
            raise PlanPolicyError("plan mode must be COPY or MOVE")
        if type(self.max_path_chars) is not int or not 64 <= self.max_path_chars <= 32_767:
            raise PlanPolicyError("max_path_chars must be between 64 and 32767")
        if not isinstance(self.output_root, Path):
            raise PlanPolicyError("output_root must be a pathlib.Path")
        if self.schema_version != PLAN_SCHEMA_VERSION:
            raise PlanPolicyError(f"unsupported plan schema version: {self.schema_version!r}")
        if self.planner_version != PLANNER_VERSION:
            raise PlanPolicyError(f"unsupported planner version: {self.planner_version!r}")
        object.__setattr__(self, "mode", normalized_mode)


@dataclass(frozen=True, slots=True)
class PlanEntry:
    position: int
    media_id: int
    bundle_id: str | None
    bundle_role: str | None
    source_root: str
    source_path: str
    target_path: str
    target_key: str
    action: str
    expected_size: int
    expected_sha256: str
    source_mtime_ns: int
    decision_source: str
    scene_category: str
    disposition: str

    def to_dict(self) -> dict[str, object]:
        return {
            "position": self.position,
            "media_id": self.media_id,
            "bundle_id": self.bundle_id,
            "bundle_role": self.bundle_role,
            "source_root": self.source_root,
            "source_path": self.source_path,
            "target_path": self.target_path,
            "action": self.action,
            "expected_size": self.expected_size,
            "expected_sha256": self.expected_sha256,
            "source_mtime_ns": self.source_mtime_ns,
            "decision_source": self.decision_source,
            "scene_category": self.scene_category,
            "disposition": self.disposition,
        }


@dataclass(frozen=True, slots=True)
class OrganizationPlan:
    plan_id: str
    schema_version: str
    planner_version: str
    created_at: str
    config_fingerprint: str
    source_root_fingerprint: str
    output_root: str
    mode: str
    max_path_chars: int
    payload_sha256: str
    entries: tuple[PlanEntry, ...]
    warnings: tuple[str, ...]
    approval_state: str
    approval_revision: int

    def _payload_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "planner_version": self.planner_version,
            "config_fingerprint": self.config_fingerprint,
            "source_root_fingerprint": self.source_root_fingerprint,
            "output_root": self.output_root,
            "mode": self.mode,
            "max_path_chars": self.max_path_chars,
            "entries": [entry.to_dict() for entry in self.entries],
            "warnings": list(self.warnings),
        }

    def canonical_payload(self) -> bytes:
        return _canonical_json(self._payload_dict()).encode("utf-8")

    def canonical_json(self) -> str:
        return _canonical_json(
            {
                "plan_id": self.plan_id,
                "schema_version": self.schema_version,
                "planner_version": self.planner_version,
                "created_at": self.created_at,
                "config_fingerprint": self.config_fingerprint,
                "source_root_fingerprint": self.source_root_fingerprint,
                "output_root": self.output_root,
                "mode": self.mode,
                "max_path_chars": self.max_path_chars,
                "payload_sha256": self.payload_sha256,
                "entries": [entry.to_dict() for entry in self.entries],
                "warnings": list(self.warnings),
            }
        )

    def to_dict(self) -> dict[str, object]:
        document = json.loads(self.canonical_json())
        document["approval_state"] = self.approval_state
        document["approval_revision"] = self.approval_revision
        return document


@dataclass(frozen=True, slots=True)
class PlanApproval:
    plan_id: str
    state: str
    revision: int
    approved_at: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class PreflightIssue:
    code: str
    severity: str
    message: str
    media_id: int | None = None
    path: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "media_id": self.media_id,
            "path": self.path,
        }


@dataclass(frozen=True, slots=True)
class PreflightReport:
    plan_id: str
    approval_state: str
    approval_revision: int
    approval_contract: str
    payload_sha256: str
    checked_entries: int
    required_bytes: int
    available_bytes: int | None
    issues: tuple[PreflightIssue, ...]

    @property
    def ok(self) -> bool:
        return not any(issue.severity == "ERROR" for issue in self.issues)

    def to_dict(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "approval_state": self.approval_state,
            "approval_revision": self.approval_revision,
            "approval_contract": self.approval_contract,
            "payload_sha256": self.payload_sha256,
            "ok": self.ok,
            "checked_entries": self.checked_entries,
            "required_bytes": self.required_bytes,
            "available_bytes": self.available_bytes,
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass(frozen=True, slots=True)
class _Candidate:
    media_id: int
    source_root: Path
    source_path: Path
    media_type: str
    extension: str
    indexed_size: int
    indexed_sha256: str | None
    captured_at: str | None
    capture_source: str
    capture_confidence: str
    bundle_id: str | None
    bundle_role: str | None
    category: str
    disposition: str
    decision_source: str
    short_desc: str


FaultInjector = Callable[[str], None]


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _windows_key(value: str | Path) -> str:
    return unicodedata.normalize("NFC", os.path.abspath(os.fspath(value))).casefold()


def _is_within(child: Path, parent: Path) -> bool:
    child_key = _windows_key(child)
    parent_key = _windows_key(parent)
    try:
        return os.path.commonpath((child_key, parent_key)) == parent_key
    except ValueError:
        return False


def _is_reparse_path(path: Path) -> bool:
    state = path.lstat()
    attributes = getattr(state, "st_file_attributes", 0)
    is_junction = getattr(os.path, "isjunction", None)
    return bool(
        stat.S_ISLNK(state.st_mode)
        or attributes & 0x400
        or (is_junction is not None and is_junction(path))
    )


def _target_parent_problem(target: Path) -> tuple[str, Path] | None:
    """Inspect existing target ancestors without traversing a reparse point."""
    anchor = Path(target.anchor)
    current = anchor
    for component in target.parent.parts[1:]:
        current /= component
        try:
            state = current.lstat()
        except FileNotFoundError:
            return None
        if _is_reparse_path(current):
            return "TARGET_PARENT_REPARSE", current
        if not stat.S_ISDIR(state.st_mode):
            return "TARGET_PARENT_NOT_DIRECTORY", current
    return None


def _validate_layout(source_roots: Iterable[Path], output_root: Path) -> None:
    for source_root in source_roots:
        if _is_within(output_root, source_root) or _is_within(source_root, output_root):
            raise PlanPolicyError(
                f"source/output overlap is unsafe: source={source_root}, output={output_root}"
            )


def _sanitize_component(value: str, *, fallback: str, maximum: int = 48) -> str:
    normalized = unicodedata.normalize("NFC", value)
    normalized = _ILLEGAL_WINDOWS_CHARS.sub("_", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip(" .")
    normalized = re.sub(r"_+", "_", normalized)
    if not normalized:
        normalized = fallback
    normalized = normalized[:maximum].rstrip(" .") or fallback
    device_stem = normalized.split(".", 1)[0].rstrip(" .").upper()
    if device_stem in _RESERVED_WINDOWS_STEMS:
        normalized = f"_{normalized}"
    return normalized


def _reliable_capture(candidate: _Candidate) -> datetime | None:
    if candidate.capture_source == "FILESYSTEM_MTIME":
        return None
    if candidate.capture_confidence not in {"HIGH", "MEDIUM"} or not candidate.captured_at:
        return None
    try:
        parsed = datetime.fromisoformat(candidate.captured_at)
    except ValueError:
        return None
    if not 1 <= parsed.year <= 9999:
        return None
    return parsed


def _hash_regular_file(path: Path, source_root: Path | None = None) -> tuple[int, int, str]:
    try:
        before = path.stat(follow_symlinks=False)
    except FileNotFoundError as error:
        raise PlanSourceMissingError(f"source is missing: {path}") from error
    if path.is_symlink() or not stat.S_ISREG(before.st_mode):
        raise PlanSourceError(f"source is not a regular no-follow file: {path}")
    digest = hashlib.sha256()
    try:
        stream_context = (
            _open_source_nofollow(path, source_root, share_delete=False)
            if source_root is not None
            else path.open("rb")
        )
        with stream_context as stream:
            while chunk := stream.read(_HASH_CHUNK_SIZE):
                digest.update(chunk)
            after_handle = os.fstat(stream.fileno())
    except FileNotFoundError as error:
        raise PlanSourceMissingError(f"source disappeared during safe read: {path}") from error
    except (OSError, PreviewError) as error:
        raise PlanSourceError(f"source cannot be opened safely: {path}") from error
    try:
        after = path.stat(follow_symlinks=False)
    except FileNotFoundError as error:
        raise PlanSourceMissingError(f"source disappeared after safe read: {path}") from error
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    handle_identity = (
        after_handle.st_dev,
        after_handle.st_ino,
        after_handle.st_size,
        after_handle.st_mtime_ns,
    )
    if identity_before != identity_after or identity_after != handle_identity:
        raise PlanSourceError(f"source changed while planning: {path}")
    return int(after.st_size), int(after.st_mtime_ns), digest.hexdigest()


def _load_candidates(workspace: Workspace) -> list[_Candidate]:
    connection = connect_database(workspace.database_path, read_only=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            WITH latest_ai AS (
                SELECT * FROM (
                    SELECT a.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY a.media_id
                               ORDER BY a.created_at DESC, a.id DESC
                           ) AS latest_rank
                    FROM ai_analysis AS a
                ) WHERE latest_rank = 1
            )
            SELECT
                m.id,m.source_root,m.original_path,m.media_type,m.extension,
                m.size_bytes,m.content_sha256,m.captured_at,m.capture_source,
                m.capture_confidence,bm.bundle_id,bm.role AS bundle_role,
                COALESCE(r.scene_category,a.scene_category,'05_其他') AS category,
                COALESCE(r.disposition,a.disposition,'REVIEW') AS disposition,
                CASE
                    WHEN r.media_id IS NOT NULL THEN 'HUMAN'
                    WHEN a.id IS NOT NULL THEN 'AI'
                    ELSE 'RULE'
                END AS decision_source,
                COALESCE(a.short_desc,'') AS short_desc
            FROM media_item AS m
            LEFT JOIN bundle_member AS bm ON bm.media_id = m.id
            LEFT JOIN latest_ai AS a ON a.media_id = m.id
            LEFT JOIN review_decision AS r ON r.media_id = m.id
            WHERE m.source_present = 1
              AND m.media_type IN ('IMAGE','VIDEO','SIDECAR')
            ORDER BY m.id
            """
        ).fetchall()
    finally:
        connection.close()
    return [
        _Candidate(
            media_id=int(row["id"]),
            source_root=Path(str(row["source_root"])),
            source_path=Path(str(row["original_path"])),
            media_type=str(row["media_type"]),
            extension=str(row["extension"]).lower(),
            indexed_size=int(row["size_bytes"]),
            indexed_sha256=str(row["content_sha256"]) if row["content_sha256"] else None,
            captured_at=str(row["captured_at"]) if row["captured_at"] else None,
            capture_source=str(row["capture_source"]),
            capture_confidence=str(row["capture_confidence"]),
            bundle_id=str(row["bundle_id"]) if row["bundle_id"] else None,
            bundle_role=str(row["bundle_role"]) if row["bundle_role"] else None,
            category=str(row["category"]),
            disposition=str(row["disposition"]),
            decision_source=str(row["decision_source"]),
            short_desc=str(row["short_desc"]),
        )
        for row in rows
    ]


def _inherit_bundle_decisions(candidates: list[_Candidate]) -> tuple[list[_Candidate], list[str]]:
    bundles: dict[str, list[_Candidate]] = defaultdict(list)
    singles: list[_Candidate] = []
    warnings: list[str] = []
    for candidate in candidates:
        if candidate.bundle_id:
            bundles[candidate.bundle_id].append(candidate)
        elif candidate.media_type == "SIDECAR":
            warnings.append(f"UNBOUND_SIDECAR:{candidate.media_id}")
        else:
            singles.append(candidate)
    selected = list(singles)
    for bundle_id in sorted(bundles):
        members = bundles[bundle_id]
        primary = min(
            members,
            key=lambda item: (
                item.bundle_role != "PRIMARY",
                item.media_type != "IMAGE",
                _windows_key(item.source_path),
                item.media_id,
            ),
        )
        for member in members:
            selected.append(
                replace(
                    member,
                    captured_at=primary.captured_at,
                    capture_source=primary.capture_source,
                    capture_confidence=primary.capture_confidence,
                    category=primary.category,
                    disposition=primary.disposition,
                    decision_source=primary.decision_source,
                    short_desc=primary.short_desc,
                )
            )
    selected.sort(key=lambda item: item.media_id)
    return selected, warnings


def _target_directory(output: Path, candidate: _Candidate, captured: datetime | None) -> Path:
    if candidate.disposition == "REJECT_CANDIDATE":
        root = output / "_待审废片"
    elif captured is None:
        root = (
            output
            / "_时间待确认"
            / _sanitize_component(candidate.category, fallback="05_其他", maximum=60)
        )
    else:
        root = output / _sanitize_component(candidate.category, fallback="05_其他", maximum=60)
    if captured is None:
        return root
    return root / f"{captured.year:04d}" / f"{captured.year:04d}-{captured.month:02d}"


def _build_entries(
    candidates: list[_Candidate], options: PlannerOptions, output_root: Path
) -> tuple[tuple[PlanEntry, ...], tuple[str, ...]]:
    selected, warnings = _inherit_bundle_decisions(candidates)
    hashed: dict[int, tuple[int, int, str]] = {}
    bundle_token: dict[str, str] = {}
    bundle_base: dict[str, tuple[Path, str]] = {}
    entries: list[PlanEntry] = []
    target_keys: set[str] = set()
    for candidate in selected:
        resolved_source = candidate.source_path.resolve(strict=True)
        resolved_root = candidate.source_root.resolve(strict=True)
        if not _is_within(resolved_source, resolved_root):
            raise PlanSourceError(f"source escaped its indexed root: {candidate.source_path}")
        size, mtime_ns, digest = _hash_regular_file(candidate.source_path, resolved_root)
        if size != candidate.indexed_size:
            raise PlanSourceError(f"source size differs from index: {candidate.source_path}")
        if candidate.indexed_sha256 and digest != candidate.indexed_sha256:
            raise PlanSourceError(f"source content differs from index: {candidate.source_path}")
        hashed[candidate.media_id] = (size, mtime_ns, digest)
        captured = _reliable_capture(candidate)
        directory = _target_directory(output_root, candidate, captured)
        description = _sanitize_component(
            candidate.short_desc or candidate.source_path.stem,
            fallback="media",
        )
        identity = candidate.bundle_id or f"{candidate.media_id}:{candidate.source_path}:{digest}"
        short_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:10]
        timestamp = captured.strftime("%Y%m%d_%H%M%S") if captured else "TIME_UNKNOWN"
        if candidate.bundle_id:
            if candidate.bundle_id not in bundle_token:
                bundle_token[candidate.bundle_id] = short_id
                bundle_base[candidate.bundle_id] = (
                    directory,
                    f"{timestamp}_{description}_{short_id}",
                )
            directory, filename_stem = bundle_base[candidate.bundle_id]
        else:
            filename_stem = f"{timestamp}_{description}_{short_id}"
        extension = _sanitize_component(
            candidate.extension.lstrip("."), fallback="bin", maximum=12
        ).lower()
        target = directory / f"{filename_stem}.{extension}"
        overflow = len(str(target)) - options.max_path_chars
        if overflow > 0:
            reduced = max(1, len(description) - overflow)
            description = _sanitize_component(description[:reduced], fallback="m", maximum=reduced)
            filename_stem = f"{timestamp}_{description}_{short_id}"
            if candidate.bundle_id:
                bundle_base[candidate.bundle_id] = (directory, filename_stem)
            target = directory / f"{filename_stem}.{extension}"
        if len(str(target)) > options.max_path_chars:
            raise PlanPolicyError(f"target path cannot fit safe path budget: {target}")
        target_key = _windows_key(target)
        if target_key in target_keys:
            raise PlanPolicyError(f"deterministic target collision could not be resolved: {target}")
        target_keys.add(target_key)
        entries.append(
            PlanEntry(
                position=len(entries),
                media_id=candidate.media_id,
                bundle_id=candidate.bundle_id,
                bundle_role=candidate.bundle_role,
                source_root=str(candidate.source_root.resolve(strict=True)),
                source_path=str(candidate.source_path),
                target_path=str(target),
                target_key=target_key,
                action=options.mode,
                expected_size=size,
                expected_sha256=digest,
                source_mtime_ns=mtime_ns,
                decision_source=candidate.decision_source,
                scene_category=candidate.category,
                disposition=candidate.disposition,
            )
        )
    return tuple(entries), tuple(sorted(warnings))


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _approval_contract(
    plan_id: str,
    payload_sha256: str,
    approval_state: str,
    approval_revision: int,
) -> str:
    return _fingerprint(
        {
            "plan_id": plan_id,
            "payload_sha256": payload_sha256,
            "approval_state": approval_state,
            "approval_revision": approval_revision,
        }
    )


def build_plan(
    workspace: Workspace,
    options: PlannerOptions,
    *,
    fault_injector: FaultInjector | None = None,
) -> OrganizationPlan:
    """Build or reuse one immutable plan without changing any source or output file."""
    output_root = options.output_root.expanduser().resolve(strict=False)
    candidates = _load_candidates(workspace)
    if not candidates:
        raise PlanSourceError("no present media is available to plan")
    source_roots = sorted({candidate.source_root.resolve(strict=True) for candidate in candidates})
    _validate_layout(source_roots, output_root)
    if output_root.exists() and not output_root.is_dir():
        raise PlanPolicyError(f"output root is not a directory: {output_root}")
    entries, warnings = _build_entries(candidates, options, output_root)
    config_fingerprint = _fingerprint(
        {
            "output_root": str(output_root),
            "mode": options.mode,
            "max_path_chars": options.max_path_chars,
            "schema_version": options.schema_version,
            "planner_version": options.planner_version,
        }
    )
    source_root_fingerprint = _fingerprint([_windows_key(root) for root in source_roots])
    draft = OrganizationPlan(
        plan_id="",
        schema_version=options.schema_version,
        planner_version=options.planner_version,
        created_at="",
        config_fingerprint=config_fingerprint,
        source_root_fingerprint=source_root_fingerprint,
        output_root=str(output_root),
        mode=options.mode,
        max_path_chars=options.max_path_chars,
        payload_sha256="",
        entries=entries,
        warnings=warnings,
        approval_state="PENDING",
        approval_revision=1,
    )
    payload_sha256 = hashlib.sha256(draft.canonical_payload()).hexdigest()
    plan_id = f"plan-{payload_sha256[:32]}"

    connection = connect_database(workspace.database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT plan_id FROM organization_plan WHERE payload_sha256 = ?",
            (payload_sha256,),
        ).fetchone()
        if existing is not None:
            connection.rollback()
            return inspect_plan(workspace, str(existing[0]))
        created_at = _utc_now()
        plan = replace(
            draft,
            plan_id=plan_id,
            created_at=created_at,
            payload_sha256=payload_sha256,
        )
        connection.execute(
            """
            INSERT INTO organization_plan(
                plan_id,schema_version,planner_version,created_at,config_fingerprint,
                source_root_fingerprint,output_root,mode,max_path_chars,payload_sha256,
                canonical_json,warning_json,entry_count
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                plan.plan_id,
                plan.schema_version,
                plan.planner_version,
                plan.created_at,
                plan.config_fingerprint,
                plan.source_root_fingerprint,
                plan.output_root,
                plan.mode,
                plan.max_path_chars,
                plan.payload_sha256,
                plan.canonical_json(),
                _canonical_json(list(plan.warnings)),
                len(plan.entries),
            ),
        )
        for entry in plan.entries:
            connection.execute(
                """
                INSERT INTO plan_entry(
                    plan_id,position,media_id,bundle_id,bundle_role,source_root,source_path,
                    target_path,target_key,action,expected_size,expected_sha256,source_mtime_ns,
                    decision_source,scene_category,disposition
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    plan.plan_id,
                    entry.position,
                    entry.media_id,
                    entry.bundle_id,
                    entry.bundle_role,
                    entry.source_root,
                    entry.source_path,
                    entry.target_path,
                    entry.target_key,
                    entry.action,
                    entry.expected_size,
                    entry.expected_sha256,
                    entry.source_mtime_ns,
                    entry.decision_source,
                    entry.scene_category,
                    entry.disposition,
                ),
            )
            if fault_injector is not None:
                fault_injector("AFTER_ENTRY_INSERT")
        connection.execute(
            """
            INSERT INTO plan_approval(plan_id,state,revision,approved_at,updated_at)
            VALUES (?,'PENDING',1,NULL,?)
            """,
            (plan.plan_id, created_at),
        )
        if fault_injector is not None:
            fault_injector("BEFORE_PLAN_COMMIT")
        connection.commit()
        return plan
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _row_entry(row: sqlite3.Row) -> PlanEntry:
    return PlanEntry(
        position=int(row["position"]),
        media_id=int(row["media_id"]),
        bundle_id=str(row["bundle_id"]) if row["bundle_id"] is not None else None,
        bundle_role=str(row["bundle_role"]) if row["bundle_role"] is not None else None,
        source_root=str(row["source_root"]),
        source_path=str(row["source_path"]),
        target_path=str(row["target_path"]),
        target_key=str(row["target_key"]),
        action=str(row["action"]),
        expected_size=int(row["expected_size"]),
        expected_sha256=str(row["expected_sha256"]),
        source_mtime_ns=int(row["source_mtime_ns"]),
        decision_source=str(row["decision_source"]),
        scene_category=str(row["scene_category"]),
        disposition=str(row["disposition"]),
    )


def _inspect_plan_connection(connection: sqlite3.Connection, plan_id: str) -> OrganizationPlan:
    row = connection.execute(
        """
        SELECT p.*,a.state AS approval_state,a.revision AS approval_revision
        FROM organization_plan AS p
        JOIN plan_approval AS a ON a.plan_id = p.plan_id
        WHERE p.plan_id = ?
        """,
        (plan_id,),
    ).fetchone()
    if row is None:
        raise PlanStateError(f"plan was not found: {plan_id}")
    entries = tuple(
        _row_entry(entry)
        for entry in connection.execute(
            "SELECT * FROM plan_entry WHERE plan_id = ? ORDER BY position", (plan_id,)
        )
    )
    try:
        warnings_value = json.loads(str(row["warning_json"]))
        warnings = tuple(str(value) for value in warnings_value)
    except (TypeError, ValueError) as error:
        raise PlanStateError("plan warning state is invalid") from error
    plan = OrganizationPlan(
        plan_id=str(row["plan_id"]),
        schema_version=str(row["schema_version"]),
        planner_version=str(row["planner_version"]),
        created_at=str(row["created_at"]),
        config_fingerprint=str(row["config_fingerprint"]),
        source_root_fingerprint=str(row["source_root_fingerprint"]),
        output_root=str(row["output_root"]),
        mode=str(row["mode"]),
        max_path_chars=int(row["max_path_chars"]),
        payload_sha256=str(row["payload_sha256"]),
        entries=entries,
        warnings=warnings,
        approval_state=str(row["approval_state"]),
        approval_revision=int(row["approval_revision"]),
    )
    if plan.schema_version != PLAN_SCHEMA_VERSION:
        raise PlanStateError(f"unsupported persisted plan schema version: {plan.schema_version}")
    if plan.planner_version != PLANNER_VERSION:
        raise PlanStateError(f"unsupported persisted planner version: {plan.planner_version}")
    if len(entries) != int(row["entry_count"]):
        raise PlanStateError("plan entry count is inconsistent")
    if hashlib.sha256(plan.canonical_payload()).hexdigest() != plan.payload_sha256:
        raise PlanStateError("plan payload digest is inconsistent")
    if plan.canonical_json() != str(row["canonical_json"]):
        raise PlanStateError("plan canonical document is inconsistent")
    return plan


def inspect_plan(workspace: Workspace, plan_id: str) -> OrganizationPlan:
    """Return and integrity-check a previewable immutable plan snapshot."""
    connection = connect_database(workspace.database_path, read_only=True)
    connection.row_factory = sqlite3.Row
    try:
        return _inspect_plan_connection(connection, plan_id)
    finally:
        connection.close()


def approve_plan(workspace: Workspace, plan_id: str) -> PlanApproval:
    """Atomically validate the immutable payload and record explicit approval."""
    connection = connect_database(workspace.database_path)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN IMMEDIATE")
        _inspect_plan_connection(connection, plan_id)
        row = connection.execute(
            "SELECT * FROM plan_approval WHERE plan_id = ?", (plan_id,)
        ).fetchone()
        if row is None:
            raise PlanStateError(f"plan was not found: {plan_id}")
        if str(row["state"]) != "APPROVED":
            now = _utc_now()
            connection.execute(
                """
                UPDATE plan_approval
                SET state='APPROVED',revision=revision+1,approved_at=?,updated_at=?
                WHERE plan_id=?
                """,
                (now, now, plan_id),
            )
        row = connection.execute(
            "SELECT * FROM plan_approval WHERE plan_id = ?", (plan_id,)
        ).fetchone()
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
    assert row is not None
    return PlanApproval(
        plan_id=plan_id,
        state=str(row["state"]),
        revision=int(row["revision"]),
        approved_at=str(row["approved_at"]) if row["approved_at"] else None,
        updated_at=str(row["updated_at"]),
    )


def revoke_plan(workspace: Workspace, plan_id: str) -> PlanApproval:
    """Revoke approval without changing immutable plan content."""
    connection = connect_database(workspace.database_path)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM plan_approval WHERE plan_id = ?", (plan_id,)
        ).fetchone()
        if row is None:
            raise PlanStateError(f"plan was not found: {plan_id}")
        if str(row["state"]) != "REVOKED":
            now = _utc_now()
            connection.execute(
                """
                UPDATE plan_approval
                SET state='REVOKED',revision=revision+1,approved_at=NULL,updated_at=?
                WHERE plan_id=?
                """,
                (now, plan_id),
            )
        row = connection.execute(
            "SELECT * FROM plan_approval WHERE plan_id = ?", (plan_id,)
        ).fetchone()
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
    assert row is not None
    return PlanApproval(
        plan_id=plan_id,
        state=str(row["state"]),
        revision=int(row["revision"]),
        approved_at=None,
        updated_at=str(row["updated_at"]),
    )


def _nearest_existing(path: Path) -> Path:
    candidate = path
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    return candidate


def _issue(
    code: str,
    message: str,
    *,
    severity: str = "ERROR",
    entry: PlanEntry | None = None,
    path: Path | None = None,
) -> PreflightIssue:
    return PreflightIssue(
        code=code,
        severity=severity,
        message=message,
        media_id=entry.media_id if entry else None,
        path=str(path) if path else None,
    )


def _bundle_preflight_issues(workspace: Workspace, plan: OrganizationPlan) -> list[PreflightIssue]:
    planned: dict[str, set[int]] = defaultdict(set)
    for entry in plan.entries:
        if entry.bundle_id:
            planned[entry.bundle_id].add(entry.media_id)
    if not planned:
        return []
    issues: list[PreflightIssue] = []
    with closing(connect_database(workspace.database_path, read_only=True)) as connection:
        for bundle_id, media_ids in sorted(planned.items()):
            current = {
                int(row[0])
                for row in connection.execute(
                    """
                    SELECT bm.media_id
                    FROM bundle_member AS bm
                    JOIN media_item AS m ON m.id = bm.media_id
                    WHERE bm.bundle_id = ? AND m.source_present = 1
                    """,
                    (bundle_id,),
                )
            }
            warning = connection.execute(
                "SELECT warning_status FROM asset_bundle WHERE id = ?", (bundle_id,)
            ).fetchone()
            if current != media_ids:
                issues.append(
                    _issue(
                        "BUNDLE_INCOMPLETE",
                        f"bundle membership changed after plan: {bundle_id}",
                    )
                )
            if warning is not None and warning[0]:
                issues.append(
                    _issue(
                        "BUNDLE_WARNING",
                        f"bundle requires review: {bundle_id}",
                        severity="WARNING",
                    )
                )
    return issues


def preflight_plan(workspace: Workspace, plan_id: str) -> PreflightReport:
    """Read-only validation gate. It performs no planned filesystem action."""
    plan = inspect_plan(workspace, plan_id)
    starting_revision = plan.approval_revision
    issues: list[PreflightIssue] = []
    if plan.approval_state != "APPROVED":
        issues.append(_issue("PLAN_NOT_APPROVED", "plan requires explicit approval"))

    output = Path(plan.output_root)
    if _windows_key(output.resolve(strict=False)) != _windows_key(output):
        issues.append(
            _issue(
                "OUTPUT_PATH_CHANGED", "output root now resolves to a different path", path=output
            )
        )
    if output.exists() and (output.is_symlink() or not output.is_dir()):
        issues.append(
            _issue("OUTPUT_NOT_DIRECTORY", "output root is not a safe directory", path=output)
        )
    output_anchor = _nearest_existing(output)
    if not os.access(output_anchor, os.W_OK):
        issues.append(_issue("OUTPUT_NOT_WRITABLE", "output root is not writable", path=output))
    if not os.access(workspace.root, os.W_OK):
        issues.append(
            _issue("WORKSPACE_NOT_WRITABLE", "workspace is not writable", path=workspace.root)
        )
    for source_root_text in sorted({entry.source_root for entry in plan.entries}):
        source_root = Path(source_root_text)
        if _is_within(output, source_root) or _is_within(source_root, output):
            issues.append(
                _issue(
                    "SOURCE_OUTPUT_OVERLAP",
                    "source and output roots overlap",
                    path=source_root,
                )
            )

    required_bytes = 0
    for entry in plan.entries:
        source = Path(entry.source_path)
        try:
            size, mtime_ns, digest = _hash_regular_file(source, Path(entry.source_root))
        except PlanSourceMissingError as error:
            issues.append(_issue("SOURCE_MISSING", str(error), entry=entry, path=source))
            continue
        except PlanSourceError as error:
            issues.append(_issue("SOURCE_UNREADABLE", str(error), entry=entry, path=source))
            continue
        if size != entry.expected_size or digest != entry.expected_sha256:
            issues.append(
                _issue(
                    "STALE_SOURCE", "source content changed after plan", entry=entry, path=source
                )
            )
        elif mtime_ns != entry.source_mtime_ns:
            issues.append(
                _issue(
                    "SOURCE_METADATA_CHANGED",
                    "source mtime changed but verified content is unchanged",
                    severity="WARNING",
                    entry=entry,
                    path=source,
                )
            )
        target = Path(entry.target_path)
        if not _is_within(target, output):
            issues.append(
                _issue(
                    "TARGET_OUTSIDE_OUTPUT", "target escaped output root", entry=entry, path=target
                )
            )
            continue
        parent_problem = _target_parent_problem(target)
        if parent_problem is not None:
            code, problem_path = parent_problem
            message = (
                "target parent contains a symlink, junction, or reparse point"
                if code == "TARGET_PARENT_REPARSE"
                else "target parent path is obstructed by a non-directory"
            )
            issues.append(_issue(code, message, entry=entry, path=problem_path))
            continue
        if target.exists():
            if target.is_symlink() or not target.is_file():
                issues.append(
                    _issue(
                        "TARGET_CONFLICT", "target is not a regular file", entry=entry, path=target
                    )
                )
            else:
                try:
                    _, _, target_digest = _hash_regular_file(target)
                except PlanSourceError as error:
                    issues.append(_issue("TARGET_CONFLICT", str(error), entry=entry, path=target))
                else:
                    if target_digest == entry.expected_sha256:
                        issues.append(
                            _issue(
                                "ALREADY_PRESENT",
                                "target already contains the planned content",
                                severity="INFO",
                                entry=entry,
                                path=target,
                            )
                        )
                    else:
                        issues.append(
                            _issue(
                                "TARGET_CONFLICT",
                                "target exists with different content",
                                entry=entry,
                                path=target,
                            )
                        )
        else:
            required_bytes += entry.expected_size

    issues.extend(_bundle_preflight_issues(workspace, plan))
    with closing(connect_database(workspace.database_path, read_only=True)) as connection:
        incomplete = connection.execute(
            """
            SELECT transaction_id,state FROM operation_transaction
            WHERE state NOT IN ('DONE','ROLLED_BACK')
            ORDER BY created_at,transaction_id LIMIT 1
            """
        ).fetchone()
    if incomplete is not None:
        issues.append(
            _issue(
                "INCOMPLETE_TRANSACTION",
                f"unfinished transaction {incomplete[0]} is in state {incomplete[1]}",
            )
        )
    for lock_path in (workspace.root / "state" / "apply.lock", workspace.root / ".spt-apply-lock"):
        if lock_path.exists():
            issues.append(_issue("APPLY_LOCKED", "another apply may be active", path=lock_path))

    available_bytes: int | None = None
    try:
        available_bytes = int(shutil.disk_usage(output_anchor).free)
    except OSError:
        issues.append(
            _issue(
                "SPACE_UNKNOWN",
                "available output capacity could not be determined",
                severity="WARNING",
                path=output,
            )
        )
    if available_bytes is not None and required_bytes > available_bytes:
        issues.append(
            _issue(
                "INSUFFICIENT_SPACE",
                f"plan requires {required_bytes} bytes but only {available_bytes} are available",
                path=output,
            )
        )

    final = inspect_plan(workspace, plan_id)
    if plan.approval_state == "APPROVED" and (
        final.approval_state != "APPROVED" or final.approval_revision != starting_revision
    ):
        issues.append(_issue("APPROVAL_CHANGED", "approval changed during preflight"))
    approval_contract = _approval_contract(
        final.plan_id,
        final.payload_sha256,
        final.approval_state,
        final.approval_revision,
    )
    return PreflightReport(
        plan_id=plan.plan_id,
        approval_state=final.approval_state,
        approval_revision=final.approval_revision,
        approval_contract=approval_contract,
        payload_sha256=final.payload_sha256,
        checked_entries=len(plan.entries),
        required_bytes=required_bytes,
        available_bytes=available_bytes,
        issues=tuple(issues),
    )


def preflight_approval_is_current(workspace: Workspace, report: PreflightReport) -> bool:
    """Check a preflight approval contract in one database snapshot.

    A future executor must repeat this comparison while holding its workspace mutation lock and
    immediately before mutation. A stale report alone is never file-operation authority.
    """
    if not report.ok or report.approval_state != "APPROVED":
        return False
    with closing(connect_database(workspace.database_path, read_only=True)) as connection:
        row = connection.execute(
            """
            SELECT p.payload_sha256,a.state,a.revision
            FROM organization_plan AS p
            JOIN plan_approval AS a ON a.plan_id=p.plan_id
            WHERE p.plan_id=?
            """,
            (report.plan_id,),
        ).fetchone()
    if row is None:
        return False
    payload_sha256, state, revision = str(row[0]), str(row[1]), int(row[2])
    current_contract = _approval_contract(
        report.plan_id,
        payload_sha256,
        state,
        revision,
    )
    return bool(
        state == "APPROVED"
        and revision == report.approval_revision
        and payload_sha256 == report.payload_sha256
        and current_contract == report.approval_contract
    )
