"""Phase 5 acceptance: an MCP client answers the README questions through the installed server.

fixture -> `archdiscov mcp` over stdio -> prompt -> tool calls -> `submit_answer` -> cited
evidence, through the installed script outside the checkout. The fixtures are the checked-in
README suite's case files, served as copied: each case's tool calls and proposal are the ones its
fake transcript makes for `ask`, so `ask` over the same files, run in process, is the
reference an accepted answer must equal. The adversarial suite's proposals are submitted too:
each unsupported claim is rejected with its code and a corrected proposal is accepted in the
same session. The server runs with no credentials and no checkout: its whole `PYTHONPATH` is
the offline guard, which fails and records any network use, and, for the boundary check,
every file the server opens in the workspace.
"""

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from archdiscov.assistant.mcp_server import PRESENT_ANSWER
from archdiscov.assistant.questions import README_QUESTIONS
from archdiscov.assistant.tools.catalog import default_registry
from archdiscov.cli import app
from tests.paths import FIXTURES_ROOT
from tests.support.answer_claims import INJECTION
from tests.support.evaluation_suites import ADVERSARIAL_SUITE, README_SUITE
from tests.support.installed_cli import (
    CLI,
    Workspace,
    case_selection,
    digest,
    suite_cases,
)
from tests.support.mcp_clients import StdioSession, submitted_proposals, transcript_calls

PROMPTS = {question: kind.value for kind, question in README_QUESTIONS.items()}
# The case that asks about two snapshots has no prompt: its question is not a README question.
UNPROMPTED = {"ownership_compared"}
# Each journey is module-scoped; its tests share one xdist worker, and the two run in parallel.
pytestmark = pytest.mark.xdist_group("phase-five")
REFUSALS = pytest.mark.xdist_group("phase-five-refusals")
runner = CliRunner()


def _serve(ws: Workspace, selection: list[str], env: dict[str, str] | None = None) -> StdioSession:
    """`archdiscov mcp` over the selection, as a client starts it; use it as a context."""
    command = [*CLI, "mcp", *selection]
    return StdioSession(command, ws.root, ws.guard.env if env is None else env)


def _replay(session: StdioSession, transcript: Path) -> list[tuple[bool, list[str]]]:
    """Make the transcript's tool calls, and return each call's error flag and text items."""
    return [session.tool(name, arguments) for name, arguments in transcript_calls(transcript)]


def _first_evidence(session: StdioSession, answer: dict) -> dict | None:
    """The first evidence ID the answer cites, retrieved through the `get_evidence` tool."""
    cited = [
        evidence_id
        for conclusion in answer["conclusions"]
        for finding in conclusion["findings"]
        for evidence_id in finding["evidence_ids"]
    ]
    if not cited:
        return None
    return {"id": cited[0], "reply": session.tool("get_evidence", {"evidence_id": cited[0]})}


def _ask(arguments: list[str]) -> dict:
    """`ask` over the same files, in process: the reference an accepted answer must equal.

    The installed `ask` command is exercised by `test_installed_ask.py`; here only its result
    matters, so it runs without a subprocess.
    """
    data, readable = (runner.invoke(app, ["ask", *arguments, *flag]) for flag in (["--json"], []))
    for result in (data, readable):
        assert result.exit_code == 0, result.output
    return {"data": json.loads(data.stdout), "readable": readable.stdout}


def _answered(ws: Workspace, case: dict) -> dict:
    """What `ask` and the server give for the case's files, and how the server ended."""
    selection = case_selection(ws, "readme", case)
    fixtures = ws.path("fixtures", "readme")
    config = fixtures / case["id"] / "provider.toml"
    arguments = [case["question"], *selection, "--config", str(config)]
    asked = _ask(arguments)
    transcript = fixtures / case["fake_transcript"]
    [proposal] = submitted_proposals(transcript)
    session = _serve(ws, selection)
    with session:
        tools = [tool["name"] for tool in session.request("tools/list")["tools"]]
        prompts = [prompt["name"] for prompt in session.request("prompts/list")["prompts"]]
        prompt = None
        if case["id"] not in UNPROMPTED:
            reply = session.request("prompts/get", {"name": PROMPTS[case["question"]]})
            [message] = reply["messages"]
            prompt = message["content"]["text"]
        assert not any(is_error for is_error, _ in _replay(session, transcript)), case["id"]
        is_error, served = session.tool("submit_answer", proposal)
        assert not is_error, (case["id"], served)
        evidence = _first_evidence(session, json.loads(served[2])["answer"])
    return {
        "case": case,
        "ask": asked,
        "tools": tools,
        "prompts": prompts,
        "prompt": prompt,
        "served": served,
        "evidence": evidence,
        "session": session,
    }


