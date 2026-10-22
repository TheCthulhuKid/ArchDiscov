"""The installed `ask` command, outside the checkout, with the fake provider and no network.

Both entry points answer the five README example questions in both output forms, and refuse
a missing configuration, an unknown snapshot, a real provider kind without an adapter, an
Anthropic configuration without its key, and an existing transcript path before the run, and
report a scripted provider error and a run limit as answer outcomes, each with its documented
exit code. The Anthropic adapter also answers a README question through a mock Messages API
on one loopback port, the only connection the run may make. The child sees no credentials
and no checkout: its whole `PYTHONPATH` is the offline guard, which fails and records any
other network use.
"""

import hashlib
import json
from pathlib import Path

import pytest

from archdiscov.assistant.fake import FakeTranscript
from archdiscov.assistant.runs import RunTranscript
from archdiscov.models.enums import AnalyzerKind
from tests.acceptance.test_installed_end_to_end import Workspace
from tests.assistant.provider_harness import messages_body
from tests.assistant.test_answer_service import SEARCH
from tests.assistant.test_orchestrator import _calls, _fail
from tests.assistant.test_providers import UNADAPTED_KIND, unadapted_config
from tests.services.test_ask import _fake
from tests.support.ask_examples import EXAMPLES, README_QUESTIONS, write_example
from tests.support.installed_cli import ENTRY_POINTS, run_installed
from tests.support.mock_api import mock_api_server

# Each entry point runs every case once per module: keep them on one xdist worker.
pytestmark = pytest.mark.xdist_group("installed-ask")

# One example per analyzer answers each README question over its audit scenario.
README_EXAMPLES = [kind.value for kind in AnalyzerKind]
# The example whose snapshot and configuration the failure cases reuse.
SNAPSHOT = "internet_exposure"
# The example the Anthropic adapter answers through the mock Messages API.
ADAPTED = "ownership"
KEY_VARIABLE = "ASK_INSTALLED_ANTHROPIC_KEY"
DUMMY_KEY = "sk-ant-api03-INSTALLEDdummy0123456789abcdefXYZ"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _missing_config(ws: Workspace, snapshot: Path) -> tuple[list, int, str]:
    options = ["--snapshot", snapshot, "--config", ws.path("missing.toml")]
    return options, 1, "provider_config_unreadable"


def _unknown_snapshot(ws: Workspace, snapshot: Path) -> tuple[list, int, str]:
    config = ws.path("examples", SNAPSHOT, "provider.toml")
    return ["--snapshot", ws.path("missing.json"), "--config", config], 1, "snapshot_io_error"


def _no_adapter(ws: Workspace, snapshot: Path) -> tuple[list, int, str]:
    directory = ws.path("cases", "no-adapter")
    directory.mkdir(parents=True)
    config = unadapted_config(directory)
    return ["--snapshot", snapshot, "--config", config], 2, "provider_adapter_unavailable"


def _existing_transcript(ws: Workspace, snapshot: Path) -> tuple[list, int, str]:
    config = ws.path("examples", SNAPSHOT, "provider.toml")
    existing = ws.path("kept.json")
    existing.write_text("kept", encoding="utf-8")
    options = ["--snapshot", snapshot, "--config", config, "--transcript", existing]
    return options, 3, "snapshot_conflict"


def _anthropic_config(directory: Path, endpoint: str | None = None) -> Path:
    path = directory / "anthropic.toml"
    lines = ['kind = "anthropic"', 'model = "claude-sonnet-5-5"', f'api_key_env = "{KEY_VARIABLE}"']
    if endpoint is not None:
        lines.append(f'endpoint = "{endpoint}"')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _missing_key(ws: Workspace, snapshot: Path) -> tuple[list, int, str]:
    directory = ws.path("cases", "missing-key")
    directory.mkdir(parents=True)
    options = ["--snapshot", snapshot, "--config", _anthropic_config(directory)]
    return options, 2, "provider_credential_missing"


