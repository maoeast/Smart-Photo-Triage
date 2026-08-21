"""SQLite persistence for v1.2.1 provider cache and route audit.

Only normalized result fields and redacted routing metadata are persisted.  Raw media,
paths, content hashes, API keys, authorization headers, and request bodies never enter
these tables.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

from smart_photo_triage.ai import VisionAnalysis, validate_analysis_result
from smart_photo_triage.model_routing import ProviderConfig, RouteExecution, TaskType


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _result_payload(result: dict[int, VisionAnalysis]) -> str:
    rows = [
        {
            "item_id": item_id,
            "scene_category": analysis.scene_category,
            "disposition": analysis.disposition,
            "confidence": analysis.confidence,
            "quality_score": analysis.quality_score,
            "tags": list(analysis.tags),
            "short_desc": analysis.short_desc,
            "reason": analysis.reason,
        }
        for item_id, analysis in sorted(result.items())
    ]
    return json.dumps(rows, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class SQLiteProviderCache:
    """A provider-result cache keyed by the non-sensitive router fingerprint."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def get(self, cache_key: str) -> dict[int, VisionAnalysis] | None:
        row = self.connection.execute(
            "SELECT result_json FROM provider_analysis_cache WHERE cache_key=?", (cache_key,)
        ).fetchone()
        if row is None:
            return None
        try:
            raw = json.loads(str(row[0]))
            if not isinstance(raw, list):
                raise ValueError
            result = {analysis.item_id: analysis for analysis in map(validate_analysis_result, raw)}
            if len(result) != len(raw):
                raise ValueError
            return result
        except (ValueError, TypeError, json.JSONDecodeError):
            # Corrupt cache must never become a business result.  Leave it for diagnosis,
            # but force a fresh provider result rather than deleting history.
            return None

    def put(
        self,
        cache_key: str,
        task_type: TaskType,
        provider: ProviderConfig,
        result: dict[int, VisionAnalysis],
    ) -> None:
        self.connection.execute(
            """
            INSERT OR IGNORE INTO provider_analysis_cache(
                cache_key,task_type,provider_id,driver,model,endpoint_identity,
                capability_profile_version,result_json,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                cache_key,
                task_type.value,
                provider.provider_id,
                provider.driver,
                provider.model,
                provider.endpoint_identity(),
                provider.capabilities.capability_profile_version,
                _result_payload(result),
                _utc_now(),
            ),
        )
        self.connection.commit()


def persist_route_execution(connection: sqlite3.Connection, execution: RouteExecution) -> str:
    """Persist a route decision and attempts.  Inputs have already been redacted by design."""
    run_id = uuid4().hex
    now = _utc_now()
    effective = execution.effective_provider_id
    effective_model = next(
        (
            attempt.model
            for attempt in reversed(execution.attempts)
            if attempt.provider_id == effective
        ),
        None,
    )
    connection.execute(
        """
        INSERT INTO ai_route_run(
            id,task_type,started_at,completed_at,status,effective_provider_id,effective_model,
            escalated,attempt_count
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            run_id,
            execution.task_type.value,
            now,
            now,
            "COMPLETE" if execution.result is not None else "FAILED",
            effective,
            effective_model,
            int(execution.escalated),
            len(execution.attempts),
        ),
    )
    connection.executemany(
        """
        INSERT INTO ai_route_attempt(
            route_run_id,attempt_index,provider_id,driver,model,status,error_class,cache_hit,
            remote_preview_bytes,route_reason
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (
                run_id,
                attempt.attempt_index,
                attempt.provider_id,
                attempt.driver,
                attempt.model,
                attempt.status,
                attempt.error_class.value if attempt.error_class is not None else None,
                int(attempt.cache_hit),
                attempt.remote_preview_bytes,
                attempt.route_reason,
            )
            for attempt in execution.attempts
        ],
    )
    connection.commit()
    return run_id
