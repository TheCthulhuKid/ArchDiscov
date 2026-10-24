"""Scripted runs that submit a proposal, and one rejected run per `RejectionCode`."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from archdiscov.assistant.fake import FakeProvider, FakeTranscript
from archdiscov.assistant.orchestrator import RunResult, run_assistant
from archdiscov.assistant.rejections import RejectionCode
from archdiscov.assistant.runs import Proposed
from archdiscov.assistant.tools.contracts import ToolLimits
from archdiscov.assistant.tools.session import ToolSession
from archdiscov.graph import GraphIndex
from archdiscov.models.enums import AnalyzerKind, SnapshotSourceKind
from archdiscov.services.analyses import save_analysis
from archdiscov.services.analysis_execution import run_analyzer
from archdiscov.services.snapshots import load_snapshot, validate_snapshot
from tests.paths import FIXTURES_ROOT
from tests.support.answer_claims import INJECTION, claimable_status, service_result
from tests.support.audit_scenarios import SCENARIOS, with_coverage
from tests.support.secrets import SECRET

SNAPSHOTS = FIXTURES_ROOT / "snapshots"
OWNERSHIP_SNAPSHOT = FIXTURES_ROOT / "analysis" / "ownership-snapshot.json"


def tool_turn(name: str, arguments: dict, call_id: str) -> dict:
    return {
        "kind": "respond",
        "response": {
            "message": {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": call_id,
                        "name": name,
                        "arguments": {"kind": "parsed", "value": arguments},
                    }
                ],
            },
            "stop_reason": "tool_use",
        },
    }


def proposed_run(
    session: ToolSession,
    calls: list[tuple[str, dict]],
    proposal: dict,
) -> RunResult:
    turns = [
        tool_turn(name, arguments, f"c{number}") for number, (name, arguments) in enumerate(calls)
    ]
    turns.append(tool_turn("submit_answer", proposal, "answer"))
    provider = FakeProvider(FakeTranscript.model_validate({"turns": turns}))
    result = run_assistant(
        f"Question with a token {SECRET}?",
        provider=provider,
        session=session,
        provider_kind="fake",
        model="fake",
    )
    assert isinstance(result.outcome, Proposed), result.outcome
    assert SECRET not in result.transcript.model_dump_json()
    return result


def finding_proposal(session: ToolSession, findings: list, *, status: str, **fields: Any) -> dict:
    data: dict[str, Any] = {
        "status": status,
        "contexts": [{"side": "primary", "source_kind": session.index.source_kind.value}],
        "conclusions": [
            {
                "kind": "analyzer_finding",
                "citations": [{"finding_ids": [finding.id for finding in findings]}],
            }
        ]
        if findings
        else [],
        "limitations": [
            {"finding_id": finding.id, "limitation": code}
            for finding in findings
            for code in sorted({limitation.code for limitation in finding.limitations})
        ],
        "summary": f"Explanation that mentions a token {SECRET}.",
    }
    if status == "insufficient_evidence":
        data["insufficient_reason"] = "no_applicable_findings"
    data.update(fields)
    return data


def proposal_status(findings: list) -> str:
    return claimable_status({finding.status.value for finding in findings})


def _ownership(coverage: str | None = None) -> tuple[ToolSession, list]:
    scenario = SCENARIOS[AnalyzerKind.OWNERSHIP]
    snapshot = with_coverage(scenario.build(), coverage) if coverage else scenario.build()
    return ToolSession(snapshot), service_result(snapshot, AnalyzerKind.OWNERSHIP, {}, 3).findings


RUN_OWNERSHIP = [("run_analyzer", {"kind": "ownership"})]


def _side_unavailable(tmp_path: Path) -> tuple:
    session, findings = _ownership()
    contexts = [
        {"side": "primary", "source_kind": session.index.source_kind.value},
        {"side": "comparison", "source_kind": "aws"},
    ]
    return (
        session,
        RUN_OWNERSHIP,
        finding_proposal(session, findings, status=proposal_status(findings), contexts=contexts),
    )


def _source_kind_mismatch(tmp_path: Path) -> tuple:
    session, findings = _ownership()
    contexts = [{"side": "primary", "source_kind": "terraform_plan"}]
    return (
        session,
        RUN_OWNERSHIP,
        finding_proposal(session, findings, status=proposal_status(findings), contexts=contexts),
    )


def _unknown_id(tmp_path: Path) -> tuple:
    session, _ = _ownership()
    conclusions = [
        {"kind": "analyzer_finding", "citations": [{"finding_ids": [f"fabricated-{SECRET}"]}]}
    ]
    return (
        session,
        RUN_OWNERSHIP,
        finding_proposal(session, [], status="insufficient_evidence", conclusions=conclusions),
    )


def _compared(tmp_path: Path) -> ToolSession:
    """Left is the ownership snapshot; right renames its resource and has a saved result."""
    left = load_snapshot(OWNERSHIP_SNAPSHOT)
    data = left.model_dump(mode="json")
    data["snapshot_id"] = "00000000-0000-4000-8000-0000000000dd"
    for evidence in data["evidence"].values():
        evidence["snapshot_id"] = data["snapshot_id"]
    right = validate_snapshot(json.loads(json.dumps(data).replace("vpc-owner", "vpc-renamed")))
    paths = {name: tmp_path / f"{name}.json" for name in ("left", "right", "result")}
    paths["left"].write_text(left.model_dump_json(), encoding="utf-8")
    paths["right"].write_text(right.model_dump_json(), encoding="utf-8")
    save_analysis(
        run_analyzer(GraphIndex(right), {"kind": "ownership"}), paths["result"], snapshot=right
    )
    return ToolSession.from_paths(
        paths["left"], comparison_path=paths["right"], analysis_paths=[paths["result"]]
    )


def _other_snapshot_id(tmp_path: Path) -> tuple:
    session = _compared(tmp_path)
    right_findings = session.analyses[0].findings
    contexts = [
        {"side": "primary", "source_kind": session.index.source_kind.value},
        {"side": "comparison", "source_kind": session.comparison_index.source_kind.value},
    ]
    conclusions = [
        {"kind": "analyzer_finding", "citations": [{"finding_ids": [right_findings[0].id]}]}
    ]
    proposal = finding_proposal(
        session, [], status="insufficient_evidence", contexts=contexts, conclusions=conclusions
    )
    return session, [("get_saved_analysis", {"number": 1})], proposal


def _not_returned(tmp_path: Path) -> tuple:
    """The finding is delivered and cited correctly; the configuration was never queried."""
    session, findings = _ownership()
    proposal = finding_proposal(session, findings, status=proposal_status(findings))
    proposal["conclusions"].append(
        {
            "kind": "recorded_configuration",
            "citations": [
                {
                    "evidence_ids": [session.index.evidence_ids[0]],
                    "resource_ids": [session.index.resource_ids[0]],
                }
            ],
        }
    )
    return session, RUN_OWNERSHIP, proposal


def _evidence_detached(tmp_path: Path) -> tuple:
    """Both resources are inspected, but one resource's evidence is cited for the other."""
    session = ToolSession.from_paths(SNAPSHOTS / "network.json")
    index = session.index
    first, second = [
        resource for resource in index.resource_ids if index.resource_evidence_ids(resource)
    ][:2]
    conclusions = [
        {
            "kind": "recorded_configuration",
            "citations": [
                {
                    "evidence_ids": [index.resource_evidence_ids(first)[0]],
                    "resource_ids": [second],
                }
            ],
        }
    ]
    proposal = finding_proposal(
        session, [], status="insufficient_evidence", conclusions=conclusions
    )
    calls = [
        ("inspect_resource", {"resource_id": first}),
        ("inspect_resource", {"resource_id": second}),
    ]
    return session, calls, proposal


