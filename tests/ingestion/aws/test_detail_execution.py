import threading

import boto3
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from botocore.stub import Stubber

from archdiscov.ingestion.adapter import AdapterContext, IngestionError
from archdiscov.ingestion.detail_execution import DetailCall, DetailRunner
from archdiscov.ingestion.requests import AwsAccountConfig
from archdiscov.models.enums import AwsRegion, AwsService, CoverageStatus
from tests.support.aws_execution import Clock, Session, detail_runner


def load_balancers(run, **call):
    return run.run(
        "DescribeLoadBalancers",
        [DetailCall("DescribeLoadBalancers", result_key="LoadBalancers", **call)],
    )


@pytest.mark.parametrize("fail_second", [False, True])
def test_real_elbv2_next_marker_and_failed_later_page_keeps_earlier_pages(monkeypatch, fail_second):
    client = boto3.client(
        "elbv2", region_name="us-east-1", aws_access_key_id="a", aws_secret_access_key="b"
    )
    run, session = detail_runner(client, monkeypatch)
    with Stubber(client) as stub:
        stub.add_response(
            "describe_load_balancers",
            {"LoadBalancers": [{"LoadBalancerName": "first"}], "NextMarker": "next"},
            {},
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
                {"LoadBalancers": [{"LoadBalancerName": "second"}]},
                {"Marker": "next"},
            )
        result = load_balancers(run, paginate=True)
        stub.assert_no_pending_responses()
    assert result.coverage.status == (
        CoverageStatus.INCOMPLETE if fail_second else CoverageStatus.SUCCESS
    )
    assert result.coverage.result_count == (1 if fail_second else 2)
    assert result.responses[0].value == {"LoadBalancerName": "first"}
    assert result.coverage.region == "us-east-1"
    assert session.calls[0][1]["region_name"] == "us-east-1"
    assert "protected-secret" not in result.coverage.model_dump_json()
    if not fail_second:
        assert result.responses[1].path == "Request[{}].Page[1].LoadBalancers[0]"


def test_request_identity_excludes_marker_and_bad_batches_create_no_client(monkeypatch):
    class Client:
        def describe_load_balancers(self, **params):
            assert params == {"Marker": "start"}
            return {"LoadBalancers": [{"LoadBalancerName": "one"}]}

    run, _ = detail_runner(Client(), monkeypatch)
    call = DetailCall("DescribeLoadBalancers", {"Marker": "start"}, "LoadBalancers", True)
    result = run.run("DescribeLoadBalancers", [call])
    assert result.responses[0].path == "Request[{}].Page[0].LoadBalancers[0]"
    run, session = detail_runner(None, monkeypatch)
    with pytest.raises(IngestionError, match="duplicate"):
        run.run("DescribeLoadBalancers", [call, DetailCall("DescribeLoadBalancers")])
    for operation in ("DeleteLoadBalancer", "DescribeLoadBalancers"):
        with pytest.raises(IngestionError) as exc:
            run.run(operation, [DetailCall("DeleteLoadBalancer")])
        assert exc.value.code == "operation_not_allowed"
    assert session.calls == []


def test_repeated_marker_stops_with_observed_pages_and_incomplete_coverage(monkeypatch):
    class Client:
        def describe_load_balancers(self, **params):
            return {"LoadBalancers": [{"LoadBalancerName": "page"}], "NextMarker": "same"}

    run, _ = detail_runner(Client(), monkeypatch)
    result = load_balancers(run, paginate=True)
    assert result.coverage.status == CoverageStatus.INCOMPLETE
    assert result.coverage.result_count == 2
    assert [failure.code for failure in result.coverage.failures] == ["MalformedResponse"]


def test_token_pagination_uses_the_service_policy_and_sdk_name():
    class Client:
        def list_clusters(self, **params):
            if not params:
                return {"clusterArns": ["first"], "nextToken": "next"}
            assert params == {"nextToken": "next"}
            return {"clusterArns": ["second"]}

    run = DetailRunner(
        Session(Client()),
        AwsAccountConfig(
            profile="dev",
            expected_account_id="123456789012",
            regions=("us-east-1",),
            services=(AwsService.ECS,),
        ),
        AdapterContext(clock=Clock()),
        service=AwsService.ECS,
        region=AwsRegion.US_EAST_1,
        client_name="ecs",
        pagination="nextToken",
    )
    result = run.run(
        "ListClusters", [DetailCall("ListClusters", result_key="clusterArns", paginate=True)]
    )
    assert result.coverage.status == CoverageStatus.SUCCESS
    assert [response.value for response in result.responses] == ["first", "second"]
    assert result.responses[1].path == "Request[{}].Page[1].clusterArns[0]"


@pytest.mark.parametrize(
    "page,status",
    [({"LoadBalancers": []}, CoverageStatus.SUCCESS), ({}, CoverageStatus.INCOMPLETE)],
    ids=["empty-is-zero", "missing-is-not"],
)
def test_empty_result_is_successful_zero_but_missing_result_is_not(monkeypatch, page, status):
    class Client:
        def describe_load_balancers(self):
            return page

    result = load_balancers(detail_runner(Client(), monkeypatch)[0])
    assert result.coverage.status == status
    assert result.coverage.result_count == 0
    assert bool(result.coverage.failures) is (status == CoverageStatus.INCOMPLETE)


