"""The installed `collect` command outside the checkout: its help names every service and
control, and an invalid request exits 2 with nothing written."""

import json

import pytest
from click import unstyle

from archdiscov.ingestion.requests import COLLECTABLE_SERVICES
from tests.support.installed_cli import run_installed
from tests.support.offline_guard import OfflineGuard

ACCOUNT = "123456789012"


@pytest.mark.parametrize("styled", [False, True], ids=["plain", "styled"])
def test_installed_collect_controls_outside_checkout(tmp_path, styled):
    env = OfflineGuard(tmp_path).env
    env.update(TERM="xterm", TTY_COMPATIBLE="1" if styled else "0")
    help_text = unstyle(run_installed(tmp_path, "collect", "--help", env=env).stdout)
    for flag in (
        "--all-services",
        "--region-scope",
        "--max-in-flight-calls",
        "--max-active-accounts",
    ):
        assert flag in help_text
    assert all(service.value in help_text for service in COLLECTABLE_SERVICES)
    output = tmp_path / "not-created.json"
    arguments = ["collect", "--account", f"dev:{ACCOUNT}", "-o", str(output), "--json"]
    result = run_installed(
        tmp_path, *arguments, "--all-services", "--service", "iam", expect=2, env=env
    )
    assert json.loads(result.stdout)["error"]["code"] == "invalid_request"
    assert not output.exists()
