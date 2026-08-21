"""Versioned SQLite schema and migration contract."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4


class MigrationError(RuntimeError):
    """Raised when the on-disk schema cannot be migrated safely."""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]


MIGRATIONS = (
    Migration(
        version=1,
        name="create_workspace_metadata",
        statements=(
            """
            CREATE TABLE schema_migration (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE TABLE workspace_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """,
        ),
    ),
    Migration(
        version=2,
        name="create_phase_b_scan_schema",
        statements=(
            """
            CREATE TABLE scan_run (
                id TEXT PRIMARY KEY,
                source_root TEXT NOT NULL,
                source_root_key TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                status TEXT NOT NULL,
                discovered_count INTEGER NOT NULL DEFAULT 0,
                indexed_count INTEGER NOT NULL DEFAULT 0,
                unchanged_count INTEGER NOT NULL DEFAULT 0,
                missing_count INTEGER NOT NULL DEFAULT 0,
                error_count INTEGER NOT NULL DEFAULT 0,
                warning_count INTEGER NOT NULL DEFAULT 0,
                bundle_count INTEGER NOT NULL DEFAULT 0
            )
            """,
            """
            CREATE TABLE media_item (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                original_path TEXT NOT NULL,
                path_key TEXT NOT NULL UNIQUE,
                source_root TEXT NOT NULL,
                source_root_key TEXT NOT NULL,
                parent_key TEXT NOT NULL,
                bundle_stem TEXT NOT NULL,
                media_type TEXT NOT NULL,
                extension TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                source_present INTEGER NOT NULL DEFAULT 1,
                content_sha256 TEXT,
                captured_at TEXT,
                capture_source TEXT NOT NULL,
                capture_confidence TEXT NOT NULL,
                capture_timezone_status TEXT NOT NULL,
                width INTEGER,
                height INTEGER,
                duration_seconds REAL,
                preview_path TEXT,
                preview_version TEXT,
                last_seen_at TEXT NOT NULL,
                last_seen_scan_id TEXT NOT NULL,
                metadata_error TEXT
            )
            """,
            """
            CREATE INDEX media_item_source_seen_idx
            ON media_item(source_root_key, source_present, last_seen_scan_id)
            """,
            """
            CREATE INDEX media_item_bundle_idx
            ON media_item(source_root_key, source_present, parent_key, bundle_stem, path_key)
            """,
            """
            CREATE TABLE asset_bundle (
                id TEXT PRIMARY KEY,
                bundle_type TEXT NOT NULL,
                bundle_key TEXT NOT NULL UNIQUE,
                source_root_key TEXT NOT NULL,
                warning_status TEXT
            )
            """,
            """
            CREATE TABLE bundle_member (
                bundle_id TEXT NOT NULL REFERENCES asset_bundle(id) ON DELETE CASCADE,
                media_id INTEGER NOT NULL REFERENCES media_item(id) ON DELETE CASCADE,
                role TEXT NOT NULL,
                PRIMARY KEY (bundle_id, media_id)
            )
            """,
            """
            CREATE INDEX bundle_member_media_idx ON bundle_member(media_id)
            """,
            """
            CREATE TABLE scan_warning (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id TEXT NOT NULL REFERENCES scan_run(id) ON DELETE CASCADE,
                media_id INTEGER REFERENCES media_item(id) ON DELETE SET NULL,
                path_key TEXT,
                code TEXT NOT NULL,
                message TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX scan_warning_run_idx ON scan_warning(scan_id, code)
            """,
        ),
    ),
    Migration(
        version=3,
        name="add_scan_resume_state",
        statements=(
            """
            ALTER TABLE scan_run
            ADD COLUMN config_fingerprint TEXT NOT NULL DEFAULT ''
            """,
            """
            ALTER TABLE scan_run
            ADD COLUMN resume_of TEXT REFERENCES scan_run(id)
            """,
            """
            CREATE INDEX scan_run_resume_idx
            ON scan_run(source_root_key, config_fingerprint, status, started_at)
            """,
        ),
    ),
    Migration(
        version=4,
        name="add_scan_owner_audit",
        statements=(
            """
            ALTER TABLE scan_run
            ADD COLUMN owner_pid INTEGER
            """,
            """
            ALTER TABLE scan_run
            ADD COLUMN owner_token TEXT
            """,
            """
            ALTER TABLE scan_run
            ADD COLUMN terminal_reason TEXT
            """,
        ),
    ),
    Migration(
        version=5,
        name="create_phase_c_preview_and_group_schema",
        statements=(
            """
            CREATE TABLE preprocess_run (
                id TEXT PRIMARY KEY,
                config_fingerprint TEXT NOT NULL,
                preview_version TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                status TEXT NOT NULL,
                processed_count INTEGER NOT NULL DEFAULT 0,
                cache_hit_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0
            )
            """,
            """
            CREATE INDEX preprocess_run_status_idx
            ON preprocess_run(status, started_at)
            """,
            """
            CREATE TABLE media_preprocess (
                media_id INTEGER PRIMARY KEY REFERENCES media_item(id) ON DELETE CASCADE,
                source_fingerprint TEXT NOT NULL,
                preview_fingerprint TEXT NOT NULL,
                preview_path TEXT,
                preview_version TEXT NOT NULL,
                preview_status TEXT NOT NULL,
                preview_error TEXT,
                perceptual_hash TEXT,
                perceptual_hash_version TEXT,
                quality_json TEXT,
                quality_score REAL,
                preview_width INTEGER,
                preview_height INTEGER,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX media_preprocess_status_idx
            ON media_preprocess(preview_status, preview_version, media_id)
            """,
            """
            CREATE INDEX media_preprocess_hash_idx
            ON media_preprocess(perceptual_hash_version, perceptual_hash, media_id)
            """,
            """
            CREATE TABLE duplicate_group (
                id TEXT PRIMARY KEY,
                algorithm_version TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (algorithm_version, content_sha256)
            )
            """,
            """
            CREATE TABLE duplicate_member (
                group_id TEXT NOT NULL REFERENCES duplicate_group(id) ON DELETE CASCADE,
                media_id INTEGER NOT NULL REFERENCES media_item(id) ON DELETE CASCADE,
                PRIMARY KEY (group_id, media_id)
            )
            """,
            """
            CREATE INDEX duplicate_member_media_idx ON duplicate_member(media_id)
            """,
            """
            CREATE TABLE burst_group (
                id TEXT PRIMARY KEY,
                algorithm_version TEXT NOT NULL,
                representative_media_id INTEGER NOT NULL
                    REFERENCES media_item(id) ON DELETE CASCADE,
                time_window_seconds REAL NOT NULL,
                distance_threshold INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE burst_member (
                group_id TEXT NOT NULL REFERENCES burst_group(id) ON DELETE CASCADE,
                media_id INTEGER NOT NULL REFERENCES media_item(id) ON DELETE CASCADE,
                distance INTEGER NOT NULL,
                quality_score REAL NOT NULL,
                is_representative INTEGER NOT NULL,
                is_best_shot INTEGER NOT NULL,
                PRIMARY KEY (group_id, media_id)
            )
            """,
            """
            CREATE INDEX burst_member_media_idx ON burst_member(media_id)
            """,
            """
            CREATE INDEX media_item_phase_c_candidates_idx
            ON media_item(source_present, media_type, size_bytes, captured_at, id)
            """,
        ),
    ),
    Migration(
        version=6,
        name="add_phase_c_grouping_audit",
        statements=(
            """
            ALTER TABLE burst_group
            ADD COLUMN comparison_cap INTEGER NOT NULL DEFAULT 32
            """,
            """
            CREATE TABLE grouping_run (
                id TEXT PRIMARY KEY,
                algorithm_version TEXT NOT NULL,
                time_window_seconds REAL NOT NULL,
                distance_threshold INTEGER NOT NULL,
                comparison_cap INTEGER NOT NULL,
                comparison_count INTEGER NOT NULL,
                warning_count INTEGER NOT NULL,
                warning_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
        ),
    ),
    Migration(
        version=7,
        name="add_preview_artifact_identity",
        statements=(
            """
            ALTER TABLE media_preprocess
            ADD COLUMN preview_sha256 TEXT NOT NULL DEFAULT ''
            """,
        ),
    ),
    Migration(
        version=8,
        name="persist_preprocess_deferred_count",
        statements=(
            """
            ALTER TABLE preprocess_run
            ADD COLUMN deferred_count INTEGER NOT NULL DEFAULT 0
            """,
        ),
    ),
    Migration(
        version=9,
        name="create_phase_d_ai_analysis_schema",
        statements=(
            """
            CREATE TABLE ai_run (
                id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                status TEXT NOT NULL,
                item_count INTEGER NOT NULL DEFAULT 0,
                pending_count INTEGER NOT NULL DEFAULT 0,
                cache_hit_count INTEGER NOT NULL DEFAULT 0,
                analyzed_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                request_count INTEGER NOT NULL DEFAULT 0,
                upload_bytes INTEGER NOT NULL DEFAULT 0,
                batch_count INTEGER NOT NULL DEFAULT 0
            )
            """,
            """
            CREATE INDEX ai_run_status_idx
            ON ai_run(status, started_at)
            """,
            """
            CREATE TABLE ai_analysis (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                media_id INTEGER NOT NULL REFERENCES media_item(id) ON DELETE CASCADE,
                input_fingerprint TEXT NOT NULL,
                preview_fingerprint TEXT NOT NULL,
                preview_version TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                scene_category TEXT NOT NULL,
                disposition TEXT NOT NULL,
                confidence REAL NOT NULL,
                quality_score REAL NOT NULL,
                tags_json TEXT NOT NULL,
                short_desc TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (
                    media_id, input_fingerprint, preview_fingerprint, preview_version,
                    provider, model, prompt_version, schema_version
                )
            )
            """,
            """
            CREATE INDEX ai_analysis_latest_idx
            ON ai_analysis(media_id, created_at, id)
            """,
        ),
    ),
    Migration(
        version=10,
        name="create_phase_e_review_decision_schema",
        statements=(
            """
            CREATE TABLE review_decision (
                media_id INTEGER PRIMARY KEY REFERENCES media_item(id) ON DELETE CASCADE,
                scene_category TEXT NOT NULL CHECK (
                    scene_category IN (
                        '01_家庭生活', '02_旅行风光', '03_工作与文档',
                        '04_截图与备忘', '05_其他'
                    )
                ),
                disposition TEXT NOT NULL CHECK (
                    disposition IN ('KEEP', 'REVIEW', 'REJECT_CANDIDATE')
                ),
                decision_source TEXT NOT NULL CHECK (decision_source = 'HUMAN'),
                revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX review_decision_filter_idx
            ON review_decision(scene_category, disposition, media_id)
            """,
        ),
    ),
    Migration(
        version=11,
        name="create_phase_f_plan_and_journal_schema",
        statements=(
            """
            CREATE TABLE organization_plan (
                plan_id TEXT PRIMARY KEY,
                schema_version TEXT NOT NULL,
                planner_version TEXT NOT NULL,
                created_at TEXT NOT NULL,
                config_fingerprint TEXT NOT NULL,
                source_root_fingerprint TEXT NOT NULL,
                output_root TEXT NOT NULL,
                mode TEXT NOT NULL CHECK (mode IN ('COPY', 'MOVE')),
                max_path_chars INTEGER NOT NULL CHECK (max_path_chars >= 64),
                payload_sha256 TEXT NOT NULL UNIQUE,
                canonical_json TEXT NOT NULL,
                warning_json TEXT NOT NULL,
                entry_count INTEGER NOT NULL CHECK (entry_count >= 0)
            )
            """,
            """
            CREATE TABLE plan_entry (
                plan_id TEXT NOT NULL REFERENCES organization_plan(plan_id),
                position INTEGER NOT NULL CHECK (position >= 0),
                media_id INTEGER NOT NULL,
                bundle_id TEXT,
                bundle_role TEXT,
                source_root TEXT NOT NULL,
                source_path TEXT NOT NULL,
                target_path TEXT NOT NULL,
                target_key TEXT NOT NULL,
                action TEXT NOT NULL CHECK (action IN ('COPY', 'MOVE')),
                expected_size INTEGER NOT NULL CHECK (expected_size >= 0),
                expected_sha256 TEXT NOT NULL,
                source_mtime_ns INTEGER NOT NULL,
                decision_source TEXT NOT NULL CHECK (
                    decision_source IN ('HUMAN', 'AI', 'RULE')
                ),
                scene_category TEXT NOT NULL,
                disposition TEXT NOT NULL CHECK (
                    disposition IN ('KEEP', 'REVIEW', 'REJECT_CANDIDATE')
                ),
                PRIMARY KEY (plan_id, position),
                UNIQUE (plan_id, target_key)
            )
            """,
            """
            CREATE INDEX plan_entry_media_idx ON plan_entry(media_id, plan_id)
            """,
            """
            CREATE TABLE plan_approval (
                plan_id TEXT PRIMARY KEY REFERENCES organization_plan(plan_id),
                state TEXT NOT NULL CHECK (state IN ('PENDING', 'APPROVED', 'REVOKED')),
                revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
                approved_at TEXT,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX plan_approval_state_idx ON plan_approval(state, plan_id)
            """,
            """
            CREATE TABLE operation_transaction (
                transaction_id TEXT PRIMARY KEY,
                plan_id TEXT NOT NULL REFERENCES organization_plan(plan_id),
                mode TEXT NOT NULL CHECK (mode IN ('COPY', 'MOVE')),
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX operation_transaction_state_idx
            ON operation_transaction(state, updated_at)
            """,
            """
            CREATE TABLE operation_journal (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                transaction_id TEXT NOT NULL
                    REFERENCES operation_transaction(transaction_id),
                media_id INTEGER NOT NULL,
                bundle_id TEXT,
                operation TEXT NOT NULL,
                source_path TEXT NOT NULL,
                target_path TEXT NOT NULL,
                source_sha256 TEXT NOT NULL,
                target_sha256 TEXT,
                state TEXT NOT NULL,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX operation_journal_transaction_idx
            ON operation_journal(transaction_id, state, id)
            """,
            """
            CREATE TRIGGER organization_plan_no_update
            BEFORE UPDATE ON organization_plan
            BEGIN
                SELECT RAISE(ABORT, 'organization plan is immutable');
            END
            """,
            """
            CREATE TRIGGER organization_plan_no_delete
            BEFORE DELETE ON organization_plan
            BEGIN
                SELECT RAISE(ABORT, 'organization plan is immutable');
            END
            """,
            """
            CREATE TRIGGER plan_entry_no_update
            BEFORE UPDATE ON plan_entry
            BEGIN
                SELECT RAISE(ABORT, 'plan entry is immutable');
            END
            """,
            """
            CREATE TRIGGER plan_entry_no_delete
            BEFORE DELETE ON plan_entry
            BEGIN
                SELECT RAISE(ABORT, 'plan entry is immutable');
            END
            """,
        ),
    ),
    Migration(
        version=12,
        name="create_v121_provider_cache_and_route_audit",
        statements=(
            """
            CREATE TABLE provider_analysis_cache (
                cache_key TEXT PRIMARY KEY,
                task_type TEXT NOT NULL CHECK (task_type IN ('item_analysis', 'burst_review')),
                provider_id TEXT NOT NULL,
                driver TEXT NOT NULL,
                model TEXT NOT NULL,
                endpoint_identity TEXT NOT NULL,
                capability_profile_version TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX provider_analysis_cache_provider_idx
            ON provider_analysis_cache(provider_id, model, created_at)
            """,
            """
            CREATE TABLE ai_route_run (
                id TEXT PRIMARY KEY,
                task_type TEXT NOT NULL CHECK (task_type IN ('item_analysis', 'burst_review')),
                started_at TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                status TEXT NOT NULL,
                effective_provider_id TEXT,
                effective_model TEXT,
                escalated INTEGER NOT NULL CHECK (escalated IN (0, 1)),
                attempt_count INTEGER NOT NULL CHECK (attempt_count >= 0)
            )
            """,
            """
            CREATE INDEX ai_route_run_task_idx
            ON ai_route_run(task_type, started_at)
            """,
            """
            CREATE TABLE ai_route_attempt (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                route_run_id TEXT NOT NULL REFERENCES ai_route_run(id) ON DELETE CASCADE,
                attempt_index INTEGER NOT NULL CHECK (attempt_index >= 1),
                provider_id TEXT NOT NULL,
                driver TEXT NOT NULL,
                model TEXT NOT NULL,
                status TEXT NOT NULL,
                error_class TEXT,
                cache_hit INTEGER NOT NULL CHECK (cache_hit IN (0, 1)),
                remote_preview_bytes INTEGER NOT NULL CHECK (remote_preview_bytes >= 0),
                route_reason TEXT NOT NULL,
                UNIQUE(route_run_id, attempt_index)
            )
            """,
            """
            CREATE INDEX ai_route_attempt_provider_idx
            ON ai_route_attempt(provider_id, model, status)
            """,
        ),
    ),
)

_SCAN_RUN_COLUMNS_V2 = (
    ("id", "TEXT", 0, 1),
    ("source_root", "TEXT", 1, 0),
    ("source_root_key", "TEXT", 1, 0),
    ("started_at", "TEXT", 1, 0),
    ("completed_at", "TEXT", 0, 0),
    ("status", "TEXT", 1, 0),
    ("discovered_count", "INTEGER", 1, 0),
    ("indexed_count", "INTEGER", 1, 0),
    ("unchanged_count", "INTEGER", 1, 0),
    ("missing_count", "INTEGER", 1, 0),
    ("error_count", "INTEGER", 1, 0),
    ("warning_count", "INTEGER", 1, 0),
    ("bundle_count", "INTEGER", 1, 0),
)

_SCAN_RUN_COLUMNS_V3 = _SCAN_RUN_COLUMNS_V2 + (
    ("config_fingerprint", "TEXT", 1, 0),
    ("resume_of", "TEXT", 0, 0),
)

_SCAN_RUN_COLUMNS_V4 = _SCAN_RUN_COLUMNS_V3 + (
    ("owner_pid", "INTEGER", 0, 0),
    ("owner_token", "TEXT", 0, 0),
    ("terminal_reason", "TEXT", 0, 0),
)

_BURST_GROUP_COLUMNS_V5 = (
    ("id", "TEXT", 0, 1),
    ("algorithm_version", "TEXT", 1, 0),
    ("representative_media_id", "INTEGER", 1, 0),
    ("time_window_seconds", "REAL", 1, 0),
    ("distance_threshold", "INTEGER", 1, 0),
    ("created_at", "TEXT", 1, 0),
)

_MEDIA_PREPROCESS_COLUMNS_V6 = (
    ("media_id", "INTEGER", 0, 1),
    ("source_fingerprint", "TEXT", 1, 0),
    ("preview_fingerprint", "TEXT", 1, 0),
    ("preview_path", "TEXT", 0, 0),
    ("preview_version", "TEXT", 1, 0),
    ("preview_status", "TEXT", 1, 0),
    ("preview_error", "TEXT", 0, 0),
    ("perceptual_hash", "TEXT", 0, 0),
    ("perceptual_hash_version", "TEXT", 0, 0),
    ("quality_json", "TEXT", 0, 0),
    ("quality_score", "REAL", 0, 0),
    ("preview_width", "INTEGER", 0, 0),
    ("preview_height", "INTEGER", 0, 0),
    ("updated_at", "TEXT", 1, 0),
)

_PREPROCESS_RUN_COLUMNS_V7 = (
    ("id", "TEXT", 0, 1),
    ("config_fingerprint", "TEXT", 1, 0),
    ("preview_version", "TEXT", 1, 0),
    ("started_at", "TEXT", 1, 0),
    ("completed_at", "TEXT", 0, 0),
    ("status", "TEXT", 1, 0),
    ("processed_count", "INTEGER", 1, 0),
    ("cache_hit_count", "INTEGER", 1, 0),
    ("failed_count", "INTEGER", 1, 0),
)

_REQUIRED_COLUMNS = {
    "schema_migration": (
        ("version", "INTEGER", 0, 1),
        ("name", "TEXT", 1, 0),
        ("applied_at", "TEXT", 1, 0),
    ),
    "workspace_metadata": (
        ("key", "TEXT", 0, 1),
        ("value", "TEXT", 1, 0),
    ),
    "scan_run": (*_SCAN_RUN_COLUMNS_V4,),
    "media_item": (
        ("id", "INTEGER", 0, 1),
        ("original_path", "TEXT", 1, 0),
        ("path_key", "TEXT", 1, 0),
        ("source_root", "TEXT", 1, 0),
        ("source_root_key", "TEXT", 1, 0),
        ("parent_key", "TEXT", 1, 0),
        ("bundle_stem", "TEXT", 1, 0),
        ("media_type", "TEXT", 1, 0),
        ("extension", "TEXT", 1, 0),
        ("size_bytes", "INTEGER", 1, 0),
        ("mtime_ns", "INTEGER", 1, 0),
        ("source_present", "INTEGER", 1, 0),
        ("content_sha256", "TEXT", 0, 0),
        ("captured_at", "TEXT", 0, 0),
        ("capture_source", "TEXT", 1, 0),
        ("capture_confidence", "TEXT", 1, 0),
        ("capture_timezone_status", "TEXT", 1, 0),
        ("width", "INTEGER", 0, 0),
        ("height", "INTEGER", 0, 0),
        ("duration_seconds", "REAL", 0, 0),
        ("preview_path", "TEXT", 0, 0),
        ("preview_version", "TEXT", 0, 0),
        ("last_seen_at", "TEXT", 1, 0),
        ("last_seen_scan_id", "TEXT", 1, 0),
        ("metadata_error", "TEXT", 0, 0),
    ),
    "asset_bundle": (
        ("id", "TEXT", 0, 1),
        ("bundle_type", "TEXT", 1, 0),
        ("bundle_key", "TEXT", 1, 0),
        ("source_root_key", "TEXT", 1, 0),
        ("warning_status", "TEXT", 0, 0),
    ),
    "bundle_member": (
        ("bundle_id", "TEXT", 1, 1),
        ("media_id", "INTEGER", 1, 2),
        ("role", "TEXT", 1, 0),
    ),
    "scan_warning": (
        ("id", "INTEGER", 0, 1),
        ("scan_id", "TEXT", 1, 0),
        ("media_id", "INTEGER", 0, 0),
        ("path_key", "TEXT", 0, 0),
        ("code", "TEXT", 1, 0),
        ("message", "TEXT", 1, 0),
    ),
    "preprocess_run": (*_PREPROCESS_RUN_COLUMNS_V7, ("deferred_count", "INTEGER", 1, 0)),
    "media_preprocess": (
        *_MEDIA_PREPROCESS_COLUMNS_V6,
        ("preview_sha256", "TEXT", 1, 0),
    ),
    "grouping_run": (
        ("id", "TEXT", 0, 1),
        ("algorithm_version", "TEXT", 1, 0),
        ("time_window_seconds", "REAL", 1, 0),
        ("distance_threshold", "INTEGER", 1, 0),
        ("comparison_cap", "INTEGER", 1, 0),
        ("comparison_count", "INTEGER", 1, 0),
        ("warning_count", "INTEGER", 1, 0),
        ("warning_json", "TEXT", 1, 0),
        ("created_at", "TEXT", 1, 0),
    ),
    "duplicate_group": (
        ("id", "TEXT", 0, 1),
        ("algorithm_version", "TEXT", 1, 0),
        ("content_sha256", "TEXT", 1, 0),
        ("size_bytes", "INTEGER", 1, 0),
        ("created_at", "TEXT", 1, 0),
    ),
    "duplicate_member": (
        ("group_id", "TEXT", 1, 1),
        ("media_id", "INTEGER", 1, 2),
    ),
    "burst_group": (*_BURST_GROUP_COLUMNS_V5, ("comparison_cap", "INTEGER", 1, 0)),
    "burst_member": (
        ("group_id", "TEXT", 1, 1),
        ("media_id", "INTEGER", 1, 2),
        ("distance", "INTEGER", 1, 0),
        ("quality_score", "REAL", 1, 0),
        ("is_representative", "INTEGER", 1, 0),
        ("is_best_shot", "INTEGER", 1, 0),
    ),
    "ai_run": (
        ("id", "TEXT", 0, 1),
        ("provider", "TEXT", 1, 0),
        ("model", "TEXT", 1, 0),
        ("prompt_version", "TEXT", 1, 0),
        ("schema_version", "TEXT", 1, 0),
        ("started_at", "TEXT", 1, 0),
        ("completed_at", "TEXT", 0, 0),
        ("status", "TEXT", 1, 0),
        ("item_count", "INTEGER", 1, 0),
        ("pending_count", "INTEGER", 1, 0),
        ("cache_hit_count", "INTEGER", 1, 0),
        ("analyzed_count", "INTEGER", 1, 0),
        ("failed_count", "INTEGER", 1, 0),
        ("request_count", "INTEGER", 1, 0),
        ("upload_bytes", "INTEGER", 1, 0),
        ("batch_count", "INTEGER", 1, 0),
    ),
    "ai_analysis": (
        ("id", "INTEGER", 0, 1),
        ("media_id", "INTEGER", 1, 0),
        ("input_fingerprint", "TEXT", 1, 0),
        ("preview_fingerprint", "TEXT", 1, 0),
        ("preview_version", "TEXT", 1, 0),
        ("provider", "TEXT", 1, 0),
        ("model", "TEXT", 1, 0),
        ("prompt_version", "TEXT", 1, 0),
        ("schema_version", "TEXT", 1, 0),
        ("scene_category", "TEXT", 1, 0),
        ("disposition", "TEXT", 1, 0),
        ("confidence", "REAL", 1, 0),
        ("quality_score", "REAL", 1, 0),
        ("tags_json", "TEXT", 1, 0),
        ("short_desc", "TEXT", 1, 0),
        ("reason", "TEXT", 1, 0),
        ("created_at", "TEXT", 1, 0),
    ),
    "review_decision": (
        ("media_id", "INTEGER", 0, 1),
        ("scene_category", "TEXT", 1, 0),
        ("disposition", "TEXT", 1, 0),
        ("decision_source", "TEXT", 1, 0),
        ("revision", "INTEGER", 1, 0),
        ("created_at", "TEXT", 1, 0),
        ("updated_at", "TEXT", 1, 0),
    ),
    "organization_plan": (
        ("plan_id", "TEXT", 0, 1),
        ("schema_version", "TEXT", 1, 0),
        ("planner_version", "TEXT", 1, 0),
        ("created_at", "TEXT", 1, 0),
        ("config_fingerprint", "TEXT", 1, 0),
        ("source_root_fingerprint", "TEXT", 1, 0),
        ("output_root", "TEXT", 1, 0),
        ("mode", "TEXT", 1, 0),
        ("max_path_chars", "INTEGER", 1, 0),
        ("payload_sha256", "TEXT", 1, 0),
        ("canonical_json", "TEXT", 1, 0),
        ("warning_json", "TEXT", 1, 0),
        ("entry_count", "INTEGER", 1, 0),
    ),
    "plan_entry": (
        ("plan_id", "TEXT", 1, 1),
        ("position", "INTEGER", 1, 2),
        ("media_id", "INTEGER", 1, 0),
        ("bundle_id", "TEXT", 0, 0),
        ("bundle_role", "TEXT", 0, 0),
        ("source_root", "TEXT", 1, 0),
        ("source_path", "TEXT", 1, 0),
        ("target_path", "TEXT", 1, 0),
        ("target_key", "TEXT", 1, 0),
        ("action", "TEXT", 1, 0),
        ("expected_size", "INTEGER", 1, 0),
        ("expected_sha256", "TEXT", 1, 0),
        ("source_mtime_ns", "INTEGER", 1, 0),
        ("decision_source", "TEXT", 1, 0),
        ("scene_category", "TEXT", 1, 0),
        ("disposition", "TEXT", 1, 0),
    ),
    "plan_approval": (
        ("plan_id", "TEXT", 0, 1),
        ("state", "TEXT", 1, 0),
        ("revision", "INTEGER", 1, 0),
        ("approved_at", "TEXT", 0, 0),
        ("updated_at", "TEXT", 1, 0),
    ),
    "operation_transaction": (
        ("transaction_id", "TEXT", 0, 1),
        ("plan_id", "TEXT", 1, 0),
        ("mode", "TEXT", 1, 0),
        ("state", "TEXT", 1, 0),
        ("created_at", "TEXT", 1, 0),
        ("updated_at", "TEXT", 1, 0),
    ),
    "operation_journal": (
        ("id", "INTEGER", 0, 1),
        ("transaction_id", "TEXT", 1, 0),
        ("media_id", "INTEGER", 1, 0),
        ("bundle_id", "TEXT", 0, 0),
        ("operation", "TEXT", 1, 0),
        ("source_path", "TEXT", 1, 0),
        ("target_path", "TEXT", 1, 0),
        ("source_sha256", "TEXT", 1, 0),
        ("target_sha256", "TEXT", 0, 0),
        ("state", "TEXT", 1, 0),
        ("error", "TEXT", 0, 0),
        ("created_at", "TEXT", 1, 0),
        ("updated_at", "TEXT", 1, 0),
    ),
    "provider_analysis_cache": (
        ("cache_key", "TEXT", 0, 1),
        ("task_type", "TEXT", 1, 0),
        ("provider_id", "TEXT", 1, 0),
        ("driver", "TEXT", 1, 0),
        ("model", "TEXT", 1, 0),
        ("endpoint_identity", "TEXT", 1, 0),
        ("capability_profile_version", "TEXT", 1, 0),
        ("result_json", "TEXT", 1, 0),
        ("created_at", "TEXT", 1, 0),
    ),
    "ai_route_run": (
        ("id", "TEXT", 0, 1),
        ("task_type", "TEXT", 1, 0),
        ("started_at", "TEXT", 1, 0),
        ("completed_at", "TEXT", 1, 0),
        ("status", "TEXT", 1, 0),
        ("effective_provider_id", "TEXT", 0, 0),
        ("effective_model", "TEXT", 0, 0),
        ("escalated", "INTEGER", 1, 0),
        ("attempt_count", "INTEGER", 1, 0),
    ),
    "ai_route_attempt": (
        ("id", "INTEGER", 0, 1),
        ("route_run_id", "TEXT", 1, 0),
        ("attempt_index", "INTEGER", 1, 0),
        ("provider_id", "TEXT", 1, 0),
        ("driver", "TEXT", 1, 0),
        ("model", "TEXT", 1, 0),
        ("status", "TEXT", 1, 0),
        ("error_class", "TEXT", 0, 0),
        ("cache_hit", "INTEGER", 1, 0),
        ("remote_preview_bytes", "INTEGER", 1, 0),
        ("route_reason", "TEXT", 1, 0),
    ),
}

_TABLE_VERSION = {
    "schema_migration": 1,
    "workspace_metadata": 1,
    "scan_run": 2,
    "media_item": 2,
    "asset_bundle": 2,
    "bundle_member": 2,
    "scan_warning": 2,
    "preprocess_run": 5,
    "media_preprocess": 5,
    "grouping_run": 6,
    "duplicate_group": 5,
    "duplicate_member": 5,
    "burst_group": 5,
    "burst_member": 5,
    "ai_run": 9,
    "ai_analysis": 9,
    "review_decision": 10,
    "organization_plan": 11,
    "plan_entry": 11,
    "plan_approval": 11,
    "operation_transaction": 11,
    "operation_journal": 11,
    "provider_analysis_cache": 12,
    "ai_route_run": 12,
    "ai_route_attempt": 12,
}


def connect_database(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Open a workspace database with safe baseline connection settings."""
    if read_only:
        uri = f"{path.resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def _user_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row is not None else 0


def _application_objects(connection: sqlite3.Connection) -> set[tuple[str, str]]:
    return {
        (str(row[0]), str(row[1]))
        for row in connection.execute(
            """
            SELECT type, name
            FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%'
            """
        )
    }


def _table_columns(
    connection: sqlite3.Connection, table: str
) -> tuple[tuple[str, str, int, int], ...]:
    return tuple(
        (str(row[1]), str(row[2]).upper(), int(row[3]), int(row[5]))
        for row in connection.execute(f'PRAGMA table_info("{table}")')
    )


def _has_unique_name_constraint(connection: sqlite3.Connection) -> bool:
    for index_row in connection.execute('PRAGMA index_list("schema_migration")'):
        if not bool(index_row[2]):
            continue
        index_name = str(index_row[1]).replace('"', '""')
        columns = tuple(
            str(column_row[2])
            for column_row in connection.execute(f'PRAGMA index_info("{index_name}")')
        )
        if columns == ("name",):
            return True
    return False


def _has_unique_constraint(
    connection: sqlite3.Connection, table: str, expected_columns: tuple[str, ...]
) -> bool:
    quoted_table = table.replace('"', '""')
    for index_row in connection.execute(f'PRAGMA index_list("{quoted_table}")'):
        if not bool(index_row[2]) or bool(index_row[4]):
            continue
        index_name = str(index_row[1]).replace('"', '""')
        columns = tuple(
            str(column_row[2])
            for column_row in connection.execute(f'PRAGMA index_info("{index_name}")')
        )
        if columns == expected_columns:
            return True
    return False


def _named_index(
    connection: sqlite3.Connection, index_name: str
) -> tuple[str, bool, tuple[str, ...]] | None:
    row = connection.execute(
        "SELECT tbl_name FROM sqlite_master WHERE type = 'index' AND name = ?",
        (index_name,),
    ).fetchone()
    if row is None:
        return None
    table = str(row[0])
    quoted_table = table.replace('"', '""')
    index_rows = connection.execute(f'PRAGMA index_list("{quoted_table}")').fetchall()
    matching = [index_row for index_row in index_rows if str(index_row[1]) == index_name]
    if len(matching) != 1:
        return None
    quoted_index = index_name.replace('"', '""')
    columns = tuple(
        str(column_row[2])
        for column_row in connection.execute(f'PRAGMA index_info("{quoted_index}")')
    )
    return table, bool(matching[0][2]), columns


def _foreign_keys(
    connection: sqlite3.Connection, table: str
) -> set[tuple[str, str, str, str, str]]:
    quoted_table = table.replace('"', '""')
    return {
        (str(row[3]), str(row[2]), str(row[4]), str(row[5]).upper(), str(row[6]).upper())
        for row in connection.execute(f'PRAGMA foreign_key_list("{quoted_table}")')
    }


_REQUIRED_UNIQUE_CONSTRAINTS = {
    "media_item": (("path_key",),),
    "asset_bundle": (("bundle_key",),),
    "bundle_member": (("bundle_id", "media_id"),),
    "duplicate_group": (("algorithm_version", "content_sha256"),),
    "duplicate_member": (("group_id", "media_id"),),
    "burst_member": (("group_id", "media_id"),),
    "ai_analysis": (
        (
            "media_id",
            "input_fingerprint",
            "preview_fingerprint",
            "preview_version",
            "provider",
            "model",
            "prompt_version",
            "schema_version",
        ),
    ),
    "organization_plan": (("payload_sha256",),),
    "plan_entry": (
        ("plan_id", "position"),
        ("plan_id", "target_key"),
    ),
    "ai_route_attempt": (("route_run_id", "attempt_index"),),
}

_REQUIRED_INDEXES = {
    "scan_run_resume_idx": (
        "scan_run",
        False,
        ("source_root_key", "config_fingerprint", "status", "started_at"),
    ),
    "media_item_source_seen_idx": (
        "media_item",
        False,
        ("source_root_key", "source_present", "last_seen_scan_id"),
    ),
    "media_item_bundle_idx": (
        "media_item",
        False,
        ("source_root_key", "source_present", "parent_key", "bundle_stem", "path_key"),
    ),
    "bundle_member_media_idx": ("bundle_member", False, ("media_id",)),
    "scan_warning_run_idx": ("scan_warning", False, ("scan_id", "code")),
    "preprocess_run_status_idx": ("preprocess_run", False, ("status", "started_at")),
    "media_preprocess_status_idx": (
        "media_preprocess",
        False,
        ("preview_status", "preview_version", "media_id"),
    ),
    "media_preprocess_hash_idx": (
        "media_preprocess",
        False,
        ("perceptual_hash_version", "perceptual_hash", "media_id"),
    ),
    "duplicate_member_media_idx": ("duplicate_member", False, ("media_id",)),
    "burst_member_media_idx": ("burst_member", False, ("media_id",)),
    "media_item_phase_c_candidates_idx": (
        "media_item",
        False,
        ("source_present", "media_type", "size_bytes", "captured_at", "id"),
    ),
    "ai_run_status_idx": ("ai_run", False, ("status", "started_at")),
    "ai_analysis_latest_idx": (
        "ai_analysis",
        False,
        ("media_id", "created_at", "id"),
    ),
    "review_decision_filter_idx": (
        "review_decision",
        False,
        ("scene_category", "disposition", "media_id"),
    ),
    "plan_entry_media_idx": ("plan_entry", False, ("media_id", "plan_id")),
    "plan_approval_state_idx": ("plan_approval", False, ("state", "plan_id")),
    "operation_transaction_state_idx": (
        "operation_transaction",
        False,
        ("state", "updated_at"),
    ),
    "operation_journal_transaction_idx": (
        "operation_journal",
        False,
        ("transaction_id", "state", "id"),
    ),
    "provider_analysis_cache_provider_idx": (
        "provider_analysis_cache",
        False,
        ("provider_id", "model", "created_at"),
    ),
    "ai_route_run_task_idx": ("ai_route_run", False, ("task_type", "started_at")),
    "ai_route_attempt_provider_idx": (
        "ai_route_attempt",
        False,
        ("provider_id", "model", "status"),
    ),
}

_INDEX_VERSION = {
    "scan_run_resume_idx": 3,
    "media_item_source_seen_idx": 2,
    "media_item_bundle_idx": 2,
    "bundle_member_media_idx": 2,
    "scan_warning_run_idx": 2,
    "preprocess_run_status_idx": 5,
    "media_preprocess_status_idx": 5,
    "media_preprocess_hash_idx": 5,
    "duplicate_member_media_idx": 5,
    "burst_member_media_idx": 5,
    "media_item_phase_c_candidates_idx": 5,
    "ai_run_status_idx": 9,
    "ai_analysis_latest_idx": 9,
    "review_decision_filter_idx": 10,
    "plan_entry_media_idx": 11,
    "plan_approval_state_idx": 11,
    "operation_transaction_state_idx": 11,
    "operation_journal_transaction_idx": 11,
    "provider_analysis_cache_provider_idx": 12,
    "ai_route_run_task_idx": 12,
    "ai_route_attempt_provider_idx": 12,
}

_REQUIRED_FOREIGN_KEYS = {
    "scan_run": {("resume_of", "scan_run", "id", "NO ACTION", "NO ACTION")},
    "bundle_member": {
        ("bundle_id", "asset_bundle", "id", "NO ACTION", "CASCADE"),
        ("media_id", "media_item", "id", "NO ACTION", "CASCADE"),
    },
    "scan_warning": {
        ("scan_id", "scan_run", "id", "NO ACTION", "CASCADE"),
        ("media_id", "media_item", "id", "NO ACTION", "SET NULL"),
    },
    "media_preprocess": {("media_id", "media_item", "id", "NO ACTION", "CASCADE")},
    "duplicate_member": {
        ("group_id", "duplicate_group", "id", "NO ACTION", "CASCADE"),
        ("media_id", "media_item", "id", "NO ACTION", "CASCADE"),
    },
    "burst_group": {("representative_media_id", "media_item", "id", "NO ACTION", "CASCADE")},
    "burst_member": {
        ("group_id", "burst_group", "id", "NO ACTION", "CASCADE"),
        ("media_id", "media_item", "id", "NO ACTION", "CASCADE"),
    },
    "ai_analysis": {("media_id", "media_item", "id", "NO ACTION", "CASCADE")},
    "review_decision": {("media_id", "media_item", "id", "NO ACTION", "CASCADE")},
    "plan_entry": {("plan_id", "organization_plan", "plan_id", "NO ACTION", "NO ACTION")},
    "plan_approval": {("plan_id", "organization_plan", "plan_id", "NO ACTION", "NO ACTION")},
    "operation_transaction": {
        ("plan_id", "organization_plan", "plan_id", "NO ACTION", "NO ACTION")
    },
    "operation_journal": {
        (
            "transaction_id",
            "operation_transaction",
            "transaction_id",
            "NO ACTION",
            "NO ACTION",
        )
    },
    "ai_route_attempt": {("route_run_id", "ai_route_run", "id", "NO ACTION", "CASCADE")},
}

_REQUIRED_TRIGGERS = {
    "organization_plan_no_update": (
        11,
        """
        CREATE TRIGGER organization_plan_no_update
        BEFORE UPDATE ON organization_plan
        BEGIN
            SELECT RAISE(ABORT, 'organization plan is immutable');
        END
        """,
    ),
    "organization_plan_no_delete": (
        11,
        """
        CREATE TRIGGER organization_plan_no_delete
        BEFORE DELETE ON organization_plan
        BEGIN
            SELECT RAISE(ABORT, 'organization plan is immutable');
        END
        """,
    ),
    "plan_entry_no_update": (
        11,
        """
        CREATE TRIGGER plan_entry_no_update
        BEFORE UPDATE ON plan_entry
        BEGIN
            SELECT RAISE(ABORT, 'plan entry is immutable');
        END
        """,
    ),
    "plan_entry_no_delete": (
        11,
        """
        CREATE TRIGGER plan_entry_no_delete
        BEFORE DELETE ON plan_entry
        BEGIN
            SELECT RAISE(ABORT, 'plan entry is immutable');
        END
        """,
    ),
}


def _normalized_schema_sql(value: object) -> str:
    return " ".join(str(value).split()).casefold()


def _normalize_workspace_id(workspace_id: str) -> str:
    try:
        normalized = UUID(workspace_id).hex
    except (ValueError, AttributeError) as error:
        raise MigrationError("Workspace identity is not a valid UUID") from error
    if normalized != workspace_id:
        raise MigrationError("Workspace identity must use canonical lowercase UUID hex")
    return normalized


def read_workspace_id(connection: sqlite3.Connection) -> str:
    """Read and validate the singleton identity stored in workspace metadata."""
    try:
        rows = connection.execute(
            "SELECT key, value FROM workspace_metadata ORDER BY key"
        ).fetchall()
    except sqlite3.Error as error:
        raise MigrationError(f"Database workspace identity is unreadable: {error}") from error
    if len(rows) != 1 or str(rows[0][0]) != "workspace_id":
        raise MigrationError("Database schema does not contain one workspace identity")
    return _normalize_workspace_id(str(rows[0][1]))


def validate_database(
    connection: sqlite3.Connection,
    *,
    allow_empty: bool = False,
    expected_workspace_id: str | None = None,
) -> int:
    """Validate schema version, complete migration ledger, and critical table shapes."""
    latest_version = MIGRATIONS[-1].version if MIGRATIONS else 0
    current_version = _user_version(connection)
    if current_version > latest_version:
        raise MigrationError(
            f"Database schema version {current_version} is newer than supported {latest_version}"
        )

    objects = _application_objects(connection)
    if current_version == 0:
        if allow_empty and not objects:
            return 0
        raise MigrationError("Database schema is not an empty or recognized SPT schema")

    expected_ledger = [
        (migration.version, migration.name)
        for migration in MIGRATIONS
        if migration.version <= current_version
    ]
    if ("table", "schema_migration") not in objects:
        raise MigrationError("Database schema is missing critical table schema_migration")
    try:
        actual_ledger = [
            (int(row[0]), str(row[1]))
            for row in connection.execute(
                "SELECT version, name FROM schema_migration ORDER BY version"
            )
        ]
    except sqlite3.Error as error:
        raise MigrationError(f"Database migration ledger is unreadable: {error}") from error
    if actual_ledger != expected_ledger:
        raise MigrationError(
            "Database migration ledger history mismatch: "
            f"expected {expected_ledger}, got {actual_ledger}"
        )

    for table, latest_columns in _REQUIRED_COLUMNS.items():
        if _TABLE_VERSION[table] > current_version:
            continue
        expected_columns = (
            {2: _SCAN_RUN_COLUMNS_V2, 3: _SCAN_RUN_COLUMNS_V3}.get(current_version, latest_columns)
            if table == "scan_run"
            else _PREPROCESS_RUN_COLUMNS_V7
            if table == "preprocess_run" and current_version <= 7
            else _BURST_GROUP_COLUMNS_V5
            if table == "burst_group" and current_version == 5
            else _MEDIA_PREPROCESS_COLUMNS_V6
            if table == "media_preprocess" and current_version <= 6
            else latest_columns
        )
        if ("table", table) not in objects:
            raise MigrationError(f"Database schema is missing critical table {table}")
        actual_columns = _table_columns(connection, table)
        if actual_columns != expected_columns:
            raise MigrationError(
                f"Database schema mismatch for {table}: "
                f"expected {expected_columns}, got {actual_columns}"
            )
    if not _has_unique_name_constraint(connection):
        raise MigrationError("Database schema is missing the migration name uniqueness constraint")
    if current_version >= 2:
        for table, constraints in _REQUIRED_UNIQUE_CONSTRAINTS.items():
            if _TABLE_VERSION[table] > current_version:
                continue
            for columns in constraints:
                if not _has_unique_constraint(connection, table, columns):
                    joined = ", ".join(columns)
                    raise MigrationError(
                        f"Database schema is missing unique constraint on {table}({joined})"
                    )
        for index_name, expected in _REQUIRED_INDEXES.items():
            if _INDEX_VERSION[index_name] > current_version:
                continue
            actual = _named_index(connection, index_name)
            if actual != expected:
                raise MigrationError(
                    f"Database schema index {index_name} mismatch: "
                    f"expected {expected}, got {actual}"
                )
        for table, expected_foreign_keys in _REQUIRED_FOREIGN_KEYS.items():
            if _TABLE_VERSION[table] > current_version or (
                table == "scan_run" and current_version < 3
            ):
                continue
            actual_foreign_keys = _foreign_keys(connection, table)
            if actual_foreign_keys != expected_foreign_keys:
                raise MigrationError(
                    f"Database schema foreign key mismatch for {table}: "
                    f"expected {sorted(expected_foreign_keys)}, "
                    f"got {sorted(actual_foreign_keys)}"
                )
        for trigger_name, (trigger_version, expected_sql) in _REQUIRED_TRIGGERS.items():
            if trigger_version > current_version:
                continue
            if ("trigger", trigger_name) not in objects:
                raise MigrationError(f"Database schema is missing critical trigger {trigger_name}")
            row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                (trigger_name,),
            ).fetchone()
            actual_sql = row[0] if row is not None else None
            if _normalized_schema_sql(actual_sql) != _normalized_schema_sql(expected_sql):
                raise MigrationError(f"Database schema trigger {trigger_name} semantics mismatch")
    actual_workspace_id = read_workspace_id(connection)
    if expected_workspace_id is not None:
        normalized_expected = _normalize_workspace_id(expected_workspace_id)
        if actual_workspace_id != normalized_expected:
            raise MigrationError(
                "Database workspace identity mismatch: "
                f"expected {normalized_expected}, got {actual_workspace_id}"
            )
    return current_version


def apply_migrations(connection: sqlite3.Connection, *, workspace_id: str | None = None) -> int:
    """Atomically validate and apply every pending migration under a SQLite write lock."""
    if connection.in_transaction:
        raise MigrationError("Cannot migrate while the connection has an active transaction")

    connection.execute("BEGIN IMMEDIATE")
    try:
        current_version = validate_database(
            connection,
            allow_empty=True,
            expected_workspace_id=workspace_id,
        )
        selected_workspace_id = (
            _normalize_workspace_id(workspace_id) if workspace_id is not None else uuid4().hex
        )
        for migration in MIGRATIONS:
            if migration.version <= current_version:
                continue
            for statement in migration.statements:
                connection.execute(statement)
            if migration.version == 1:
                connection.execute(
                    "INSERT INTO workspace_metadata(key, value) VALUES ('workspace_id', ?)",
                    (selected_workspace_id,),
                )
            connection.execute(
                "INSERT INTO schema_migration(version, name) VALUES (?, ?)",
                (migration.version, migration.name),
            )
            connection.execute(f"PRAGMA user_version = {migration.version}")
            current_version = migration.version
        validated_version = validate_database(connection, expected_workspace_id=workspace_id)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    return validated_version
