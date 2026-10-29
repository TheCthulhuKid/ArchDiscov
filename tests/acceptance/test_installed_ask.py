"""The installed `ask` command, outside the checkout, with the fake provider and no network.

Four journeys, each on its own xdist worker. The first asks each README case of the
checked-in evaluation suite with its fake transcript, over the case's files as copied: every
README question is answered from the findings its tools returned, cited evidence is recorded
in the answered snapshot (one record is inspected through the CLI), and answers short of
`supported` state their limitations. The second asks the adversarial cases that submit a
fabricated or unreturned citation (rejected after the repair, exit 5), repair a fabricated
citation (answered after the repair), and answer over imported text that tries to instruct
the model (shown only as quoted data).

The third refuses a missing configuration, a Bedrock profile that cannot be loaded, and an
Anthropic configuration without its key before the run, and reports a scripted provider error
and a run limit as answer outcomes, each with its documented exit code. In the fourth, the
Anthropic adapter, the OpenAI-compatible adapter through each protocol, and the Bedrock adapter
answer a README question through a mock API on one loopback port, the only connection the run
may make, and give the fake's answer. The child sees no credentials and no checkout, except a
dummy key or AWS profile forwarded to an adapter's run alone: its whole `PYTHONPATH` is the
offline guard, which fails and records any other network use. Each command's JSON is checked
here; its readable form once, since `tests/cli` renders every outcome readably in process.
"""

import json
import re
import shutil
from collections.abc import Callable
from functools import partial
from pathlib import Path
from urllib.parse import quote, unquote

import pytest

from archdiscov.assistant.fake import FakeTranscript
from archdiscov.assistant.runs import RunTranscript
from archdiscov.services import load_snapshot
from tests.support.answer_claims import INJECTION
from tests.support.ask_cli_cases import PRE_RUN_ERRORS
from tests.support.ask_examples import (
    EXAMPLES,
    README_QUESTIONS,
    ExampleRun,
    write_example,
)
from tests.support.ask_runs import fake_config
from tests.support.evaluation_suites import ADVERSARIAL_SUITE, README_SUITE
from tests.support.installed_cli import (
    DUMMY_KEY,
    INSTALLED_KEY_VARIABLE,
    Workspace,
    case_selection,
    digest,
    harness_config,
    run_installed,
    suite_cases,
)
from tests.support.mock_api import mock_api_server
from tests.support.provider_data import MISSING_PROFILE
from tests.support.provider_harness import (
    HARNESS_PROFILE,
    AnthropicHarness,
    BedrockHarness,
    ChatCompletionsHarness,
    MockedHarness,
    ResponsesHarness,
)
from tests.support.scripted_answers import SEARCH
from tests.support.scripted_turns import calls_turn, fail_turn

# Each journey is module-scoped; its tests share one xdist worker, and the four run in parallel.
pytestmark = pytest.mark.xdist_group("installed-ask")
ANSWERS = pytest.mark.xdist_group("installed-ask-answers")
PATHS_GROUP = pytest.mark.xdist_group("installed-ask-paths")
ADAPTERS_GROUP = pytest.mark.xdist_group("installed-ask-adapters")

SUITES = {"readme": README_SUITE, "adversarial": ADVERSARIAL_SUITE}
# Adversarial cases the journey asks: (expected failure or status, exit code, rejection).
PATHS = {
    "fabricated_finding": ("rejected_after_repair", 5, "answer_unknown_id"),
    "unreturned_evidence": ("rejected_after_repair", 5, "answer_id_not_returned"),
    "repaired": ("answered_after_repair", 0, None),
    "injected_text": ("answered", 0, None),
}

# The examples whose files the failure and adapter cases reuse.
SNAPSHOT = "internet_exposure"
ADAPTED = "ownership"
ADAPTERS = [AnthropicHarness(), ChatCompletionsHarness(), ResponsesHarness(), BedrockHarness()]
BEARER = re.escape(f"Bearer {DUMMY_KEY}")
# The harness profile's key, which signs each Converse request with SigV4.
AWS_KEY = "AKIAHARNESSKEY000000"
AWS_SECRET = "harnessSecretKey000000000000000000000000"
SIGV4 = (
    rf"AWS4-HMAC-SHA256 Credential={AWS_KEY}/\d{{8}}/eu-west-1/bedrock/aws4_request, "
    r"SignedHeaders=[a-z0-9;-]+, Signature=[0-9a-f]{64}"
)
CONVERSE = f"/model/{quote(BedrockHarness().configuration('')['model'], safe='')}/converse"
# Each adapter's API root under the mock server, the path its requests go to, and the
# `Authorization` header they carry.
ROUTES = {
    "anthropic": ("", "/v1/messages", BEARER),
    "chat_completions": ("/v1", "/v1/chat/completions", BEARER),
    "responses": ("/v1", "/v1/responses", BEARER),
    "bedrock": ("", CONVERSE, SIGV4),
}


