"""Live provider tests are opt-in: deselected by default, and skipped without their variables."""

import os
import subprocess
import sys
import tomllib

from tests.live import (
    test_anthropic_live,
    test_bedrock_live,
    test_evaluation_live,
    test_openai_live,
)
from tests.paths import PROJECT_ROOT, TESTS_ROOT
from tests.support import live

# pytest's exit code when every collected test was deselected.
NO_TESTS_COLLECTED = 5
OPENAI_REQUIRED = (
    live.OPENAI_KEY_ENV,
    live.OPENAI_ENDPOINT,
    live.OPENAI_MODEL,
    live.OPENAI_PROTOCOLS,
)


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
    modules = (test_anthropic_live, test_openai_live, test_bedrock_live, test_evaluation_live)
    for module in modules:
        assert module.pytestmark.name == "live"
    output = _pytest("--collect-only", "-q", str(TESTS_ROOT / "live"), expect=NO_TESTS_COLLECTED)
    assert "_live.py" not in output and "5 deselected" in output


def test_an_opted_in_run_without_its_variables_skips_with_a_reason() -> None:
    output = _pytest("-m", "live", "-rs", str(TESTS_ROOT / "live"))
    assert "5 skipped" in output
    assert f"set {live.ANTHROPIC_KEY_ENV}, {live.ANTHROPIC_MODEL} to run" in output
    assert f"set {', '.join(OPENAI_REQUIRED)} to run" in output
    assert f"set {live.BEDROCK_PROFILE}, {live.BEDROCK_REGION}, {live.BEDROCK_MODEL}" in output
    assert test_evaluation_live.NO_PROVIDER in output


def test_an_openai_run_skips_the_protocols_it_does_not_list(monkeypatch) -> None:
    for name in OPENAI_REQUIRED:
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv(live.OPENAI_PROTOCOLS, "responses")
    output = _pytest(
        "-m", "live", "-rs", str(TESTS_ROOT / "live" / "test_openai_live.py"), forward=True
    )
    assert "2 skipped" in output
    assert f"{live.OPENAI_PROTOCOLS} does not list chat_completions" in output
    assert f"{live.OPENAI_KEY_ENV} names x, which holds no key" in output