@pytest.fixture(scope="module")
def summary(tmp_path_factory) -> dict:
    ws = Workspace(tmp_path_factory.mktemp("phase-five"))
    shutil.copytree(README_SUITE.parent, ws.path("fixtures", "readme"))
    fixtures = {path: digest(path) for path in ws.path("fixtures").rglob("*") if path.is_file()}
    cases = {case_id: _answered(ws, case) for case_id, case in suite_cases(ws, "readme").items()}
    # Serving never changes a fixture, and nothing reaches the network.
    assert {path: digest(path) for path in fixtures} == fixtures
    return {"cases": cases, "network": ws.guard.attempts}


def test_every_readme_question_is_answered_from_its_prompt(summary):
    expected_tools = [*(tool.name for tool in default_registry().declarations()), "submit_answer"]
    asked = set()
    for case_id, run in summary["cases"].items():
        assert run["tools"] == expected_tools, case_id
        assert run["prompts"] == [kind.value for kind in README_QUESTIONS], case_id
        if case_id in UNPROMPTED:
            assert run["prompt"] is None and run["case"]["question"] not in PROMPTS
            continue
        assert run["prompt"] == run["case"]["question"], case_id
        asked.add(run["prompt"])
    assert asked == set(README_QUESTIONS.values())


def test_every_accepted_answer_equals_what_ask_gives(summary):
    for case_id, run in summary["cases"].items():
        present, readable, data = run["served"]
        assert present == PRESENT_ANSWER, case_id
        # `ask` prints its run summary, a blank line, and then the readable answer; the
        # answer JSON is `ask`'s result.
        _, ask_readable = run["ask"]["readable"].split("\n\n", 1)
        assert readable == ask_readable.rstrip("\n"), case_id
        assert json.loads(data) == run["ask"]["data"]["result"], case_id
        assert json.loads(data)["answer"]["outcome"]["status"] == "answered", case_id


def test_cited_evidence_is_retrieved_through_a_tool(summary):
    retrieved = [run["evidence"] for run in summary["cases"].values() if run["evidence"]]
    for evidence in retrieved:
        is_error, [text] = evidence["reply"]
        assert not is_error, evidence["id"]
        assert json.loads(text)["evidence"]["evidence_id"] == evidence["id"]
    assert len(retrieved) >= len(README_QUESTIONS)


def test_the_server_exits_cleanly_and_nothing_reaches_the_network(summary):
    for case_id, run in summary["cases"].items():
        session = run["session"]
        # Every standard-output line was a reply the client read.
        assert (session.exit_code, session.stderr, session.extra_output) == (0, "", ""), case_id
    assert summary["network"] == []


# ---- refusals, corrections, and the file boundary -----------------------------------

SUMMARY = "The conclusions above are rendered from the cited records."
FABRICATED = "finding-fabricated-0001"


def _cite_real_finding(proposal: dict, session: StdioSession, replies: list) -> dict:
    """Cite the first finding the session's `run_analyzer` call returned instead."""
    [text] = replies[-1][1]
    real = json.loads(text)["findings"]["items"][0]["finding_id"]
    return json.loads(json.dumps(proposal).replace(FABRICATED, real))


def _retrieve_cited_evidence(proposal: dict, session: StdioSession, replies: list) -> dict:
    """Correct a citation by calling the tools that return what it cites; the proposal stays."""
    citations = [c for conclusion in proposal["conclusions"] for c in conclusion["citations"]]
    for citation in citations:
        for evidence_id in citation.get("evidence_ids", ()):
            assert not session.tool("get_evidence", {"evidence_id": evidence_id})[0]
        for resource_id in citation.get("resource_ids", ()):
            assert not session.tool("inspect_resource", {"resource_id": resource_id})[0]
    return proposal


