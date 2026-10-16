import boto3
import pytest
from botocore.stub import Stubber

from archdiscov.ingestion import execution_common
from archdiscov.ingestion.adapter import AdapterContext, IngestionError
from archdiscov.ingestion.detail_execution import DetailCall, DetailRunner
from archdiscov.ingestion.requests import AwsAccountConfig
from archdiscov.models.enums import AwsRegion, AwsService, CoverageStatus

from .test_iam_execution import Clock, Session


def runner(client, monkeypatch):
    # The collector's operation entries are introduced in the later discovery stage.
    monkeypatch.setattr(
        execution_common,
        "ALLOWED_OPERATIONS",
        execution_common.ALLOWED_OPERATIONS
        | {(AwsService.ELASTIC_LOAD_BALANCING, "DescribeLoadBalancers")},
    )
    session = Session(client)
    account = AwsAccountConfig(
        profile="dev",
        expected_account_id="123456789012",
        regions=("us-east-1",),
        services=(AwsService.ELASTIC_LOAD_BALANCING,),
    )
    return DetailRunner(
        session,
        account,
        AdapterContext(clock=Clock()),
        service=AwsService.ELASTIC_LOAD_BALANCING,
        region=AwsRegion.US_EAST_1,
        client_name="elbv2",
        pagination="elbv2",
    ), session


@pytest.mark.parametrize("fail_second", [False, True])
def test_real_elbv2_next_marker_and_retained_empty_page(monkeypatch, fail_second):
    client = boto3.client(
        "elbv2", region_name="us-east-1", aws_access_key_id="a", aws_secret_access_key="b"
    )
    run, session = runner(client, monkeypatch)
    with Stubber(client) as stub:
        stub.add_response(
            "describe_load_balancers", {"LoadBalancers": [], "NextMarker": "next"}, {}
        )
        if fail_second:
            stub.add_client_error(
                "describe_load_balancers",
                "AccessDenied",
                "protected-secret",
                expected_params={"Marker": "next"},
            )
        else:
            stub.add_response(
                "describe_load_balancers",
                {"LoadBalancers": [{"LoadBalancerName": "retained"}]},
                {"Marker": "next"},
            )
        result = run.run(
            "DescribeLoadBalancers",
            [DetailCall("DescribeLoadBalancers", result_key="LoadBalancers", paginate=True)],
        )
        stub.assert_no_pending_responses()
    assert result.coverage.status == (
        CoverageStatus.INCOMPLETE if fail_second else CoverageStatus.SUCCESS
    )
    assert result.coverage.result_count == (0 if fail_second else 1)
    assert result.coverage.region == "us-east-1"
    assert session.calls[0][1]["region_name"] == "us-east-1"
    assert "protected-secret" not in result.coverage.model_dump_json()
    if not fail_second:
        assert result.responses[0].path == "Request[{}].Page[1].LoadBalancers[0]"


def test_marker_excluded_from_request_identity_and_duplicates(monkeypatch):
    class Client:
        def describe_load_balancers(self, **params):
            assert params == {"Marker": "start"}
            return {"LoadBalancers": [{"LoadBalancerName": "one"}]}

    run, _ = runner(Client(), monkeypatch)
    call = DetailCall("DescribeLoadBalancers", {"Marker": "start"}, "LoadBalancers", True)
    result = run.run("DescribeLoadBalancers", [call])
    assert result.responses[0].path == "Request[{}].Page[0].LoadBalancers[0]"
    run, session = runner(None, monkeypatch)
    with pytest.raises(IngestionError, match="duplicate"):
        run.run("DescribeLoadBalancers", [call, DetailCall("DescribeLoadBalancers")])
    assert session.calls == []


def test_repeated_elbv2_marker_keeps_pages(monkeypatch):
    class Client:
        def describe_load_balancers(self, **params):
            return {"LoadBalancers": [], "NextMarker": "same"}

    run, _ = runner(Client(), monkeypatch)
    result = run.run(
        "DescribeLoadBalancers",
        [DetailCall("DescribeLoadBalancers", result_key="LoadBalancers", paginate=True)],
    )
    assert result.coverage.status == CoverageStatus.INCOMPLETE
    assert result.coverage.result_count == 0
    assert len(result.coverage.failures) == 1


@pytest.mark.parametrize(
    "service,client_name,policy,operation,method,key,token_in,token_out",
    [
        (
            AwsService.ECS,
            "ecs",
            "nextToken",
            "ListClusters",
            "list_clusters",
            "clusterArns",
            "nextToken",
            "nextToken",
        ),
        (
            AwsService.LAMBDA,
            "lambda",
            "lambda",
            "ListFunctions",
            "list_functions",
            "Functions",
            "Marker",
            "NextMarker",
        ),
        (
            AwsService.RDS,
            "rds",
            "rds",
            "DescribeDBInstances",
            "describe_db_instances",
            "DBInstances",
            "Marker",
            "Marker",
        ),
        (
            AwsService.SQS,
            "sqs",
            "sqs",
            "ListQueues",
            "list_queues",
            "QueueUrls",
            "NextToken",
            "NextToken",
        ),
    ],
)
def test_service_pagination_and_sdk_names(
    service, client_name, policy, operation, method, key, token_in, token_out
):
    class Client:
        def __getattr__(self, name):
            assert name == method

            def read(**params):
                if not params:
                    return {key: ["first"], token_out: "next"}
                assert params == {token_in: "next"}
                return {key: ["second"]}

            return read

    run = DetailRunner(
        Session(Client()),
        AwsAccountConfig(
            profile="dev",
            expected_account_id="123456789012",
            regions=("us-east-1",),
            services=(service,),
        ),
        AdapterContext(clock=Clock()),
        service=service,
        region=AwsRegion.US_EAST_1,
        client_name=client_name,
        pagination=policy,
    )
    result = run.run(operation, [DetailCall(operation, result_key=key, paginate=True)])
    assert result.coverage.status == CoverageStatus.SUCCESS
    assert [response.value for response in result.responses] == ["first", "second"]
    assert result.responses[1].path == f"Request[{{}}].Page[1].{key}[0]"


def test_missing_result_collection_is_not_successful_zero(monkeypatch):
    class Client:
        def describe_load_balancers(self):
            return {}

    run, _ = runner(Client(), monkeypatch)
    result = run.run(
        "DescribeLoadBalancers", [DetailCall("DescribeLoadBalancers", result_key="LoadBalancers")]
    )
    assert result.coverage.status == CoverageStatus.INCOMPLETE
    assert result.coverage.failures
    assert result.coverage.result_count == 0
