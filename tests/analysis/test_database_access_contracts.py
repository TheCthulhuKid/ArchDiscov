import json

import pytest
from pydantic import ValidationError

from archdiscov.graph import GraphIndex
from archdiscov.models.analysis.database_access import AccessElementDecision, DatabaseAccess
from archdiscov.models.analysis.references import UNSUPPORTED_PLATFORM_CODE, AnalysisLimitation
from archdiscov.models.analysis.results import AnalysisFinding, AnalysisResult
from archdiscov.models.enums import (
    AccessElement,
    NetworkBlockerCode,
    NetworkDecision,
)
from archdiscov.models.network_path import compose_decisions
from archdiscov.services import AnalysisValidationError, validate_analysis_result, validate_snapshot
from tests.paths import FIXTURES_ROOT

D = NetworkDecision
B = NetworkBlockerCode
ACCOUNT, REGION = "123456789012", "eu-west-1"
DB_ARN = f"arn:aws:rds:{REGION}:{ACCOUNT}:db:app-db"
TASK_ARN = f"arn:aws:ecs:{REGION}:{ACCOUNT}:task/app/0123456789abcdef"
FUNCTION_ARN = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:worker"


def _identity(service, physical_id):
    return {"account_id": ACCOUNT, "region": REGION, "service": service, "physical_id": physical_id}