def _ask(ws: Workspace, suite: str, case: dict, expect: int = 0, readable: bool = False) -> dict:
    """Ask the case over its files; the JSON result, and the readable output if asked for."""
    config = ws.path("fixtures", suite, case["id"], "provider.toml")
    arguments = [case["question"], *case_selection(ws, suite, case), "--config", str(config)]
    transcript = ws.path("results", f"{suite}-{case['id']}-transcript.json")
    data = ws.json("ask", *arguments, "--transcript", transcript, expect=expect)
    RunTranscript.model_validate_json(transcript.read_bytes())
    shown = ws.run("ask", *arguments, expect=expect).stdout if readable else None
    return {"data": data, "readable": shown, "snapshot": arguments[2]}


def _findings(answer: dict) -> list[dict]:
    return [finding for conclusion in answer["conclusions"] for finding in conclusion["findings"]]


def _evidence_ids(answer: dict) -> list[str]:
    return [item for finding in _findings(answer) for item in finding["evidence_ids"]]


def _suite(root: Path, name: str) -> Workspace:
    """A workspace with the named evaluation suite copied to `fixtures/<name>`."""
    ws = Workspace(root)
    shutil.copytree(SUITES[name].parent, ws.path("fixtures", name))
    return ws


def _unchanged(ws: Workspace, ask: Callable[[], dict]) -> dict:
    """Run the journey; asking never changes a fixture, and nothing reaches the network."""
    fixtures = {path: digest(path) for path in ws.path("fixtures").rglob("*") if path.is_file()}
    summary = ask()
    assert {path: digest(path) for path in fixtures} == fixtures
    assert ws.guard.attempts == []
    return summary


@pytest.fixture(scope="module")
def answers(tmp_path_factory) -> dict:
    """Each README case asked in JSON, the first also readably; the first cited evidence
    record inspected through the CLI, and every cited ID checked in the answered snapshot."""
    ws = _suite(tmp_path_factory.mktemp("ask-answers"), "readme")

    def ask() -> dict:
        examples, inspected = {}, None
        for index, (case_id, case) in enumerate(suite_cases(ws, "readme").items()):
            asked = _ask(ws, "readme", case, readable=index == 0)
            cited = _evidence_ids(asked["data"]["result"]["answer"])
            recorded = load_snapshot(Path(asked["snapshot"])).evidence
            asked["recorded"] = all(item in recorded for item in cited)
            if inspected is None and cited:
                found = ws.json("evidence", cited[0], "--snapshot", asked["snapshot"])
                inspected = (cited[0], found)
            examples[case_id] = (case, asked)
        return {"examples": examples, "inspected": inspected}

    return _unchanged(ws, ask)


@pytest.fixture(scope="module")
def paths(tmp_path_factory) -> dict:
    ws = _suite(tmp_path_factory.mktemp("ask-paths"), "adversarial")
    adversarial = suite_cases(ws, "adversarial")
    return _unchanged(
        ws,
        lambda: {
            case_id: _ask(ws, "adversarial", adversarial[case_id], code)
            for case_id, (_, code, _) in PATHS.items()
        },
    )


def _missing_key(harness: MockedHarness, directory: Path) -> tuple[Path, list, dict]:
    return harness_config(directory, harness), [], {}


PRE_RUN = {case.__name__.strip("_"): (case, code, error) for case, code, error in PRE_RUN_ERRORS}
# A missing configuration, shown in both forms, and the refusals that depend on the child's
# environment, which holds no credentials; the ask service tests reach the others in process.
REFUSALS = {
    **{name: PRE_RUN[name] for name in ("missing_config", "missing_profile")},
    "missing_anthropic_key": (
        partial(_missing_key, AnthropicHarness()),
        2,
        "provider_credential_missing",
    ),
}
# Scripted runs that start and fail: (turns, extra configuration, exit code, failure reason).
FAILED_RUNS = {
    "provider-error": ([fail_turn("provider_unavailable")], "", 6, "provider_error"),
    "limit": ([calls_turn(SEARCH)], "max_requests = 1\n", 6, "limit_reached"),
}


