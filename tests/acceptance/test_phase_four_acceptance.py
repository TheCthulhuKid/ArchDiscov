"""Phase 4 closes with one credential-free, network-free journey through the installed CLI.

fixture -> ingest -> `ask` for each README example -> validated answer -> evidence
inspection, plus the failure and repair paths, through the installed script outside the
checkout. The fixtures are the checked-in evaluation suites'
case files: the README cases answer each example question with a fake transcript, and the
adversarial cases submit a fabricated or unreturned citation (always rejected, exit 5),
repair a fabricated citation (answered after the repair), and answer over imported text
that tries to instruct the model (shown only as quoted data). The child sees no
credentials and no checkout: its whole `PYTHONPATH` is the offline guard, which fails and
records any network use.
"""

import json
import shutil
from pathlib import Path

import pytest

from archdiscov.assistant.runs import RunTranscript
from archdiscov.services import load_snapshot
from tests.support.answer_claims import INJECTION
from tests.support.ask_examples import README_QUESTIONS
from tests.support.evaluation_suites import ADVERSARIAL_SUITE, README_SUITE
from tests.support.installed_cli import (
    Workspace,
    digest,
    ingest_selection,
    suite_cases,
)

SUITES = {"readme": README_SUITE, "adversarial": ADVERSARIAL_SUITE}
# Adversarial cases the journey asks: (expected failure or status, exit code, rejection).
PATHS = {
    "fabricated_finding": ("rejected_after_repair", 5, "answer_unknown_id"),
    "unreturned_evidence": ("rejected_after_repair", 5, "answer_id_not_returned"),
    "repaired": ("answered_after_repair", 0, None),
    "injected_text": ("answered", 0, None),
}


# The journey is module-scoped, so its tests share one xdist worker.
pytestmark = pytest.mark.xdist_group("phase-four")


def _ingest(ws: Workspace, suite: str, case: dict) -> list[str]:
    """Ingest the case's snapshots and return the `ask` arguments over the ingested files."""
    selection = ingest_selection(ws, suite, case)
    config = ws.path("fixtures", suite, case["id"], "provider.toml")
    return [case["question"], *selection, "--config", str(config)]


def _ask(ws: Workspace, suite: str, case: dict, expect: int = 0) -> dict:
    arguments = _ingest(ws, suite, case)
    transcript = ws.path("results", f"{suite}-{case['id']}-transcript.json")
    data = ws.json("ask", *arguments, "--transcript", transcript, expect=expect)
    readable = ws.run("ask", *arguments, expect=expect).stdout
    RunTranscript.model_validate_json(transcript.read_bytes())
    return {"data": data, "readable": readable, "snapshot": arguments[2]}


def _findings(answer: dict) -> list[dict]:
    return [finding for conclusion in answer["conclusions"] for finding in conclusion["findings"]]


def _inspected(ws: Workspace, answer: dict, snapshot: str) -> dict:
    """The first cited evidence record, inspected through the CLI in the answered snapshot,
    and whether every cited evidence ID is recorded there (checked in process, since one
    command per ID would dominate the journey)."""
    evidence_ids = [item for finding in _findings(answer) for item in finding["evidence_ids"]]
    recorded = load_snapshot(Path(snapshot)).evidence
    inspected = (
        ws.json("evidence", evidence_ids[0], "--snapshot", snapshot) if evidence_ids else None
    )
    return {
        "first": evidence_ids[0] if evidence_ids else None,
        "inspected": inspected,
        "recorded": all(item in recorded for item in evidence_ids),
    }


