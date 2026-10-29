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

OPENAI_REQUIRED = (
    live.OPENAI_KEY_ENV,
    live.OPENAI_ENDPOINT,
    live.OPENAI_MODEL,
    live.OPENAI_PROTOCOLS,
)


def test_the_default_options_deselect_live_tests(request) -> None:
    options = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pytest_options = options["tool"]["pytest"]["ini_options"]
    assert '-m "not live"' in pytest_options["addopts"]
    assert any(marker.startswith("live:") for marker in pytest_options["markers"])
    modules = (test_anthropic_live, test_openai_live, test_bedrock_live, test_evaluation_live)
    for module in modules:
        assert module.pytestmark.name == "live"
    # This run applies those options, so the live tests are deselected from it.
    assert request.config.option.markexpr == "not live"


def test_an_opted_in_run_without_keys_skips_each_test_with_its_reason() -> None:
    """One opted-in run with no Anthropic or Bedrock variables, and OpenAI variables that
    list only the Responses protocol and name a key variable holding no key."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("ARCHDISCOV_LIVE_", "ANTHROPIC_", "OPENAI_"))
    }
    env.update(dict.fromkeys(OPENAI_REQUIRED, "x"), **{live.OPENAI_PROTOCOLS: "responses"})
    env.pop("x", None)
    live_dir = str(TESTS_ROOT / "live")
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-m", "live", "-rs", live_dir],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    output = completed.stdout
    assert "5 skipped" in output
    assert f"set {live.ANTHROPIC_KEY_ENV}, {live.ANTHROPIC_MODEL} to run" in output
    assert f"{live.OPENAI_PROTOCOLS} does not list chat_completions" in output
    assert f"{live.OPENAI_KEY_ENV} names x, which holds no key" in output
    assert f"set {live.BEDROCK_PROFILE}, {live.BEDROCK_REGION}, {live.BEDROCK_MODEL}" in output
    assert live.NO_PROVIDER in output
