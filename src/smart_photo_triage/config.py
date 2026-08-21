"""Configuration contract for Phase A."""

import tomllib
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
