"""Live provider tests are opt-in: deselected by default, and skipped without their variables."""

import os
import subprocess
import sys
import tomllib

from tests.live import test_anthropic_live
from tests.paths import PROJECT_ROOT, TESTS_ROOT

# pytest's exit code when every collected test was deselected.
NO_TESTS_COLLECTED = 5


def _pytest(*arguments: str, expect: int = 0) -> str:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("ARCHDISCOV_LIVE_", "ANTHROPIC_"))
    }
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *arguments],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == expect, completed.stdout + completed.stderr
    return completed.stdout


def test_the_default_options_deselect_live_tests() -> None:
    options = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pytest_options = options["tool"]["pytest"]["ini_options"]
    assert '-m "not live"' in pytest_options["addopts"]
    assert any(marker.startswith("live:") for marker in pytest_options["markers"])
    assert test_anthropic_live.pytestmark.name == "live"
    output = _pytest("--collect-only", "-q", str(TESTS_ROOT / "live"), expect=NO_TESTS_COLLECTED)
    assert "test_anthropic_live" not in output and "deselected" in output


def test_an_opted_in_run_without_its_variables_skips_with_a_reason() -> None:
    output = _pytest("-m", "live", str(TESTS_ROOT / "live"))
    assert "1 skipped" in output
    assert f"set {test_anthropic_live.KEY_ENV} and {test_anthropic_live.MODEL}" in output
