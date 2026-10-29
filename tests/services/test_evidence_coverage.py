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
    model = ArchitectureSnapshot.model_validate_json(snapshot.model_dump_json())
    assert model == snapshot
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


def test_evidence_states_its_value_or_why_it_is_unavailable() -> None:
    data = evidence_data()
    data["value"] = None
    assert Evidence.model_validate(data).value_status == "present"
    del data["value"]
    with pytest.raises(ValidationError):
        Evidence.model_validate(data)
    data["value_status"] = "omitted"
    with pytest.raises(ValidationError):
        Evidence.model_validate(data)
    data["limitation"] = "Sensitive source value excluded"
    assert Evidence.model_validate(data).value is None
    data["value"] = "must not be retained"
    with pytest.raises(ValidationError):
        Evidence.model_validate(data)
    data.update(value="[REDACTED]", value_status="redacted", limitation=None)
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
    data.update(
        configuration_path="Resources.Database.Properties.MasterUserPassword", value="secret"
    )
    scalar = Evidence.model_validate(data)
    assert (scalar.value, scalar.redacted_paths, scalar.value_status) == (
        "[REDACTED]",
        [""],
        "redacted",
    )


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


@pytest.mark.parametrize(
    "change,diagnostic",
    [
        ({"account_id": "999999999999"}, "operation coverage account must match snapshot account"),
        ({"region": "us-east-1"}, "operation coverage region must be included in requested scope"),
        ({"region": "global"}, "global operation coverage requires account_wide scope"),
    ],
)
def test_operation_context_must_match_snapshot_scope(change: dict, diagnostic: str) -> None:
    data = snapshot_data()
    data["coverage"]["operations"][0].update(change)
    with pytest.raises(ValidationError, match=diagnostic):
        ArchitectureSnapshot.model_validate(data)


def test_account_wide_operation_uses_global_scope() -> None:
    data = snapshot_data()
    data["coverage"]["account_wide"] = True
    data["coverage"]["operations"][0].update(region="global", service="iam", operation="ListRoles")
    assert ArchitectureSnapshot.model_validate(data).coverage.operations[0].region == "global"


def test_uncollected_coverage_cannot_claim_zero_results() -> None:
    data = snapshot_data()
    operation = data["coverage"]["operations"][1]
    operation.update(status="skipped", reason="Not collected", result_count=0, failures=[])
    with pytest.raises(
        ValidationError, match="only successful or incomplete coverage can have a result_count"
    ):
        ArchitectureSnapshot.model_validate(data)
