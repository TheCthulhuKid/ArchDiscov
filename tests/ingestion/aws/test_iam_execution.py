from datetime import UTC, datetime

import boto3
from botocore.stub import Stubber

from archdiscov.ingestion.adapter import AdapterContext
from archdiscov.ingestion.iam.execution import IamCall, IamRunner
from archdiscov.ingestion.operations import IamOperation
from archdiscov.ingestion.requests import AwsAccountConfig
from archdiscov.models.enums import AwsRegion, AwsService, CoverageStatus
from tests.support.aws_execution import Clock, Session

ACCOUNT = "123456789012"


def role(name):
    return {
        "Path": "/",
        "RoleName": name,
        "RoleId": "AROA12345678901234567",
        "Arn": f"arn:aws:iam::{ACCOUNT}:role/{name}",
        "CreateDate": datetime(2025, 1, 1, tzinfo=UTC),
    }


def test_real_stubber_pagination_retains_entries_and_exact_paths_on_one_client():
    client = boto3.client("iam", aws_access_key_id="a", aws_secret_access_key="b")
    session, clock = Session(client), Clock()
    account = AwsAccountConfig(profile="dev", expected_account_id=ACCOUNT, services=["iam"])
    run = IamRunner(session, account, AdapterContext(clock=clock))
    call = IamCall(IamOperation.LIST_ROLES, {"PathPrefix": "/", "MaxItems": 2}, "Roles", True)
    with Stubber(client) as stub:
        stub.add_response(
            "list_roles",
            {"Roles": [role("one"), role("two")], "IsTruncated": True, "Marker": "next"},
            {"PathPrefix": "/", "MaxItems": 2},
        )
        stub.add_response(
            "list_roles",
            {"Roles": [role("three")], "IsTruncated": False},
            {"PathPrefix": "/", "MaxItems": 2, "Marker": "next"},
        )
        stub.add_response("get_role", {"Role": role("one")}, {"RoleName": "one"})
        result = run.run(IamOperation.LIST_ROLES, [call])
        run.run(IamOperation.GET_ROLE, [IamCall(IamOperation.GET_ROLE, {"RoleName": "one"})])
        stub.assert_no_pending_responses()
    assert [entry.value["RoleName"] for entry in result.responses] == ["one", "two", "three"]
    prefix = 'Request[{"MaxItems":2,"PathPrefix":"/"}]'
    assert [entry.path for entry in result.responses] == [
        f"{prefix}.Page[0].Roles[0]",
        f"{prefix}.Page[0].Roles[1]",
        f"{prefix}.Page[1].Roles[0]",
    ]
    assert all(entry.call is call for entry in result.responses)
    assert result.coverage.status == CoverageStatus.SUCCESS
    assert result.coverage.result_count == 3
    assert result.coverage.region == AwsRegion.GLOBAL
    assert result.coverage.service == AwsService.IAM
    assert result.coverage.account_id == ACCOUNT
    assert result.coverage.operation == "ListRoles"
    assert result.coverage.started_at == min(clock.values)
    # One client serves every operation, created without a region for global IAM.
    assert len(session.calls) == 1
    assert session.calls[0][0] == "iam"
    assert "region_name" not in session.calls[0][1]
    assert call.params == {"PathPrefix": "/", "MaxItems": 2}
