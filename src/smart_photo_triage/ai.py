"""Privacy-reduced, provider-neutral Phase D vision analysis."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import sqlite3
import stat
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import islice
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from uuid import uuid4

from smart_photo_triage.config import ConfigError, load_config
from smart_photo_triage.database import connect_database
from smart_photo_triage.preprocess import _open_source_nofollow
from smart_photo_triage.workspace import Workspace

SCENE_CATEGORIES = frozenset(
    {
        "01_家庭生活",
        "02_旅行风光",
        "03_工作与文档",
        "04_截图与备忘",
        "05_其他",
    }
)
DISPOSITIONS = frozenset({"KEEP", "REVIEW", "REJECT_CANDIDATE"})
_RESULT_FIELDS = frozenset(
    {
        "item_id",
        "scene_category",
        "disposition",
        "confidence",
        "quality_score",
        "tags",
        "short_desc",
        "reason",
    }
)
_QUALITY_FIELDS = frozenset({"sharpness", "exposure", "clipping", "resolution", "score"})
_QUALITY_JSON_MAX_CHARS = 16 * 1024
_FAKE_HISTORY_MAX_BYTES = 64 * 1024
_MAX_PREVIEW_BYTES = 32 * 1024 * 1024
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_TEXT_LENGTH = 2_000
_MAX_TAGS = 32
MAX_BATCH_SIZE = 100
MAX_RETRIES = 10
MAX_PROVIDER_TIMEOUT_SECONDS = 300.0
ANALYSIS_POLICY_VERSION = "analysis-policy-v1"


def _best_effort_rollback(connection: sqlite3.Connection) -> None:
    with suppress(sqlite3.Error):
        connection.rollback()


@contextmanager
def _database_boundary(path: Path, *, read_only: bool = False):  # type: ignore[no-untyped-def]
    connection = None
    body_error = False
    try:
        try:
            connection = connect_database(path, read_only=read_only)
        except sqlite3.Error:
            raise AnalysisError("DB_WRITE_ERROR") from None
        try:
            yield connection
        except sqlite3.Error:
            body_error = True
            _best_effort_rollback(connection)
            raise AnalysisError("DB_WRITE_ERROR") from None
        except BaseException:
            body_error = True
            raise
    finally:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                if not body_error:
                    raise AnalysisError("DB_WRITE_ERROR") from None


class AnalysisError(RuntimeError):
    """Base Phase D analysis error."""


class SchemaValidationError(AnalysisError):
    """Raised when provider output violates the business schema."""


class CloudDisabledError(AnalysisError):
    """Raised before a cloud provider can perform network I/O."""


class TransientProviderError(AnalysisError):
    """Raised for retryable rate-limit, server, and timeout failures."""


class PermanentProviderError(AnalysisError):
    """Raised for non-retryable provider failures."""


class SplittableProviderError(AnalysisError):
    """Raised when a provider explicitly identifies a batch as item-isolatable."""


@dataclass(frozen=True, slots=True)
class VisionRequestItem:
    item_id: int
    media_type: str
    preview_mime_type: str
    preview_bytes: bytes
    quality: dict[str, object]


@dataclass(frozen=True, slots=True)
class VisionRequest:
    items: tuple[VisionRequestItem, ...]
    prompt_version: str
    schema_version: str

    def to_payload(self) -> dict[str, object]:
        """Serialize only controlled previews and allow-listed anonymous quality metrics."""
        return {
            "prompt_version": self.prompt_version,
            "schema_version": self.schema_version,
            "items": [
                {
                    "item_id": item.item_id,
                    "media_type": item.media_type,
                    "preview": {
                        "mime_type": item.preview_mime_type,
                        "data": base64.b64encode(item.preview_bytes).decode("ascii"),
                    },
                    "quality": {
                        key: value
                        for key, value in sorted(item.quality.items())
                        if key in _QUALITY_FIELDS
                    },
                }
                for item in self.items
            ],
        }


@dataclass(frozen=True, slots=True)
class VisionAnalysis:
    item_id: int
    scene_category: str
    disposition: str
    confidence: float
    quality_score: float
    tags: tuple[str, ...]
    short_desc: str
    reason: str


class VisionProvider(Protocol):
    name: str
    model: str
    is_cloud: bool

    def analyze(self, request: VisionRequest) -> object: ...


@dataclass(frozen=True, slots=True)
class AnalysisOptions:
    prompt_version: str = "vision-prompt-v1"
    schema_version: str = "vision-schema-v1"
    confidence_threshold: float = 0.65
    batch_size: int = 8
    max_retries: int = 2

    def __post_init__(self) -> None:
        if not self.prompt_version or not self.schema_version:
            raise ValueError("analysis prompt and schema versions must not be empty")
        if (
            isinstance(self.confidence_threshold, bool)
            or not math.isfinite(self.confidence_threshold)
            or not 0 <= self.confidence_threshold <= 1
        ):
            raise ValueError("analysis confidence threshold must be between 0 and 1")
        if type(self.batch_size) is not int:
            raise ValueError("analysis batch size must be an integer")
        if not 1 <= self.batch_size <= MAX_BATCH_SIZE:
            raise ValueError(f"analysis batch size must be between 1 and {MAX_BATCH_SIZE}")
        if type(self.max_retries) is not int:
            raise ValueError("analysis max retries must be an integer")
        if not 0 <= self.max_retries <= MAX_RETRIES:
            raise ValueError(f"analysis max retries must be between 0 and {MAX_RETRIES}")


_DEFAULT_ANALYSIS_OPTIONS = AnalysisOptions()


@dataclass(frozen=True, slots=True)
class AnalysisEstimate:
    item_count: int
    pending_count: int
    cache_hit_count: int
    upload_bytes: int
    request_batch_count: int


@dataclass(frozen=True, slots=True)
class AnalyzeResult:
    run_id: str
    analyzed_count: int
    cache_hit_count: int
    failed_count: int
    request_count: int
    failures: tuple[tuple[int, str], ...]


@dataclass(frozen=True, slots=True)
class _PendingItem:
    request_item: VisionRequestItem
    input_fingerprint: str
    preview_fingerprint: str
    preview_version: str


@dataclass(frozen=True, slots=True)
class _PendingDescriptor:
    item_id: int
    media_type: str
    preview_path: str
    preview_sha256: str
    quality: dict[str, object]
    input_fingerprint: str
    preview_fingerprint: str
    preview_version: str
    upload_bytes: int


@dataclass(slots=True)
class _DescriptorStats:
    item_count: int = 0
    pending_count: int = 0
    cache_hits: int = 0
    upload_bytes: int = 0
    failures: list[tuple[int, str]] | None = None

    def add_failure(self, media_id: int) -> None:
        if self.failures is not None:
            self.failures.append((media_id, "PREVIEW_READ_ERROR"))


@dataclass(slots=True)
class _BatchBudget:
    retries_remaining: int
    requests_remaining: int


class FakeVisionProvider:
    """Deterministic offline provider used by every core and E2E test."""

    name = "fake"
    is_cloud = False

    def __init__(
        self,
        *,
        model: str = "fake-v1",
        record_requests: bool = False,
        history_limit: int = 16,
    ) -> None:
        if not model:
            raise ValueError("fake provider model must not be empty")
        if type(history_limit) is not int or not 1 <= history_limit <= 32:
            raise ValueError("fake provider history limit must be an integer between 1 and 32")
        self.model = model
        self.record_requests = record_requests
        self.history_limit = history_limit
        self.requests: list[VisionRequest] = []
        self._request_sizes: list[int] = []

    def analyze(self, request: VisionRequest) -> object:
        if self.record_requests:
            redacted = VisionRequest(
                items=tuple(
                    VisionRequestItem(
                        item_id=item.item_id,
                        media_type=item.media_type,
                        preview_mime_type=item.preview_mime_type,
                        preview_bytes=b"",
                        quality=dict(item.quality),
                    )
                    for item in request.items
                ),
                prompt_version=request.prompt_version,
                schema_version=request.schema_version,
            )
            self.requests.append(redacted)
            serialized_size = len(
                json.dumps(redacted.to_payload(), separators=(",", ":")).encode("utf-8")
            )
            self._request_sizes.append(serialized_size)
            while (
                len(self.requests) > self.history_limit
                or sum(self._request_sizes) > _FAKE_HISTORY_MAX_BYTES
            ):
                self.requests.pop(0)
                self._request_sizes.pop(0)
        categories = tuple(sorted(SCENE_CATEGORIES))
        results: list[dict[str, object]] = []
        for item in request.items:
            local_score = item.quality.get("score", 0.5)
            quality_score = (
                float(local_score)
                if not isinstance(local_score, bool) and isinstance(local_score, int | float)
                else 0.5
            )
            results.append(
                {
                    "item_id": item.item_id,
                    "scene_category": categories[(item.item_id - 1) % len(categories)],
                    "disposition": "KEEP" if quality_score >= 0.55 else "REVIEW",
                    "confidence": 0.95,
                    "quality_score": max(0.0, min(1.0, quality_score)),
                    "tags": ["synthetic", item.media_type.casefold()],
                    "short_desc": f"offline synthetic item {item.item_id}",
                    "reason": "deterministic Fake Vision Provider result",
                }
            )
        return results


class GeminiVisionProvider:
    """Small standard-library adapter for Gemini's generateContent HTTP API."""

    name = "gemini"
    is_cloud = True

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        opener: Callable[..., object] | None = None,
        timeout_seconds: float = 30.0,
        api_base: str = "https://generativelanguage.googleapis.com/v1beta",
    ) -> None:
        if not model:
            raise ValueError("Gemini model must not be empty")
        if not api_key:
            raise ValueError("Gemini API key must not be empty")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int | float)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
            or timeout_seconds > MAX_PROVIDER_TIMEOUT_SECONDS
        ):
            raise ValueError(
                "Gemini timeout must be positive, finite, and no more than "
                f"{MAX_PROVIDER_TIMEOUT_SECONDS:g} seconds"
            )
        self.model = model
        self.api_key = api_key
        self.opener = opener or urlopen
        self.timeout_seconds = timeout_seconds
        self.api_base = api_base.rstrip("/")

    @staticmethod
    def _parts(request: VisionRequest) -> list[dict[str, object]]:
        instruction = {
            "task": "Return one JSON array element for every item_id using the required schema.",
            "prompt_version": request.prompt_version,
            "schema_version": request.schema_version,
            "allowed_scene_categories": sorted(SCENE_CATEGORIES),
            "allowed_dispositions": sorted(DISPOSITIONS),
            "required_fields": sorted(_RESULT_FIELDS),
        }
        parts: list[dict[str, object]] = [
            {"text": json.dumps(instruction, ensure_ascii=False, separators=(",", ":"))}
        ]
        for item in request.items:
            item_metadata = {
                "item_id": item.item_id,
                "media_type": item.media_type,
                "quality": {
                    key: value
                    for key, value in sorted(item.quality.items())
                    if key in _QUALITY_FIELDS
                },
            }
            parts.extend(
                (
                    {"text": json.dumps(item_metadata, ensure_ascii=False, separators=(",", ":"))},
                    {
                        "inline_data": {
                            "mime_type": item.preview_mime_type,
                            "data": base64.b64encode(item.preview_bytes).decode("ascii"),
                        }
                    },
                )
            )
        return parts

    def analyze(self, request: VisionRequest) -> object:
        endpoint = f"{self.api_base}/models/{quote(self.model, safe='')}:generateContent"
        payload = json.dumps(
            {
                "contents": [{"role": "user", "parts": self._parts(request)}],
                "generationConfig": {"responseMimeType": "application/json"},
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        http_request = Request(
            endpoint,
            data=payload,
            headers={"Content-Type": "application/json", "X-Goog-Api-Key": self.api_key},
            method="POST",
        )
        try:
            response_context = self.opener(http_request, timeout=self.timeout_seconds)
            with response_context as response:  # type: ignore[attr-defined]
                response_bytes = response.read(_MAX_RESPONSE_BYTES + 1)  # type: ignore[attr-defined]
        except HTTPError as error:
            if error.code == 429 or 500 <= error.code <= 599:
                raise TransientProviderError(f"Gemini HTTP {error.code}") from error
            raise PermanentProviderError(f"Gemini HTTP {error.code}") from error
        except TimeoutError as error:
            raise TransientProviderError("Gemini request timed out") from error
        except URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise TransientProviderError("Gemini request timed out") from error
            raise TransientProviderError("Gemini network request failed") from error
        except OSError as error:
            raise TransientProviderError("Gemini network request failed") from error
        if len(response_bytes) > _MAX_RESPONSE_BYTES:
            raise PermanentProviderError("Gemini response exceeds the bounded size")
        try:
            envelope = json.loads(response_bytes)
            text = envelope["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(text)
        except (IndexError, KeyError, TypeError, UnicodeError, json.JSONDecodeError) as error:
            raise PermanentProviderError("Gemini returned malformed structured output") from error


def _bounded_number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SchemaValidationError(f"{field} must be a number")
    number = float(value)
    if not math.isfinite(number) or not 0 <= number <= 1:
        raise SchemaValidationError(f"{field} must be between 0 and 1")
    return number


def _bounded_text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise SchemaValidationError(f"{field} must be a string")
    normalized = value.strip()
    if not normalized or len(normalized) > _MAX_TEXT_LENGTH:
        raise SchemaValidationError(f"{field} must be non-empty and bounded")
    return normalized


def validate_analysis_result(
    payload: object, *, confidence_threshold: float = 0.65
) -> VisionAnalysis:
    """Validate one exact provider result and apply low-confidence protection."""
    if not isinstance(payload, dict):
        raise SchemaValidationError("analysis result must be an object")
    fields = set(payload)
    if fields != _RESULT_FIELDS:
        missing = sorted(_RESULT_FIELDS - fields)
        unexpected = sorted(fields - _RESULT_FIELDS)
        raise SchemaValidationError(
            f"analysis result field mismatch: missing={missing} unexpected={unexpected}"
        )
    item_id = payload["item_id"]
    if isinstance(item_id, bool) or not isinstance(item_id, int) or item_id <= 0:
        raise SchemaValidationError("item_id must be a positive integer")
    scene_category = payload["scene_category"]
    if not isinstance(scene_category, str) or scene_category not in SCENE_CATEGORIES:
        raise SchemaValidationError("scene_category is not allowed")
    disposition = payload["disposition"]
    if not isinstance(disposition, str) or disposition not in DISPOSITIONS:
        raise SchemaValidationError("disposition is not allowed")
    confidence = _bounded_number(payload["confidence"], "confidence")
    quality_score = _bounded_number(payload["quality_score"], "quality_score")
    tags_value = payload["tags"]
    if not isinstance(tags_value, list) or len(tags_value) > _MAX_TAGS:
        raise SchemaValidationError("tags must be a bounded list")
    tags: list[str] = []
    for value in tags_value:
        tag = _bounded_text(value, "tag")
        if tag not in tags:
            tags.append(tag)
    if (
        isinstance(confidence_threshold, bool)
        or not isinstance(confidence_threshold, int | float)
        or not math.isfinite(confidence_threshold)
        or not 0 <= confidence_threshold <= 1
    ):
        raise ValueError("confidence threshold must be between 0 and 1")
    if disposition == "REJECT_CANDIDATE" and confidence < confidence_threshold:
        disposition = "REVIEW"
    return VisionAnalysis(
        item_id=item_id,
        scene_category=scene_category,
        disposition=disposition,
        confidence=confidence,
        quality_score=quality_score,
        tags=tuple(tags),
        short_desc=_bounded_text(payload["short_desc"], "short_desc"),
        reason=_bounded_text(payload["reason"], "reason"),
    )


def validate_response_mapping(
    payload: object,
    *,
    expected_item_ids: tuple[int, ...],
    confidence_threshold: float = 0.65,
) -> dict[int, VisionAnalysis]:
    """Validate a whole response and map it independently of provider ordering."""
    if not isinstance(payload, list):
        raise SchemaValidationError("provider response must be a JSON array")
    expected = set(expected_item_ids)
    if len(expected) != len(expected_item_ids):
        raise ValueError("expected item IDs must be unique")
    mapped: dict[int, VisionAnalysis] = {}
    for raw_result in payload:
        result = validate_analysis_result(raw_result, confidence_threshold=confidence_threshold)
        if result.item_id in mapped:
            raise SchemaValidationError(f"duplicate item_id {result.item_id}")
        mapped[result.item_id] = result
    actual = set(mapped)
    if actual != expected:
        raise SchemaValidationError(
            f"provider item_id mismatch: missing={sorted(expected - actual)} "
            f"unexpected={sorted(actual - expected)}"
        )
    return {item_id: mapped[item_id] for item_id in sorted(expected)}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _mime_type(path: Path) -> str:
    return {
        ".webp": "image/webp",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
    }.get(path.suffix.casefold(), "application/octet-stream")


def _anonymous_quality(raw: object) -> dict[str, object]:
    raw_text = str(raw) if raw else ""
    if len(raw_text) > _QUALITY_JSON_MAX_CHARS:
        return {}
    try:
        parsed = json.loads(raw_text) if raw_text else {}
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    result: dict[str, object] = {}
    for key in sorted(_QUALITY_FIELDS & set(parsed)):
        value = parsed[key]
        if isinstance(value, bool) or not isinstance(value, int | float):
            continue
        number = float(value)
        if not math.isfinite(number):
            continue
        if key == "resolution":
            if not 0 <= number <= 1_000_000_000_000:
                continue
        elif not 0 <= number <= 1:
            continue
        result[key] = value
    return result


def analysis_input_fingerprint(
    *,
    preview_sha256: str,
    preview_fingerprint: str,
    preview_version: str,
    media_type: str,
    quality: dict[str, object],
    provider: VisionProvider,
    options: AnalysisOptions,
) -> str:
    """Hash every controlled input that can change a request or final disposition."""
    contract = {
        "policy_version": ANALYSIS_POLICY_VERSION,
        "preview_sha256": preview_sha256,
        "preview_fingerprint": preview_fingerprint,
        "preview_version": preview_version,
        "media_type": media_type,
        "quality": quality,
        "provider": provider.name,
        "model": provider.model,
        "prompt_version": options.prompt_version,
        "schema_version": options.schema_version,
        "confidence_threshold": options.confidence_threshold,
    }
    canonical = json.dumps(
        contract,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _cloud_allowed(
    workspace: Workspace, provider: VisionProvider, allow_cloud: bool | None
) -> None:
    if not provider.is_cloud:
        return
    if allow_cloud is None:
        try:
            selected = load_config(workspace.config_path).allow_cloud
        except ConfigError:
            raise AnalysisError("workspace configuration is invalid") from None
    else:
        selected = allow_cloud
    if selected is not True:
        raise CloudDisabledError(
            "Cloud analysis is disabled; set allow_cloud=true explicitly before using it"
        )


def _normalized_preview_path(workspace: Workspace, path_text: object) -> tuple[Path, Path]:
    path = Path(str(path_text))
    root = Path(os.path.abspath(workspace.root))
    normalized = Path(os.path.abspath(path))
    try:
        relative = normalized.relative_to(root)
    except ValueError as error:
        raise AnalysisError("preview path is outside the workspace") from error
    if not relative.parts or relative.parts[0].casefold() != "previews":
        raise AnalysisError("AI input must come from the controlled previews directory")
    return root, normalized


def _controlled_preview_size(workspace: Workspace, path_text: object) -> int:
    root, normalized = _normalized_preview_path(workspace, path_text)
    try:
        with _open_source_nofollow(normalized, root, share_delete=False) as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise AnalysisError("preview input is not a regular file")
            if opened.st_size > _MAX_PREVIEW_BYTES:
                raise AnalysisError("preview input exceeds the upload size bound")
            return int(opened.st_size)
    except OSError as error:
        raise AnalysisError("preview input is unreadable") from error


def _controlled_preview_digest(
    workspace: Workspace, path_text: object, expected_sha256: object
) -> str:
    root, normalized = _normalized_preview_path(workspace, path_text)
    digest = hashlib.sha256()
    total = 0
    try:
        with _open_source_nofollow(normalized, root, share_delete=False) as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise AnalysisError("preview input is not a regular file")
            while chunk := stream.read(1024 * 1024):
                total += len(chunk)
                if total > _MAX_PREVIEW_BYTES:
                    raise AnalysisError("preview input exceeds the upload size bound")
                digest.update(chunk)
    except OSError as error:
        raise AnalysisError("preview input is unreadable") from error
    actual_digest = digest.hexdigest()
    if not expected_sha256 or actual_digest != str(expected_sha256):
        raise AnalysisError("preview input digest does not match the READY artifact")
    return actual_digest


def _controlled_preview_bytes(
    workspace: Workspace, path_text: object, expected_sha256: object
) -> tuple[Path, bytes, str]:
    root, normalized = _normalized_preview_path(workspace, path_text)
    digest = hashlib.sha256()
    payload = bytearray()
    try:
        with _open_source_nofollow(normalized, root, share_delete=False) as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise AnalysisError("preview input is not a regular file")
            while chunk := stream.read(1024 * 1024):
                payload.extend(chunk)
                if len(payload) > _MAX_PREVIEW_BYTES:
                    raise AnalysisError("preview input exceeds the upload size bound")
                digest.update(chunk)
    except OSError as error:
        raise AnalysisError("preview input is unreadable") from error
    actual_digest = digest.hexdigest()
    if not expected_sha256 or actual_digest != str(expected_sha256):
        raise AnalysisError("preview input digest does not match the READY artifact")
    return normalized, bytes(payload), actual_digest


def _cache_hit(
    connection: sqlite3.Connection,
    *,
    media_id: int,
    input_fingerprint: str,
    preview_fingerprint: str,
    preview_version: str,
    provider: VisionProvider,
    options: AnalysisOptions,
) -> bool:
    row = connection.execute(
        """
        SELECT 1 FROM ai_analysis
        WHERE media_id=? AND input_fingerprint=? AND preview_fingerprint=?
          AND preview_version=? AND provider=? AND model=?
          AND prompt_version=? AND schema_version=?
        LIMIT 1
        """,
        (
            media_id,
            input_fingerprint,
            preview_fingerprint,
            preview_version,
            provider.name,
            provider.model,
            options.prompt_version,
            options.schema_version,
        ),
    ).fetchone()
    return row is not None


def _iter_descriptors(
    workspace: Workspace,
    connection: sqlite3.Connection,
    provider: VisionProvider,
    options: AnalysisOptions,
    *,
    verify_cache_artifacts: bool,
) -> tuple[Iterator[_PendingDescriptor], _DescriptorStats]:
    stats = _DescriptorStats()

    def generate() -> Iterator[_PendingDescriptor]:
        rows = connection.execute(
            """
            SELECT m.id,m.media_type,p.preview_path,p.preview_version,
                   p.preview_fingerprint,p.preview_sha256,p.quality_json
            FROM media_item AS m
            JOIN media_preprocess AS p ON p.media_id=m.id
            WHERE m.source_present=1 AND m.media_type IN ('IMAGE','VIDEO')
              AND p.preview_status='READY'
            ORDER BY m.id
            """
        )
        while page := rows.fetchmany(256):
            for row in page:
                stats.item_count += 1
                media_id = int(row[0])
                media_type = str(row[1])
                preview_fingerprint = str(row[4])
                preview_version = str(row[3])
                preview_sha256 = str(row[5])
                quality = _anonymous_quality(row[6])
                input_fingerprint = analysis_input_fingerprint(
                    preview_sha256=preview_sha256,
                    preview_fingerprint=preview_fingerprint,
                    preview_version=preview_version,
                    media_type=media_type,
                    quality=quality,
                    provider=provider,
                    options=options,
                )
                cache_candidate = _cache_hit(
                    connection,
                    media_id=media_id,
                    input_fingerprint=input_fingerprint,
                    preview_fingerprint=preview_fingerprint,
                    preview_version=preview_version,
                    provider=provider,
                    options=options,
                )
                if cache_candidate:
                    if verify_cache_artifacts:
                        try:
                            _controlled_preview_digest(workspace, row[2], preview_sha256)
                        except AnalysisError:
                            stats.add_failure(media_id)
                            continue
                    stats.cache_hits += 1
                    continue
                try:
                    preview_size = _controlled_preview_size(workspace, row[2])
                except AnalysisError:
                    stats.add_failure(media_id)
                    continue
                stats.pending_count += 1
                stats.upload_bytes += preview_size
                yield _PendingDescriptor(
                    item_id=media_id,
                    media_type=media_type,
                    preview_path=str(row[2]),
                    preview_sha256=preview_sha256,
                    quality=quality,
                    input_fingerprint=input_fingerprint,
                    preview_fingerprint=preview_fingerprint,
                    preview_version=preview_version,
                    upload_bytes=preview_size,
                )

    return generate(), stats


def _descriptor_batches(
    descriptors: Iterable[_PendingDescriptor], size: int
) -> Iterator[list[_PendingDescriptor]]:
    iterator = iter(descriptors)
    while batch := list(islice(iterator, size)):
        yield batch


def _load_pending_batch(
    workspace: Workspace, descriptors: list[_PendingDescriptor]
) -> tuple[list[_PendingItem], list[tuple[int, str]]]:
    pending: list[_PendingItem] = []
    failures: list[tuple[int, str]] = []
    for descriptor in descriptors:
        try:
            preview_path, preview_bytes, actual_digest = _controlled_preview_bytes(
                workspace, descriptor.preview_path, descriptor.preview_sha256
            )
        except AnalysisError:
            failures.append((descriptor.item_id, "PREVIEW_READ_ERROR"))
            continue
        if actual_digest != descriptor.preview_sha256:
            failures.append((descriptor.item_id, "PREVIEW_READ_ERROR"))
            continue
        pending.append(
            _PendingItem(
                request_item=VisionRequestItem(
                    item_id=descriptor.item_id,
                    media_type=descriptor.media_type,
                    preview_mime_type=_mime_type(preview_path),
                    preview_bytes=preview_bytes,
                    quality=descriptor.quality,
                ),
                input_fingerprint=descriptor.input_fingerprint,
                preview_fingerprint=descriptor.preview_fingerprint,
                preview_version=descriptor.preview_version,
            )
        )
    return pending, failures


def estimate_workspace_analysis(
    workspace: Workspace,
    *,
    provider: VisionProvider,
    options: AnalysisOptions = _DEFAULT_ANALYSIS_OPTIONS,
    allow_cloud: bool | None = None,
) -> AnalysisEstimate:
    """Estimate requests and upload bytes without calling any provider."""
    _cloud_allowed(workspace, provider, allow_cloud)
    with _database_boundary(workspace.database_path, read_only=True) as connection:
        descriptors, stats = _iter_descriptors(
            workspace,
            connection,
            provider,
            options,
            verify_cache_artifacts=False,
        )
        for _descriptor in descriptors:
            pass
    return AnalysisEstimate(
        item_count=stats.item_count,
        pending_count=stats.pending_count,
        cache_hit_count=stats.cache_hits,
        upload_bytes=stats.upload_bytes,
        request_batch_count=math.ceil(stats.pending_count / options.batch_size),
    )


def _persist_analysis(
    connection: sqlite3.Connection,
    item: _PendingItem,
    analysis: VisionAnalysis,
    provider: VisionProvider,
    options: AnalysisOptions,
) -> None:
    connection.execute(
        """
        INSERT INTO ai_analysis(
            media_id,input_fingerprint,preview_fingerprint,preview_version,
            provider,model,prompt_version,schema_version,scene_category,
            disposition,confidence,quality_score,tags_json,short_desc,reason,created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(
            media_id,input_fingerprint,preview_fingerprint,preview_version,
            provider,model,prompt_version,schema_version
        ) DO NOTHING
        """,
        (
            analysis.item_id,
            item.input_fingerprint,
            item.preview_fingerprint,
            item.preview_version,
            provider.name,
            provider.model,
            options.prompt_version,
            options.schema_version,
            analysis.scene_category,
            analysis.disposition,
            analysis.confidence,
            analysis.quality_score,
            json.dumps(analysis.tags, ensure_ascii=False, separators=(",", ":")),
            analysis.short_desc,
            analysis.reason,
            _utc_now(),
        ),
    )


def analyze_workspace(
    workspace: Workspace,
    *,
    provider: VisionProvider,
    options: AnalysisOptions = _DEFAULT_ANALYSIS_OPTIONS,
    allow_cloud: bool | None = None,
    retry_sleep: Callable[[float], None] = time.sleep,
) -> AnalyzeResult:
    """Analyze READY previews with bounded retry, split isolation, and versioned cache."""
    _cloud_allowed(workspace, provider, allow_cloud)
    if not provider.name or not provider.model:
        raise ValueError("provider name and model must not be empty")

    run_id = uuid4().hex
    analyzed_count = 0
    request_count = 0
    failures: list[tuple[int, str]] = []
    with _database_boundary(workspace.database_path) as connection:
        summary_failures: list[tuple[int, str]] = []
        summary_descriptors, summary = _iter_descriptors(
            workspace,
            connection,
            provider,
            options,
            verify_cache_artifacts=True,
        )
        summary.failures = summary_failures
        for _descriptor in summary_descriptors:
            pass
        item_count = summary.item_count
        cache_hits = summary.cache_hits
        batch_count = math.ceil(summary.pending_count / options.batch_size)
        try:
            connection.execute(
                """
                INSERT INTO ai_run(
                    id,provider,model,prompt_version,schema_version,started_at,status,
                    item_count,pending_count,cache_hit_count,failed_count,
                    upload_bytes,batch_count
                ) VALUES (?,?,?,?,?,?,'RUNNING',?,?,?,?,?,?)
                """,
                (
                    run_id,
                    provider.name,
                    provider.model,
                    options.prompt_version,
                    options.schema_version,
                    _utc_now(),
                    item_count,
                    summary.pending_count,
                    cache_hits,
                    len(summary_failures),
                    summary.upload_bytes,
                    batch_count,
                ),
            )
            connection.commit()
        except sqlite3.Error:
            _best_effort_rollback(connection)
            raise AnalysisError("DB_WRITE_ERROR") from None

        def isolate_or_fail(batch: list[_PendingItem], code: str, budget: _BatchBudget) -> None:
            if len(batch) == 1:
                failures.append((batch[0].request_item.item_id, code))
                return
            midpoint = len(batch) // 2
            process_batch(batch[:midpoint], budget)
            process_batch(batch[midpoint:], budget)

        def process_batch(batch: list[_PendingItem], budget: _BatchBudget) -> None:
            nonlocal analyzed_count, request_count
            request = VisionRequest(
                items=tuple(item.request_item for item in batch),
                prompt_version=options.prompt_version,
                schema_version=options.schema_version,
            )
            while True:
                if budget.requests_remaining <= 0:
                    failures.extend(
                        (item.request_item.item_id, "TRANSIENT_RETRY_EXHAUSTED") for item in batch
                    )
                    return
                budget.requests_remaining -= 1
                request_count += 1
                try:
                    raw_response = provider.analyze(request)
                    mapped = validate_response_mapping(
                        raw_response,
                        expected_item_ids=tuple(item.request_item.item_id for item in batch),
                        confidence_threshold=options.confidence_threshold,
                    )
                    break
                except TransientProviderError:
                    if budget.retries_remaining <= 0:
                        failures.extend(
                            (
                                item.request_item.item_id,
                                "TRANSIENT_RETRY_EXHAUSTED",
                            )
                            for item in batch
                        )
                        return
                    retry_index = options.max_retries - budget.retries_remaining
                    budget.retries_remaining -= 1
                    retry_sleep(min(1.0, 0.05 * (2**retry_index)))
                except PermanentProviderError:
                    raise PermanentProviderError("GLOBAL_PERMANENT_PROVIDER_ERROR") from None
                except SplittableProviderError:
                    isolate_or_fail(batch, "ITEM_SPLITTABLE_PROVIDER_ERROR", budget)
                    return
                except SchemaValidationError:
                    isolate_or_fail(batch, "ITEM_SCHEMA_ERROR", budget)
                    return
                except TimeoutError:
                    if budget.retries_remaining <= 0:
                        failures.extend(
                            (
                                item.request_item.item_id,
                                "TRANSIENT_RETRY_EXHAUSTED",
                            )
                            for item in batch
                        )
                        return
                    retry_index = options.max_retries - budget.retries_remaining
                    budget.retries_remaining -= 1
                    retry_sleep(min(1.0, 0.05 * (2**retry_index)))
                except Exception:
                    raise PermanentProviderError("GLOBAL_PERMANENT_PROVIDER_ERROR") from None
            by_id = {item.request_item.item_id: item for item in batch}
            try:
                for media_id, analysis in mapped.items():
                    _persist_analysis(connection, by_id[media_id], analysis, provider, options)
                connection.commit()
            except sqlite3.Error:
                _best_effort_rollback(connection)
                failures.extend((item.request_item.item_id, "DB_WRITE_ERROR") for item in batch)
                raise AnalysisError("DB_WRITE_ERROR") from None
            analyzed_count += len(mapped)

        execution_stats: _DescriptorStats | None = None
        try:
            descriptor_stream, stream_stats = _iter_descriptors(
                workspace,
                connection,
                provider,
                options,
                verify_cache_artifacts=True,
            )
            execution_stats = stream_stats
            stream_stats.failures = failures
            for descriptors in _descriptor_batches(descriptor_stream, options.batch_size):
                batch, load_failures = _load_pending_batch(workspace, descriptors)
                failures.extend(load_failures)
                if batch:
                    process_batch(
                        batch,
                        _BatchBudget(
                            retries_remaining=options.max_retries,
                            requests_remaining=2 * len(batch) - 1 + options.max_retries,
                        ),
                    )
            item_count = stream_stats.item_count
            cache_hits = stream_stats.cache_hits
            status = "COMPLETE_WITH_FAILURES" if failures else "COMPLETE"
            connection.execute(
                """
                UPDATE ai_run
                SET completed_at=?,status=?,item_count=?,pending_count=?,cache_hit_count=?,
                    analyzed_count=?,failed_count=?,request_count=?,upload_bytes=?,batch_count=?
                WHERE id=?
                """,
                (
                    _utc_now(),
                    status,
                    item_count,
                    stream_stats.pending_count,
                    cache_hits,
                    analyzed_count,
                    len(failures),
                    request_count,
                    stream_stats.upload_bytes,
                    math.ceil(stream_stats.pending_count / options.batch_size),
                    run_id,
                ),
            )
            connection.commit()
        except BaseException as original_error:
            _best_effort_rollback(connection)
            if execution_stats is not None:
                cache_hits = min(summary.cache_hits, execution_stats.cache_hits)
                pending_count = execution_stats.pending_count
            else:
                cache_hits = summary.cache_hits
                pending_count = summary.pending_count
            item_count = summary.item_count
            failed_count = max(len(failures), item_count - cache_hits - analyzed_count)
            try:
                connection.execute(
                    """
                    UPDATE ai_run
                    SET completed_at=?,status='FAILED',item_count=?,pending_count=?,
                        cache_hit_count=?,analyzed_count=?,failed_count=?,request_count=?
                    WHERE id=?
                    """,
                    (
                        _utc_now(),
                        item_count,
                        pending_count,
                        cache_hits,
                        analyzed_count,
                        failed_count,
                        request_count,
                        run_id,
                    ),
                )
                connection.commit()
            except sqlite3.Error:
                _best_effort_rollback(connection)
                raise AnalysisError("DB_WRITE_ERROR") from None
            if isinstance(original_error, sqlite3.Error):
                raise AnalysisError("DB_WRITE_ERROR") from None
            raise
    return AnalyzeResult(
        run_id=run_id,
        analyzed_count=analyzed_count,
        cache_hit_count=cache_hits,
        failed_count=len(failures),
        request_count=request_count,
        failures=tuple(sorted(failures)),
    )
