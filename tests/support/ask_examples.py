"""The README example questions as `ask` inputs: snapshots, fake configurations, transcripts.

Each example writes its files into a directory. Its transcript calls the tools a model would,
then submits a proposal built from what those tools return on a dry run over the same files,
as a model copies IDs from tool results, so the fixtures stay valid when unrelated data
changes. The proposal's status is derived from the returned findings, never stated, so an
example's expected status is a real check.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from archdiscov.assistant.questions import README_QUESTIONS as QUESTIONS
from archdiscov.assistant.tools.session import ToolSession
from archdiscov.models.enums import AnalyzerKind
from archdiscov.services import SupportedSnapshot, compare_snapshots, save_comparison
from archdiscov.services.snapshots import load_snapshot, save_snapshot, validate_snapshot
from tests.paths import FIXTURES_ROOT
from tests.support.answer_claims import dispatch_tool
from tests.support.answer_scenarios import OWNERSHIP_SNAPSHOT, tool_turn
from tests.support.audit_scenarios import SCENARIOS, with_coverage

INTERNET = QUESTIONS[AnalyzerKind.INTERNET_EXPOSURE]
DATABASE = QUESTIONS[AnalyzerKind.DATABASE_ACCESS]
SECURITY_GROUP = QUESTIONS[AnalyzerKind.SECURITY_GROUP_IMPACT]
TRUST = QUESTIONS[AnalyzerKind.CROSS_ACCOUNT_TRUST]
OWNERSHIP = QUESTIONS[AnalyzerKind.OWNERSHIP]
README_QUESTIONS = tuple(QUESTIONS.values())
CHANGED = "What changed in ownership between the two snapshots?"
SUMMARY = "The conclusions above are rendered from the cited records."
# The saved comparison's creation time, fixed so the written files are reproducible.
COMPARED_AT = datetime(2026, 1, 1, tzinfo=UTC)

type Call = tuple[str, dict[str, Any]]
# The turns of a transcript, from the example's tool-call turns and its honest proposal.
type Script = Callable[[list[dict], dict], list[dict]]


@dataclass(frozen=True)
class Example:
    question: str
    status: str
    snapshot: Callable[[], SupportedSnapshot]
    calls: tuple[Call, ...]
    insufficient_reason: str | None = None
    comparison: Callable[[], SupportedSnapshot] | None = None


@dataclass(frozen=True)
class ExampleRun:
    """The command arguments after `ask`, and the IDs the submitted proposal cites."""

    arguments: list[str]
    finding_ids: tuple[str, ...]
    difference_ids: dict[str, tuple[str, ...]] = field(default_factory=dict)


def _analysis(
    kind: AnalyzerKind, question: str, status: str, coverage: str | None = None, **fields: Any
) -> Example:
    """The analyzer's audit scenario, with every recorded operation at `coverage` if given."""
    scenario = SCENARIOS[kind]

    def snapshot() -> SupportedSnapshot:
        built = scenario.build()
        return built if coverage is None else with_coverage(built, coverage)

    calls = (
        ("search_resources", {"limit": 5}),
        ("list_analyzers", {}),
        ("run_analyzer", {"kind": kind.value, **scenario.parameters}),
    )
    return Example(question, status, snapshot, calls, **fields)


def _planned() -> SupportedSnapshot:
    return load_snapshot(FIXTURES_ROOT / "snapshots" / "terraform-plan.json")


_SAVED_COMPARISON = (("list_saved_results", {}), ("get_saved_comparison", {"number": 1}))
EXAMPLES = {
    "internet_exposure": _analysis(AnalyzerKind.INTERNET_EXPOSURE, INTERNET, "supported"),
    "database_access": _analysis(AnalyzerKind.DATABASE_ACCESS, DATABASE, "partial"),
    "security_group_impact": _analysis(
        AnalyzerKind.SECURITY_GROUP_IMPACT, SECURITY_GROUP, "partial"
    ),
    "cross_account_trust": _analysis(AnalyzerKind.CROSS_ACCOUNT_TRUST, TRUST, "supported"),
    "ownership": _analysis(AnalyzerKind.OWNERSHIP, OWNERSHIP, "supported"),
    "trust_limited_coverage": _analysis(
        AnalyzerKind.CROSS_ACCOUNT_TRUST, TRUST, "partial", coverage="failed"
    ),
    "internet_insufficient_evidence": _analysis(
        AnalyzerKind.INTERNET_EXPOSURE,
        INTERNET,
        "insufficient_evidence",
        coverage="skipped",
        insufficient_reason="limited_coverage",
    ),
    "ownership_planned": Example(
        OWNERSHIP,
        "insufficient_evidence",
        _planned,
        (("run_analyzer", {"kind": "ownership"}),),
        insufficient_reason="limited_coverage",
    ),
    "ownership_compared": Example(
        CHANGED,
        "partial",
        lambda: renamed_snapshots()[0],
        _SAVED_COMPARISON,
        comparison=lambda: renamed_snapshots()[1],
    ),
}


