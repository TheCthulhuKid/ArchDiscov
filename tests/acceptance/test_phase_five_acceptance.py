"""Phase 5 acceptance: an MCP client answers the README questions through the installed server.

fixture -> ingest -> `archdiscov mcp` over stdio -> prompt -> tool calls -> `submit_answer`
-> cited evidence, through each entry point outside the checkout, the two on separate
workers. The fixtures are the checked-in README suite's case files: each case's tool calls and
proposal are the ones its fake transcript makes for `ask`, so `ask` over the same files is
the reference an accepted answer must equal. The adversarial suite's proposals are submitted
too: each unsupported claim is rejected with its code and a corrected proposal is accepted in
the same session. The server and `ask` run with no credentials and no checkout: their whole
`PYTHONPATH` is the offline guard, which fails and records any network use, and, for the
boundary check, every file the server opens in the workspace.
"""

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from archdiscov.assistant.mcp_server import PRESENT_ANSWER
from archdiscov.assistant.questions import README_QUESTIONS
from archdiscov.assistant.tools.catalog import default_registry
from tests.paths import FIXTURES_ROOT
from tests.support.answer_claims import INJECTION
from tests.support.evaluation_suites import ADVERSARIAL_SUITE, README_SUITE
from tests.support.installed_cli import (
    ENTRY_POINTS,
    Workspace,
    cli_command,
    digest,
    ingest_selection,
    suite_cases,
)
from tests.support.mcp_clients import StdioSession, submitted_proposals, transcript_calls

PROMPTS = {question: kind.value for kind, question in README_QUESTIONS.items()}
# The case that asks about two snapshots has no prompt: its question is not a README question.
UNPROMPTED = {"ownership_compared"}


def _serve(ws: Workspace, selection: list[str], env: dict[str, str] | None = None) -> StdioSession:
    """`archdiscov mcp` over the selection, as a client starts it; use it as a context."""
    command = [*cli_command(ws.entry_point), "mcp", *selection]
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


def _answered(ws: Workspace, case: dict) -> dict:
    """What `ask` and the server give for the case's files, and how the server ended."""
    selection = ingest_selection(ws, "readme", case)
    fixtures = ws.path("fixtures", "readme")
    config = fixtures / case["id"] / "provider.toml"
    arguments = [case["question"], *selection, "--config", str(config)]
    asked = {"data": ws.json("ask", *arguments), "readable": ws.run("ask", *arguments).stdout}
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
        is_error, texts = session.tool("submit_answer", proposal)
        assert not is_error, (case["id"], texts)
        served = {"is_error": is_error, "texts": texts}
        evidence = _first_evidence(session, json.loads(texts[2])["answer"])
    return {
        "case": case,
        "ask": asked,
        "tools": tools,
        "prompts": prompts,
        "prompt": prompt,
        "served": served,
        "evidence": evidence,
        "exit_code": session.exit_code,
        "stderr": session.stderr,
        "extra_output": session.extra_output,
    }


def journey(entry_point: str, root: Path) -> dict:
    ws = Workspace(entry_point, root)
    shutil.copytree(README_SUITE.parent, ws.path("fixtures", "readme"))
    fixtures = {path: digest(path) for path in ws.path("fixtures").rglob("*") if path.is_file()}
    cases = {case_id: _answered(ws, case) for case_id, case in suite_cases(ws, "readme").items()}
    # Serving never changes a fixture, and nothing reaches the network.
    assert {path: digest(path) for path in fixtures} == fixtures
    return {"cases": cases, "network": ws.guard.attempts}


@pytest.fixture(
    scope="module",
    params=[
        pytest.param(entry_point, marks=pytest.mark.xdist_group(f"phase-five-{entry_point}"))
        for entry_point in ENTRY_POINTS
    ],
)
def summary(request, tmp_path_factory) -> dict:
    """One entry point's journey; each runs once, on its own xdist worker."""
    return journey(request.param, tmp_path_factory.mktemp(f"phase-five-{request.param}"))


def _answer(run: dict) -> dict:
    return json.loads(run["served"]["texts"][2])


def test_the_server_lists_the_tools_and_the_readme_prompts(summary):
    expected_tools = [*(tool.name for tool in default_registry().declarations()), "submit_answer"]
    for case_id, run in summary["cases"].items():
        assert run["tools"] == expected_tools, case_id
        assert run["prompts"] == [kind.value for kind in README_QUESTIONS], case_id


def test_every_readme_question_is_answered_from_its_prompt(summary):
    asked = set()
    for case_id, run in summary["cases"].items():
        case = run["case"]
        if case_id in UNPROMPTED:
            assert run["prompt"] is None and case["question"] not in PROMPTS
            continue
        assert run["prompt"] == case["question"], case_id
        asked.add(run["prompt"])
    assert asked == set(README_QUESTIONS.values())


