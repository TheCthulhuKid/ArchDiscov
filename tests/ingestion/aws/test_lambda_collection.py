from datetime import UTC, datetime
from uuid import UUID

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from archdiscov.ingestion.adapter import AdapterContext
from archdiscov.ingestion.configuration_common import ServiceCollection
from archdiscov.ingestion.functions import collect_lambda
from archdiscov.ingestion.requests import AwsAccountConfig
from archdiscov.models.architecture import ArchitectureSnapshot
from archdiscov.models.enums import CoverageStatus, EvidenceValueStatus
from archdiscov.models.resources.functions import LambdaFunction
from archdiscov.services import load_snapshot, save_snapshot
from tests.support.lambda_data import ACCOUNT, ARN, REGION, ROLE, SECRET, Client, configuration

NOW = datetime(2026, 1, 1, tzinfo=UTC)


class Session:
    def __init__(self, client):
        self.api = client

    def client(self, name, **kwargs):
        assert name == "lambda"
        assert kwargs["region_name"] == REGION
        return self.api


def collect(client):
    session = Session(client)
    return collect_lambda(
        session,
        AwsAccountConfig(
            profile="offline",
            expected_account_id=ACCOUNT,
            regions=(REGION,),
            services=("lambda",),
            max_concurrency=1,
        ),
        AdapterContext(clock=lambda: NOW),
        UUID(int=16),
        ACCOUNT,
        REGION,
    )


def coverage(result):
    return {c.operation: c for c in result.coverage}


def snapshot(result):
    return ArchitectureSnapshot(
        schema_version="1",
        snapshot_id=UUID(int=16),
        account_id=ACCOUNT,
        collection_started_at=NOW,
        collection_completed_at=NOW,
        source={"kind": "aws", "profile": "offline"},
        coverage={"regions": [REGION], "account_wide": False, "operations": result.coverage},
        regions={REGION: {"resources": result.resources}},
        evidence=result.evidence,
    )


def test_real_stubber_marker_pages_and_exact_detail_requests():
    api = boto3.client(
        "lambda", region_name=REGION, aws_access_key_id="offline", aws_secret_access_key="offline"
    )
    second = ARN + "2"
    with Stubber(api) as stub:
        stub.add_response(
            "list_functions", {"Functions": [configuration()], "NextMarker": "next"}, {}
        )
        stub.add_response(
            "list_functions", {"Functions": [configuration(second)]}, {"Marker": "next"}
        )
        for arn in (ARN, second):
            stub.add_response(
                "get_function_configuration", configuration(arn), {"FunctionName": arn}
            )
        result = collect(api)
        stub.assert_no_pending_responses()
    assert isinstance(result, ServiceCollection)
    assert len(result.resources) == 2
    assert all(isinstance(r, LambdaFunction) and not r.children for r in result.resources)
    assert len(result.coverage) == 2
    assert all(c.status == CoverageStatus.SUCCESS and c.result_count == 2 for c in result.coverage)
    item = result.resources[1]
    whole = [result.evidence[e] for e in item.evidence_ids]
    assert any(e.configuration_path == "Request[{}].Page[1].Functions[0]" for e in whole)
    for field, source in {
        "arn": "FunctionArn",
        "role_arn": "Role",
        "runtime": "Runtime",
        "package_type": "PackageType",
        "state": "State",
        "last_update_status": "LastUpdateStatus",
        "memory_size": "MemorySize",
        "timeout": "Timeout",
        "architectures": "Architectures",
        "vpc_id": "VpcConfig.VpcId",
        "subnet_ids": "VpcConfig.SubnetIds",
        "security_group_ids": "VpcConfig.SecurityGroupIds",
    }.items():
        fact = result.evidence[item.fact_evidence[f"/{field}"][0]]
        assert fact.configuration_path == (
            f'Request[{{"FunctionName":"{second}"}}].Page[0].{source}'
        )
        assert fact.value == getattr(item, field)
        assert fact.resource_id == item.id
    assert result.evidence[item.fact_evidence["/subnet_ids/0"][0]].configuration_path.endswith(
        ".VpcConfig.SubnetIds[0]"
    )


def test_empty_discovery_is_success_without_detail_calls():
    client = Client(pages=[{"Functions": []}])
    result = collect(client)
    assert result.resources == [] and result.evidence == {}
    assert len(client.calls) == 1
    assert all(c.status == CoverageStatus.SUCCESS and c.result_count == 0 for c in result.coverage)


