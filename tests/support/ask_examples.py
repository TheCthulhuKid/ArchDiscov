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
from pathlib import Path
from typing import Any

from archdiscov.assistant.conversation import ParsedToolArguments, ToolCall
from archdiscov.assistant.tools.catalog import default_registry
from archdiscov.assistant.tools.session import ToolSession
from archdiscov.models.enums import AnalyzerKind
from archdiscov.services import SupportedSnapshot, compare_snapshots, save_comparison
from archdiscov.services.snapshots import load_snapshot, save_snapshot
from tests.assistant.test_answer_rendering import _renamed
from tests.assistant.test_answer_suite import _tool
from tests.paths import FIXTURES_ROOT
from tests.support.audit_scenarios import SCENARIOS, with_coverage

INTERNET = "What receives traffic from the public internet?"
DATABASE = "Which services can access this database?"
SECURITY_GROUP = "What would be affected if this security group changed?"
TRUST = "Where are the cross-account trust relationships?"
OWNERSHIP = "Which resources appear to have no clear owner?"
README_QUESTIONS = (INTERNET, DATABASE, SECURITY_GROUP, TRUST, OWNERSHIP)
CHANGED = "What changed in ownership between the two snapshots?"

SUMMARY = "The conclusions above are rendered from the cited records."
_REGISTRY = default_registry()

type Call = tuple[str, dict[str, Any]]


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
        lambda: _renamed()[0],
        _SAVED_COMPARISON,
        comparison=lambda: _renamed()[1],
    ),
}


def write_example(example: Example, directory: Path) -> ExampleRun:
    """Write the example's snapshots, saved comparison, transcript, and configuration."""
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
            compare_snapshots(snapshot, comparison),
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
    turns = [_tool(name, call, f"c{number}") for number, (name, call) in enumerate(example.calls)]
    turns.append(_tool("submit_answer", proposal, "answer"))
    (directory / "transcript.json").write_text(json.dumps({"turns": turns}), encoding="utf-8")
    config_path = directory / "provider.toml"
    config_path.write_text('kind = "fake"\ntranscript = "transcript.json"\n', encoding="utf-8")
    finding_ids = tuple(item["finding_id"] for item in _findings(outputs))
    return ExampleRun(
        [*arguments, "--config", str(config_path)], finding_ids, _differences(outputs)
    )


def _dry_run(session: ToolSession, name: str, arguments: dict) -> dict:
    call = ToolCall(id="dry", name=name, arguments=ParsedToolArguments(value=arguments))
    output = json.loads(_REGISTRY.dispatch(session, call).content)
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