def write_provider_config(path: Path, fields: dict[str, Any]) -> Path:
    """A provider configuration file; JSON strings, numbers, booleans, and lists of
    strings are also TOML."""
    lines = (f"{key} = {json.dumps(value)}" for key, value in fields.items())
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def submit(proposal: dict, call_id: str = "answer") -> dict:
    return tool_turn("submit_answer", proposal, call_id)


def answered(calls: list[dict], proposal: dict) -> list[dict]:
    """The tool calls, then the honest proposal."""
    return [*calls, submit(proposal)]


def write_example(example: Example, directory: Path, script: Script = answered) -> ExampleRun:
    """Write the example's snapshots, saved comparison, transcript, and configuration.

    `script` turns the tool calls and the honest proposal into the transcript's turns.
    """
    snapshot_path = directory / "snapshot.json"
    snapshot = example.snapshot()
    save_snapshot(snapshot, snapshot_path)
    arguments = [example.question, "--snapshot", str(snapshot_path)]
    comparison_path, result_paths = None, []
    if example.comparison is not None:
        comparison = example.comparison()
        comparison_path = directory / "comparison.json"
        save_snapshot(comparison, comparison_path)
        result_paths = [directory / "comparison-result.json"]
        save_comparison(
            compare_snapshots(snapshot, comparison, now=COMPARED_AT),
            result_paths[0],
            left=snapshot,
            right=comparison,
        )
        arguments += ["--compare", str(comparison_path)]
        arguments += ["--comparison-result", str(result_paths[0])]
    session = ToolSession.from_paths(
        snapshot_path, comparison_path=comparison_path, comparison_result_paths=result_paths
    )
    outputs = [_dry_run(session, name, call) for name, call in example.calls]
    proposal = _proposal(example, session, outputs)
    calls = [
        tool_turn(name, call, f"c{number}") for number, (name, call) in enumerate(example.calls)
    ]
    transcript = {"turns": script(calls, proposal)}
    text = json.dumps(transcript, indent=2) + "\n"
    (directory / "transcript.json").write_text(text, encoding="utf-8")
    config_path = write_provider_config(
        directory / "provider.toml", {"kind": "fake", "transcript": "transcript.json"}
    )
    finding_ids = tuple(item["finding_id"] for item in _findings(outputs))
    return ExampleRun(
        [*arguments, "--config", str(config_path)], finding_ids, _differences(outputs)
    )


def _dry_run(session: ToolSession, name: str, arguments: dict) -> dict:
    output = dispatch_tool(session, name, **arguments)
    assert output["status"] == "ok", output
    return output


def _findings(outputs: list[dict]) -> list[dict]:
    return [item for output in outputs for item in output.get("findings", {}).get("items", [])]


def _differences(outputs: list[dict]) -> dict[str, tuple[str, ...]]:
    """Each side's resources in the matched entries that record differences."""
    matched = [
        item
        for output in outputs
        for item in output.get("matched", {}).get("items", [])
        if item["differences"]
    ]
    if not matched:
        return {}
    return {
        side: tuple(item[key]["resource_id"] for item in matched)
        for side, key in (("primary", "left"), ("comparison", "right"))
    }


def _status(findings: list[dict]) -> str:
    """The strongest status the findings allow; differences alone allow at most partial."""
    statuses = {item["status"] for item in findings}
    if statuses == {"supported"}:
        return "supported"
    if statuses == {"insufficient_evidence"}:
        return "insufficient_evidence"
    return "partial"


def _proposal(example: Example, session: ToolSession, outputs: list[dict]) -> dict:
    findings = _findings(outputs)
    differences = _differences(outputs)
    contexts = [{"side": "primary", "source_kind": session.index.source_kind.value}]
    if session.comparison_index is not None:
        contexts.append(
            {"side": "comparison", "source_kind": session.comparison_index.source_kind.value}
        )
    conclusions = []
    if findings:
        finding_ids = [item["finding_id"] for item in findings]
        conclusions.append(
            {"kind": "analyzer_finding", "citations": [{"finding_ids": finding_ids}]}
        )
    if differences:
        citations = [{"side": side, "resource_ids": list(ids)} for side, ids in differences.items()]
        conclusions.append({"kind": "recorded_difference", "citations": citations})
    proposal: dict[str, Any] = {
        "status": _status(findings),
        "contexts": contexts,
        "conclusions": conclusions,
        "limitations": [
            {"finding_id": item["finding_id"], "limitation": code}
            for item in findings
            for code in sorted({limitation["code"] for limitation in item["limitations"]})
        ],
        "summary": SUMMARY,
    }
    if example.insufficient_reason is not None:
        proposal["insufficient_reason"] = example.insufficient_reason
    return proposal


def renamed_snapshots() -> tuple:
    """The ownership snapshot, and a copy whose first resource has a new name."""
    left = load_snapshot(OWNERSHIP_SNAPSHOT)
    data = left.model_dump(mode="json")
    data["snapshot_id"] = "00000000-0000-4000-8000-0000000000cc"
    for evidence in data["evidence"].values():
        evidence["snapshot_id"] = data["snapshot_id"]
    next(iter(data["regions"].values()))["resources"][0]["name"] = "renamed"
    return left, validate_snapshot(data)