def denied():
    return ClientError({"Error": {"Code": "AccessDeniedException", "Message": SECRET}}, "read")


def test_detail_failure_keeps_inventory_without_invented_empty_fields():
    client = Client(
        pages=[{"Functions": [{"FunctionArn": ARN, "Runtime": "python3.13"}]}], detail=denied()
    )
    result = collect(client)
    item = result.resources[0]
    assert item.runtime == "python3.13"
    assert item.role_arn is None and item.subnet_ids is None and item.security_group_ids is None
    assert coverage(result)["ListFunctions"].status == CoverageStatus.SUCCESS
    assert coverage(result)["GetFunctionConfiguration"].status == CoverageStatus.FAILED
    assert result.evidence[item.fact_evidence["/runtime"][0]].configuration_path == (
        "Request[{}].Page[0].Functions[0].Runtime"
    )
    assert SECRET not in snapshot(result).model_dump_json()


def test_partial_discovery_qualifies_successful_details():
    result = collect(
        Client(pages=[{"Functions": [configuration()], "NextMarker": "next"}, denied()])
    )
    assert len(result.resources) == 1
    assert coverage(result)["ListFunctions"].status == CoverageStatus.INCOMPLETE
    detail = coverage(result)["GetFunctionConfiguration"]
    assert detail.status == CoverageStatus.INCOMPLETE and detail.result_count == 1
    assert detail.reason == "Discovery was unavailable or incomplete: ListFunctions"


def test_failed_discovery_skips_detail_without_claiming_empty_success():
    client = Client(pages=[denied()])
    result = collect(client)
    assert result.resources == []
    assert coverage(result)["ListFunctions"].status == CoverageStatus.FAILED
    assert coverage(result)["GetFunctionConfiguration"].status == CoverageStatus.SKIPPED
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "key,value,path",
    [
        ("Runtime", "future-runtime", "Runtime"),
        ("Timeout", True, "Timeout"),
        ("MemorySize", "256", "MemorySize"),
        ("State", {"raw": SECRET}, "State"),
        ("Architectures", ["future"], "Architectures"),
        ("VpcConfig", {"SubnetIds": ["bad subnet"]}, "VpcConfig.SubnetIds"),
        ("VpcConfig", "bad", "VpcConfig"),
    ],
)
def test_malformed_detail_values_are_unavailable_and_precisely_reported(key, value, path):
    detail = {**configuration(), key: value}
    result = collect(Client(detail=detail))
    detail_coverage = coverage(result)["GetFunctionConfiguration"]
    assert detail_coverage.status == CoverageStatus.INCOMPLETE
    assert any(
        u.configuration_path == f'Request[{{"FunctionName":"{ARN}"}}].Page[0].{path}'
        for u in detail_coverage.unresolved_values
    )
    if key == "VpcConfig":
        assert result.resources[0].subnet_ids is None
    else:
        field = {
            "Runtime": "runtime",
            "Timeout": "timeout",
            "MemorySize": "memory_size",
            "State": "state",
            "Architectures": "architectures",
        }[key]
        assert getattr(result.resources[0], field) is None
    assert SECRET not in snapshot(result).model_dump_json()


def test_malformed_inventory_identity_qualifies_discovery_and_skips_details():
    result = collect(Client(pages=[{"Functions": [{"FunctionArn": SECRET}]}]))
    assert result.resources == []
    assert coverage(result)["ListFunctions"].status == CoverageStatus.INCOMPLETE
    assert coverage(result)["ListFunctions"].unresolved_values[0].configuration_path == (
        "Request[{}].Page[0].Functions[0].FunctionArn"
    )
    assert coverage(result)["GetFunctionConfiguration"].status == CoverageStatus.SKIPPED


