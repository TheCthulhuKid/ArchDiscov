import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from archdiscov.models.architecture import ArchitectureSnapshot, Evidence
from tests.paths import FIXTURES_ROOT


def snapshot_data() -> dict:
    path = FIXTURES_ROOT / "snapshots" / "partial-coverage.json"
    return json.loads(path.read_text())


def evidence_data() -> dict:
    return snapshot_data()["evidence"]["sg-rule"]


def test_rule_citation_and_missing_route_evidence_round_trip() -> None:
    snapshot = ArchitectureSnapshot.model_validate(snapshot_data())
    reloaded = ArchitectureSnapshot.model_validate_json(snapshot.model_dump_json())
    assert reloaded == snapshot
    for model in (snapshot, reloaded):
        resource = model.regions["eu-west-1"].resources[0]
        assert resource.aws_resource_type == "AWS::EC2::SecurityGroup"
        assert resource.attributes["ingress"] == [
            {"protocol": "tcp", "from_port": 443, "to_port": 443, "cidr": "0.0.0.0/0"}
        ]
        assert resource.evidence_ids == ["sg-rule"]
        assert resource.fact_evidence == {"/attributes/ingress/0": ["sg-rule"]}
        evidence = model.evidence["sg-rule"]
        assert evidence.snapshot_id == model.snapshot_id
        assert evidence.resource_id == resource.id
        assert evidence.locator == "DescribeSecurityGroups"
        assert evidence.configuration_path == "SecurityGroups[0].IpPermissions[0]"
        assert evidence.value_status == "present"
        assert evidence.value == {
            "IpProtocol": "tcp",
            "FromPort": 443,
            "ToPort": 443,
            "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
        }
        collected, missing, empty = model.coverage.operations
        assert collected.operation == "DescribeSecurityGroups"
        assert collected.status == "success" and collected.result_count == 1
        assert collected.failures == [] and collected.unresolved_values == []
        assert missing.operation == "DescribeRouteTables"
        assert missing.status == "failed" and missing.result_count is None
        assert [failure.model_dump(mode="json") for failure in missing.failures] == [
            {"code": "UnauthorizedOperation", "message": "Route-table collection denied"}
        ]
        assert empty.operation == "DescribeInstances"
        assert empty.status == "success" and empty.result_count == 0
        assert empty.failures == [] and empty.unresolved_values == []


@pytest.mark.parametrize("field", ["snapshot_id", "resource_id", "locator", "configuration_path"])
def test_evidence_requires_context(field: str) -> None:
    data = evidence_data()
    del data[field]
    with pytest.raises(ValidationError):
        Evidence.model_validate(data)


@pytest.mark.parametrize("path", ["attributes.ingress", "/bad~escape", ""])
def test_fact_paths_require_nonempty_json_pointers(path: str) -> None:
    data = snapshot_data()
    data["regions"]["eu-west-1"]["resources"][0]["fact_evidence"] = {path: ["sg-rule"]}
    with pytest.raises(ValidationError):
        ArchitectureSnapshot.model_validate(data)


def test_fact_citation_requires_evidence_id() -> None:
    data = snapshot_data()
    data["regions"]["eu-west-1"]["resources"][0]["fact_evidence"] = {"/name": []}
    with pytest.raises(ValidationError):
        ArchitectureSnapshot.model_validate(data)


@pytest.mark.parametrize("status", ["omitted", "unresolved"])
def test_unavailable_evidence_requires_explanation(status: str) -> None:
    data = evidence_data()
    del data["value"]
    data["value_status"] = status
    with pytest.raises(ValidationError):
        Evidence.model_validate(data)
    data["limitation"] = (
        "Sensitive source value excluded" if status == "omitted" else "Unknown at plan time"
    )
    evidence = Evidence.model_validate(data)
    assert evidence.value is None
    assert Evidence.model_validate_json(evidence.model_dump_json()) == evidence
    data["value"] = "must not be retained"
    with pytest.raises(ValidationError):
        Evidence.model_validate(data)


def test_missing_value_is_not_silently_treated_as_null() -> None:
    data = evidence_data()
    data["value"] = None
    assert Evidence.model_validate(data).value_status == "present"
    del data["value"]
    with pytest.raises(ValidationError):
        Evidence.model_validate(data)


def test_evidence_sanitization_retains_network_values_and_records_redactions() -> None:
    data = evidence_data()
    data["value"]["Credentials"] = {"Password": "do-not-store"}
    original = deepcopy(data)
    evidence = Evidence.model_validate(data)
    assert data == original
    assert evidence.value["Credentials"] == "[REDACTED]"
    assert evidence.value["FromPort"] == 443
    assert evidence.value_status == "redacted"
    assert evidence.redacted_paths == ["/Credentials"]
    assert "do-not-store" not in evidence.model_dump_json()
    assert Evidence.model_validate_json(evidence.model_dump_json()) == evidence


