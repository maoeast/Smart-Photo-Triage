"""Backward-compatible workspace configuration and v1.2.1 AI routing contract."""

from __future__ import annotations

import os
import re
import secrets
import tomllib
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from smart_photo_triage.model_routing import (
    Budget,
    ModelRouter,
    NetworkScope,
    ProviderCapabilities,
    ProviderConfig,
    ProviderRegistry,
    RoutePolicy,
    TaskType,
    builtin_capabilities,
)


class ConfigError(ValueError):
    """Raised when the intentionally narrow configuration contract is invalid."""


@dataclass(frozen=True, slots=True)
class AppConfig:
    allow_cloud: bool = False
    allow_lan: bool = False
    providers: tuple[ProviderConfig, ...] = ()
    routes: tuple[tuple[TaskType, RoutePolicy], ...] = ()
    budget: Budget | None = None
    max_provider_attempts_per_task: int = 3

    def route_map(self) -> dict[TaskType, RoutePolicy]:
        return dict(self.routes)


_DEFAULT_URLS = {
    "fake": "http://127.0.0.1:9999/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
}


def _legacy_default() -> tuple[
    tuple[ProviderConfig, ...], tuple[tuple[TaskType, RoutePolicy], ...]
]:
    provider = ProviderConfig(
        provider_id="default_fake",
        driver="fake",
        model="fake-v1",
        base_url=_DEFAULT_URLS["fake"],
        network_scope=NetworkScope.LOOPBACK,
        capabilities=builtin_capabilities("fake"),
    )
    return (
        (provider,),
        (
            (TaskType.ITEM_ANALYSIS, RoutePolicy("default_fake")),
            (TaskType.BURST_REVIEW, RoutePolicy("default_fake")),
        ),
    )


def _expect_mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ConfigError(f"{name} must be a TOML table")
    return value


def _expect_bool(value: object, name: str, default: bool = False) -> bool:
    selected = default if value is None else value
    if not isinstance(selected, bool):
        raise ConfigError(f"{name} must be a boolean")
    return selected