@pytest.mark.parametrize(
    ("result_key", "path"),
    [(None, "Request[{}].Page[0]"), ("LoadBalancer", "Request[{}].Page[0].LoadBalancer")],
    ids=["whole-page", "single-value"],
)
def test_a_page_or_a_single_value_is_one_response(monkeypatch, result_key, path):
    page = {"LoadBalancer": {"LoadBalancerName": "one"}}

    class Client:
        def describe_load_balancers(self):
            return page

    run, _ = detail_runner(Client(), monkeypatch)
    call = DetailCall("DescribeLoadBalancers", result_key=result_key)
    result = run.run("DescribeLoadBalancers", [call])
    assert result.coverage.result_count == 1
    assert result.responses[0].value == (page if result_key is None else page[result_key])
    assert result.responses[0].path == path


class NamedClient:
    """Answers each balancer name with its balancers, or raises its error."""

    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls = []

    def describe_load_balancers(self, **params):
        self.calls.append(params)
        value = self.outcomes[params["Names"][0]]
        if isinstance(value, Exception):
            raise value
        return {"LoadBalancers": value}


def named(*names):
    return [
        DetailCall("DescribeLoadBalancers", {"Names": [name]}, "LoadBalancers") for name in names
    ]


DENIED = ClientError(
    {"Error": {"Code": "AccessDenied", "Message": "protected-secret"}}, "DescribeLoadBalancers"
)
BALANCER = {"LoadBalancerName": "a"}
PARTIAL = "balancer listing incomplete"


@pytest.mark.parametrize(
    ("outcomes", "status", "count", "failures"),
    [
        ({"a": DENIED, "b": DENIED}, CoverageStatus.FAILED, None, 2),
        ({"a": [], "b": []}, CoverageStatus.SUCCESS, 0, 0),
        ({"a": [BALANCER], "b": DENIED}, CoverageStatus.INCOMPLETE, 1, 1),
    ],
    ids=["all-failed", "all-empty", "one-failed"],
)
def test_calls_with_different_parameters_are_one_aggregate_in_parameter_order(
    monkeypatch, outcomes, status, count, failures
):
    client = NamedClient(outcomes)
    run, _ = detail_runner(client, monkeypatch, max_concurrency=1)
    calls = named("b", "a")
    result = run.run("DescribeLoadBalancers", calls)
    assert client.calls == [{"Names": ["a"]}, {"Names": ["b"]}]
    assert result.coverage.status == status
    assert result.coverage.result_count == count
    assert len(result.coverage.failures) == failures
    if result.responses:
        assert result.responses[0].call is calls[1]
        assert result.responses[0].path == 'Request[{"Names":["a"]}].Page[0].LoadBalancers[0]'


@pytest.mark.parametrize(
    ("outcome", "prerequisite", "status", "count"),
    [
        (None, None, CoverageStatus.SUCCESS, 0),
        (None, PARTIAL, CoverageStatus.SKIPPED, None),
        ([BALANCER], PARTIAL, CoverageStatus.INCOMPLETE, 1),
        (DENIED, PARTIAL, CoverageStatus.FAILED, None),
    ],
    ids=["no-calls", "no-calls-after-partial", "read-after-partial", "failed-after-partial"],
)
def test_a_prerequisite_qualifies_reads_not_failures_and_no_calls_create_no_client(
    monkeypatch, outcome, prerequisite, status, count
):
    calls = [] if outcome is None else named("a")
    run, session = detail_runner(NamedClient({"a": outcome}), monkeypatch)
    result = run.run("DescribeLoadBalancers", calls, prerequisite=prerequisite)
    assert result.coverage.status == status
    assert result.coverage.result_count == count
    assert result.coverage.reason == prerequisite
    assert len(session.calls) == len(calls)


def test_a_failed_client_creation_is_a_safe_failure(monkeypatch):
    run, session = detail_runner(
        EndpointConnectionError(endpoint_url="protected-secret"), monkeypatch
    )
    result = load_balancers(run)
    assert result.coverage.status == CoverageStatus.FAILED
    assert result.coverage.result_count is None
    assert "protected-secret" not in result.coverage.model_dump_json()
    assert len(session.calls) == 1


def test_concurrency_is_bounded_and_responses_keep_parameter_order(monkeypatch):
    class Client:
        def __init__(self):
            self.barrier = threading.Barrier(3)
            self.lock = threading.Lock()
            self.active = 0
            self.maximum = 0

        def describe_load_balancers(self, **params):
            with self.lock:
                self.active += 1
                self.maximum = max(self.maximum, self.active)
            try:
                self.barrier.wait(timeout=5)
                return {"LoadBalancers": [{"LoadBalancerName": params["Names"][0]}]}
            finally:
                with self.lock:
                    self.active -= 1

    client = Client()
    run, session = detail_runner(client, monkeypatch, max_concurrency=3)
    names = [str(i) for i in range(9)]
    result = run.run("DescribeLoadBalancers", named(*reversed(names)))
    assert client.maximum == 3
    assert len(session.calls) == 1
    assert result.coverage.status == CoverageStatus.SUCCESS
    assert [item.value["LoadBalancerName"] for item in result.responses] == names