def test_projection_and_exact_citations_survive_persistence_without_secret_bytes(tmp_path):
    detail = {
        **configuration(),
        "Environment": {"Variables": {"ORDINARY_NAME": SECRET}},
        "Description": SECRET,
        "StateReason": SECRET,
        "LastUpdateStatusReason": SECRET,
        "Code": {"Location": f"https://example.invalid/{SECRET}"},
        "SigningJobArn": SECRET,
        "RuntimeVersionConfig": {"Error": {"Message": SECRET}},
        "AuthToken": SECRET,
        "FutureField": {"ordinary": SECRET},
    }
    result = collect(Client(pages=[{"Functions": [detail]}], detail=detail))
    item = result.resources[0]
    whole = result.evidence[item.evidence_ids[-1]]
    assert whole.value_status == EvidenceValueStatus.REDACTED
    assert "/Environment" in whole.redacted_paths and "/Description" in whole.redacted_paths
    assert "Environment" not in whole.value and "FutureField" not in whole.value
    document = snapshot(result)
    path = save_snapshot(document, tmp_path / "lambda.json")
    assert SECRET.encode() not in path.read_bytes()
    loaded = load_snapshot(path)
    assert loaded == document
    assert isinstance(loaded.regions[REGION].resources[0], LambdaFunction)
    assert loaded.evidence == result.evidence


def test_direct_model_configuration_is_projected_again_on_save(tmp_path):
    result = collect(Client())
    item = result.resources[0]
    item.configuration["Environment"] = {"Variables": {"NORMAL": SECRET}}
    item.configuration["Description"] = SECRET
    item.configuration["Code"] = {"Location": SECRET}
    item.attributes["Environment"] = {"Variables": {"NORMAL": SECRET}}
    document = snapshot(result)
    path = save_snapshot(document, tmp_path / "lambda-direct.json")
    assert SECRET.encode() not in path.read_bytes()


@pytest.mark.parametrize("functions", [None, "bad", {"FunctionArn": ARN}, [None]])
def test_malformed_function_container_or_entry_does_not_become_empty_success(functions):
    result = collect(Client(pages=[{"Functions": functions}]))
    assert result.resources == []
    assert coverage(result)["ListFunctions"].status == CoverageStatus.INCOMPLETE
    assert coverage(result)["ListFunctions"].unresolved_values
    assert coverage(result)["GetFunctionConfiguration"].status == CoverageStatus.SKIPPED


def test_repeated_inventory_entry_has_one_detail_read_and_retains_page_evidence():
    client = Client(pages=[{"Functions": [configuration(), configuration()]}])
    result = collect(client)
    assert len(result.resources) == 1
    assert len([call for call in client.calls if call[0] == "get_function_configuration"]) == 1
    paths = [result.evidence[e].configuration_path for e in result.resources[0].evidence_ids]
    assert "Request[{}].Page[0].Functions[0]" in paths
    assert "Request[{}].Page[0].Functions[1]" in paths


def test_mismatched_detail_identity_does_not_overwrite_inventory():
    result = collect(Client(detail={**configuration(ARN + "2"), "MemorySize": 512}))
    assert len(result.resources) == 1
    assert result.resources[0].arn == ARN
    assert result.resources[0].memory_size == 256
    assert coverage(result)["GetFunctionConfiguration"].status == CoverageStatus.INCOMPLETE
    assert coverage(result)["GetFunctionConfiguration"].unresolved_values[0].configuration_path == (
        f'Request[{{"FunctionName":"{ARN}"}}].Page[0].FunctionArn'
    )


def test_image_function_runtime_can_be_unavailable_and_empty_vpc_is_observed():
    detail = configuration()
    detail.pop("Runtime")
    detail["PackageType"] = "Image"
    detail["VpcConfig"] = {"VpcId": "", "SubnetIds": [], "SecurityGroupIds": []}
    result = collect(Client(pages=[{"Functions": [detail]}], detail=detail))
    item = result.resources[0]
    assert item.runtime is None and item.package_type == "Image"
    assert item.vpc_id == "" and item.subnet_ids == [] and item.security_group_ids == []
    assert all(c.status == CoverageStatus.SUCCESS for c in result.coverage)
    assert result.evidence[item.fact_evidence["/subnet_ids"][0]].value == []


def test_some_detail_reads_fail_in_one_aggregate_without_losing_inventory():
    class MixedClient(Client):
        def get_function_configuration(self, **params):
            if params["FunctionName"] == ARN:
                raise denied()
            return configuration(params["FunctionName"])

    result = collect(
        MixedClient(pages=[{"Functions": [configuration(), configuration(ARN + "2")]}])
    )
    assert len(result.resources) == 2 and len(result.coverage) == 2
    detail = coverage(result)["GetFunctionConfiguration"]
    assert detail.status == CoverageStatus.INCOMPLETE and detail.result_count == 1
    assert len(detail.failures) == 1
    assert all(r.role_arn == ROLE for r in result.resources)