def test_scalar_sensitive_source_path_is_sanitized() -> None:
    data = evidence_data()
    data.update(
        configuration_path="Resources.Database.Properties.MasterUserPassword", value="secret"
    )
    evidence = Evidence.model_validate(data)
    assert evidence.value == "[REDACTED]"
    assert evidence.redacted_paths == [""]
    assert evidence.value_status == "redacted"


def test_generic_resource_dependency_and_metadata_use_shared_policy() -> None:
    data = snapshot_data()
    resource = data["regions"]["eu-west-1"]["resources"][0]
    resource["attributes"]["nested"] = [{"UserData": "private-script"}]
    target = deepcopy(resource)
    target["identity"]["physical_id"] = "sg-target"
    target["evidence_ids"] = []
    target["fact_evidence"] = {}
    data["regions"]["eu-west-1"]["resources"].append(target)
    data["metadata"] = {"api_key": "private-key"}
    data["regions"]["eu-west-1"]["metadata"] = {"password": "private-region"}
    data["dependencies_by_region"] = {
        "eu-west-1": [
            {
                "source_id": data["evidence"]["sg-rule"]["resource_id"],
                "target_id": "123456789012/eu-west-1/ec2/physical/sg-target",
                "relationship": "attached_to",
                "attributes": {"token": "private-token"},
                "evidence_ids": ["sg-rule"],
            }
        ]
    }
    snapshot = ArchitectureSnapshot.model_validate(data)
    assert snapshot.metadata["api_key"] == "[REDACTED]"
    assert snapshot.regions["eu-west-1"].metadata["password"] == "[REDACTED]"
    assert (
        snapshot.regions["eu-west-1"].resources[0].attributes["nested"][0]["UserData"]
        == "[REDACTED]"
    )
    assert snapshot.dependencies_by_region["eu-west-1"][0].attributes["token"] == "[REDACTED]"
    assert "private-" not in snapshot.model_dump_json()


@pytest.mark.parametrize("status", ["skipped", "unsupported", "incomplete"])
def test_coverage_limitations_round_trip(status: str) -> None:
    data = snapshot_data()
    operation = data["coverage"]["operations"][0]
    operation["status"] = status
    if status != "incomplete":
        del operation["result_count"]
    diagnostic = (
        "incomplete coverage requires a retained count and a limitation"
        if status == "incomplete"
        else "skipped/unsupported coverage requires a reason and no collected data"
    )
    with pytest.raises(ValidationError, match=diagnostic):
        ArchitectureSnapshot.model_validate(data)
    operation["reason"] = (
        "Unsupported expression" if status == "incomplete" else "Operation not collected"
    )
    if status == "incomplete":
        operation["unresolved_values"] = [
            {
                "resource_id": data["evidence"]["sg-rule"]["resource_id"],
                "configuration_path": "SecurityGroups[0].VpcId",
                "reason": "Unknown at plan time",
            }
        ]
    snapshot = ArchitectureSnapshot.model_validate(data)
    assert snapshot.coverage.operations[0].status == status
    assert ArchitectureSnapshot.model_validate_json(snapshot.model_dump_json()) == snapshot


@pytest.mark.parametrize(
    "change,diagnostic",
    [
        ({"result_count": None}, "successful coverage requires a count"),
        ({"result_count": -1}, "greater_than_equal"),
        ({"result_count": True}, "int_type"),
        ({"status": "failed", "result_count": None}, "failed coverage requires a failure"),
        ({"status": "invalid"}, "enum"),
        (
            {"started_at": "2026-10-13T08:00:20Z", "completed_at": "2026-10-13T08:00:10Z"},
            "coverage completed_at must not precede started_at",
        ),
        ({"started_at": "2026-10-13T08:00:00"}, "timezone_aware"),
        (
            {"failures": [{"code": "Denied", "message": "Denied"}]},
            "successful coverage requires a count and no failures/unresolved values",
        ),
        (
            {"unresolved_values": [{"configuration_path": "VpcId", "reason": "Unknown"}]},
            "successful coverage requires a count and no failures/unresolved values",
        ),
    ],
)
def test_inconsistent_operation_outcomes_are_rejected(change: dict, diagnostic: str) -> None:
    data = snapshot_data()
    ArchitectureSnapshot.model_validate(data)
    data["coverage"]["operations"][0].update(change)
    with pytest.raises(ValidationError, match=diagnostic):
        ArchitectureSnapshot.model_validate(data)