def test_every_accepted_answer_equals_what_ask_gives(summary):
    for case_id, run in summary["cases"].items():
        served = run["served"]
        assert not served["is_error"], case_id
        present, readable, _ = served["texts"]
        assert present == PRESENT_ANSWER, case_id
        # `ask` prints its run summary, a blank line, and then the readable answer; the
        # answer JSON is `ask`'s result.
        _, ask_readable = run["ask"]["readable"].split("\n\n", 1)
        assert readable == ask_readable.rstrip("\n"), case_id
        assert _answer(run) == run["ask"]["data"]["result"], case_id
        answer = _answer(run)["answer"]
        assert answer["outcome"]["status"] == "answered", case_id
        assert answer["status"] in run["case"]["expect"]["statuses"], case_id


def test_answers_cite_only_the_findings_the_tools_returned(summary):
    for case_id, run in summary["cases"].items():
        answer = _answer(run)["answer"]
        cited = {
            item
            for conclusion in answer["conclusions"]
            for citation in conclusion["citations"]
            for item in citation["finding_ids"]
        }
        assert cited == set(run["case"]["expect"].get("finding_ids", ())), case_id
        assert answer["conclusions"], case_id


def test_cited_evidence_is_retrieved_through_a_tool(summary):
    retrieved = 0
    for case_id, run in summary["cases"].items():
        answer = _answer(run)["answer"]
        findings = [f for c in answer["conclusions"] for f in c["findings"]]
        # A finding that supports anything cites evidence; insufficient evidence may not.
        for finding in findings:
            if finding["status"] != "insufficient_evidence":
                assert finding["evidence_ids"], case_id
        evidence = run["evidence"]
        if evidence is None:
            continue
        is_error, [text] = evidence["reply"]
        assert not is_error, case_id
        assert json.loads(text)["evidence"]["evidence_id"] == evidence["id"], case_id
        retrieved += 1
    assert retrieved >= len(README_QUESTIONS)


def test_answers_short_of_supported_state_their_limitations(summary):
    limited = {}
    for case_id, run in summary["cases"].items():
        answer = _answer(run)["answer"]
        if answer["status"] != "supported":
            assert answer["qualifications"], case_id
            limited[case_id] = answer["qualifications"]
    assert {"trust_limited_coverage", "internet_insufficient_evidence", "ownership_planned"} <= (
        set(limited)
    )


def test_the_server_exits_cleanly_and_nothing_reaches_the_network(summary):
    for case_id, run in summary["cases"].items():
        assert run["exit_code"] == 0, case_id
        assert run["stderr"] == "", case_id
        # Every standard-output line was a reply the client read.
        assert run["extra_output"] == "", case_id
    assert summary["network"] == []


# ---- refusals, corrections, and the file boundary -----------------------------------

SUMMARY = "The conclusions above are rendered from the cited records."
FABRICATED = "finding-fabricated-0001"


def _real_finding(replies: list[tuple[bool, list[str]]]) -> str:
    """The first finding the session's `run_analyzer` call returned."""
    [text] = replies[-1][1]
    return json.loads(text)["findings"]["items"][0]["finding_id"]


def _cite_real_finding(proposal: dict, session: StdioSession, replies: list) -> dict:
    return json.loads(json.dumps(proposal).replace(FABRICATED, _real_finding(replies)))


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


def _refused(ws: Workspace, case: dict, correct: Correction) -> dict:
    """Submit the case's proposal, correct it in the same session, and submit it again."""
    selection = ingest_selection(ws, "adversarial", case)
    transcript = ws.path("fixtures", "adversarial", case["fake_transcript"])
    [proposal, *_] = submitted_proposals(transcript)
    with _serve(ws, selection) as session:
        replies = _replay(session, transcript)
        refused = session.tool("submit_answer", proposal)
        accepted = session.tool("submit_answer", correct(proposal, session, replies))
        original = session.tool("submit_answer", proposal)
    return {"refused": refused, "accepted": accepted, "original": original}


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


def _injected(ws: Workspace, case: dict) -> dict:
    """The injected-text case: the tool replies that hold the text, and the accepted answer."""
    selection = ingest_selection(ws, "adversarial", case)
    transcript = ws.path("fixtures", "adversarial", case["fake_transcript"])
    [proposal] = submitted_proposals(transcript)
    with _serve(ws, selection) as session:
        replies = _replay(session, transcript)
        accepted = session.tool("submit_answer", proposal)
    return {"replies": replies, "accepted": accepted}


def _pre_serve_failures(ws: Workspace) -> dict[str, Any]:
    fixtures = ws.path("fixtures", "readme")
    invalid = ws.path("snapshots", "invalid.json")
    invalid.write_text("{}", encoding="utf-8")
    snapshot = fixtures / "ownership" / "snapshot.json"
    cases = {
        "missing": (1, ["--snapshot", ws.path("snapshots", "missing.json")]),
        "invalid": (2, ["--snapshot", invalid]),
        "unselected": (
            2,
            [
                "--snapshot",
                snapshot,
                "--compare",
                fixtures / "internet_exposure" / "snapshot.json",
                "--comparison-result",
                fixtures / "ownership_compared" / "comparison-result.json",
            ],
        ),
    }
    return {name: ws.run("mcp", *options, expect=code) for name, (code, options) in cases.items()}


