"""Run the installed CLI from outside the checkout, without PYTHONPATH help."""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

ENTRY_POINTS = ["script", "module"]


def cli_command(entry_point: str) -> list[str]:
    if entry_point == "script":
        return [str(Path(sys.executable).with_name("archdiscov"))]
    return [sys.executable, "-m", "archdiscov"]


def run_installed(
    entry_point: str,
    cwd: Path,
    *arguments: str,
    expect: int = 0,
    timeout: int = 60,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the installed CLI; assert its exit code and a traceback-free stderr.

    Without `env` the child inherits the environment minus `PYTHONPATH`.
    """
    if env is None:
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [*cli_command(entry_point), *arguments],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    assert result.returncode == expect, result.stderr + result.stdout
    assert "Traceback" not in result.stderr
    return result


def run_installed_json(
    entry_point: str, cwd: Path, *arguments: str, expect: int = 0, timeout: int = 60
) -> Any:
    """Run a command that prints JSON; assert its exit code and a traceback-free stderr."""
    result = run_installed(entry_point, cwd, *arguments, expect=expect, timeout=timeout)
    return json.loads(result.stdout)
