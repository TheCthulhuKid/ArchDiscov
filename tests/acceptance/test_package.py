"""Check installed entry points without relying on the repository working directory."""

import os
import sys
from importlib.metadata import version
from pathlib import Path

import pytest
from click import unstyle

from tests.support.installed_cli import CLI, run_installed


@pytest.mark.parametrize("entry_point", ["script", "module"])
@pytest.mark.parametrize("styled", [False, True], ids=["plain", "styled"])
def test_cli_outside_repository(tmp_path: Path, entry_point: str, styled: bool) -> None:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.update(TERM="xterm", TTY_COMPATIBLE="1" if styled else "0")
    command = CLI if entry_point == "script" else [sys.executable, "-m", "archdiscov"]
    for arguments in ([], ["--help"], ["--version"]):
        result = run_installed(tmp_path, *arguments, env=env, command=command)
        if arguments == ["--version"]:
            assert result.stdout.strip() == f"archdiscov {version('archdiscov')}"
        else:
            help_text = unstyle(result.stdout)
            assert "--help" in help_text
            assert "--version" in help_text


def test_model_imports_outside_repository(tmp_path: Path) -> None:
    code = (
        "from archdiscov.models.architecture import ArchitectureSnapshot; "
        "from archdiscov.models.enums import AwsRegion, ResourceType; "
        "ArchitectureSnapshot(account_id='123456789012', schema_version='1', "
        "snapshot_id='00000000-0000-4000-8000-000000000001', "
        "collection_started_at='2026-10-13T08:00:00Z', "
        "collection_completed_at='2026-10-13T08:01:00Z', "
        "source={'kind': 'mock', 'label': 'packaging-test'}, "
        "coverage={'regions': ['eu-west-1'], 'account_wide': False}); "
        "assert AwsRegion.EU_WEST_1 == 'eu-west-1'; "
        "assert ResourceType.VPC == 'AWS::EC2::VPC'"
    )
    run_installed(tmp_path, command=[sys.executable, "-c", code])


@pytest.mark.parametrize(
    "module",
    [
        "archdiscov.graph",
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
