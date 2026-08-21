"""Configuration contract for Phase A."""

import os
import secrets
import tomllib
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AppConfig:
    allow_cloud: bool


class ConfigError(ValueError):
    """Raised when the minimal configuration contract is invalid."""


def load_config(path: Path | None = None) -> AppConfig:
    """Load the minimal local configuration, defaulting to cloud access disabled."""
    if path is None:
        return AppConfig(allow_cloud=False)

    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"Unable to load configuration from {path}: {error}") from error

    unexpected = set(data) - {"allow_cloud"}
    if unexpected:
        names = ", ".join(sorted(unexpected))
        raise ConfigError(f"Unsupported configuration key(s): {names}")

    allow_cloud = data.get("allow_cloud", False)
    if not isinstance(allow_cloud, bool):
        raise ConfigError("allow_cloud must be a boolean")
    return AppConfig(allow_cloud=allow_cloud)


def save_config(path: Path, config: AppConfig) -> None:
    """Atomically persist the intentionally small workspace configuration."""
    if not isinstance(config, AppConfig):
        raise TypeError("config must be an AppConfig")
    if not isinstance(config.allow_cloud, bool):
        raise ConfigError("allow_cloud must be a boolean")
    content = f"allow_cloud = {'true' if config.allow_cloud else 'false'}\n"
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        os.replace(temporary, path)
    except OSError as error:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise ConfigError(f"Unable to save configuration: {error}") from error
