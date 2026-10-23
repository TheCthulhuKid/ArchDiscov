"""The installed `evaluate` and `evaluation` commands, outside the checkout and offline.

Both entry points evaluate the checked-in README and adversarial suites, copied out of the
checkout, with the fake provider in both output forms; every case passes, and the reports
equal each other apart from timing. The README suite is also evaluated through the
Anthropic adapter against a mock API on one loopback port, opted in with `--allow-external`,
to the fake's records. An existing output path and a non-fake configuration without the
opt-in are refused before any request, and two reports are compared and the comparison
saved. The child sees no credentials, except the dummy key forwarded to the adapter's run
alone, and its offline guard fails and records any other network use.
"""

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from archdiscov.assistant.evaluation import (
    SAFETY_CHECKS,
    EvaluationReport,
    ReportComparison,
    parse_report,
)
from archdiscov.assistant.fake import FakeTranscript
from tests.acceptance.test_installed_ask import DUMMY_KEY, KEY_VARIABLE, _adapter_config
from tests.acceptance.test_installed_end_to_end import Workspace
from tests.assistant.provider_harness import AnthropicHarness
from tests.support.evaluation_suites import (
    ADVERSARIAL_SUITE,
    README_SUITE,
    SUITE,
    write_fake_config,
)
from tests.support.installed_cli import ENTRY_POINTS, run_installed
from tests.support.mock_api import mock_api_server

# Each entry point runs every suite once per module: keep them on one xdist worker.
pytestmark = pytest.mark.xdist_group("installed-evaluation")

SUITES = {"readme": README_SUITE, "adversarial": ADVERSARIAL_SUITE}
HARNESS = AnthropicHarness()