def _weaken(proposal: dict, session: StdioSession, replies: list) -> dict:
    return {**proposal, "status": "partial"}


def _plain_summary(proposal: dict, session: StdioSession, replies: list) -> dict:
    return {**proposal, "summary": SUMMARY}


# Each adversarial case the server must reject: the rejection code, how a client corrects the
# proposal, and whether the original proposal is still rejected after the correction.
REJECTED = {
    "fabricated_finding": ("answer_unknown_id", _cite_real_finding, True),
    "unreturned_evidence": ("answer_id_not_returned", _retrieve_cited_evidence, False),
    "status_upgrade": ("answer_status_upgrade", _weaken, True),
    "injected_narrative": ("answer_narrative_imported_text", _plain_summary, True),
}
Correction = Callable[[dict, StdioSession, list], dict]


def _adversarial(ws: Workspace, case: dict, correct: Correction | None = None) -> dict:
    """Make the case's tool calls and submit its proposal; with a correction, also submit the
    corrected proposal and then the original again, in the same session."""
    selection = case_selection(ws, "adversarial", case)
    transcript = ws.path("fixtures", "adversarial", case["fake_transcript"])
    proposal = submitted_proposals(transcript)[0]
    with _serve(ws, selection) as session:
        run = {"replies": _replay(session, transcript)}
        run["submitted"] = session.tool("submit_answer", proposal)
        if correct is not None:
            corrected = correct(proposal, session, run["replies"])
            run["accepted"] = session.tool("submit_answer", corrected)
            run["original"] = session.tool("submit_answer", proposal)
    return run


def _quoted_and_bare(value: Any, text: str, key: str | None = None) -> tuple[int, int]:
    """How many strings holding `text` are quoted imported text, and how many are not."""
    if isinstance(value, str):
        if text not in value:
            return 0, 0
        return (1, 0) if key == "untrusted_text" else (0, 1)
    items = (
        value.items()
        if isinstance(value, dict)
        else enumerate(value)
        if isinstance(value, list)
        else ()
    )
    counts = [_quoted_and_bare(item, text, name) for name, item in items]
    return sum(c[0] for c in counts), sum(c[1] for c in counts)


def _every_tool(session: StdioSession, decoys: list[Path]) -> set[str]:
    """Call every tool, then call tools with the decoys' paths in place of IDs; the names
    of the tools called."""
    called = set()

    def call(name: str, arguments: dict | None = None) -> dict:
        called.add(name)
        _, [text] = session.tool(name, arguments)
        return json.loads(text)

    resource_id = call("search_resources", {"limit": 5})["resources"]["items"][0]["resource_id"]
    inspected = call("inspect_resource", {"resource_id": resource_id})
    call("get_neighbours", {"resource_id": resource_id})
    call("find_paths", {"resource_id": resource_id})
    call("inspect_coverage")
    call("get_evidence", {"evidence_id": inspected["evidence"]["items"][0]["evidence_id"]})
    call("list_analyzers")
    call("run_analyzer", {"kind": "ownership"})
    call("list_saved_results")
    call("get_saved_analysis", {"number": 1})
    call("get_saved_comparison", {"number": 1})
    for decoy in decoys:
        # An ID is looked up in the selected snapshot, never opened as a path, and an argument
        # no tool declares is refused.
        assert call("inspect_resource", {"resource_id": str(decoy)})["status"] == "error"
        assert call("get_evidence", {"evidence_id": str(decoy)})["status"] == "error"
        assert call("search_resources", {"snapshot": str(decoy)})["status"] == "error"
        assert call("get_saved_comparison", {"path": str(decoy)})["status"] == "error"
    return called


