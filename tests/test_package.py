"""Check installed entry points without relying on the repository working directory."""

import os
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

import pytest


@pytest.mark.parametrize("entry_point", ["script", "module"])
def test_cli_outside_repository(tmp_path: Path, entry_point: str) -> None:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    command = (
        [str(Path(sys.executable).with_name("archdiscov"))]
        if entry_point == "script"
        else [sys.executable, "-m", "archdiscov"]
    )
    for arguments in ([], ["--help"], ["--version"]):
        result = subprocess.run(
            [*command, *arguments],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        if arguments == ["--version"]:
            assert result.stdout.strip() == f"archdiscov {version('archdiscov')}"
        else:
            assert "--help" in result.stdout
            assert "--version" in result.stdout


def test_model_imports_outside_repository(tmp_path: Path) -> None:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from archdiscov.models.architecture import ArchitectureSnapshot; "
            "from archdiscov.models.enums import AwsRegion, ResourceType; "
            "ArchitectureSnapshot(account_id='123456789012', schema_version='1', "
            "snapshot_id='00000000-0000-4000-8000-000000000001', "
            "collection_started_at='2026-10-13T08:00:00Z', "
            "collection_completed_at='2026-10-13T08:01:00Z', "
            "source={'kind': 'mock', 'label': 'packaging-test'}, "
            "coverage={'regions': ['eu-west-1'], 'account_wide': False}); "
            "assert AwsRegion.EU_WEST_1 == 'eu-west-1'; "
            "assert ResourceType.VPC == 'AWS::EC2::VPC'",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
