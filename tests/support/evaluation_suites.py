"""Evaluation suites built from the README examples, with their files beside the suite.

Each case's snapshots, saved comparison, and fake transcript are written by
`tests/support/ask_examples.py` into a directory named after the case, and its expectations
are what that example's answer cites: its status, finding IDs, and analyzers.
"""

import json
from pathlib import Path
from typing import Any

from tests.support.ask_examples import EXAMPLES, ExampleRun, write_example, write_provider_config

SUITE = "suite.json"
_PATH_OPTIONS = {"--snapshot": "snapshot", "--compare": "comparison"}


def example_analyzers(name: str) -> list[str]:
    """The analyzer kinds the example's transcript runs."""
    return [args["kind"] for tool, args in EXAMPLES[name].calls if tool == "run_analyzer"]


def example_expectations(name: str, run: ExampleRun) -> dict[str, Any]:
    """What the example's answer must show: its status, cited findings, and analyzers."""
    expect: dict[str, Any] = {"outcomes": ["answered"], "statuses": [EXAMPLES[name].status]}
    if run.finding_ids:
        expect["finding_ids"] = list(run.finding_ids)
    if analyzers := example_analyzers(name):
        expect["analyzers"] = analyzers
    return expect


def _case(name: str, directory: Path) -> dict[str, Any]:
    (directory / name).mkdir()
    run = write_example(EXAMPLES[name], directory / name)
    question, *pairs = run.arguments
    case: dict[str, Any] = {"id": name, "question": question}
    for option, value in zip(pairs[::2], pairs[1::2], strict=True):
        relative = Path(value).relative_to(directory).as_posix()
        if option == "--comparison-result":
            case.setdefault("comparison_results", []).append(relative)
        elif option in _PATH_OPTIONS:
            case[_PATH_OPTIONS[option]] = relative
    case["fake_transcript"] = f"{name}/transcript.json"
    case["expect"] = example_expectations(name, run)
    return case


def write_readme_suite(directory: Path, names: list[str] | None = None) -> Path:
    """A suite of the named examples (all by default) and their files, in `directory`."""
    directory.mkdir(parents=True, exist_ok=True)
    cases = [_case(name, directory) for name in names or list(EXAMPLES)]
    path = directory / SUITE
    suite = {"schema_version": "1", "name": "readme", "cases": cases}
    path.write_text(json.dumps(suite, indent=2) + "\n", encoding="utf-8")
    return path


def write_fake_config(path: Path, **fields: Any) -> Path:
    """A fake provider configuration; each case's transcript scripts it."""
    return write_provider_config(path, {"kind": "fake", **fields})