def _optional_nonnegative_int(value: object, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(f"{name} must be a non-negative integer or omitted")
    return value


def _capabilities(driver: str, raw: object) -> ProviderCapabilities:
    if raw is None:
        return builtin_capabilities(driver)
    data = _expect_mapping(raw, "capabilities")
    allowed = {
        "supports_image",
        "supports_multi_image",
        "supports_structured_json",
        "supports_json_schema",
        "max_images_per_request",
        "max_request_bytes",
        "supported_image_mime_types",
        "supports_system_prompt",
        "supports_streaming",
        "capability_profile_version",
    }
    unexpected = set(data) - allowed
    if unexpected:
        raise ConfigError(f"unsupported capabilities key(s): {', '.join(sorted(unexpected))}")
    default = builtin_capabilities(driver)
    mimes = data.get("supported_image_mime_types", list(default.supported_image_mime_types))
    if not isinstance(mimes, list) or not all(isinstance(item, str) and item for item in mimes):
        raise ConfigError("supported_image_mime_types must be a non-empty string list")
    try:
        return ProviderCapabilities(
            supports_image=_expect_bool(
                data.get("supports_image"), "supports_image", default.supports_image
            ),
            supports_multi_image=_expect_bool(
                data.get("supports_multi_image"),
                "supports_multi_image",
                default.supports_multi_image,
            ),
            supports_structured_json=_expect_bool(
                data.get("supports_structured_json"),
                "supports_structured_json",
                default.supports_structured_json,
            ),
            supports_json_schema=_expect_bool(
                data.get("supports_json_schema"),
                "supports_json_schema",
                default.supports_json_schema,
            ),
            max_images_per_request=(
                _optional_nonnegative_int(
                    data.get("max_images_per_request"), "max_images_per_request"
                )
                if "max_images_per_request" in data
                else default.max_images_per_request
            ),
            max_request_bytes=(
                _optional_nonnegative_int(data.get("max_request_bytes"), "max_request_bytes")
                if "max_request_bytes" in data
                else default.max_request_bytes
            ),
            supported_image_mime_types=frozenset(mimes),
            supports_system_prompt=_expect_bool(
                data.get("supports_system_prompt"),
                "supports_system_prompt",
                default.supports_system_prompt,
            ),
            supports_streaming=_expect_bool(
                data.get("supports_streaming"), "supports_streaming", default.supports_streaming
            ),
            capability_profile_version=str(
                data.get("capability_profile_version", default.capability_profile_version)
            ),
        )
    except ValueError as error:
        raise ConfigError(str(error)) from error


def _provider(provider_id: str, raw: object) -> ProviderConfig:
    data = _expect_mapping(raw, f"provider {provider_id}")
    allowed = {
        "driver",
        "model",
        "base_url",
        "endpoint",
        "display_name",
        "api_key_env",
        "network_scope",
        "enabled",
        "estimated_cost_per_request",
        "capabilities",
    }
    unexpected = set(data) - allowed
    if unexpected:
        raise ConfigError(f"unsupported provider key(s): {', '.join(sorted(unexpected))}")
    driver = data.get("driver")
    model = data.get("model")
    if not isinstance(driver, str) or not driver:
        raise ConfigError(f"provider {provider_id} requires driver")
    if not isinstance(model, str) or not model:
        raise ConfigError(f"provider {provider_id} requires model")
    base_url = data.get("base_url", data.get("endpoint", _DEFAULT_URLS.get(driver)))
    if not isinstance(base_url, str) or not base_url:
        raise ConfigError(f"provider {provider_id} requires base_url")
    scope_text = data.get("network_scope")
    if not isinstance(scope_text, str):
        raise ConfigError(f"provider {provider_id} requires network_scope")
    try:
        scope = NetworkScope(scope_text)
    except ValueError as error:
        raise ConfigError(f"invalid network_scope for {provider_id}") from error
    api_key_env = data.get("api_key_env")
    if api_key_env is not None and not isinstance(api_key_env, str):
        raise ConfigError("api_key_env must be a string")
    display_name = data.get("display_name")
    if display_name is not None and not isinstance(display_name, str):
        raise ConfigError("display_name must be a string")
    cost = data.get("estimated_cost_per_request")
    if cost is not None and (isinstance(cost, bool) or not isinstance(cost, int | float)):
        raise ConfigError("estimated_cost_per_request must be numeric")
    try:
        return ProviderConfig(
            provider_id=provider_id,
            driver=driver,
            model=model,
            base_url=base_url,
            network_scope=scope,
            capabilities=_capabilities(driver, data.get("capabilities")),
            display_name=display_name,
            api_key_env=api_key_env,
            enabled=_expect_bool(data.get("enabled"), "enabled", True),
            estimated_cost_per_request=float(cost) if cost is not None else None,
        )
    except ValueError as error:
        raise ConfigError(str(error)) from error


def _route(task_type: TaskType, raw: object) -> RoutePolicy:
    data = _expect_mapping(raw, f"route {task_type.value}")
    allowed = {"primary", "fallbacks", "escalation"}
    unexpected = set(data) - allowed
    if unexpected:
        raise ConfigError(f"unsupported route key(s): {', '.join(sorted(unexpected))}")
    primary = data.get("primary")
    fallbacks = data.get("fallbacks", [])
    if not isinstance(primary, str) or not primary:
        raise ConfigError(f"route {task_type.value} requires primary")
    if not isinstance(fallbacks, list) or not all(isinstance(item, str) for item in fallbacks):
        raise ConfigError(f"route {task_type.value} fallbacks must be a string list")
    escalation = data.get("escalation")
    if escalation is None:
        return RoutePolicy(primary, tuple(fallbacks))
    item = _expect_mapping(escalation, "escalation")
    if set(item) - {"confidence_below", "to", "max_escalations"}:
        raise ConfigError("unsupported escalation key")
    threshold = item.get("confidence_below")
    destination = item.get("to")
    maximum = item.get("max_escalations", 1)
    if isinstance(threshold, bool) or not isinstance(threshold, int | float):
        raise ConfigError("escalation confidence_below must be numeric")
    if not isinstance(destination, str) or not destination:
        raise ConfigError("escalation to must be a provider id")
    if isinstance(maximum, bool) or not isinstance(maximum, int):
        raise ConfigError("max_escalations must be an integer")
    try:
        return RoutePolicy(primary, tuple(fallbacks), float(threshold), destination, maximum)
    except ValueError as error:
        raise ConfigError(str(error)) from error


def _load_v121(data: dict[str, Any]) -> AppConfig:
    unexpected = set(data) - {"ai"}
    if unexpected:
        raise ConfigError(f"Unsupported configuration key(s): {', '.join(sorted(unexpected))}")
    ai = _expect_mapping(data.get("ai"), "ai")
    allowed = {"enabled", "allow_cloud", "allow_lan", "providers", "routes", "limits"}
    unexpected = set(ai) - allowed
    if unexpected:
        raise ConfigError(f"unsupported ai key(s): {', '.join(sorted(unexpected))}")
    if ai.get("enabled", True) is not True:
        raise ConfigError("ai.enabled=false is not supported. Omit providers to use offline fake")
    providers_table = _expect_mapping(ai.get("providers", {}), "providers")
    providers = tuple(_provider(provider_id, raw) for provider_id, raw in providers_table.items())
    if not providers:
        providers, default_routes = _legacy_default()
    else:
        default_routes = ()
    routes_table = _expect_mapping(ai.get("routes", {}), "routes")
    if routes_table:
        aliases = {"item_analysis": TaskType.ITEM_ANALYSIS, "burst_review": TaskType.BURST_REVIEW}
        if set(routes_table) - set(aliases):
            raise ConfigError("unsupported route name")
        routes = tuple(
            (aliases[name], _route(aliases[name], raw)) for name, raw in routes_table.items()
        )
    else:
        if not default_routes:
            first = providers[0].provider_id
            default_routes = (
                (TaskType.ITEM_ANALYSIS, RoutePolicy(first)),
                (TaskType.BURST_REVIEW, RoutePolicy(first)),
            )
        routes = default_routes
    if {task_type for task_type, _policy in routes} != {
        TaskType.ITEM_ANALYSIS,
        TaskType.BURST_REVIEW,
    }:
        raise ConfigError("both item_analysis and burst_review routes are required")
    limits = _expect_mapping(ai.get("limits", {}), "limits")
    allowed_limits = {
        "max_provider_attempts_per_task",
        "max_requests_per_run",
        "max_remote_preview_mb_per_run",
        "max_estimated_cost_per_run",
    }
    if set(limits) - allowed_limits:
        raise ConfigError("unsupported limits key")
    mb = _optional_nonnegative_int(
        limits.get("max_remote_preview_mb_per_run"), "max_remote_preview_mb_per_run"
    )
    cost = limits.get("max_estimated_cost_per_run")
    if cost is not None and (
        isinstance(cost, bool) or not isinstance(cost, int | float) or cost < 0
    ):
        raise ConfigError("max_estimated_cost_per_run must be a non-negative number")
    attempts = _optional_nonnegative_int(
        limits.get("max_provider_attempts_per_task"), "max_provider_attempts_per_task"
    )
    if attempts is not None and not 1 <= attempts <= 32:
        raise ConfigError("max_provider_attempts_per_task must be between 1 and 32")
    result = AppConfig(
        allow_cloud=_expect_bool(ai.get("allow_cloud"), "allow_cloud"),
        allow_lan=_expect_bool(ai.get("allow_lan"), "allow_lan"),
        providers=providers,
        routes=routes,
        budget=Budget(
            max_requests=_optional_nonnegative_int(
                limits.get("max_requests_per_run"), "max_requests_per_run"
            ),
            max_remote_preview_bytes=mb * 1024 * 1024 if mb is not None else None,
            max_estimated_cost=float(cost) if cost is not None else None,
        ),
        max_provider_attempts_per_task=attempts or 3,
    )
    try:
        ModelRouter(
            ProviderRegistry(result.providers),
            result.route_map(),
            max_provider_attempts_per_task=result.max_provider_attempts_per_task,
        )
    except ValueError as error:
        raise ConfigError(str(error)) from error
    return result


def load_config(path: Path | None = None) -> AppConfig:
    """Load legacy root config or v1.2.1 ``[ai]`` config. Cloud stays opt-in."""
    if path is None:
        return AppConfig()
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"Unable to load configuration from {path}: {error}") from error
    if "ai" in data:
        return _load_v121(data)
    unexpected = set(data) - {"allow_cloud", "provider", "model"}
    if unexpected:
        raise ConfigError(f"Unsupported configuration key(s): {', '.join(sorted(unexpected))}")
    allow_cloud = _expect_bool(data.get("allow_cloud"), "allow_cloud")
    provider_name = data.get("provider")
    model = data.get("model")
    providers: tuple[ProviderConfig, ...] = ()
    routes: tuple[tuple[TaskType, RoutePolicy], ...] = ()
    if provider_name is not None or model is not None:
        if not isinstance(provider_name, str) or not isinstance(model, str):
            raise ConfigError("legacy provider and model must both be strings")
        driver = provider_name if provider_name in _DEFAULT_URLS else "openai_compatible"
        endpoint = _DEFAULT_URLS.get(driver)
        if endpoint is None:
            raise ConfigError("legacy custom provider needs v1.2.1 base_url configuration")
        scope = NetworkScope.LOOPBACK if driver == "fake" else NetworkScope.REMOTE
        providers = (
            ProviderConfig(
                provider_id="legacy_default",
                driver=driver,
                model=model,
                base_url=endpoint,
                network_scope=scope,
                capabilities=builtin_capabilities(driver),
            ),
        )
        routes = (
            (TaskType.ITEM_ANALYSIS, RoutePolicy("legacy_default")),
            (TaskType.BURST_REVIEW, RoutePolicy("legacy_default")),
        )
    return AppConfig(allow_cloud=allow_cloud, providers=providers, routes=routes)


