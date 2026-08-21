"""Command-line entry point for workspace and read-only scan operations."""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

from smart_photo_triage.ai import (
    MAX_BATCH_SIZE,
    MAX_PROVIDER_TIMEOUT_SECONDS,
    MAX_RETRIES,
    AnalysisError,
    AnalysisOptions,
    CloudDisabledError,
    FakeVisionProvider,
    GeminiVisionProvider,
    analyze_workspace,
    analyze_workspace_routed,
    estimate_workspace_analysis,
)
from smart_photo_triage.config import ConfigError, load_config, normalized_ai_config
from smart_photo_triage.executor import (
    ExecutorError,
    apply_plan,
    doctor_workspace,
    resume_transaction,
    rollback_transaction,
)
from smart_photo_triage.grouping import group_workspace
from smart_photo_triage.gui import serve_gui
from smart_photo_triage.model_routing import ModelRouter, ProviderRegistry, TaskType
from smart_photo_triage.planner import (
    PlannerError,
    PlannerOptions,
    PlanPolicyError,
    approve_plan,
    build_plan,
    inspect_plan,
    preflight_plan,
    revoke_plan,
)
from smart_photo_triage.preprocess import PreviewConfig, preprocess_workspace
from smart_photo_triage.provider_drivers import build_driver
from smart_photo_triage.review import ReviewError, serve_review
from smart_photo_triage.scanner import (
    ScanLayoutError,
    scan_library,
    validate_scan_layout,
)
from smart_photo_triage.workspace import (
    WorkspaceOwnershipError,
    initialize_workspace,
    open_workspace,
)


def _print_path_message(prefix: str, path: Path) -> None:
    message = f"{prefix}: {path}"
    try:
        print(message)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "ascii"
        escaped = message.encode(encoding, errors="backslashreplace").decode(encoding)
        sys.stdout.write(f"{escaped}\n")


def _positive_finite_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return parsed


def _unit_interval_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if not math.isfinite(parsed) or not 0 <= parsed <= 1:
        raise argparse.ArgumentTypeError("must be between 0 and 1")
    return parsed


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if parsed < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return parsed


def _bounded_batch_size(value: str) -> int:
    parsed = _positive_int(value)
    if parsed > MAX_BATCH_SIZE:
        raise argparse.ArgumentTypeError(f"must be no more than {MAX_BATCH_SIZE}")
    return parsed


def _bounded_retry_count(value: str) -> int:
    parsed = _nonnegative_int(value)
    if parsed > MAX_RETRIES:
        raise argparse.ArgumentTypeError(f"must be no more than {MAX_RETRIES}")
    return parsed


def _provider_timeout_float(value: str) -> float:
    parsed = _positive_finite_float(value)
    if parsed > MAX_PROVIDER_TIMEOUT_SECONDS:
        raise argparse.ArgumentTypeError(f"must be no more than {MAX_PROVIDER_TIMEOUT_SECONDS:g}")
    return parsed


