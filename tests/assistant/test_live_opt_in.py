"""Live provider tests are opt-in: deselected by default, and skipped without their variables."""

import os
import subprocess
import sys
import tomllib

from tests.live import test_anthropic_live, test_openai_live
from tests.paths import PROJECT_ROOT, TESTS_ROOT

# pytest's exit code when every collected test was deselected.
NO_TESTS_COLLECTED = 5


def _pytest(*arguments: str, expect: int = 0, forward: bool = False) -> str:
    """Run pytest without provider variables, or, when `forward`, with only the live ones."""
    env = {
        key: value
        for key, value in os.environ.items()
        if (forward and key.startswith("ARCHDISCOV_LIVE_"))
        or not key.startswith(("ARCHDISCOV_LIVE_", "ANTHROPIC_", "OPENAI_"))
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
    for module in (test_anthropic_live, test_openai_live):
        assert module.pytestmark.name == "live"
    output = _pytest("--collect-only", "-q", str(TESTS_ROOT / "live"), expect=NO_TESTS_COLLECTED)
    assert "_live.py" not in output and "3 deselected" in output


def test_an_opted_in_run_without_its_variables_skips_with_a_reason() -> None:
    output = _pytest("-m", "live", "-rs", str(TESTS_ROOT / "live"))
    assert "3 skipped" in output
    assert f"set {test_anthropic_live.KEY_ENV} and {test_anthropic_live.MODEL}" in output
    assert f"set {', '.join(test_openai_live.REQUIRED)}" in output


def test_an_openai_run_skips_the_protocols_it_does_not_list(monkeypatch) -> None:
    for name in test_openai_live.REQUIRED:
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv(test_openai_live.PROTOCOLS, "responses")
    output = _pytest(
        "-m", "live", "-rs", str(TESTS_ROOT / "live" / "test_openai_live.py"), forward=True
    )
    assert "2 skipped" in output
    assert f"{test_openai_live.PROTOCOLS} does not list chat_completions" in output
    assert f"{test_openai_live.KEY_ENV} names x, which holds no key" in output
