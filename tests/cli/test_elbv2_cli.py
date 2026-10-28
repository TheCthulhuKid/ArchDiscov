import json
import os
import subprocess
import sys
from pathlib import Path

from tests.support.elbv2_data import ACCOUNT


def test_installed_entrypoint_help_and_region_validation_outside_checkout(tmp_path):
    executable = Path(sys.executable).parent / "archdiscov"
    env = {
        key: value
        for key, value in os.environ.items()
        if key != "PYTHONPATH" and not key.startswith("AWS_")
    }
    env["AWS_EC2_METADATA_DISABLED"] = "true"
    help_result = subprocess.run(
        [str(executable), "collect", "--help"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert help_result.returncode == 0
    assert "elasticloadbalancing" in help_result.stdout
    result = subprocess.run(
        [
            str(executable),
            "collect",
            "--account",
            f"dev:{ACCOUNT}",
            "--service",
            "elasticloadbalancing",
            "-o",
            str(tmp_path / "not-created.json"),
            "--json",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 2
    assert json.loads(result.stdout)["error"]["code"] == "invalid_request"
    assert not (tmp_path / "not-created.json").exists()