def _snapshot():
    """The hierarchy fixture plus a database, an ECS task, and a Lambda function."""
    data = json.loads((FIXTURES_ROOT / "snapshots" / "hierarchy.json").read_text())
    data["regions"][REGION]["resources"].extend(
        [
            {
                "identity": _identity("rds", DB_ARN),
                "resource_type": "AWS::RDS::DBInstance",
                "arn": DB_ARN,
                "endpoint_port": 5432,
            },
            {
                "identity": _identity("ecs", TASK_ARN),
                "resource_type": "AWS::ECS::Task",
                "arn": TASK_ARN,
            },
            {
                "identity": _identity("lambda", FUNCTION_ARN),
                "resource_type": "AWS::Lambda::Function",
                "arn": FUNCTION_ARN,
            },
        ]
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
TASK = rid("AWS::ECS::Task")
FUNCTION = rid("AWS::Lambda::Function")
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


def result(findings, kind="database_access", **parameters):
    return {
        "snapshot_id": SNAPSHOT_ID,
        "analyzer": kind,
        "analyzer_version": "1",
        "parameters": {"kind": kind, "resource_id": DATABASE, "port": 5432, **parameters},
        "created_at": "2026-10-13T09:00:00Z",
        "findings": findings,
    }


def element(kind, decision="allow", codes=()):
    return {"element": kind, "decision": decision, "blocker_codes": list(codes)}


# ---- element and path vocabulary ----------------------------------------------------


@pytest.mark.parametrize(
    ("decision", "codes"),
    [
        (D.ALLOW, []),
        (D.DENY, [B.DENIED]),
        (D.UNKNOWN, [B.COVERAGE_LIMITED, B.MISSING_EVIDENCE]),
    ],
)
def test_every_element_decision_round_trips(decision, codes):
    model = AccessElementDecision(
        element=AccessElement.ROUTE, decision=decision, blocker_codes=codes
    )
    assert AccessElementDecision.model_validate_json(model.model_dump_json()) == model


@pytest.mark.parametrize(
    ("decision", "codes"),
    [
        (D.ALLOW, [B.MISSING_EVIDENCE]),
        (D.DENY, [B.DENIED, B.MISSING_EVIDENCE]),
        (D.UNKNOWN, [B.DENIED]),
        (D.UNKNOWN, [B.MISSING_EVIDENCE, B.COVERAGE_LIMITED]),
    ],
    ids=[
        "allow-blocked",
        "deny-mixed",
        "unknown-denied",
        "unsorted",
    ],
)
def test_invalid_element_decisions_are_rejected(decision, codes):
    with pytest.raises(ValidationError):
        AccessElementDecision(element=AccessElement.ROUTE, decision=decision, blocker_codes=codes)


def explained(kind, decision):
    codes = {D.ALLOW: [], D.DENY: [B.DENIED], D.UNKNOWN: [B.MISSING_EVIDENCE]}[decision]
    return element(kind, decision, codes)


@pytest.mark.parametrize(
    "decisions",
    [(D.ALLOW, D.ALLOW), (D.ALLOW, D.UNKNOWN), (D.UNKNOWN, D.DENY)],
    ids=["allow", "unknown", "deny"],
)
def test_the_composed_decision_follows_all_of(decisions):
    elements = [
        explained(AccessElement.SOURCE_EGRESS, decisions[0]),
        explained(AccessElement.DATABASE_INGRESS, decisions[1]),
    ]
    expected = compose_decisions(list(decisions))
    assert DatabaseAccess.model_validate(access(decision=expected, elements=elements))
    wrong = next(d for d in D if d != expected)
    with pytest.raises(ValidationError, match="compose"):
        DatabaseAccess.model_validate(access(decision=wrong, elements=elements))


def test_deny_dominates_unknown_and_both_dominate_allow():
    assert compose_decisions([D.ALLOW, D.UNKNOWN, D.DENY]) == D.DENY
    assert compose_decisions([D.ALLOW, D.UNKNOWN]) == D.UNKNOWN
    assert compose_decisions([D.ALLOW]) == D.ALLOW


def test_all_five_elements_in_canonical_order_are_accepted():
    elements = [element(kind) for kind in AccessElement]
    assert len(DatabaseAccess.model_validate(access(elements=elements)).elements) == 5


@pytest.mark.parametrize(
    "fields",
    [
        {"elements": []},
        {"elements": [element("route"), element("source_egress")]},
        {"port": 0},
        {"port": "5432"},
    ],
    ids=[
        "empty",
        "unordered",
        "port-low",
        "port-str",
    ],
)
def test_invalid_access_values_are_rejected(fields):
    with pytest.raises(ValidationError):
        DatabaseAccess.model_validate(access(**fields))


# ---- finding identity and shape -----------------------------------------------------


def test_every_database_access_field_is_part_of_finding_identity():
    base = AnalysisFinding.model_validate(finding(access())).id
    unknown_elements = [
        element("source_egress"),
        element("database_ingress", "unknown", ["missing_evidence"]),
    ]
    variants = [
        {"source_kind": "ecs_task"},
        {"port": 3306},
        {"decision": "unknown", "elements": unknown_elements},
        {"elements": [element("source_egress"), element("route")]},
    ]
    ids = {base}
    for fields in variants:
        status = {"status": "partial", "limitations": [{"code": "x", "message": "x"}]}
        ids.add(AnalysisFinding.model_validate(finding(access(**fields), **status)).id)
    assert len(ids) == 1 + len(variants)
    assert AnalysisFinding.model_validate(finding()).id not in ids


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
    insufficient = {
        "status": "insufficient_evidence",
        "evidence_ids": [],
        "limitations": [{"code": "x", "message": "m"}],
    }
    assert AnalysisFinding.model_validate(finding(unknown, **insufficient))


def test_decided_findings_are_supported_and_cited():
    elements = [element("source_egress", "deny", ["denied"])]
    model = AnalysisFinding.model_validate(finding(access(decision="deny", elements=elements)))
    assert model.status == "supported"
    assert model.evidence_ids == [EVIDENCE]


# ---- results ------------------------------------------------------------------------


def test_database_access_findings_require_database_access_parameters():
    valid = AnalysisResult.model_validate(result([finding(access())]))
    assert AnalysisResult.model_validate_json(valid.model_dump_json()) == valid
    with pytest.raises(ValidationError, match="database_access parameters"):
        AnalysisResult.model_validate(
            {**result([finding(access())], kind="ownership"), "parameters": {"kind": "ownership"}}
        )


def test_the_database_and_port_must_match_the_parameters():
    with pytest.raises(ValidationError, match="selected database"):
        AnalysisResult.model_validate(result([finding(access(), database=INSTANCE)]))
    with pytest.raises(ValidationError, match="selected port"):
        AnalysisResult.model_validate(result([finding(access(port=3306))]))


def test_result_limit_is_a_validated_parameter_defaulting_to_100():
    assert AnalysisResult.model_validate(result([])).parameters.result_limit == 100
    assert AnalysisResult.model_validate(result([], result_limit=5)).parameters.result_limit == 5


def test_results_validate_against_a_snapshot_for_every_source_kind():
    for source, kind in (
        (INSTANCE, "ec2_instance"),
        (TASK, "ecs_task"),
        (FUNCTION, "lambda_function"),
    ):
        data = result([finding(access(source_kind=kind), source=source)])
        [checked] = validate_analysis_result(data, SNAPSHOT).findings
        assert checked.resources[0].scope.account_id == ACCOUNT
        assert checked.evidence_ids == [EVIDENCE]
        assert checked.database_access.port == 5432


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (result([finding(access(source_kind="ecs_task"))]), "database_source_mismatch"),
        (result([finding(access(), database=SUBNET)]), "database_target_mismatch"),
    ],
    ids=[
        "kind-mismatch",
        "subnet-database",
    ],
)
def test_integrity_rejects_mismatched_sources_and_databases(data, code):
    parameters = data["parameters"]
    if code == "database_target_mismatch":
        parameters["resource_id"] = SUBNET
    with pytest.raises(AnalysisValidationError) as error:
        validate_analysis_result(data, SNAPSHOT)
    assert error.value.details[0].type == code


def test_unsupported_platform_code_is_shared_and_needs_no_access_block():
    model = AnalysisFinding(
        snapshot_id=SNAPSHOT_ID,
        summary="Database access is not analyzed for this cloud.",
        status="insufficient_evidence",
        limitations=[AnalysisLimitation(code=UNSUPPORTED_PLATFORM_CODE, message="AWS only.")],
    )
    assert model.database_access is None