def normalized_ai_config(config: AppConfig) -> AppConfig:
    """Supply the offline legacy route only when a caller actually uses v1.2.1 routing."""
    if config.providers:
        return config
    providers, routes = _legacy_default()
    return AppConfig(
        allow_cloud=config.allow_cloud,
        allow_lan=config.allow_lan,
        providers=providers,
        routes=routes,
        budget=config.budget or Budget(),
        max_provider_attempts_per_task=config.max_provider_attempts_per_task,
    )


def save_config(path: Path, config: AppConfig) -> None:
    """Atomically update the GUI cloud switch without rewriting provider configuration."""
    if not isinstance(config, AppConfig):
        raise TypeError("config must be an AppConfig")
    if not isinstance(config.allow_cloud, bool):
        raise ConfigError("allow_cloud must be a boolean")
    setting = f"allow_cloud = {'true' if config.allow_cloud else 'false'}"
    content = f"{setting}\n"
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
            parsed = tomllib.loads(existing)
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise ConfigError(f"Unable to load configuration from {path}: {error}") from error
        lines = existing.splitlines(keepends=True)
        ai_header = next(
            (
                index
                for index, line in enumerate(lines)
                if re.match(r"^\s*\[ai\]\s*(?:#.*)?$", line)
            ),
            None,
        )
        if "ai" in parsed and ai_header is not None:
            section_end = next(
                (
                    index
                    for index in range(ai_header + 1, len(lines))
                    if re.match(r"^\s*\[.+\]", lines[index])
                ),
                len(lines),
            )
            for index in range(ai_header + 1, section_end):
                if re.match(r"^\s*allow_cloud\s*=", lines[index]):
                    lines[index] = f"{setting}\n"
                    break
            else:
                lines.insert(ai_header + 1, f"{setting}\n")
            content = "".join(lines)
        elif "ai" not in parsed:
            for index, line in enumerate(lines):
                if re.match(r"^\s*allow_cloud\s*=", line):
                    lines[index] = f"{setting}\n"
                    break
            else:
                lines.insert(0, f"{setting}\n")
            content = "".join(lines)
        else:
            raise ConfigError("Unable to update [ai] allow_cloud safely")
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        os.replace(temporary, path)
    except OSError as error:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise ConfigError(f"Unable to save configuration: {error}") from error
