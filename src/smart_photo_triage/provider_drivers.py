"""Small standard-library provider adapters behind the v1.2.1 driver contract.

All adapters receive only ``VisionRequest`` controlled previews.  Redirects are disabled
at transport creation, so a loopback or LAN endpoint cannot silently redirect media to a
remote host.
"""

from __future__ import annotations

import base64
import json
import socket
from collections.abc import Callable
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from smart_photo_triage.ai import GeminiVisionProvider, VisionRequest
from smart_photo_triage.model_routing import ErrorClass, ProviderConfig, ProviderFailure

_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class _Response(Protocol):
    def __enter__(self) -> _Response: ...

    def __exit__(self, *_args: object) -> None: ...

    def read(self, size: int = -1) -> bytes: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


def no_redirect_opener(request: Request, timeout: float) -> _Response:
    return build_opener(_NoRedirect()).open(request, timeout=timeout)  # type: ignore[return-value]


def _api_key(provider: ProviderConfig) -> str:
    key = provider.resolve_api_key()
    if not key:
        raise ProviderFailure(
            ErrorClass.CONFIG_ERROR, "configured API key environment variable is absent"
        )
    return key


def _failure(error: BaseException) -> ProviderFailure:
    if isinstance(error, ProviderFailure):
        return error
    if isinstance(error, HTTPError):
        if error.code == 401 or error.code == 403:
            return ProviderFailure(ErrorClass.AUTH_ERROR)
        if error.code == 402:
            return ProviderFailure(ErrorClass.BILLING_ERROR)
        if error.code == 429:
            return ProviderFailure(ErrorClass.RATE_LIMIT)
        if 500 <= error.code <= 599:
            return ProviderFailure(ErrorClass.SERVER_ERROR)
        return ProviderFailure(ErrorClass.UNKNOWN_PROVIDER_ERROR)
    if isinstance(error, (TimeoutError, socket.timeout)):
        return ProviderFailure(ErrorClass.TIMEOUT)
    if isinstance(error, (URLError, OSError)):
        return ProviderFailure(ErrorClass.NETWORK_ERROR)
    return ProviderFailure(ErrorClass.UNKNOWN_PROVIDER_ERROR)


def _read_json(opener: Callable[..., object], request: Request, timeout: float) -> object:
    try:
        response_context = opener(request, timeout=timeout)
        with response_context as response:  # type: ignore[attr-defined]
            response = response  # make the bounded read contract explicit for type checkers
            payload = response.read(_MAX_RESPONSE_BYTES + 1)  # type: ignore[attr-defined]
    except BaseException as error:
        raise _failure(error) from None
    if len(payload) > _MAX_RESPONSE_BYTES:
        raise ProviderFailure(ErrorClass.SCHEMA_INVALID, "provider response exceeds bounded size")
    try:
        return json.loads(payload)
    except (UnicodeError, json.JSONDecodeError):
        raise ProviderFailure(ErrorClass.SCHEMA_INVALID, "provider returned invalid JSON") from None


def _instruction(request: VisionRequest) -> str:
    return json.dumps(
        {
            "task": "Return a JSON array. Exactly one v1.2 result object per item_id.",
            "prompt_version": request.prompt_version,
            "schema_version": request.schema_version,
            "privacy": "Inputs contain only controlled previews and anonymous quality metrics.",
        },
        separators=(",", ":"),
    )


class OpenAICompatibleVisionDriver:
    """One adapter for OpenAI, Qwen, Doubao, GLM, and other compatible endpoints."""

    def __init__(
        self,
        provider: ProviderConfig,
        *,
        opener: Callable[..., object] | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.provider = provider
        self.opener = opener or no_redirect_opener
        self.timeout_seconds = timeout_seconds

    def analyze(self, request: VisionRequest) -> object:
        content: list[dict[str, object]] = [{"type": "text", "text": _instruction(request)}]
        for item in request.items:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": (
                            f"data:{item.preview_mime_type};base64,"
                            f"{base64.b64encode(item.preview_bytes).decode('ascii')}"
                        )
                    },
                }
            )
        payload: dict[str, object] = {
            "model": self.provider.model,
            "messages": [{"role": "user", "content": content}],
        }
        if self.provider.capabilities.supports_json_schema:
            payload["response_format"] = {"type": "json_object"}
        endpoint = f"{self.provider.base_url.rstrip('/')}/chat/completions"
        response = _read_json(
            self.opener,
            Request(
                endpoint,
                data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {_api_key(self.provider)}",
                },
                method="POST",
            ),
            self.timeout_seconds,
        )
        try:
            content_value = response["choices"][0]["message"]["content"]  # type: ignore[index]
            if isinstance(content_value, list):
                content_value = "".join(
                    str(item.get("text", "")) for item in content_value if isinstance(item, dict)
                )
            if not isinstance(content_value, str):
                raise TypeError
            return json.loads(content_value)
        except (IndexError, KeyError, TypeError, json.JSONDecodeError):
            raise ProviderFailure(
                ErrorClass.SCHEMA_INVALID, "provider response lacks structured content"
            ) from None


class AnthropicVisionDriver:
    """Native Anthropic Messages/Vision adapter, normalised to the same JSON result."""

    def __init__(
        self,
        provider: ProviderConfig,
        *,
        opener: Callable[..., object] | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.provider = provider
        self.opener = opener or no_redirect_opener
        self.timeout_seconds = timeout_seconds

    def analyze(self, request: VisionRequest) -> object:
        content: list[dict[str, object]] = [{"type": "text", "text": _instruction(request)}]
        for item in request.items:
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": item.preview_mime_type,
                        "data": base64.b64encode(item.preview_bytes).decode("ascii"),
                    },
                }
            )
        response = _read_json(
            self.opener,
            Request(
                f"{self.provider.base_url.rstrip('/')}/messages",
                data=json.dumps(
                    {
                        "model": self.provider.model,
                        "max_tokens": 4096,
                        "messages": [{"role": "user", "content": content}],
                    },
                    separators=(",", ":"),
                ).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": _api_key(self.provider),
                    "anthropic-version": "2023-06-01",
                },
                method="POST",
            ),
            self.timeout_seconds,
        )
        try:
            text = response["content"][0]["text"]  # type: ignore[index]
            return json.loads(text)
        except (IndexError, KeyError, TypeError, json.JSONDecodeError):
            raise ProviderFailure(
                ErrorClass.SCHEMA_INVALID, "provider response lacks structured content"
            ) from None


def build_driver(
    provider: ProviderConfig,
    *,
    opener: Callable[..., object] | None = None,
    timeout_seconds: float = 30.0,
) -> object:
    """Instantiate a driver without performing network I/O or logging secrets."""
    if provider.driver == "gemini":
        return GeminiVisionProvider(
            model=provider.model,
            api_key=_api_key(provider),
            opener=opener or no_redirect_opener,
            timeout_seconds=timeout_seconds,
            api_base=provider.base_url,
        )
    if provider.driver in {"openai", "openai_compatible"}:
        return OpenAICompatibleVisionDriver(
            provider, opener=opener, timeout_seconds=timeout_seconds
        )
    if provider.driver == "anthropic":
        return AnthropicVisionDriver(provider, opener=opener, timeout_seconds=timeout_seconds)
    raise ProviderFailure(ErrorClass.CONFIG_ERROR, "fake drivers must be supplied by the caller")