def _first_value(value: Any, key: str) -> Any:
    """The first value stored under `key` anywhere in the decoded JSON."""
    if isinstance(value, dict):
        if key in value:
            return value[key]
        value = list(value.values())
    if isinstance(value, list):
        for item in value:
            if (found := _first_value(item, key)) is not None:
                return found
    return None


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
    evidence_id = _first_value(inspected, "evidence_id")
    assert evidence_id is not None
    call("get_evidence", {"evidence_id": evidence_id})
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
    # Everything else the workspace holds: the other cases' ingested snapshots, and copies of
    # a snapshot, a saved comparison, and a saved analysis under other names.
    for other in ("ownership", "internet_exposure"):
        ingest_selection(ws, "readme", cases[other])
    decoys = [
        shutil.copy(source, ws.path(directory, "decoy.json"))
        for source, directory in (
            (fixtures / "internet_exposure" / "snapshot.json", "snapshots"),
            (fixtures / "ownership_compared" / "comparison-result.json", "comparisons"),
            (FIXTURES_ROOT / "analysis" / "ownership.json", "results"),
        )
    ]
    case = cases["ownership_compared"]
    selection = ingest_selection(ws, "readme", case)
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


def refusal_journey(entry_point: str, root: Path) -> dict:
    ws = Workspace(entry_point, root)
    shutil.copytree(README_SUITE.parent, ws.path("fixtures", "readme"))
    shutil.copytree(ADVERSARIAL_SUITE.parent, ws.path("fixtures", "adversarial"))
    adversarial = suite_cases(ws, "adversarial")
    summary = {
        "rejected": {
            case_id: _refused(ws, adversarial[case_id], correct)
            for case_id, (_, correct, _) in REJECTED.items()
        },
        "injected": _injected(ws, adversarial["injected_text"]),
        "pre_serve": _pre_serve_failures(ws),
        "boundary": _boundary(ws),
    }
    assert ws.guard.attempts == []
    return summary


@pytest.fixture(
    scope="module",
    params=[
        pytest.param(entry_point, marks=pytest.mark.xdist_group(f"phase-five-{entry_point}"))
        for entry_point in ENTRY_POINTS
    ],
)
def refusals(request, tmp_path_factory) -> dict:
    """One entry point's refusal journey; each runs once, on the entry point's worker."""
    root = tmp_path_factory.mktemp(f"phase-five-refusals-{request.param}")
    return refusal_journey(request.param, root)


def _reply(result: tuple[bool, list[str]]) -> dict:
    is_error, [text] = result
    assert is_error
    return json.loads(text)


def test_each_unsupported_claim_is_rejected_with_its_code(refusals):
    for case_id, (code, _, _) in REJECTED.items():
        reply = _reply(refusals["rejected"][case_id]["refused"])
        assert reply["error"] == "answer_rejected", case_id
        assert code in {item["code"] for item in reply["rejections"]}, case_id
        # Rejections state fixed messages; they never repeat what the client claimed.
        assert FABRICATED not in json.dumps(reply) and INJECTION not in json.dumps(reply)


def test_a_corrected_proposal_is_accepted_in_the_same_session(refusals):
    for case_id, (_, _, still_rejected) in REJECTED.items():
        run = refusals["rejected"][case_id]
        is_error, [present, _, data] = run["accepted"]
        assert not is_error and present == PRESENT_ANSWER, case_id
        assert json.loads(data)["answer"]["outcome"]["status"] == "answered", case_id
        # Each submission stands alone: an accepted answer does not excuse the rejected
        # proposal, unless the correction was to retrieve what it cites.
        assert run["original"][0] is still_rejected, case_id


def test_imported_instructions_are_only_quoted_data(refusals):
    injected = refusals["injected"]
    quoted = bare = 0
    for is_error, [text] in injected["replies"]:
        assert not is_error
        counts = _quoted_and_bare(json.loads(text), INJECTION)
        quoted, bare = quoted + counts[0], bare + counts[1]
    assert quoted and not bare
    is_error, [_, _, data] = injected["accepted"]
    assert not is_error
    answer = json.loads(data)["answer"]
    assert _quoted_and_bare(answer, INJECTION)[0] and not _quoted_and_bare(answer, INJECTION)[1]
    assert INJECTION not in json.dumps(answer["narrative"])


def test_pre_serve_failures_exit_with_a_diagnostic_and_serve_nothing(refusals):
    codes = {"missing": "snapshot_io_error", "invalid": "invalid_snapshot"}
    for name, result in refusals["pre_serve"].items():
        assert result.stdout == "", name
        expected = codes.get(name, "result_snapshot_unselected")
        assert result.stderr.startswith(f"Error [{expected}]: "), name


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