def _port_number(value: str) -> int:
    parsed = _nonnegative_int(value)
    if parsed > 65535:
        raise argparse.ArgumentTypeError("must be no more than 65535")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spt",
        description="Smart-Photo-Triage local-first media organization tooling.",
    )
    parser.add_argument("--version", action="version", version="%(prog)s 1.2.1")
    commands = parser.add_subparsers(dest="command")
    init_parser = commands.add_parser("init", help="Initialize an idempotent local workspace")
    init_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path(".spt"),
        help="Workspace path (default: .spt)",
    )
    scan_parser = commands.add_parser("scan", help="Read-only scan a media source")
    scan_parser.add_argument("source", type=Path, help="Source directory to scan recursively")
    scan_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path(".spt"),
        help="Workspace path (default: .spt)",
    )
    scan_parser.add_argument(
        "--output",
        type=Path,
        help="Future organization output root to exclude from the source scan",
    )
    scan_parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="Relative source glob to exclude (repeatable)",
    )
    scan_parser.add_argument(
        "--full-hash",
        action="store_true",
        help="Compute SHA-256 while scanning instead of deferring it",
    )
    preprocess_parser = commands.add_parser(
        "preprocess", help="Generate versioned local previews and quality metrics"
    )
    preprocess_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path(".spt"),
        help="Workspace path (default: .spt)",
    )
    preprocess_parser.add_argument(
        "--max-edge",
        type=int,
        default=1024,
        help="Maximum preview edge in pixels (default: 1024)",
    )
    preprocess_parser.add_argument(
        "--preview-version",
        default="preview-v1",
        help="Preview algorithm/cache version (default: preview-v1)",
    )
    preprocess_parser.add_argument(
        "--lock-wait-seconds",
        type=_positive_finite_float,
        help=(
            "Optional positive publication-lock wait budget; by default a known live owner "
            "is waited for without a deadline"
        ),
    )
    group_parser = commands.add_parser(
        "group", help="Build exact duplicate and burst candidate groups"
    )
    group_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path(".spt"),
        help="Workspace path (default: .spt)",
    )
    group_parser.add_argument(
        "--burst-window",
        type=float,
        default=3.0,
        help="Burst capture-time window in seconds (default: 3.0)",
    )
    group_parser.add_argument(
        "--burst-distance",
        type=int,
        default=8,
        help="Maximum 64-bit perceptual hash distance (default: 8)",
    )
    group_parser.add_argument(
        "--burst-comparison-cap",
        type=int,
        default=32,
        help="Maximum burst similarity work per item (default: 32)",
    )
    analyze_parser = commands.add_parser(
        "analyze", help="Analyze controlled previews with a versioned Vision Provider"
    )
    analyze_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path(".spt"),
        help="Workspace path (default: .spt)",
    )
    analyze_parser.add_argument(
        "--provider",
        choices=("fake", "gemini"),
        default="fake",
        help="Vision Provider (default: offline fake)",
    )
    analyze_parser.add_argument(
        "--model",
        help="Provider model identifier; required for gemini, optional for fake",
    )
    analyze_parser.add_argument(
        "--prompt-version", default="vision-prompt-v1", help="Prompt cache version"
    )
    analyze_parser.add_argument(
        "--schema-version", default="vision-schema-v1", help="Output schema cache version"
    )
    analyze_parser.add_argument(
        "--confidence-threshold",
        type=_unit_interval_float,
        default=0.65,
        help="Low-confidence reject protection threshold (default: 0.65)",
    )
    analyze_parser.add_argument(
        "--batch-size", type=_bounded_batch_size, default=8, help="Maximum items per request"
    )
    analyze_parser.add_argument(
        "--max-retries",
        type=_bounded_retry_count,
        default=2,
        help="Retries for transient provider failures (default: 2)",
    )
    analyze_parser.add_argument(
        "--timeout-seconds",
        type=_provider_timeout_float,
        default=30.0,
        help="Cloud provider request timeout in seconds (default: 30)",
    )
    analyze_parser.add_argument(
        "--estimate-only",
        action="store_true",
        help="Show pending items, cache hits, upload bytes, and batches without calling provider",
    )
    review_parser = commands.add_parser(
        "review", help="Open the local-only review UI and persist HUMAN decisions"
    )
    review_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path(".spt"),
        help="Workspace path (default: .spt)",
    )
    review_parser.add_argument(
        "--host",
        choices=("127.0.0.1",),
        default="127.0.0.1",
        help="Fixed loopback bind address (default: 127.0.0.1)",
    )
    review_parser.add_argument(
        "--port",
        type=_port_number,
        default=0,
        help="Local port; 0 selects an available port (default: 0)",
    )
    review_parser.add_argument(
        "--no-open",
        action="store_true",
        help="Do not open the system browser automatically",
    )
    gui_parser = commands.add_parser("gui", help="Open the local one-click workflow control panel")
    gui_parser.add_argument(
        "--workspace", type=Path, default=Path(".spt"), help="Workspace path (default: .spt)"
    )
    gui_parser.add_argument(
        "--port", type=_port_number, default=0, help="Local port; 0 selects an available port"
    )
    gui_parser.add_argument("--no-open", action="store_true", help="Do not open a browser")
    ai_parser = commands.add_parser(
        "ai", help="Inspect v1.2.1 provider routing without exposing secrets"
    )
    ai_commands = ai_parser.add_subparsers(dest="ai_command", required=True)
    for command_name, help_text in (
        ("providers", "List configured providers with redacted credential status"),
        ("doctor", "Validate routing, privacy gates, and environment-key presence without network"),
        ("estimate", "Show configured route and current READY-preview count without network"),
        ("run", "Analyze controlled previews through configured v1.2.1 item route"),
    ):
        command = ai_commands.add_parser(command_name, help=help_text)
        command.add_argument(
            "--workspace", type=Path, default=Path(".spt"), help="Workspace path (default: .spt)"
        )
    explain_parser = ai_commands.add_parser("route", help="Explain one deterministic task route")
    explain_commands = explain_parser.add_subparsers(dest="ai_route_command", required=True)
    explain = explain_commands.add_parser(
        "explain", help="Show primary, fallback, escalation, and privacy"
    )
    explain.add_argument("task_type", choices=("item_analysis", "burst_review"))
    explain.add_argument(
        "--workspace", type=Path, default=Path(".spt"), help="Workspace path (default: .spt)"
    )
    probe = ai_commands.add_parser(
        "probe", help="Validate declared capability profile using synthetic metadata"
    )
    probe.add_argument("provider_id")
    probe.add_argument(
        "--workspace", type=Path, default=Path(".spt"), help="Workspace path (default: .spt)"
    )
    plan_parser = commands.add_parser(
        "plan", help="Build, inspect, approve, and preflight immutable organization plans"
    )
    plan_commands = plan_parser.add_subparsers(dest="plan_command", required=True)
    plan_build = plan_commands.add_parser("build", help="Build or reuse a deterministic plan")
    plan_build.add_argument(
        "--workspace", type=Path, default=Path(".spt"), help="Workspace path (default: .spt)"
    )
    plan_build.add_argument("--output", type=Path, required=True, help="Organization output root")
    plan_build.add_argument(
        "--mode",
        type=str.upper,
        choices=("COPY", "MOVE"),
        default="COPY",
        help="Planned action only; no file action is performed (default: COPY)",
    )
    plan_build.add_argument(
        "--max-path-chars",
        type=_positive_int,
        default=240,
        help="Maximum planned target path characters (default: 240)",
    )
    for command_name, help_text in (
        ("inspect", "Print the canonical plan and approval state"),
        ("approve", "Explicitly approve a plan for later apply preflight"),
        ("revoke", "Revoke a previously approved plan"),
        ("preflight", "Run read-only apply preflight checks"),
    ):
        command = plan_commands.add_parser(command_name, help=help_text)
        command.add_argument("plan_id", help="Immutable plan identifier")
        command.add_argument(
            "--workspace", type=Path, default=Path(".spt"), help="Workspace path (default: .spt)"
        )
    apply_parser = commands.add_parser(
        "apply", help="Dry-run or execute an approved immutable plan"
    )
    apply_parser.add_argument("plan_id", help="Immutable approved plan identifier")
    apply_parser.add_argument(
        "--workspace", type=Path, default=Path(".spt"), help="Workspace path (default: .spt)"
    )
    apply_mode = apply_parser.add_mutually_exclusive_group()
    apply_mode.add_argument(
        "--execute",
        action="store_true",
        help="Perform the approved COPY/MOVE plan; omitted means dry-run",
    )
    apply_mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Explicitly request the default zero-mutation dry-run",
    )
    doctor_parser = commands.add_parser(
        "doctor", help="Diagnose locks, partials, journals, and recovery state"
    )
    doctor_parser.add_argument(
        "--workspace", type=Path, default=Path(".spt"), help="Workspace path (default: .spt)"
    )
    for command_name, help_text in (
        ("resume", "Resume a durable incomplete transaction"),
        ("rollback", "Safely rollback a journaled transaction"),
    ):
        command = commands.add_parser(command_name, help=help_text)
        command.add_argument("transaction_id", help="Operation transaction identifier")
        command.add_argument(
            "--workspace", type=Path, default=Path(".spt"), help="Workspace path (default: .spt)"
        )
    return parser


