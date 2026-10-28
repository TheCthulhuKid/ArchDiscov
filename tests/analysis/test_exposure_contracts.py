import json

import pytest
from pydantic import ValidationError

from archdiscov.graph import GraphIndex
from archdiscov.models.analysis.exposure import InternetExposure
from archdiscov.models.analysis.references import UNSUPPORTED_PLATFORM_CODE, AnalysisLimitation
from archdiscov.models.analysis.results import AnalysisFinding, AnalysisResult
from archdiscov.models.enums import (
    ExposureElement,
    ExposureEntryKind,
    NetworkBlockerCode,
    NetworkDecision,
    TargetHealthState,
)
from archdiscov.services import AnalysisValidationError, validate_analysis_result, validate_snapshot
from tests.paths import FIXTURES_ROOT

D = NetworkDecision
B = NetworkBlockerCode
X = ExposureElement
ACCOUNT, REGION = "123456789012", "eu-west-1"
LB_ARN = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:loadbalancer/app/web/1234567890abcdef"


def _identity(service, physical_id):
    return {"account_id": ACCOUNT, "region": REGION, "service": service, "physical_id": physical_id}


def _snapshot():
    """The hierarchy fixture plus a network interface and a load balancer."""
    data = json.loads((FIXTURES_ROOT / "snapshots" / "hierarchy.json").read_text())
    data["regions"][REGION]["resources"].extend(
        [
            {
                "identity": _identity("ec2", "eni-1"),
                "resource_type": "AWS::EC2::NetworkInterface",
                "public_ip": "198.51.100.7",
            },
            {
                "identity": _identity("elasticloadbalancing", LB_ARN),
                "resource_type": "AWS::ElasticLoadBalancingV2::LoadBalancer",
                "arn": LB_ARN,
                "scheme": "internet-facing",
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
ENI = rid("AWS::EC2::NetworkInterface")
LOAD_BALANCER = rid("AWS::ElasticLoadBalancingV2::LoadBalancer")
SUBNET = rid("AWS::EC2::Subnet")


def element(kind, decision="allow", codes=()):
    return {"element": kind, "decision": decision, "blocker_codes": list(codes)}


PUBLIC_PATH = [element(k) for k in (X.PUBLIC_ADDRESS, X.ROUTE, X.NACL, X.SECURITY_GROUP)]
LB_PATH = [element(k) for k in (X.SCHEME, X.ROUTE, X.NACL, X.SECURITY_GROUP, X.LISTENER, X.TARGET)]


def exposure(**fields):
    return {
        "entry_kind": "public_address",
        "protocol": "tcp",
        "port_start": 443,
        "port_end": 443,
        "decision": "allow",
        "elements": PUBLIC_PATH,
        **fields,
    }


def balancer(**fields):
    return exposure(**{"entry_kind": "load_balancer", "elements": LB_PATH, **fields})


def finding(data=None, entry=ENI, **overrides):
    value = {
        "snapshot_id": SNAPSHOT_ID,
        "summary": "A recorded path could expose this entry point.",
        "status": "supported",
        "evidence_ids": [EVIDENCE],
        "resources": [{"resource_id": entry, "scope": SCOPE}],
        **overrides,
    }
    if data is not None:
        value["internet_exposure"] = data
    return value


def result(findings, kind="internet_exposure", **parameters):
    return {
        "snapshot_id": SNAPSHOT_ID,
        "analyzer": kind,
        "analyzer_version": "1",
        "parameters": {"kind": kind, **parameters},
        "created_at": "2026-10-13T09:00:00Z",
        "findings": findings,
    }


def explained(kind, decision):
    codes = {D.ALLOW: [], D.DENY: [B.DENIED], D.UNKNOWN: [B.MISSING_EVIDENCE]}[decision]
    return element(kind, decision, codes)


# ---- elements and composition -------------------------------------------------------


def test_the_decision_must_compose_the_element_decisions():
    elements = [explained(X.PUBLIC_ADDRESS, D.ALLOW), explained(X.ROUTE, D.DENY)]
    assert InternetExposure.model_validate(exposure(decision=D.DENY, elements=elements))
    with pytest.raises(ValidationError, match="compose"):
        InternetExposure.model_validate(exposure(decision=D.ALLOW, elements=elements))


@pytest.mark.parametrize("kind", ExposureEntryKind)
def test_each_entry_kind_accepts_its_full_path(kind):
    data = exposure() if kind == ExposureEntryKind.PUBLIC_ADDRESS else balancer()
    assert InternetExposure.model_validate(data).entry_kind == kind


def test_an_inverted_port_range_is_rejected():
    with pytest.raises(ValidationError, match="port_start"):
        InternetExposure.model_validate(exposure(port_start=444, port_end=443))


def test_load_balancer_elements_are_not_valid_for_a_public_address_and_vice_versa():
    with pytest.raises(ValidationError, match="not valid"):
        InternetExposure.model_validate(exposure(elements=[element("listener")]))
    with pytest.raises(ValidationError, match="not valid"):
        InternetExposure.model_validate(balancer(elements=[element("public_address")]))


# ---- health observation -------------------------------------------------------------


def test_target_health_is_an_observation_that_never_changes_the_decision():
    healthy = InternetExposure.model_validate(balancer())
    unhealthy = InternetExposure.model_validate(
        balancer(observed_target_health=["unhealthy", "healthy"][::-1])
    )
    assert healthy.decision == unhealthy.decision == D.ALLOW
    assert unhealthy.observed_target_health == [
        TargetHealthState.HEALTHY,
        TargetHealthState.UNHEALTHY,
    ]
    assert ExposureElement.TARGET in {item.element for item in unhealthy.elements}
    assert "health" not in {item.value for item in ExposureElement}


def test_observed_health_must_be_sorted_and_unique():
    with pytest.raises(ValidationError, match="unique and sorted"):
        InternetExposure.model_validate(balancer(observed_target_health=["unhealthy", "healthy"]))


def test_health_is_observed_only_through_a_load_balancer():
    with pytest.raises(ValidationError, match="load balancer"):
        InternetExposure.model_validate(exposure(observed_target_health=["healthy"]))


# ---- finding identity and shape -----------------------------------------------------


def test_every_exposure_field_is_part_of_finding_identity():
    base = AnalysisFinding.model_validate(finding(exposure())).id
    unknown = [element("public_address"), element("route", "unknown", ["missing_evidence"])]
    partial = {"status": "partial", "limitations": [{"code": "x", "message": "m"}]}
    variants = [
        ({"port_start": 80, "port_end": 80}, {}),
        ({"port_end": 444}, {}),
        ({"protocol": "udp"}, {}),
        ({"decision": "unknown", "elements": unknown}, partial),
        ({"elements": [element("public_address"), element("route")]}, {}),
        ({"entry_kind": "load_balancer", "elements": LB_PATH}, {"entry": LOAD_BALANCER}),
        ({**balancer(), "observed_target_health": ["healthy"]}, {"entry": LOAD_BALANCER}),
    ]
    ids = {base}
    for fields, extra in variants:
        data = {**exposure(), **fields}
        ids.add(AnalysisFinding.model_validate(finding(data, **extra)).id)
    assert len(ids) == 1 + len(variants)
    assert AnalysisFinding.model_validate(finding()).id not in ids


def test_finding_shape_rules():
    with pytest.raises(ValidationError, match="at most one"):
        AnalysisFinding.model_validate(
            finding(
                exposure(),
                impact={"selected_group_id": ENI, "kind": "direct_attachment", "label": "observed"},
            )
        )
    with pytest.raises(ValidationError, match="entry point"):
        AnalysisFinding.model_validate(finding(exposure(), resources=[]))


def test_an_unknown_decision_cannot_be_supported():
    unknown = exposure(
        decision="unknown", elements=[element("public_address", "unknown", ["missing_evidence"])]
    )
    with pytest.raises(ValidationError, match="cannot be supported"):
        AnalysisFinding.model_validate(finding(unknown))
    insufficient = {
        "status": "insufficient_evidence",
        "evidence_ids": [],
        "limitations": [{"code": "x", "message": "m"}],
    }
    assert AnalysisFinding.model_validate(finding(unknown, **insufficient))


def test_decided_findings_are_supported_and_cited():
    data = exposure(decision="deny", elements=[element("public_address", "deny", ["denied"])])
    model = AnalysisFinding.model_validate(finding(data))
    assert (model.status, model.evidence_ids) == ("supported", [EVIDENCE])


def test_backend_targets_may_follow_the_entry_point():
    targets = [{"resource_id": INSTANCE, "scope": SCOPE}]
    model = AnalysisFinding.model_validate(
        finding(
            balancer(),
            entry=LOAD_BALANCER,
            resources=[{"resource_id": LOAD_BALANCER, "scope": SCOPE}, *targets],
        )
    )
    assert [r.resource_id for r in model.resources] == [LOAD_BALANCER, INSTANCE]


# ---- results ------------------------------------------------------------------------


def test_exposure_findings_require_internet_exposure_parameters():
    valid = AnalysisResult.model_validate(result([finding(exposure())], max_depth=2))
    assert AnalysisResult.model_validate_json(valid.model_dump_json()) == valid
    assert (valid.parameters.max_depth, valid.parameters.result_limit) == (2, 100)
    with pytest.raises(ValidationError, match="internet_exposure parameters"):
        AnalysisResult.model_validate(result([finding(exposure())], kind="ownership"))


def test_results_validate_against_a_snapshot_for_every_entry_kind():
    for data, entry in ((exposure(), ENI), (exposure(), INSTANCE), (balancer(), LOAD_BALANCER)):
        [checked] = validate_analysis_result(
            result([finding(data, entry=entry)]), SNAPSHOT
        ).findings
        assert checked.resources[0].scope.account_id == ACCOUNT
        assert checked.evidence_ids == [EVIDENCE]
        assert checked.internet_exposure.port_start == 443


def test_integrity_rejects_an_entry_that_does_not_match_its_kind():
    with pytest.raises(AnalysisValidationError) as error:
        validate_analysis_result(result([finding(balancer(), entry=ENI)]), SNAPSHOT)

    assert error.value.details[0].type == "exposure_entry_mismatch"


def test_unsupported_platform_code_is_shared_and_needs_no_exposure_block():
    model = AnalysisFinding(
        snapshot_id=SNAPSHOT_ID,
        summary="Internet exposure is not analyzed for this cloud.",
        status="insufficient_evidence",
        limitations=[AnalysisLimitation(code=UNSUPPORTED_PLATFORM_CODE, message="AWS only.")],
    )
    assert model.internet_exposure is None