REFUSALS = [_missing_config, _unknown_snapshot, _no_adapter, _missing_key, _existing_transcript]
# Scripted runs that start and fail: (turns, extra configuration, exit code, failure reason).
FAILED_RUNS = {
    "provider-error": ([_fail("provider_unavailable")], "", 6, "provider_error"),
    "limit": ([_calls(SEARCH)], "max_requests = 1\n", 6, "limit_reached"),
}


def _examples(ws: Workspace) -> dict:
    """Each README example answered in JSON and readably, keyed by name."""
    answers = {}
    for name in README_EXAMPLES:
        directory = ws.path("examples", name)
        directory.mkdir(parents=True)
        run = write_example(EXAMPLES[name], directory)
        data = ws.json("ask", *run.arguments)
        readable = ws.run("ask", *run.arguments).stdout
        answers[name] = (run, data, readable)
    return answers


def _refusals(ws: Workspace) -> dict:
    snapshot = ws.path("examples", SNAPSHOT, "snapshot.json")
    refused = {}
    for case in REFUSALS:
        options, code, error = case(ws, snapshot)
        before = {path: _digest(path) for path in ws.root.rglob("*") if path.is_file()}
        question = (README_QUESTIONS[0], *options)
        diagnostic = ws.json("ask", *question, expect=code)["error"]
        readable = ws.run("ask", *question, expect=code)
        assert diagnostic["code"] == error
        assert readable.stdout == "" and readable.stderr.startswith(f"Error [{error}]: ")
        # A refused run neither writes nor overwrites anything.
        assert {path: _digest(path) for path in ws.root.rglob("*") if path.is_file()} == before
        refused[case.__name__.strip("_")] = diagnostic
    return refused


def _failed_runs(ws: Workspace) -> dict:
    snapshot = ws.path("examples", SNAPSHOT, "snapshot.json")
    failed = {}
    for name, (turns, config, code, reason) in FAILED_RUNS.items():
        directory = ws.path("cases", name)
        directory.mkdir(parents=True)
        question = (README_QUESTIONS[0], "--snapshot", snapshot)
        options = (*question, "--config", _fake(directory, turns, config))
        transcript = directory / "transcript.json"
        data = ws.json("ask", *options, "--transcript", transcript, expect=code)
        readable = ws.run("ask", *options, expect=code).stdout
        assert data["result"]["answer"]["failure"]["reason"] == reason
        assert f"Answer: failed ({reason}), exit code {code}" in readable
        failed[name] = (data, RunTranscript.model_validate_json(transcript.read_bytes()))
    return failed


def _through_the_adapter(ws: Workspace) -> tuple[dict, str, list]:
    """The adapted example answered in JSON and readably through the mock Messages API."""
    directory = ws.path("examples", ADAPTED)
    turns = json.loads((directory / "transcript.json").read_text(encoding="utf-8"))["turns"]
    bodies = [messages_body(turn.response) for turn in FakeTranscript(turns=turns).turns]
    arguments = [EXAMPLES[ADAPTED].question, "--snapshot", directory / "snapshot.json"]
    with mock_api_server(bodies * 2) as api:
        config = _anthropic_config(directory, f"http://127.0.0.1:{api.port}")
        env = ws.guard.allowing(api.port, **{KEY_VARIABLE: DUMMY_KEY})
        command = ["ask", *map(str, arguments), "--config", str(config)]
        data = json.loads(
            run_installed(ws.entry_point, ws.root, *command, "--json", env=env).stdout
        )
        readable = run_installed(ws.entry_point, ws.root, *command, env=env).stdout
    assert api.bodies == []
    return data, readable, api.requests


def journey(entry_point: str, root: Path) -> dict:
    ws = Workspace(entry_point, root)
    summary = {
        "examples": _examples(ws),
        "refusals": _refusals(ws),
        "failed": _failed_runs(ws),
        "adapter": _through_the_adapter(ws),
    }
    assert ws.guard.attempts == []
    return summary