def _planned_as_deployed(tmp_path: Path) -> tuple:
    session = ToolSession.from_paths(SNAPSHOTS / "terraform-plan.json")
    assert session.index.source_kind == SnapshotSourceKind.TERRAFORM_PLAN
    contexts = [{"side": "primary", "source_kind": "aws"}]
    proposal = finding_proposal(session, [], status="insufficient_evidence", contexts=contexts)
    return session, [("search_resources", {"limit": 1})], proposal


def _status_upgrade(tmp_path: Path) -> tuple:
    session, findings = _ownership("failed")
    assert proposal_status(findings) == "partial"
    return session, RUN_OWNERSHIP, finding_proposal(session, findings, status="supported")


def _limitation_unacknowledged(tmp_path: Path) -> tuple:
    session, findings = _ownership("failed")
    proposal = finding_proposal(session, findings, status="partial", limitations=[])
    return session, RUN_OWNERSHIP, proposal


def _limitation_unknown(tmp_path: Path) -> tuple:
    session, findings = _ownership("failed")
    proposal = finding_proposal(session, findings, status="partial")
    proposal["limitations"].append({"finding_id": findings[0].id, "limitation": "invented_code"})
    return session, RUN_OWNERSHIP, proposal


def _contradiction_omitted(tmp_path: Path) -> tuple:
    scenario = SCENARIOS[AnalyzerKind.SECURITY_GROUP_IMPACT]
    session = ToolSession(scenario.build(), limits=ToolLimits(result_limit=100))
    findings = service_result(scenario.build(), scenario.kind, scenario.parameters, 3).findings
    groups: dict[str, list] = {}
    for finding in (item for item in findings if item.resources):
        groups.setdefault(finding.resources[0].resource_id, []).append(finding)
    group = next(items for items in groups.values() if len({item.status for item in items}) > 1)
    cited = [group[0]]
    calls = [("run_analyzer", {"kind": scenario.kind.value, "limit": 100, **scenario.parameters})]
    return session, calls, finding_proposal(session, cited, status=proposal_status(cited))