def _refusals(ws: Workspace) -> dict:
    snapshot = ws.path("examples", SNAPSHOT, "snapshot.json")
    refused = {}
    for index, (name, (case, code, error)) in enumerate(REFUSALS.items()):
        directory = ws.path("cases", name)
        directory.mkdir(parents=True)
        config, extra, overrides = case(directory)
        before = {path: digest(path) for path in ws.root.rglob("*") if path.is_file()}
        selected = overrides.get("snapshot", snapshot)
        question = (README_QUESTIONS[0], "--snapshot", selected, "--config", config, *extra)
        diagnostic = ws.json("ask", *question, expect=code)["error"]
        assert diagnostic["code"] == error
        if index == 0:
            readable = ws.run("ask", *question, expect=code)
            assert readable.stdout == "" and readable.stderr.startswith(f"Error [{error}]: ")
        # A refused run neither writes nor overwrites anything.
        assert {path: digest(path) for path in ws.root.rglob("*") if path.is_file()} == before
        refused[name] = diagnostic
    return refused


def _failed_runs(ws: Workspace) -> dict:
    snapshot = ws.path("examples", SNAPSHOT, "snapshot.json")
    failed = {}
    for name, (turns, config, code, reason) in FAILED_RUNS.items():
        directory = ws.path("cases", name)
        directory.mkdir(parents=True)
        question = (README_QUESTIONS[0], "--snapshot", snapshot)
        options = (*question, "--config", fake_config(directory, turns, config))
        transcript = directory / "transcript.json"
        data = ws.json("ask", *options, "--transcript", transcript, expect=code)
        assert data["result"]["answer"]["failure"]["reason"] == reason
        failed[name] = (data, RunTranscript.model_validate_json(transcript.read_bytes()))
    return failed


def _credentials(directory: Path, harness: MockedHarness) -> dict[str, str]:
    """The variables that give only this adapter's run its credential: the dummy key, or for
    Bedrock the harness's AWS profile, copied into the workspace."""
    if harness.name != "bedrock":
        return {INSTALLED_KEY_VARIABLE: DUMMY_KEY}
    files = {}
    for variable, source in (
        ("AWS_CONFIG_FILE", HARNESS_PROFILE.config_file),
        ("AWS_SHARED_CREDENTIALS_FILE", HARNESS_PROFILE.credentials_file),
    ):
        copy = directory / f"aws-{source.name}"
        copy.write_bytes(source.read_bytes())
        files[variable] = str(copy)
    return files


def _through_the_adapters(ws: Workspace) -> dict[str, tuple[dict, list]]:
    """The adapted example answered through each adapter's mock API."""
    directory = ws.path("examples", ADAPTED)
    turns = json.loads((directory / "transcript.json").read_text(encoding="utf-8"))["turns"]
    arguments = [EXAMPLES[ADAPTED].question, "--snapshot", directory / "snapshot.json"]
    answered = {}
    for harness in ADAPTERS:
        bodies = [harness.body(turn.response) for turn in FakeTranscript(turns=turns).turns]
        with mock_api_server(bodies) as api:
            root, _, _ = ROUTES[harness.label]
            config = harness_config(directory, harness, f"http://127.0.0.1:{api.port}{root}")
            env = ws.guard.allowing(api.port, **_credentials(directory, harness))
            command = ["ask", *map(str, arguments), "--config", str(config)]
            data = json.loads(run_installed(ws.root, *command, "--json", env=env).stdout)
        assert api.bodies == []
        answered[harness.label] = (data, api.requests)
    return answered


def _example(root: Path, name: str) -> tuple[Workspace, ExampleRun]:
    """A workspace with the named README example written to `examples/<name>`."""
    ws = Workspace(root)
    directory = ws.path("examples", name)
    directory.mkdir(parents=True)
    return ws, write_example(EXAMPLES[name], directory)


@pytest.fixture(scope="module")
def summary(tmp_path_factory) -> dict:
    ws, _ = _example(tmp_path_factory.mktemp("ask-refusals"), SNAPSHOT)
    summary = {"refusals": _refusals(ws), "failed": _failed_runs(ws)}
    assert ws.guard.attempts == []
    return summary


@pytest.fixture(scope="module")
def adapters(tmp_path_factory) -> dict:
    ws, example = _example(tmp_path_factory.mktemp("ask-adapters"), ADAPTED)
    summary = {
        "fake": ws.json("ask", *example.arguments),
        "adapters": _through_the_adapters(ws),
    }
    assert ws.guard.attempts == []
    return summary


