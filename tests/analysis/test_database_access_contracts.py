import json

import pytest
from pydantic import ValidationError

from archdiscov.graph import GraphIndex
from archdiscov.models.analysis.database_access import AccessElementDecision, DatabaseAccess
from archdiscov.models.analysis.results import AnalysisFinding, AnalysisResult
from archdiscov.models.enums import AccessElement, NetworkBlockerCode, NetworkDecision
from archdiscov.services import AnalysisValidationError, validate_analysis_result, validate_snapshot
from tests.paths import FIXTURES_ROOT

D = NetworkDecision
B = NetworkBlockerCode
ACCOUNT, REGION = "123456789012", "eu-west-1"
DB_ARN = f"arn:aws:rds:{REGION}:{ACCOUNT}:db:app-db"


def _snapshot():
    """The hierarchy fixture plus a database."""
    data = json.loads((FIXTURES_ROOT / "snapshots" / "hierarchy.json").read_text())
    identity = {"account_id": ACCOUNT, "region": REGION, "service": "rds", "physical_id": DB_ARN}
    data["regions"][REGION]["resources"].append(
        {
            "identity": identity,
            "resource_type": "AWS::RDS::DBInstance",
            "arn": DB_ARN,
            "endpoint_port": 5432,
        }
    )
    return validate_snapshot(data)


SNAPSHOT = _snapshot()
INDEX = GraphIndex(SNAPSHOT)
SNAPSHOT_ID = str(INDEX.snapshot_id)
SCOPE = INDEX.scope.model_dump(mode="json")
EVIDENCE = "instance"


def rid(kind):
    return next(r.id for r in INDEX.resources() if r.resource_type == kind)


INSTANCE = rid("AWS::EC2::Instance")
DATABASE = rid("AWS::RDS::DBInstance")
SUBNET = rid("AWS::EC2::Subnet")
PATH = [
    {"element": e.value, "decision": "allow", "blocker_codes": []}
    for e in (AccessElement.SOURCE_EGRESS, AccessElement.DATABASE_INGRESS)
]


def access(**fields):
    return {
        "source_kind": "ec2_instance",
        "port": 5432,
        "decision": "allow",
        "elements": PATH,
        **fields,
    }


def finding(access_data=None, source=INSTANCE, database=DATABASE, **overrides):
    data = {
        "snapshot_id": SNAPSHOT_ID,
        "summary": "A recorded network path exists to the database.",
        "status": "supported",
        "evidence_ids": [EVIDENCE],
        "resources": [
            {"resource_id": source, "scope": SCOPE},
            {"resource_id": database, "scope": SCOPE},
        ],
        **overrides,
    }
    if access_data is not None:
        data["database_access"] = access_data
    return data


def result(findings, database=DATABASE):
    return {
        "snapshot_id": SNAPSHOT_ID,
        "analyzer": "database_access",
        "analyzer_version": "1",
        "parameters": {"kind": "database_access", "resource_id": database, "port": 5432},
        "created_at": "2026-10-13T09:00:00Z",
        "findings": findings,
    }


def element(kind, decision="allow", codes=()):
    return {"element": kind, "decision": decision, "blocker_codes": list(codes)}


# ---- element decisions, shared with internet exposure --------------------------------


def test_elements_are_ordered_explained_and_compose_the_decision():
    elements = [element(kind) for kind in AccessElement]
    assert len(DatabaseAccess.model_validate(access(elements=elements)).elements) == 5
    with pytest.raises(ValidationError, match="canonical order"):
        DatabaseAccess.model_validate(access(elements=[element("route"), element("source_egress")]))
    with pytest.raises(ValidationError, match="unique and sorted"):
        AccessElementDecision(
            element=AccessElement.ROUTE,
            decision=D.UNKNOWN,
            blocker_codes=[B.MISSING_EVIDENCE, B.COVERAGE_LIMITED],
        )
    with pytest.raises(ValidationError, match="no blockers"):
        AccessElementDecision(
            element=AccessElement.ROUTE, decision=D.ALLOW, blocker_codes=[B.MISSING_EVIDENCE]
        )
    denied = [element("source_egress"), element("database_ingress", "deny", ["denied"])]
    assert DatabaseAccess.model_validate(access(decision="deny", elements=denied))
    with pytest.raises(ValidationError, match="compose"):
        DatabaseAccess.model_validate(access(elements=denied))


# ---- finding shape ------------------------------------------------------------------


def test_finding_shape_rules():
    with pytest.raises(ValidationError, match="at most one"):
        AnalysisFinding.model_validate(
            finding(
                access(),
                impact={
                    "selected_group_id": DATABASE,
                    "kind": "direct_attachment",
                    "label": "observed",
                },
            )
        )
    with pytest.raises(ValidationError, match="source and the database"):
        AnalysisFinding.model_validate(
            finding(access(), resources=[{"resource_id": INSTANCE, "scope": SCOPE}])
        )


def test_an_unknown_decision_cannot_be_a_supported_finding():
    elements = [element("source_egress", "unknown", ["missing_evidence"])]
    unknown = access(decision="unknown", elements=elements)
    with pytest.raises(ValidationError, match="cannot be supported"):
        AnalysisFinding.model_validate(finding(unknown))
    partial = {"status": "partial", "limitations": [{"code": "x", "message": "m"}]}
    assert AnalysisFinding.model_validate(finding(unknown, **partial)).status == "partial"


# ---- results ------------------------------------------------------------------------


def test_the_database_and_port_must_match_the_parameters():
    with pytest.raises(ValidationError, match="selected database"):
        AnalysisResult.model_validate(result([finding(access(), database=INSTANCE)]))
    with pytest.raises(ValidationError, match="selected port"):
        AnalysisResult.model_validate(result([finding(access(port=3306))]))


def test_integrity_requires_the_source_kind_and_an_rds_database():
    [checked] = validate_analysis_result(result([finding(access())]), SNAPSHOT).findings
    assert checked.database_access.port == 5432
    for data, code in (
        (result([finding(access(source_kind="ecs_task"))]), "database_source_mismatch"),
        (result([finding(access(), database=SUBNET)], database=SUBNET), "database_target_mismatch"),
    ):
        with pytest.raises(AnalysisValidationError) as error:
            validate_analysis_result(data, SNAPSHOT)
        assert error.value.details[0].type == code
