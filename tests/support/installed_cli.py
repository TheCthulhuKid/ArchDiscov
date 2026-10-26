"""Run the installed CLI from outside the checkout, without PYTHONPATH help."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from tests.assistant.provider_harness import MockedHarness
from tests.support.ask_examples import write_provider_config
from tests.support.evaluation_suites import SUITE
from tests.support.offline_guard import OfflineGuard

# The installed console script. `tests/acceptance/test_package.py` checks that
# `python -m archdiscov` starts too; journeys run through the script alone.
CLI = [str(Path(sys.executable).with_name("archdiscov"))]


def run_installed(
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
        [*CLI, *arguments],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    assert result.returncode == expect, result.stderr + result.stdout
    assert "Traceback" not in result.stderr
    return result


def run_installed_json(cwd: Path, *arguments: str, expect: int = 0, timeout: int = 60) -> Any:
    """Run a command that prints JSON; assert its exit code and a traceback-free stderr."""
    result = run_installed(cwd, *arguments, expect=expect, timeout=timeout)
    return json.loads(result.stdout)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Workspace:
    """A directory outside the checkout and an installed CLI that cannot reach the network."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.guard = OfflineGuard(root)
        for name in ("fixtures", "snapshots", "results", "comparisons"):
            (root / name).mkdir()

    def path(self, *parts: str) -> Path:
        return self.root.joinpath(*parts)

    def run(self, *arguments: object, expect: int = 0):
        return run_installed(
            self.root,
            *map(str, arguments),
            expect=expect,
            env=self.guard.env,
        )

    def json(self, *arguments: object, expect: int = 0):
        data = json.loads(self.run(*arguments, "--json", expect=expect).stdout)
        return data

    def both(self, *arguments: object) -> tuple[dict, str]:
        """The JSON result and the readable output of one read-only command."""
        return self.json(*arguments), self.run(*arguments).stdout


INSTALLED_KEY_VARIABLE = "ASK_INSTALLED_PROVIDER_KEY"
DUMMY_KEY = "sk-proj-INSTALLEDdummy0123456789abcdefXYZ"


def harness_config(directory: Path, harness: MockedHarness, endpoint: str | None = None) -> Path:
    """The harness's configuration, keyed by `INSTALLED_KEY_VARIABLE`, at `endpoint` if given."""
    fields = harness.configuration(INSTALLED_KEY_VARIABLE)
    if endpoint is not None:
        fields["endpoint"] = endpoint
    return write_provider_config(directory / f"{harness.label}.toml", fields)


def suite_cases(ws: Workspace, suite: str) -> dict[str, dict]:
    """The cases of the evaluation suite copied to the workspace's `fixtures/<suite>`."""
    data = json.loads(ws.path("fixtures", suite, SUITE).read_text(encoding="utf-8"))
    return {case["id"]: case for case in data["cases"]}


def ingest_selection(ws: Workspace, suite: str, case: dict) -> list[str]:
    """Ingest the case's snapshots, byte for byte, and return the `--snapshot`, `--compare`,
    and `--comparison-result` options that select the ingested files."""
    fixtures = ws.path("fixtures", suite)
    selection = []
    for key, option in (("snapshot", "--snapshot"), ("comparison", "--compare")):
        if key not in case:
            continue
        output = ws.path("snapshots", f"{suite}-{case['id']}-{key}.json")
        saved = ws.json("ingest", fixtures / case[key], "-o", output)
        assert digest(output) == digest(fixtures / case[key]), saved
        selection += [option, str(output)]
    for path in case.get("comparison_results", ()):
        selection += ["--comparison-result", str(fixtures / path)]
    return selection