@ANSWERS
def test_every_readme_question_is_answered_from_tool_returned_findings(answers):
    examples = answers["examples"]
    assert set(README_QUESTIONS) <= {case["question"] for case, _ in examples.values()}
    limited = set()
    for case_id, (case, asked) in examples.items():
        answer = asked["data"]["result"]["answer"]
        expect = case["expect"]
        assert answer["outcome"]["status"] == "answered", case_id
        assert answer["status"] in expect["statuses"], case_id
        # The cited findings are exactly those the example's tools returned.
        citations = [item for c in answer["conclusions"] for item in c["citations"]]
        cited = {item for citation in citations for item in citation["finding_ids"]}
        assert cited == set(expect.get("finding_ids", ())), case_id
        assert answer["conclusions"], case_id
        assert asked["data"]["summary"]["provider_kind"] == "fake", case_id
        # A finding that supports anything cites evidence the answered snapshot records;
        # insufficient evidence may cite none.
        for finding in _findings(answer):
            if finding["status"] != "insufficient_evidence":
                assert finding["evidence_ids"], case_id
        assert asked["recorded"], case_id
        # An answer short of supported states its limitations.
        if answer["status"] != "supported":
            assert answer["qualifications"], case_id
            limited.add(case_id)
    assert {"trust_limited_coverage", "internet_insufficient_evidence", "ownership_planned"} <= (
        limited
    )
    evidence_id, inspected = answers["inspected"]
    assert inspected["evidence_id"] == evidence_id
    [(_, first)] = [item for item in examples.values() if item[1]["readable"] is not None]
    lines = first["readable"].splitlines()
    assert "Provider: fake, model fake" in lines
    assert "Answer: answered, exit code 0" in lines
    assert f"Status: {first['data']['result']['answer']['status']}" in lines


@PATHS_GROUP
def test_fabricated_and_unreturned_citations_are_rejected_and_repairs_answer(paths):
    for case_id, asked in paths.items():
        outcome_name, code, rejection = PATHS[case_id]
        result = asked["data"]["result"]
        outcome = result["answer"]["outcome"]
        assert result["exit_code"] == code, case_id
        assert (outcome["failure"] or outcome["status"]) == outcome_name, case_id
        if rejection is not None:
            assert rejection in {item["code"] for item in outcome["rejections"]}, case_id
            assert result["answer"]["conclusions"] == []
    assert paths["repaired"]["data"]["summary"]["usage"]["repair_requested"]


@PATHS_GROUP
def test_imported_instructions_are_only_quoted_data(paths):
    answer = paths["injected_text"]["data"]["result"]["answer"]
    quoted = [item["untrusted_text"] for c in answer["conclusions"] for item in c["data"]]
    statements = [item for c in answer["conclusions"] for item in c["statements"]]
    assert INJECTION in quoted
    assert not any(INJECTION in text for text in [*statements, *answer["qualifications"]])
    assert INJECTION not in json.dumps(answer["narrative"])


def test_errors_before_the_run_fail_safely(summary):
    refused = summary["refusals"]
    assert list(refused) == list(REFUSALS)
    # A Bedrock profile is named, never its contents, and fails before any request.
    assert refused["missing_profile"]["profile"] == MISSING_PROFILE
    assert refused["missing_profile"]["kind"] == "bedrock"
    assert refused["missing_config"]["path"].endswith("missing.toml")
    # A missing key names its variable, never a value, and stops the run before any request.
    assert refused["missing_anthropic_key"]["variable"] == INSTALLED_KEY_VARIABLE


def test_provider_errors_and_limits_are_answer_outcomes(summary):
    for name, (data, transcript) in summary["failed"].items():
        _, _, code, reason = FAILED_RUNS[name]
        assert data["result"]["exit_code"] == code
        assert transcript.outcome.kind == reason


@ADAPTERS_GROUP
@pytest.mark.parametrize("harness", ADAPTERS, ids=lambda harness: harness.label)
def test_each_adapter_answers_through_a_loopback_api(adapters, harness):
    data, requests = adapters["adapters"][harness.label]
    model = harness.configuration(INSTALLED_KEY_VARIABLE)["model"]
    assert (data["summary"]["provider_kind"], data["summary"]["model"]) == (harness.name, model)
    assert data["result"] == adapters["fake"]["result"]
    assert data["result"]["answer"]["status"] == EXAMPLES[ADAPTED].status
    _, path, authorization = ROUTES[harness.label]
    assert requests and all(sent == path for sent, _, _ in requests)
    for sent, headers, body in requests:
        lowered = {name.lower(): value for name, value in headers.items()}
        assert re.fullmatch(authorization, lowered["authorization"])
        # A request names its model in its body or, for Converse, in its path.
        assert body.get("model", unquote(sent.split("/")[2])) == model
    for secret in (DUMMY_KEY, AWS_SECRET):
        assert secret not in json.dumps(data)
