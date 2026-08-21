from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from smart_photo_triage.cli import main


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "smart_photo_triage", *args],
        check=False,
        capture_output=True,
        text=True,
    )


def test_t_a_001_cli_help_exits_zero() -> None:
    result = run_cli("--help")

    assert result.returncode == 0, result.stderr
    assert "Smart-Photo-Triage" in result.stdout
    assert "init" in result.stdout


def test_cli_help_direct_entrypoint_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])

    assert exit_info.value.code == 0
    assert "Smart-Photo-Triage" in capsys.readouterr().out


def test_cli_init_direct_entrypoint(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workspace = tmp_path / "direct workspace"

    assert main(["init", "--workspace", str(workspace)]) == 0
    assert str(workspace.resolve()) in capsys.readouterr().out


def test_cli_without_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "Smart-Photo-Triage" in capsys.readouterr().out


def test_windows_path_smoke_supports_spaces_and_unicode(tmp_path: Path) -> None:
    workspace = tmp_path / "相册 workspace"

    first = run_cli("init", "--workspace", str(workspace))
    second = run_cli("init", "--workspace", str(workspace))

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert (workspace / "config.toml").is_file()
    assert (workspace / "spt.sqlite3").is_file()


def test_cli_unicode_path_survives_ascii_redirected_output(tmp_path: Path) -> None:
    workspace = tmp_path / "相册 workspace"
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "ascii:strict"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "smart_photo_triage",
            "init",
            "--workspace",
            str(workspace),
        ],
        check=False,
        capture_output=True,
        env=environment,
    )

    assert result.returncode == 0, result.stderr.decode("ascii", errors="replace")
    output = result.stdout.decode("ascii")
    assert "Workspace ready:" in output
    assert "\\u" in output
    assert (workspace / "spt.sqlite3").is_file()