def _injected(tmp_path: Path) -> ToolSession:
    data = json.loads((SNAPSHOTS / "aws.json").read_text(encoding="utf-8"))
    data["regions"]["eu-west-1"]["resources"][0]["name"] = INJECTION
    path = tmp_path / "injected.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return ToolSession.from_paths(path)


def _narrative_imported_text(tmp_path: Path) -> tuple:
    session = _injected(tmp_path)
    proposal = finding_proposal(
        session, [], status="insufficient_evidence", summary=f"The VPC says: {INJECTION.upper()}"
    )
    return session, [("search_resources", {})], proposal


def _narrative_uncited_id(tmp_path: Path) -> tuple:
    session, findings = _ownership()
    resource = session.index.resource_ids[0]
    proposal = finding_proposal(
        session, findings, status=proposal_status(findings), summary=f"See {resource}."
    )
    return session, RUN_OWNERSHIP, proposal


REJECTION_SCENARIOS: dict[RejectionCode, Callable[[Path], tuple]] = {
    RejectionCode.SIDE_UNAVAILABLE: _side_unavailable,
    RejectionCode.SOURCE_KIND_MISMATCH: _source_kind_mismatch,
    RejectionCode.UNKNOWN_ID: _unknown_id,
    RejectionCode.OTHER_SNAPSHOT_ID: _other_snapshot_id,
    RejectionCode.NOT_RETURNED: _not_returned,
    RejectionCode.EVIDENCE_DETACHED: _evidence_detached,
    RejectionCode.PLANNED_AS_DEPLOYED: _planned_as_deployed,
    RejectionCode.STATUS_UPGRADE: _status_upgrade,
    RejectionCode.LIMITATION_UNACKNOWLEDGED: _limitation_unacknowledged,
    RejectionCode.LIMITATION_UNKNOWN: _limitation_unknown,
    RejectionCode.CONTRADICTION_OMITTED: _contradiction_omitted,
    RejectionCode.NARRATIVE_IMPORTED_TEXT: _narrative_imported_text,
    RejectionCode.NARRATIVE_UNCITED_ID: _narrative_uncited_id,
}