def journey(root: Path) -> dict:
    ws = Workspace(root)
    for name, suite in SUITES.items():
        shutil.copytree(suite.parent, ws.path("fixtures", name))
    fixtures = {path: digest(path) for path in ws.path("fixtures").rglob("*") if path.is_file()}
    summary: dict = {"examples": {}, "paths": {}}
    for case_id, case in suite_cases(ws, "readme").items():
        asked = _ask(ws, "readme", case)
        answer = asked["data"]["result"]["answer"]
        asked["evidence"] = _inspected(ws, answer, asked["snapshot"])
        summary["examples"][case_id] = (case, asked)
    adversarial = suite_cases(ws, "adversarial")
    for case_id, (_, code, _) in PATHS.items():
        summary["paths"][case_id] = _ask(ws, "adversarial", adversarial[case_id], code)
    # Asking never changes a fixture, and nothing reaches the network.
    assert {path: digest(path) for path in fixtures} == fixtures
    assert ws.guard.attempts == []
    return summary


@pytest.fixture(scope="module")
def summary(tmp_path_factory) -> dict:
    return journey(tmp_path_factory.mktemp("phase-four"))


def _cited(answer: dict, key: str) -> set[str]:
    return {
        item
        for conclusion in answer["conclusions"]
        for citation in conclusion["citations"]
        for item in citation[key]
    }


def test_every_readme_question_is_answered_from_tool_returned_findings(summary):
    examples = summary["examples"]
    assert set(README_QUESTIONS) <= {case["question"] for case, _ in examples.values()}
    for case_id, (case, asked) in examples.items():
        answer = asked["data"]["result"]["answer"]
        expect = case["expect"]
        assert answer["outcome"]["status"] == "answered", case_id
        assert answer["status"] in expect["statuses"], case_id
        # The cited findings are exactly those the example's tools returned.
        assert _cited(answer, "finding_ids") == set(expect.get("finding_ids", ())), case_id
        assert answer["conclusions"], case_id
        lines = asked["readable"].splitlines()
        assert "Answer: answered, exit code 0" in lines
        assert f"Status: {answer['status']}" in lines


def test_cited_evidence_is_inspected_in_the_answered_snapshot(summary):
    inspected = 0
    for case_id, (_, asked) in summary["examples"].items():
        evidence = asked["evidence"]
        answer = asked["data"]["result"]["answer"]
        # A finding that supports anything cites evidence; insufficient evidence may not.
        for finding in _findings(answer):
            if finding["status"] != "insufficient_evidence":
                assert finding["evidence_ids"], case_id
        assert evidence["recorded"], case_id
        if evidence["first"] is not None:
            assert evidence["inspected"]["evidence_id"] == evidence["first"], case_id
            inspected += 1
    assert inspected >= len(README_QUESTIONS)


def test_answers_short_of_supported_state_their_limitations(summary):
    limited = {}
    for case_id, (_, asked) in summary["examples"].items():
        answer = asked["data"]["result"]["answer"]
        if answer["status"] != "supported":
            assert answer["qualifications"], case_id
            limited[case_id] = answer["qualifications"]
    assert {"trust_limited_coverage", "internet_insufficient_evidence", "ownership_planned"} <= (
        set(limited)
    )


def test_fabricated_and_unreturned_citations_are_rejected_and_repairs_answer(summary):
    for case_id, asked in summary["paths"].items():
        outcome_name, code, rejection = PATHS[case_id]
        result = asked["data"]["result"]
        outcome = result["answer"]["outcome"]
        assert result["exit_code"] == code, case_id
        assert (outcome["failure"] or outcome["status"]) == outcome_name, case_id
        if rejection is not None:
            assert rejection in {item["code"] for item in outcome["rejections"]}, case_id
            assert result["answer"]["conclusions"] == []
            assert f"Answer: failed ({outcome_name}), exit code {code}" in asked["readable"]
    repaired = summary["paths"]["repaired"]["data"]
    assert repaired["summary"]["usage"]["repair_requested"]


def test_imported_instructions_are_only_quoted_data(summary):
    asked = summary["paths"]["injected_text"]
    answer = asked["data"]["result"]["answer"]
    quoted = [item["untrusted_text"] for c in answer["conclusions"] for item in c["data"]]
    statements = [item for c in answer["conclusions"] for item in c["statements"]]
    assert INJECTION in quoted
    assert not any(INJECTION in text for text in [*statements, *answer["qualifications"]])
    assert INJECTION not in json.dumps(answer["narrative"])