def _digests(root: Path) -> dict[Path, str]:
    return {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def _report(path: Path) -> EvaluationReport:
    return parse_report(json.loads(path.read_text(encoding="utf-8")))


def _evaluated(ws: Workspace) -> dict[str, tuple[dict, str, str]]:
    """Each suite evaluated with the fake: the JSON output, its budget statement, and the
    readable output of a second run."""
    config = write_fake_config(ws.path("fake.toml"))
    evaluated = {}
    for name, source in SUITES.items():
        shutil.copytree(source.parent, ws.path("suites", name))
        suite = ws.path("suites", name, SUITE)
        output = ws.path("results", f"{name}.json")
        run = ws.run("evaluate", suite, "--config", config, "--output", output, "--json")
        readable = ws.run(
            "evaluate", suite, "--config", config, "--output", ws.path("results", f"{name}-2.json")
        )
        evaluated[name] = (json.loads(run.stdout), run.stderr, readable.stdout)
    return evaluated


def _through_the_adapter(ws: Workspace) -> tuple[dict, list]:
    """The README suite evaluated through the Anthropic adapter's mock API."""
    suite = ws.path("suites", "readme", SUITE)
    data = json.loads(suite.read_text(encoding="utf-8"))
    bodies = [
        HARNESS.body(turn.response)
        for case in data["cases"]
        for turn in FakeTranscript.model_validate_json(
            (suite.parent / case["fake_transcript"]).read_bytes()
        ).turns
    ]
    with mock_api_server(bodies) as api:
        config = _adapter_config(ws.root, HARNESS, f"http://127.0.0.1:{api.port}")
        output = ws.path("results", "readme-anthropic.json")
        env = ws.guard.allowing(api.port, **{KEY_VARIABLE: DUMMY_KEY})
        command = ["evaluate", suite, "--config", config, "--output", output, "--allow-external"]
        result = run_installed(ws.entry_point, ws.root, *map(str, command), "--json", env=env)
    assert api.bodies == []
    return json.loads(result.stdout), api.requests


def _refusals(ws: Workspace) -> dict[str, dict]:
    suite = ws.path("suites", "readme", SUITE)
    existing = ws.path("results", "readme.json")
    external = _adapter_config(ws.path("suites"), HARNESS)
    cases = {
        "existing_output": (["--config", ws.path("fake.toml"), "--output", existing], 3),
        "external": (["--config", external, "--output", ws.path("results", "refused.json")], 2),
    }
    refused = {}
    for name, (options, code) in cases.items():
        before = _digests(ws.root)
        result = ws.run("evaluate", suite, *options, "--json", expect=code)
        assert result.stderr == ""
        assert _digests(ws.root) == before
        refused[name] = json.loads(result.stdout)["error"]
    return refused


def _compared(ws: Workspace) -> tuple[dict, str]:
    first, second = ws.path("results", "readme.json"), ws.path("results", "readme-2.json")
    output = ws.path("comparisons", "readme.json")
    data = ws.json("evaluation", first, "--compare", second, "--output", output)
    readable = ws.run("evaluation", first, "--compare", second).stdout
    assert (
        ReportComparison.model_validate_json(output.read_bytes()).model_dump(mode="json")
        == data["comparison"]
    )
    return data, readable


def journey(entry_point: str, root: Path) -> dict:
    ws = Workspace(entry_point, root)
    summary = {
        "evaluated": _evaluated(ws),
        "adapter": _through_the_adapter(ws),
        "refusals": _refusals(ws),
        "compared": _compared(ws),
        "shown": ws.run("evaluation", ws.path("results", "adversarial.json")).stdout,
        "reports": {name: _report(ws.path("results", f"{name}.json")) for name in SUITES},
    }
    assert ws.guard.attempts == []
    return summary


@pytest.fixture(scope="module")
def summaries(tmp_path_factory):
    return {
        entry_point: journey(entry_point, tmp_path_factory.mktemp(f"evaluation-{entry_point}"))
        for entry_point in ENTRY_POINTS
    }


@pytest.mark.parametrize("entry_point", ENTRY_POINTS)
def test_both_suites_pass_with_the_fake_provider(summaries, entry_point):
    for name, (data, budget, readable) in summaries[entry_point]["evaluated"].items():
        assert json.loads(budget) == {"budgets": data["budgets"]}
        report = parse_report(data["report"])
        assert report == summaries[entry_point]["reports"][name]
        [run] = report.providers
        assert run.totals.failed == 0 and run.totals.passed == len(report.suite.case_ids)
        for record in run.records:
            assert {check.name for check in record.checks if check.passed} >= set(SAFETY_CHECKS)
        lines = readable.splitlines()
        assert lines[0].startswith("Saved report: ")
        assert f"Suite: {name}, {len(report.suite.case_ids)} cases" in lines[1]


def _without_timing(report: EvaluationReport) -> dict:
    data = report.model_dump(mode="json", exclude={"created_at"})
    for run in data["providers"]:
        run["totals"].pop("latency_seconds")
        for record in run["records"]:
            record.pop("latency_seconds")
    return data


def test_reports_do_not_depend_on_the_entry_point(summaries):
    script, module = (summaries[entry_point]["reports"] for entry_point in ENTRY_POINTS)
    for name in SUITES:
        assert _without_timing(script[name]) == _without_timing(module[name])


@pytest.mark.parametrize("entry_point", ENTRY_POINTS)
def test_the_adapter_reaches_the_fakes_records_through_a_loopback_api(summaries, entry_point):
    data, requests = summaries[entry_point]["adapter"]
    [run] = parse_report(data["report"]).providers
    [fake] = summaries[entry_point]["reports"]["readme"].providers
    assert data["budgets"][0]["provider_kind"] == run.provider_kind == "anthropic"
    assert [record.id for record in run.records] == [record.id for record in fake.records]
    assert len(requests) == run.totals.provider_requests
    assert all(path == "/v1/messages" for path, _, _ in requests)
    assert DUMMY_KEY not in json.dumps(data)


@pytest.mark.parametrize("entry_point", ENTRY_POINTS)
def test_unsafe_or_unannounced_runs_are_refused(summaries, entry_point):
    refused = summaries[entry_point]["refusals"]
    assert refused["existing_output"]["code"] == "snapshot_conflict"
    assert refused["existing_output"]["path"].endswith("readme.json")
    assert refused["external"]["code"] == "evaluation_external_not_allowed"
    assert refused["external"]["path"].endswith("anthropic.toml")


@pytest.mark.parametrize("entry_point", ENTRY_POINTS)
def test_two_reports_are_compared(summaries, entry_point):
    data, readable = summaries[entry_point]["compared"]
    comparison = ReportComparison.model_validate(data["comparison"])
    assert comparison.same_suite
    assert (comparison.providers_added, comparison.providers_removed) == ((), ())
    [run] = comparison.providers
    assert run.cases and not any(case.changed for case in run.cases)
    assert "Same suite." in readable.splitlines()
    assert "0 of 9 shared cases changed" in readable
    assert (
        "Provider 1: fake, model fake: 9 passed, 0 failed of 9 cases"
        in (summaries[entry_point]["shown"])
    )
