from pathlib import Path

import pytest

from smart_photo_triage.config import AppConfig, ConfigError, load_config


def test_t_a_002_default_config_disables_cloud() -> None:
    assert load_config() == AppConfig(allow_cloud=False)


def test_config_can_explicitly_enable_cloud(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("allow_cloud = true\n", encoding="utf-8")

    assert load_config(path).allow_cloud is True


def test_missing_config_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="Unable to load"):
        load_config(tmp_path / "missing.toml")


@pytest.mark.parametrize("content", ["allow_cloud = 1\n", "unexpected = true\n"])
def test_invalid_minimal_config_is_rejected(tmp_path: Path, content: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ConfigError):
        load_config(path)