@pytest.mark.parametrize(
    "change,diagnostic",
    [
        ({"account_id": "999999999999"}, "operation coverage account must match snapshot account"),
        ({"region": "us-east-1"}, "operation coverage region must be included in requested scope"),
        ({"region": "global"}, "global operation coverage requires account_wide scope"),
        (
            {"started_at": "2026-10-13T07:59:00Z"},
            "operation coverage timestamps must fall within snapshot collection",
        ),
        (
            {"completed_at": "2026-10-13T08:02:00Z"},
            "operation coverage timestamps must fall within snapshot collection",
        ),
    ],
)
def test_operation_context_must_match_snapshot_scope_and_window(
    change: dict, diagnostic: str
) -> None:
    data = snapshot_data()
    ArchitectureSnapshot.model_validate(data)
    data["coverage"]["operations"][0].update(change)
    with pytest.raises(ValidationError, match=diagnostic):
        ArchitectureSnapshot.model_validate(data)


def test_duplicate_operation_outcomes_are_rejected() -> None:
    data = snapshot_data()
    data["coverage"]["operations"].append(deepcopy(data["coverage"]["operations"][0]))
    with pytest.raises(ValidationError):
        ArchitectureSnapshot.model_validate(data)


def test_account_wide_operation_uses_global_scope() -> None:
    data = snapshot_data()
    data["coverage"]["account_wide"] = True
    data["coverage"]["operations"][0].update(region="global", service="iam", operation="ListRoles")
    assert ArchitectureSnapshot.model_validate(data).coverage.operations[0].region == "global"


@pytest.mark.parametrize(
    "limitation",
    [
        {"failures": [{"code": "Throttling", "message": "Later page failed"}]},
        {"unresolved_values": [{"configuration_path": "VpcId", "reason": "Unknown at plan time"}]},
        {"reason": "Unsupported resource type in input"},
    ],
)
def test_incomplete_coverage_preserves_results_and_each_limitation_kind(limitation: dict) -> None:
    data = snapshot_data()
    operation = data["coverage"]["operations"][0]
    operation.update(status="incomplete", **limitation)
    snapshot = ArchitectureSnapshot.model_validate(data)
    reloaded = ArchitectureSnapshot.model_validate_json(snapshot.model_dump_json())
    assert reloaded == snapshot
    for model in (snapshot, reloaded):
        outcome = model.coverage.operations[0]
        assert outcome.status == "incomplete"
        assert outcome.result_count == 1
        retained = outcome.model_dump(mode="json", exclude_none=True)
        for field, value in limitation.items():
            assert retained[field] == value
        resource = model.regions["eu-west-1"].resources[0]
        assert resource.attributes["ingress"][0]["from_port"] == 443
        assert resource.fact_evidence == {"/attributes/ingress/0": ["sg-rule"]}


@pytest.mark.parametrize("status", ["failed", "skipped", "unsupported"])
def test_uncollected_coverage_cannot_claim_zero_results(status: str) -> None:
    data = snapshot_data()
    operation = data["coverage"]["operations"][1]
    operation.update(status=status, reason="Not collected", result_count=0)
    if status != "failed":
        operation["failures"] = []
    with pytest.raises(
        ValidationError, match="only successful or incomplete coverage can have a result_count"
    ):
        ArchitectureSnapshot.model_validate(data)


def test_nested_resources_preserve_field_citations_and_pointer_escaping() -> None:
    data = snapshot_data()
    resource = data["regions"]["eu-west-1"]["resources"][0]
    child = deepcopy(resource)
    child["identity"]["physical_id"] = "sg-child"
    child["attributes"]["a/b~c"] = 443
    child["fact_evidence"] = {"/attributes/a~1b~0c": ["child-port"]}
    child["evidence_ids"] = ["child-port"]
    resource["children"] = [child]
    data["evidence"]["child-port"] = {
        **evidence_data(),
        "resource_id": "123456789012/eu-west-1/ec2/physical/sg-child",
        "configuration_path": "SecurityGroups[1].IpPermissions[0].FromPort",
        "value": 443,
    }
    snapshot = ArchitectureSnapshot.model_validate(data)
    loaded = ArchitectureSnapshot.model_validate_json(snapshot.model_dump_json())
    assert loaded == snapshot
    assert loaded.regions["eu-west-1"].resources[0].children[0].fact_evidence == {
        "/attributes/a~1b~0c": ["child-port"]
    }


def test_source_annotated_redaction_is_retained_on_reload() -> None:
    data = evidence_data()
    data.update(value="[REDACTED]", value_status="redacted", redacted_paths=[""])
    evidence = Evidence.model_validate(data)
    assert Evidence.model_validate_json(evidence.model_dump_json()) == evidence
    data["redacted_paths"] = []
    with pytest.raises(ValidationError):
        Evidence.model_validate(data)
