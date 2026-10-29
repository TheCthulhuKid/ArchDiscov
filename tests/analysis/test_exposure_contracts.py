import json

import pytest
from pydantic import ValidationError

from archdiscov.graph import GraphIndex
from archdiscov.models.analysis.exposure import InternetExposure
from archdiscov.models.enums import ExposureElement, ExposureEntryKind, TargetHealthState
from archdiscov.services import AnalysisValidationError, validate_analysis_result, validate_snapshot
from tests.paths import FIXTURES_ROOT

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


def element(kind):
    return {"element": kind, "decision": "allow", "blocker_codes": []}


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


def result(data, entry):
    finding = {
        "snapshot_id": SNAPSHOT_ID,
        "summary": "A recorded path could expose this entry point.",
        "status": "supported",
        "evidence_ids": [EVIDENCE],
        "resources": [{"resource_id": entry, "scope": SCOPE}],
        "internet_exposure": data,
    }
    return {
        "snapshot_id": SNAPSHOT_ID,
        "analyzer": "internet_exposure",
        "analyzer_version": "1",
        "parameters": {"kind": "internet_exposure"},
        "created_at": "2026-10-13T09:00:00Z",
        "findings": [finding],
    }


def test_each_entry_kind_accepts_only_its_own_elements_and_an_ordered_port_range():
    assert (
        InternetExposure.model_validate(exposure()).entry_kind == ExposureEntryKind.PUBLIC_ADDRESS
    )
    assert InternetExposure.model_validate(balancer()).entry_kind == ExposureEntryKind.LOAD_BALANCER
    with pytest.raises(ValidationError, match="not valid"):
        InternetExposure.model_validate(exposure(elements=[element("listener")]))
    with pytest.raises(ValidationError, match="not valid"):
        InternetExposure.model_validate(balancer(elements=[element("public_address")]))
    with pytest.raises(ValidationError, match="port_start"):
        InternetExposure.model_validate(exposure(port_start=444, port_end=443))


def test_target_health_is_a_sorted_observation_only_through_a_load_balancer():
    observed = InternetExposure.model_validate(
        balancer(observed_target_health=["healthy", "unhealthy"])
    )
    assert observed.observed_target_health == [
        TargetHealthState.HEALTHY,
        TargetHealthState.UNHEALTHY,
    ]
    assert "health" not in {item.value for item in ExposureElement}
    with pytest.raises(ValidationError, match="unique and sorted"):
        InternetExposure.model_validate(balancer(observed_target_health=["unhealthy", "healthy"]))
    with pytest.raises(ValidationError, match="load balancer"):
        InternetExposure.model_validate(exposure(observed_target_health=["healthy"]))


def test_integrity_requires_the_entry_to_match_its_kind():
    for data, entry in ((exposure(), ENI), (exposure(), INSTANCE), (balancer(), LOAD_BALANCER)):
        [checked] = validate_analysis_result(result(data, entry), SNAPSHOT).findings
        assert checked.internet_exposure.entry_kind == data["entry_kind"]
    with pytest.raises(AnalysisValidationError) as error:
        validate_analysis_result(result(balancer(), ENI), SNAPSHOT)
    assert error.value.details[0].type == "exposure_entry_mismatch"
