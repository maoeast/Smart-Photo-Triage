"""Provider-neutral, bounded model routing with no filesystem authority.

This module deliberately accepts already-controlled ``VisionRequest`` objects.  It never
opens a path, writes a plan, or imports the scanner, planner, executor, or workspace.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from ipaddress import ip_address
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from smart_photo_triage.ai import VisionAnalysis, VisionRequest


class TaskType(StrEnum):
    ITEM_ANALYSIS = "item_analysis"
    BURST_REVIEW = "burst_review"


class NetworkScope(StrEnum):
    LOOPBACK = "loopback"
    LAN = "lan"
    REMOTE = "remote"


class ErrorClass(StrEnum):
    RATE_LIMIT = "RATE_LIMIT"
    TIMEOUT = "TIMEOUT"
    SERVER_ERROR = "SERVER_ERROR"
    NETWORK_ERROR = "NETWORK_ERROR"
    SCHEMA_INVALID = "SCHEMA_INVALID"
    CAPABILITY_MISMATCH = "CAPABILITY_MISMATCH"
    AUTH_ERROR = "AUTH_ERROR"
    BILLING_ERROR = "BILLING_ERROR"
    CONFIG_ERROR = "CONFIG_ERROR"
    PRIVACY_BLOCKED = "PRIVACY_BLOCKED"
    CONTENT_REJECTED = "CONTENT_REJECTED"
    BUDGET_BLOCKED = "BUDGET_BLOCKED"
    UNKNOWN_PROVIDER_ERROR = "UNKNOWN_PROVIDER_ERROR"


_DRIVERS = frozenset({"gemini", "openai", "anthropic", "openai_compatible", "fake"})
_PROVIDER_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_FALLBACK_ERRORS = frozenset(
    {
        ErrorClass.RATE_LIMIT,
        ErrorClass.TIMEOUT,
        ErrorClass.SERVER_ERROR,
        ErrorClass.NETWORK_ERROR,
        ErrorClass.SCHEMA_INVALID,
        ErrorClass.CAPABILITY_MISMATCH,
    }
)


def builtin_capabilities(driver: str) -> ProviderCapabilities:
    """Conservative built-in capability profiles, versioned for cache identity."""
    if driver not in _DRIVERS:
        raise ValueError(f"unsupported provider driver: {driver}")
    # Native vendors may support more.  These are deliberately conservative defaults.
    return ProviderCapabilities(
        supports_image=True,
        supports_multi_image=True,
        supports_structured_json=True,
        supports_json_schema=driver in {"openai", "fake"},
        max_images_per_request=8,
        max_request_bytes=32 * 1024 * 1024,
        supported_image_mime_types=frozenset({"image/jpeg", "image/png", "image/webp"}),
        supports_system_prompt=driver != "gemini",
        capability_profile_version=f"builtin-{driver}-v1",
    )


class ProviderFailure(RuntimeError):
    """A sanitised provider result.  Its text never includes request or secret data."""

    def __init__(self, error_class: ErrorClass, reason: str | None = None) -> None:
        self.error_class = error_class
        self.reason = reason or error_class.value
        super().__init__(f"{error_class.value}: {self.reason}")


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    supports_image: bool
    supports_multi_image: bool
    supports_structured_json: bool
    supports_json_schema: bool
    max_images_per_request: int | None
    max_request_bytes: int | None
    supported_image_mime_types: frozenset[str]
    supports_system_prompt: bool
    capability_profile_version: str
    supports_streaming: bool = False

    def __post_init__(self) -> None:
        if not self.supports_image:
            raise ValueError("v1.2.1 tasks require image-capable providers")
        if self.supports_multi_image and not self.supports_image:
            raise ValueError("multi-image requires image support")
        if self.max_images_per_request is not None and self.max_images_per_request < 1:
            raise ValueError("max_images_per_request must be positive when set")
        if self.max_request_bytes is not None and self.max_request_bytes < 1:
            raise ValueError("max_request_bytes must be positive when set")
        if not self.supported_image_mime_types:
            raise ValueError("supported_image_mime_types must not be empty")
        if not self.capability_profile_version.strip():
            raise ValueError("capability_profile_version must not be empty")

    def supports(self, task_type: TaskType, request: VisionRequest) -> str | None:
        count = len(request.items)
        if not self.supports_image:
            return "provider does not support images"
        if task_type is TaskType.BURST_REVIEW and (count < 2 or not self.supports_multi_image):
            return "burst review requires multi-image support"
        if self.max_images_per_request is not None and count > self.max_images_per_request:
            return "request exceeds provider image limit"
        if self.max_request_bytes is not None and preview_bytes(request) > self.max_request_bytes:
            return "request exceeds provider byte limit"
        unsupported = sorted(
            {item.preview_mime_type for item in request.items} - self.supported_image_mime_types
        )
        if unsupported:
            return "unsupported preview MIME type"
        return None


def classify_endpoint(base_url: str) -> NetworkScope:
    """Classify literal endpoint hosts conservatively.  Host names are remote.

    A string containing ``local`` is not a loopback address.  This avoids an
    attacker-controlled DNS name bypassing the privacy gate.
    """
    parts = urlsplit(base_url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("provider base_url must be an absolute HTTP(S) URL")
    if parts.username is not None or parts.password is not None:
        raise ValueError("provider base_url must not contain credentials")
    try:
        address = ip_address(parts.hostname)
    except ValueError:
        return NetworkScope.REMOTE
    if address.is_loopback:
        return NetworkScope.LOOPBACK
    if address.is_private or address.is_link_local:
        return NetworkScope.LAN
    return NetworkScope.REMOTE


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    provider_id: str
    driver: str
    model: str
    base_url: str
    network_scope: NetworkScope
    capabilities: ProviderCapabilities
    display_name: str | None = None
    api_key_env: str | None = None
    enabled: bool = True
    estimated_cost_per_request: float | None = None

    def __post_init__(self) -> None:
        if not _PROVIDER_ID.fullmatch(self.provider_id):
            raise ValueError("provider_id must be a stable lowercase identifier")
        if self.driver not in _DRIVERS:
            raise ValueError(f"unsupported provider driver: {self.driver}")
        if not self.model.strip():
            raise ValueError("provider model must not be empty")
        actual_scope = classify_endpoint(self.base_url)
        if actual_scope is not self.network_scope:
            raise ValueError("provider network_scope does not match the endpoint")
        scheme = urlsplit(self.base_url).scheme
        if self.network_scope is NetworkScope.REMOTE and scheme != "https":
            raise ValueError("remote provider endpoints require HTTPS")
        if self.api_key_env is not None and not re.fullmatch(r"[A-Z_][A-Z0-9_]*", self.api_key_env):
            raise ValueError("api_key_env must be an environment variable name")
        if self.estimated_cost_per_request is not None and (
            not math.isfinite(self.estimated_cost_per_request)
            or self.estimated_cost_per_request < 0
        ):
            raise ValueError("estimated_cost_per_request must be finite and non-negative")

    def resolve_api_key(self) -> str | None:
        if self.api_key_env is None:
            return None
        return os.environ.get(self.api_key_env) or None

    def endpoint_identity(self) -> str:
        """A non-secret stable endpoint cache component."""
        return hashlib.sha256(self.base_url.encode("utf-8")).hexdigest()

    def redacted_summary(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id,
            "driver": self.driver,
            "model": self.model,
            "network_scope": self.network_scope.value,
            "enabled": self.enabled,
            "api_key_configured": bool(self.resolve_api_key()),
            "capability_profile_version": self.capabilities.capability_profile_version,
        }


def validate_provider_network_access(
    provider: ProviderConfig, *, allow_cloud: bool = False, allow_lan: bool = False
) -> None:
    """Apply the privacy gate before any request is constructed or sent."""
    if provider.network_scope is NetworkScope.REMOTE and not allow_cloud:
        raise ProviderFailure(
            ErrorClass.PRIVACY_BLOCKED, "remote provider requires allow_cloud=true"
        )
    if provider.network_scope is NetworkScope.LAN and not allow_lan:
        raise ProviderFailure(ErrorClass.PRIVACY_BLOCKED, "LAN provider requires allow_lan=true")


@dataclass(frozen=True, slots=True)
class ProviderRegistry:
    providers: tuple[ProviderConfig, ...]

    def __post_init__(self) -> None:
        ids = [provider.provider_id for provider in self.providers]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate provider_id")
        if not self.providers:
            raise ValueError("provider registry must not be empty")

    def get(self, provider_id: str) -> ProviderConfig:
        for provider in self.providers:
            if provider.provider_id == provider_id:
                return provider
        raise KeyError(provider_id)


@dataclass(frozen=True, slots=True)
class RoutePolicy:
    primary: str
    fallbacks: tuple[str, ...] = ()
    confidence_below: float | None = None
    escalate_to: str | None = None
    max_escalations: int = 1

    def __post_init__(self) -> None:
        if not self.primary:
            raise ValueError("route primary must not be empty")
        if self.primary in self.fallbacks:
            raise ValueError("route cannot fallback to itself")
        if len(self.fallbacks) != len(set(self.fallbacks)):
            raise ValueError("route fallbacks must be unique")
        if self.confidence_below is not None and (
            not math.isfinite(self.confidence_below) or not 0 <= self.confidence_below <= 1
        ):
            raise ValueError("confidence_below must be between 0 and 1")
        if (self.confidence_below is None) != (self.escalate_to is None):
            raise ValueError("escalation needs both confidence_below and escalate_to")
        if self.max_escalations not in {0, 1}:
            raise ValueError("v1.2.1 supports at most one confidence escalation")


@dataclass(slots=True)
class Budget:
    max_requests: int | None = None
    max_remote_preview_bytes: int | None = None
    max_estimated_cost: float | None = None
    requests_used: int = 0
    remote_preview_bytes_used: int = 0
    estimated_cost_used: float = 0.0

    def __post_init__(self) -> None:
        if self.max_requests is not None and self.max_requests < 0:
            raise ValueError("max_requests must not be negative")
        if self.max_remote_preview_bytes is not None and self.max_remote_preview_bytes < 0:
            raise ValueError("max_remote_preview_bytes must not be negative")
        if self.max_estimated_cost is not None and self.max_estimated_cost < 0:
            raise ValueError("max_estimated_cost must not be negative")

    def reserve(self, provider: ProviderConfig, request: VisionRequest) -> None:
        request_bytes = (
            preview_bytes(request) if provider.network_scope is NetworkScope.REMOTE else 0
        )
        request_cost = provider.estimated_cost_per_request or 0.0
        if self.max_requests is not None and self.requests_used + 1 > self.max_requests:
            raise ProviderFailure(ErrorClass.BUDGET_BLOCKED, "maximum AI request count reached")
        if (
            self.max_remote_preview_bytes is not None
            and self.remote_preview_bytes_used + request_bytes > self.max_remote_preview_bytes
        ):
            raise ProviderFailure(ErrorClass.BUDGET_BLOCKED, "maximum remote preview bytes reached")
        if (
            self.max_estimated_cost is not None
            and self.estimated_cost_used + request_cost > self.max_estimated_cost
        ):
            raise ProviderFailure(ErrorClass.BUDGET_BLOCKED, "maximum estimated cost reached")
        self.requests_used += 1
        self.remote_preview_bytes_used += request_bytes
        self.estimated_cost_used += request_cost


class ProviderDriver(Protocol):
    def analyze(self, request: VisionRequest) -> object: ...


class FakeDriver:
    """Small adapter used by routing tests and synthetic E2E fixtures."""

    def __init__(self, handler: Callable[[VisionRequest], object]) -> None:
        self._handler = handler
        self.invocation_count = 0

    def analyze(self, request: VisionRequest) -> object:
        self.invocation_count += 1
        value = self._handler(request)
        if isinstance(value, BaseException):
            raise value
        return value


@dataclass(frozen=True, slots=True)
class RouteAttempt:
    attempt_index: int
    provider_id: str
    driver: str
    model: str
    status: str
    route_reason: str
    error_class: ErrorClass | None = None
    cache_hit: bool = False
    remote_preview_bytes: int = 0


@dataclass(frozen=True, slots=True)
class RouteExecution:
    task_type: TaskType
    result: dict[int, VisionAnalysis] | None
    effective_provider_id: str | None
    attempts: tuple[RouteAttempt, ...]
    escalated: bool

    @property
    def request_count(self) -> int:
        return sum(
            1 for attempt in self.attempts if attempt.status == "SUCCESS" and not attempt.cache_hit
        )


def preview_bytes(request: VisionRequest) -> int:
    return sum(len(item.preview_bytes) for item in request.items)


def _request_identity(task_type: TaskType, provider: ProviderConfig, request: VisionRequest) -> str:
    payload = {
        "task_type": task_type.value,
        "provider_id": provider.provider_id,
        "driver": provider.driver,
        "model": provider.model,
        "endpoint_identity": provider.endpoint_identity(),
        "capability_profile_version": provider.capabilities.capability_profile_version,
        "prompt_version": request.prompt_version,
        "schema_version": request.schema_version,
        "items": [
            {
                "id": item.item_id,
                "mime": item.preview_mime_type,
                "preview_sha256": hashlib.sha256(item.preview_bytes).hexdigest(),
                "quality": item.quality,
            }
            for item in request.items
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()


def _failure_from(error: BaseException) -> ProviderFailure:
    if isinstance(error, ProviderFailure):
        return error
    # Existing v1.2 errors remain a safe bridge, without exposing their text to audit.
    name = type(error).__name__
    if name == "SchemaValidationError":
        return ProviderFailure(ErrorClass.SCHEMA_INVALID)
    if name == "TransientProviderError":
        return ProviderFailure(ErrorClass.NETWORK_ERROR)
    if name == "CloudDisabledError":
        return ProviderFailure(ErrorClass.PRIVACY_BLOCKED)
    return ProviderFailure(ErrorClass.UNKNOWN_PROVIDER_ERROR)


class ModelRouter:
    """Execute only declared routes.  It holds no workspace or filesystem capability."""

    def __init__(
        self,
        registry: ProviderRegistry,
        routes: Mapping[TaskType, RoutePolicy],
        *,
        drivers: Mapping[str, ProviderDriver] | None = None,
        budget: Budget | None = None,
        max_provider_attempts_per_task: int = 3,
        max_retry_per_provider: int = 0,
        cache: object | None = None,
    ) -> None:
        if not 1 <= max_provider_attempts_per_task <= 32:
            raise ValueError("max_provider_attempts_per_task must be between 1 and 32")
        if not 0 <= max_retry_per_provider <= 10:
            raise ValueError("max_retry_per_provider must be between 0 and 10")
        self.registry = registry
        self.routes = dict(routes)
        self.drivers = dict(drivers or {})
        self.budget = budget or Budget()
        self.max_provider_attempts_per_task = max_provider_attempts_per_task
        self.max_retry_per_provider = max_retry_per_provider
        self.cache: object = cache if cache is not None else {}
        for task_type, route in self.routes.items():
            self._validate_route(task_type, route)

    def _validate_route(self, task_type: TaskType, route: RoutePolicy) -> None:
        del task_type
        for provider_id in (route.primary, *route.fallbacks, route.escalate_to):
            if provider_id is None:
                continue
            try:
                provider = self.registry.get(provider_id)
            except KeyError as error:
                raise ValueError(f"route references unknown provider: {provider_id}") from error
            if not provider.enabled:
                raise ValueError(f"route references disabled provider: {provider_id}")
        if route.escalate_to == route.primary:
            raise ValueError("route cannot escalate to itself")

    def run(
        self,
        task_type: TaskType,
        request: VisionRequest,
        *,
        allow_cloud: bool = False,
        allow_lan: bool = False,
    ) -> RouteExecution:
        try:
            route = self.routes[task_type]
        except KeyError as error:
            raise ValueError(f"no route configured for {task_type.value}") from error
        attempts: list[RouteAttempt] = []
        primary_result, primary_provider = self._run_candidates(
            task_type,
            request,
            (route.primary, *route.fallbacks),
            attempts,
            "primary_or_fallback",
            allow_cloud,
            allow_lan,
        )
        if primary_result is None:
            return RouteExecution(task_type, None, None, tuple(attempts), False)
        if (
            route.escalate_to is None
            or route.confidence_below is None
            or route.max_escalations == 0
            or min(analysis.confidence for analysis in primary_result.values())
            >= route.confidence_below
        ):
            return RouteExecution(
                task_type, primary_result, primary_provider, tuple(attempts), False
            )
        escalated, escalated_provider = self._run_candidates(
            task_type,
            request,
            (route.escalate_to,),
            attempts,
            "low_confidence_escalation",
            allow_cloud,
            allow_lan,
        )
        return RouteExecution(
            task_type,
            escalated if escalated is not None else primary_result,
            escalated_provider if escalated is not None else primary_provider,
            tuple(attempts),
            escalated is not None,
        )

    def _run_candidates(
        self,
        task_type: TaskType,
        request: VisionRequest,
        candidates: tuple[str, ...],
        attempts: list[RouteAttempt],
        route_reason: str,
        allow_cloud: bool,
        allow_lan: bool,
    ) -> tuple[dict[int, VisionAnalysis] | None, str | None]:
        for provider_id in candidates:
            if len(attempts) >= self.max_provider_attempts_per_task:
                return None, None
            provider = self.registry.get(provider_id)
            mismatch = provider.capabilities.supports(task_type, request)
            if mismatch is not None:
                attempts.append(
                    self._attempt(
                        provider, attempts, "SKIPPED", route_reason, ErrorClass.CAPABILITY_MISMATCH
                    )
                )
                continue
            cache_key = _request_identity(task_type, provider, request)
            cached = self._cache_get(cache_key)
            if cached is not None:
                attempts.append(
                    self._attempt(provider, attempts, "SUCCESS", route_reason, cache_hit=True)
                )
                return cached, provider.provider_id
            retries = 0
            while len(attempts) < self.max_provider_attempts_per_task:
                try:
                    validate_provider_network_access(
                        provider, allow_cloud=allow_cloud, allow_lan=allow_lan
                    )
                    driver = self.drivers.get(provider.provider_id)
                    if driver is None:
                        raise ProviderFailure(
                            ErrorClass.CONFIG_ERROR, "provider driver is unavailable"
                        )
                    self.budget.reserve(provider, request)
                    payload = driver.analyze(request)
                    from smart_photo_triage.ai import validate_response_mapping

                    mapped = validate_response_mapping(
                        payload, expected_item_ids=tuple(item.item_id for item in request.items)
                    )
                except BaseException as error:
                    failure = _failure_from(error)
                    attempts.append(
                        self._attempt(
                            provider, attempts, "FAILED", route_reason, failure.error_class
                        )
                    )
                    if failure.error_class not in _FALLBACK_ERRORS:
                        return None, None
                    if (
                        failure.error_class is not ErrorClass.CAPABILITY_MISMATCH
                        and retries < self.max_retry_per_provider
                        and len(attempts) < self.max_provider_attempts_per_task
                    ):
                        retries += 1
                        continue
                    break
                self._cache_store(cache_key, task_type, provider, mapped)
                attempts.append(self._attempt(provider, attempts, "SUCCESS", route_reason))
                return mapped, provider.provider_id
        return None, None

    @staticmethod
    def _attempt(
        provider: ProviderConfig,
        attempts: list[RouteAttempt],
        status: str,
        route_reason: str,
        error_class: ErrorClass | None = None,
        *,
        cache_hit: bool = False,
    ) -> RouteAttempt:
        return RouteAttempt(
            attempt_index=len(attempts) + 1,
            provider_id=provider.provider_id,
            driver=provider.driver,
            model=provider.model,
            status=status,
            route_reason=route_reason,
            error_class=error_class,
            cache_hit=cache_hit,
            remote_preview_bytes=0,
        )

    def _cache_get(self, cache_key: str) -> dict[int, VisionAnalysis] | None:
        getter = getattr(self.cache, "get", None)
        if not callable(getter):
            raise TypeError("provider cache must expose get(cache_key)")
        value = getter(cache_key)
        if value is None or isinstance(value, dict):
            return value
        raise TypeError("provider cache returned an invalid value")

    def _cache_store(
        self,
        cache_key: str,
        task_type: TaskType,
        provider: ProviderConfig,
        result: dict[int, VisionAnalysis],
    ) -> None:
        put = getattr(self.cache, "put", None)
        if callable(put):
            put(cache_key, task_type, provider, result)
            return
        if not isinstance(self.cache, dict):
            raise TypeError("provider cache must expose put(...) or be a dictionary")
        self.cache[cache_key] = result
