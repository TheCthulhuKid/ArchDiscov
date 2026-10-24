import pytest
from botocore.exceptions import EndpointConnectionError

from archdiscov.ingestion.detail_execution import DetailCall
from archdiscov.models.enums import CoverageStatus
from tests.support.aws_execution import FakeClient, task
from tests.support.aws_execution import detail_runner as detail_runner
from tests.support.aws_execution import operation_runner as operation_runner


@pytest.mark.parametrize("error_type", [TypeError, ValueError, RuntimeError])
def test_operation_programming_errors_propagate(error_type):
    runner, _ = operation_runner(FakeClient(error=error_type("implementation defect")))
    with pytest.raises(error_type, match="implementation defect"):
        runner.run(task())


@pytest.mark.parametrize("stage", ["client", "call"])
@pytest.mark.parametrize("error_type", [TypeError, ValueError, RuntimeError])
def test_detail_programming_errors_propagate(monkeypatch, stage, error_type):
    class Client:
        def describe_load_balancers(self, **params):
            raise error_type("implementation defect")

    runner, session = detail_runner(Client(), monkeypatch)
    if stage == "client":

        def broken_client(*args, **kwargs):
            raise error_type("implementation defect")

        session.client = broken_client
    with pytest.raises(error_type, match="implementation defect"):
        runner.run("DescribeLoadBalancers", [DetailCall("DescribeLoadBalancers")])


@pytest.mark.parametrize("page", [None, []])
def test_malformed_detail_results_are_safe_failures(monkeypatch, page):
    class Client:
        def describe_load_balancers(self, **params):
            return page

    runner, _ = detail_runner(Client(), monkeypatch)
    result = runner.run(
        "DescribeLoadBalancers",
        [DetailCall("DescribeLoadBalancers", result_key="LoadBalancers")],
    )
    assert result.coverage.status is CoverageStatus.INCOMPLETE
    assert result.responses == []
    assert result.coverage.failures[0].code == "MalformedResponse"


def test_expected_sdk_failures_remain_safe(monkeypatch):
    class Client:
        def describe_load_balancers(self, **params):
            raise EndpointConnectionError(endpoint_url="https://protected-secret")

    runner, _ = detail_runner(Client(), monkeypatch)
    result = runner.run("DescribeLoadBalancers", [DetailCall("DescribeLoadBalancers")])
    assert result.coverage.status is CoverageStatus.FAILED
    assert result.coverage.failures[0].code == "EndpointConnectionError"
    assert "protected-secret" not in result.coverage.model_dump_json()


@pytest.mark.parametrize("page", [None, [], {}, {"Vpcs": None}, {"Vpcs": "not a list"}])
def test_malformed_operation_results_are_safe_failures(page):
    runner, _ = operation_runner(FakeClient(pages=[page]))
    result = runner.run(task())
    assert result.coverage.status is CoverageStatus.INCOMPLETE
    assert result.items == []
    assert result.coverage.failures[0].code == "MalformedResponse"