def _boundary(ws: Workspace) -> dict:
    """A full session over one selection, with other snapshots and results beside it."""
    fixtures = ws.path("fixtures", "readme")
    cases = suite_cases(ws, "readme")
    # Everything else the workspace holds: the other cases' snapshots in `fixtures`, and copies
    # of a snapshot, a saved comparison, and a saved analysis under other names.
    decoys = [
        shutil.copy(source, ws.path(directory, "decoy.json"))
        for source, directory in (
            (fixtures / "internet_exposure" / "snapshot.json", "snapshots"),
            (fixtures / "ownership_compared" / "comparison-result.json", "comparisons"),
            (FIXTURES_ROOT / "analysis" / "ownership.json", "results"),
        )
    ]
    case = cases["ownership_compared"]
    selection = case_selection(ws, "readme", case)
    selected = sorted(Path(path).resolve() for path in selection[1::2])
    transcript = fixtures / case["fake_transcript"]
    [proposal] = submitted_proposals(transcript)
    watched = [ws.path(name) for name in ("fixtures", "snapshots", "results", "comparisons")]
    assert ws.guard.opened_files == []
    with _serve(ws, selection, ws.guard.watching(*watched)) as session:
        called = _every_tool(session, [Path(path) for path in decoys])
        _replay(session, transcript)
        accepted = session.tool("submit_answer", proposal)
    return {
        "selected": selected,
        "opened": ws.guard.opened_files,
        "decoys": [Path(path).resolve() for path in decoys],
        "called": called,
        "accepted": accepted,
        "session": session,
    }


@pytest.fixture(scope="module")
def refusals(tmp_path_factory) -> dict:
    ws = Workspace(tmp_path_factory.mktemp("phase-five-refusals"))
    shutil.copytree(README_SUITE.parent, ws.path("fixtures", "readme"))
    shutil.copytree(ADVERSARIAL_SUITE.parent, ws.path("fixtures", "adversarial"))
    adversarial = suite_cases(ws, "adversarial")
    summary = {
        "rejected": {
            case_id: _adversarial(ws, adversarial[case_id], correct)
            for case_id, (_, correct, _) in REJECTED.items()
        },
        "injected": _adversarial(ws, adversarial["injected_text"]),
        "boundary": _boundary(ws),
    }
    assert ws.guard.attempts == []
    return summary


@REFUSALS
def test_each_unsupported_claim_is_rejected_and_its_correction_accepted(refusals):
    for case_id, (code, _, still_rejected) in REJECTED.items():
        run = refusals["rejected"][case_id]
        is_error, [text] = run["submitted"]
        reply = json.loads(text)
        assert is_error and reply["error"] == "answer_rejected", case_id
        assert code in {item["code"] for item in reply["rejections"]}, case_id
        # Rejections state fixed messages; they never repeat what the client claimed.
        assert FABRICATED not in json.dumps(reply) and INJECTION not in json.dumps(reply)
        is_error, [present, _, data] = run["accepted"]
        assert not is_error and present == PRESENT_ANSWER, case_id
        assert json.loads(data)["answer"]["outcome"]["status"] == "answered", case_id
        # Each submission stands alone: an accepted answer does not excuse the rejected
        # proposal, unless the correction was to retrieve what it cites.
        assert run["original"][0] is still_rejected, case_id


@REFUSALS
def test_imported_instructions_are_only_quoted_data(refusals):
    injected = refusals["injected"]
    quoted = bare = 0
    for is_error, [text] in injected["replies"]:
        assert not is_error
        counts = _quoted_and_bare(json.loads(text), INJECTION)
        quoted, bare = quoted + counts[0], bare + counts[1]
    assert quoted and not bare
    is_error, [_, _, data] = injected["submitted"]
    assert not is_error
    answer = json.loads(data)["answer"]
    quoted, bare = _quoted_and_bare(answer, INJECTION)
    assert quoted and not bare
    assert INJECTION not in json.dumps(answer["narrative"])


@REFUSALS
def test_the_server_opens_only_the_files_it_was_given(refusals):
    boundary = refusals["boundary"]
    registry = {tool.name for tool in default_registry().declarations()}
    assert boundary["called"] == registry
    assert not boundary["accepted"][0]
    # The selection was read, and nothing else the workspace holds, decoys included.
    assert boundary["opened"] == boundary["selected"]
    assert not set(boundary["opened"]) & set(boundary["decoys"])
    session = boundary["session"]
    assert (session.exit_code, session.stderr, session.extra_output) == (0, "", "")
