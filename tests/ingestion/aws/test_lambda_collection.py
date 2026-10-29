from datetime import UTC, datetime
from uuid import UUID

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber
from pydantic import ValidationError

from archdiscov.ingestion.adapter import AdapterContext
from archdiscov.ingestion.configuration_common import ServiceCollection
from archdiscov.ingestion.functions import collect_lambda
from archdiscov.ingestion.requests import AwsAccountConfig
from archdiscov.models.architecture import ArchitectureSnapshot
from archdiscov.models.enums import CoverageStatus, EvidenceValueStatus
from archdiscov.models.resources.functions import LambdaFunction, safe_configuration
from tests.support.lambda_data import ACCOUNT, ARN, REGION, SECRET, Client, configuration

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


def function(**fields):
    return LambdaFunction(
        identity={
            "account_id": ACCOUNT,
            "region": REGION,
            "service": "lambda",
            "physical_id": ARN,
        },
        arn=ARN,
        **fields,
    )


def test_real_stubber_exact_requests_and_field_citations():
    api = boto3.client(
        "lambda", region_name=REGION, aws_access_key_id="offline", aws_secret_access_key="offline"
    )
    second = ARN + "2"
    with Stubber(api) as stub:
        stub.add_response(
            "list_functions", {"Functions": [configuration(), configuration(second)]}, {}
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
    assert any(e.configuration_path == "Request[{}].Page[0].Functions[1]" for e in whole)
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


@pytest.mark.parametrize(
    "page,status",
    [
        (denied(), CoverageStatus.FAILED),
        ({"Functions": [{"FunctionArn": SECRET}]}, CoverageStatus.INCOMPLETE),
    ],
    ids=["failed", "malformed-identity"],
)
def test_unusable_discovery_skips_detail_without_claiming_empty_success(page, status):
    client = Client(pages=[page])
    result = collect(client)
    assert result.resources == []
    assert coverage(result)["ListFunctions"].status == status
    assert coverage(result)["GetFunctionConfiguration"].status == CoverageStatus.SKIPPED
    assert len(client.calls) == 1
    assert SECRET not in snapshot(result).model_dump_json()


def test_malformed_detail_value_is_unavailable_and_precisely_reported():
    detail = {**configuration(), "State": {"raw": SECRET}}
    result = collect(Client(detail=detail))
    detail_coverage = coverage(result)["GetFunctionConfiguration"]
    assert detail_coverage.status == CoverageStatus.INCOMPLETE
    assert any(
        u.configuration_path == f'Request[{{"FunctionName":"{ARN}"}}].Page[0].State'
        for u in detail_coverage.unresolved_values
    )
    assert result.resources[0].state is None
    assert result.resources[0].memory_size == 256
    assert SECRET not in snapshot(result).model_dump_json()


def test_projection_excludes_secret_and_unknown_source_fields():
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
    assert SECRET not in snapshot(result).model_dump_json()


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


def test_direct_configuration_projection_never_copies_ordinary_named_environment_values():
    source = {
        **configuration(),
        "Environment": {"Variables": {"APP_MODE": SECRET}},
        "Description": SECRET,
        "Code": {"Location": SECRET},
        "Future": SECRET,
    }
    item = function(configuration=source)
    assert SECRET not in item.model_dump_json()
    assert item.configuration == configuration()
    safe, paths = safe_configuration(source)
    assert safe == configuration()
    assert "/Environment" in paths
    assert source["Environment"]["Variables"]["APP_MODE"] == SECRET


def test_arn_identity_must_match_and_typed_facts_are_validated():
    data = function().model_dump()
    data["arn"] = ARN + "2"
    with pytest.raises(ValidationError):
        LambdaFunction.model_validate(data)
    with pytest.raises(ValidationError, match="Malformed or unsupported Lambda configuration"):
        function(role_arn="https://example.invalid/secret")