@pytest.fixture(scope="module")
def summaries(tmp_path_factory):
    return {
        entry_point: journey(entry_point, tmp_path_factory.mktemp(f"ask-{entry_point}"))
        for entry_point in ENTRY_POINTS
    }


@pytest.mark.parametrize("entry_point", ENTRY_POINTS)
def test_the_readme_questions_are_answered_offline(summaries, entry_point):
    examples = summaries[entry_point]["examples"]
    assert {run.arguments[0] for run, _, _ in examples.values()} == set(README_QUESTIONS)
    for name, (run, data, readable) in examples.items():
        answer = data["result"]["answer"]
        assert data["result"]["exit_code"] == 0
        assert answer["outcome"]["status"] == "answered"
        assert answer["status"] == EXAMPLES[name].status
        cited = [
            finding_id
            for conclusion in answer["conclusions"]
            for citation in conclusion["citations"]
            for finding_id in citation["finding_ids"]
        ]
        assert tuple(cited) == run.finding_ids and cited
        assert data["summary"]["provider_kind"] == "fake"
        assert "run_analyzer" in data["summary"]["usage"]["tool_calls"]
        lines = readable.splitlines()
        assert "Provider: fake, model fake" in lines
        assert "Answer: answered, exit code 0" in lines
        assert f"Status: {EXAMPLES[name].status}" in lines


@pytest.mark.parametrize("entry_point", ENTRY_POINTS)
def test_errors_before_the_run_fail_safely(summaries, entry_point):
    refused = summaries[entry_point]["refusals"]
    assert list(refused) == [case.__name__.strip("_") for case in REFUSALS]
    # The real kind is refused before its credential variable is looked up.
    assert refused["no_adapter"]["kind"] == UNADAPTED_KIND.value
    assert "variable" not in refused["no_adapter"]
    assert refused["missing_config"]["path"].endswith("missing.toml")
    # A missing key names its variable, never a value, and stops the run before any request.
    assert refused["missing_key"]["variable"] == KEY_VARIABLE


@pytest.mark.parametrize("entry_point", ENTRY_POINTS)
def test_provider_errors_and_limits_are_answer_outcomes(summaries, entry_point):
    for name, (data, transcript) in summaries[entry_point]["failed"].items():
        _, _, code, reason = FAILED_RUNS[name]
        assert data["result"]["exit_code"] == code
        assert transcript.outcome.kind == reason


def test_answers_do_not_depend_on_the_entry_point(summaries):
    script, module = (summaries[entry_point] for entry_point in ENTRY_POINTS)
    for name in README_EXAMPLES:
        assert script["examples"][name][1]["result"] == module["examples"][name][1]["result"]


@pytest.mark.parametrize("entry_point", ENTRY_POINTS)
def test_the_anthropic_adapter_answers_through_a_loopback_api(summaries, entry_point):
    data, readable, requests = summaries[entry_point]["adapter"]
    _, fake, _ = summaries[entry_point]["examples"][ADAPTED]
    assert data["summary"]["provider_kind"] == "anthropic"
    assert data["result"] == fake["result"]
    assert data["result"]["answer"]["status"] == EXAMPLES[ADAPTED].status
    assert "Provider: anthropic, model claude-sonnet-5-5" in readable.splitlines()
    assert f"Status: {EXAMPLES[ADAPTED].status}" in readable.splitlines()
    assert requests and all(path == "/v1/messages" for path, _, _ in requests)
    for _, headers, body in requests:
        lowered = {name.lower(): value for name, value in headers.items()}
        assert lowered["authorization"] == f"Bearer {DUMMY_KEY}"
        assert body["model"] == "claude-sonnet-5-5"
    assert DUMMY_KEY not in json.dumps(data) + readable