def _load_ai_router(workspace_path: Path) -> tuple[object, ProviderRegistry, ModelRouter]:
    workspace = initialize_workspace(workspace_path)
    config = normalized_ai_config(load_config(workspace.config_path))
    registry = ProviderRegistry(config.providers)
    router = ModelRouter(
        registry,
        config.route_map(),
        max_provider_attempts_per_task=config.max_provider_attempts_per_task,
    )
    return workspace, registry, router


def _ai_observability(args: argparse.Namespace) -> int:
    try:
        workspace, registry, router = _load_ai_router(args.workspace)
    except (ConfigError, ValueError, sqlite3.Error, WorkspaceOwnershipError) as error:
        print(f"AI configuration failed: {error}", file=sys.stderr)
        return 2
    config = normalized_ai_config(load_config(workspace.config_path))
    if args.ai_command == "providers":
        print(
            json.dumps(
                [provider.redacted_summary() for provider in registry.providers], ensure_ascii=False
            )
        )
        return 0
    if args.ai_command == "doctor":
        report = {
            "valid": True,
            "network_requests": 0,
            "allow_cloud": config.allow_cloud,
            "allow_lan": config.allow_lan,
            "providers": [provider.redacted_summary() for provider in registry.providers],
            "routes": {task.value: policy.primary for task, policy in config.routes},
        }
        print(json.dumps(report, ensure_ascii=False))
        return 0
    if args.ai_command == "route":
        task = TaskType(args.task_type)
        policy = router.routes[task]
        selected = {
            "task_type": task.value,
            "primary": policy.primary,
            "fallbacks": list(policy.fallbacks),
            "escalation": (
                {"confidence_below": policy.confidence_below, "to": policy.escalate_to}
                if policy.escalate_to is not None
                else None
            ),
            "privacy": {"allow_cloud": config.allow_cloud, "allow_lan": config.allow_lan},
        }
        print(json.dumps(selected, ensure_ascii=False))
        return 0
    if args.ai_command == "estimate":
        with sqlite3.connect(workspace.database_path) as connection:
            ready = connection.execute(
                "SELECT COUNT(*) FROM media_preprocess WHERE preview_status='READY'"
            ).fetchone()
        print(
            json.dumps(
                {
                    "ready_previews": int(ready[0]) if ready is not None else 0,
                    "routes": {task.value: policy.primary for task, policy in config.routes},
                    "pricing": "estimated only when configured per provider",
                    "network_requests": 0,
                },
                ensure_ascii=False,
            )
        )
        return 0
    if args.ai_command == "run":
        drivers: dict[str, object] = {}
        for provider in registry.providers:
            if provider.driver == "fake":
                drivers[provider.provider_id] = FakeVisionProvider(model=provider.model)
            elif provider.resolve_api_key() is not None:
                drivers[provider.provider_id] = build_driver(provider)
        router.drivers = drivers
        try:
            result = analyze_workspace_routed(
                workspace,
                router=router,
                allow_cloud=config.allow_cloud,
                allow_lan=config.allow_lan,
            )
        except (AnalysisError, ValueError, sqlite3.Error) as error:
            print(f"AI route run failed: {error}", file=sys.stderr)
            return 2
        print(
            "AI route run complete: "
            f"analyzed={result.analyzed_count} cache_hits={result.cache_hit_count} "
            f"failed={result.failed_count} requests={result.request_count}"
        )
        return 0
    if args.ai_command == "probe":
        try:
            provider = registry.get(args.provider_id)
        except KeyError:
            print("AI probe failed: unknown provider", file=sys.stderr)
            return 2
        # This intentional offline probe validates declared capabilities only.  A real endpoint
        # smoke is an explicit operator action and is not run by doctor or release tests.
        print(
            json.dumps(
                {
                    "provider_id": provider.provider_id,
                    "synthetic_only": True,
                    "network_requests": 0,
                    "capability_profile_version": provider.capabilities.capability_profile_version,
                    "status": "DECLARED_PROFILE_VALID",
                }
            )
        )
        return 0
    raise AssertionError(f"unsupported ai subcommand: {args.ai_command}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "init":
        workspace = initialize_workspace(args.workspace)
        _print_path_message("Workspace ready", workspace.root)
        return 0
    if args.command == "ai":
        return _ai_observability(args)
    if args.command == "scan":
        try:
            validate_scan_layout(args.source, args.workspace, args.output)
            workspace = initialize_workspace(args.workspace)
            result = scan_library(
                workspace,
                args.source,
                output=args.output,
                exclude_globs=args.exclude,
                hash_content=args.full_hash,
            )
        except (sqlite3.Error, OSError, ScanLayoutError, WorkspaceOwnershipError) as error:
            print(f"Scan failed: {error}", file=sys.stderr)
            return 2
        print(
            "Scan complete: "
            f"indexed={result.indexed_count} missing={result.missing_count} "
            f"errors={result.error_count} warnings={result.warning_count} "
            f"bundles={result.bundle_count}"
        )
        return 0
    if args.command == "preprocess":
        try:
            workspace = initialize_workspace(args.workspace)
            result = preprocess_workspace(
                workspace,
                config=PreviewConfig(
                    max_edge=args.max_edge,
                    version=args.preview_version,
                ),
                lock_wait_timeout=args.lock_wait_seconds,
            )
        except (sqlite3.Error, OSError, ValueError, WorkspaceOwnershipError) as error:
            print(f"Preprocess failed: {error}", file=sys.stderr)
            return 2
        print(
            "Preprocess complete: "
            f"processed={result.processed_count} cache_hits={result.cache_hit_count} "
            f"failed={result.failed_count} deferred={result.deferred_count}"
        )
        return 0
    if args.command == "group":
        try:
            workspace = initialize_workspace(args.workspace)
            result = group_workspace(
                workspace,
                time_window_seconds=args.burst_window,
                distance_threshold=args.burst_distance,
                comparison_cap=args.burst_comparison_cap,
            )
        except (sqlite3.Error, OSError, ValueError, WorkspaceOwnershipError) as error:
            print(f"Grouping failed: {error}", file=sys.stderr)
            return 2
        print(
            "Grouping complete: "
            f"duplicates={result.duplicate_group_count} bursts={result.burst_group_count} "
            f"comparisons={result.comparison_count} warnings={result.warning_count}"
        )
        return 0
    if args.command == "analyze":
        try:
            workspace = initialize_workspace(args.workspace)
            options = AnalysisOptions(
                prompt_version=args.prompt_version,
                schema_version=args.schema_version,
                confidence_threshold=args.confidence_threshold,
                batch_size=args.batch_size,
                max_retries=args.max_retries,
            )
            if args.provider == "fake":
                provider = FakeVisionProvider(model=args.model or "fake-v1")
            else:
                try:
                    cloud_allowed = load_config(workspace.config_path).allow_cloud
                except ConfigError:
                    raise AnalysisError("workspace configuration is invalid") from None
                if not cloud_allowed:
                    raise CloudDisabledError(
                        "Cloud analysis is disabled; set allow_cloud=true "
                        "explicitly before using it"
                    )
                if not args.model:
                    raise ValueError("--model is required for the gemini provider")
                api_key = os.environ.get("SPT_GEMINI_API_KEY", "")
                if not api_key:
                    raise ValueError("SPT_GEMINI_API_KEY is required for the gemini provider")
                provider = GeminiVisionProvider(
                    model=args.model,
                    api_key=api_key,
                    timeout_seconds=args.timeout_seconds,
                )
            estimate = estimate_workspace_analysis(
                workspace,
                provider=provider,
                options=options,
            )
            print(
                "Analysis estimate: "
                f"items={estimate.item_count} pending={estimate.pending_count} "
                f"cache_hits={estimate.cache_hit_count} upload_bytes={estimate.upload_bytes} "
                f"batches={estimate.request_batch_count}"
            )
            if args.estimate_only:
                return 0
            result = analyze_workspace(
                workspace,
                provider=provider,
                options=options,
            )
        except (
            AnalysisError,
            ConfigError,
            sqlite3.Error,
            OSError,
            ValueError,
            WorkspaceOwnershipError,
        ) as error:
            if isinstance(error, AnalysisError | ValueError):
                detail = str(error)
            elif isinstance(error, ConfigError):
                detail = "workspace configuration is invalid"
            else:
                detail = f"LOCAL_STATE_ERROR:{type(error).__name__}"
            print(f"Analysis failed: {detail}", file=sys.stderr)
            return 2
        print(
            "Analysis complete: "
            f"analyzed={result.analyzed_count} cache_hits={result.cache_hit_count} "
            f"failed={result.failed_count} requests={result.request_count}"
        )
        return 0
    if args.command == "review":
        try:
            workspace = initialize_workspace(args.workspace)
            serve_review(
                workspace,
                host=args.host,
                port=args.port,
                open_browser=not args.no_open,
            )
        except (sqlite3.Error, OSError, ReviewError, ValueError, WorkspaceOwnershipError) as error:
            print(f"Review failed: {error}", file=sys.stderr)
            return 2
        return 0
    if args.command == "gui":
        try:
            serve_gui(args.workspace, port=args.port, open_browser=not args.no_open)
        except (sqlite3.Error, OSError, ValueError, WorkspaceOwnershipError) as error:
            print(f"GUI failed: {error}", file=sys.stderr)
            return 2
        return 0
    if args.command in {"apply", "doctor", "resume", "rollback"}:
        try:
            workspace = open_workspace(args.workspace)
            if args.command == "apply":
                report = preflight_plan(workspace, args.plan_id)
                if not report.ok:
                    print(json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True))
                    return 2
                result = apply_plan(
                    workspace,
                    report,
                    dry_run=not args.execute,
                ).to_dict()
                exit_code = 0
            elif args.command == "doctor":
                diagnosis = doctor_workspace(workspace)
                result = diagnosis.to_dict()
                exit_code = 0 if diagnosis.ok else 2
            elif args.command == "resume":
                resumed = resume_transaction(workspace, args.transaction_id)
                result = resumed.to_dict()
                exit_code = 0 if resumed.state == "DONE" else 2
            else:
                rolled_back = rollback_transaction(workspace, args.transaction_id)
                result = rolled_back.to_dict()
                exit_code = 0 if rolled_back.state == "ROLLED_BACK" else 2
        except (
            ExecutorError,
            OSError,
            PlannerError,
            sqlite3.Error,
            ValueError,
            WorkspaceOwnershipError,
        ) as error:
            print(f"{args.command.capitalize()} failed: {error}", file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return exit_code
    if args.command == "plan":
        try:
            if args.plan_command == "build":
                workspace = initialize_workspace(args.workspace)
                plan = build_plan(
                    workspace,
                    PlannerOptions(
                        output_root=args.output,
                        mode=args.mode,
                        max_path_chars=args.max_path_chars,
                    ),
                )
                result: object = {
                    "plan_id": plan.plan_id,
                    "approval_state": plan.approval_state,
                    "entry_count": len(plan.entries),
                    "payload_sha256": plan.payload_sha256,
                }
            else:
                workspace = open_workspace(args.workspace)
                if args.plan_command == "inspect":
                    result = inspect_plan(workspace, args.plan_id).to_dict()
                elif args.plan_command == "approve":
                    approval = approve_plan(workspace, args.plan_id)
                    result = {
                        "plan_id": approval.plan_id,
                        "approval_state": approval.state,
                        "approval_revision": approval.revision,
                    }
                elif args.plan_command == "revoke":
                    approval = revoke_plan(workspace, args.plan_id)
                    result = {
                        "plan_id": approval.plan_id,
                        "approval_state": approval.state,
                        "approval_revision": approval.revision,
                    }
                else:
                    report = preflight_plan(workspace, args.plan_id)
                    result = report.to_dict()
                    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
                    return 0 if report.ok else 2
        except (
            OSError,
            PlanPolicyError,
            PlannerError,
            sqlite3.Error,
            ValueError,
            WorkspaceOwnershipError,
        ) as error:
            print(f"Plan failed: {error}", file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    parser.print_help()
    return 0
