"""Check installed entry points without relying on the repository working directory."""

import os
import sys
from importlib.metadata import version
from pathlib import Path

import pytest
from click import unstyle

from tests.support.installed_cli import CLI, run_installed


@pytest.mark.parametrize("entry_point", ["script", "module"])
def test_cli_outside_repository(tmp_path: Path, entry_point: str) -> None:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    command = CLI if entry_point == "script" else [sys.executable, "-m", "archdiscov"]
    # Without a command, the CLI prints its help.
    help_text = unstyle(run_installed(tmp_path, env=env, command=command).stdout)
    assert "--help" in help_text and "--version" in help_text
    result = run_installed(tmp_path, "--version", env=env, command=command)
    assert result.stdout.strip() == f"archdiscov {version('archdiscov')}"


@pytest.mark.parametrize(
    "module",
    [
        "archdiscov.graph",
        "archdiscov.models.architecture",
        "archdiscov.services",
        "archdiscov.services.comparison",
        "archdiscov.services.comparison_results",
        "archdiscov.models.comparison.results",
        "archdiscov.cli",
    ],
)
def test_every_public_package_imports_first_without_a_cycle(tmp_path: Path, module: str) -> None:
    """Each package is the first import of a fresh interpreter, as a library caller would do."""
    run_installed(tmp_path, command=[sys.executable, "-c", f"import {module}"])
