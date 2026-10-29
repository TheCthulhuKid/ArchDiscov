import pytest
from botocore.exceptions import EndpointConnectionError

from archdiscov.ingestion.detail_execution import DetailCall
from archdiscov.models.enums import CoverageStatus
from tests.support.aws_execution import FakeClient, detail_runner, operation_runner, task


def test_programming_errors_propagate_from_both_runners(monkeypatch):
    runner, _ = operation_runner(FakeClient(error=TypeError("implementation defect")))
    with pytest.raises(TypeError, match="implementation defect"):
        runner.run(task())

    class Client:
        def describe_load_balancers(self, **params):
            raise TypeError("implementation defect")

    runner, _ = detail_runner(Client(), monkeypatch)
    with pytest.raises(TypeError, match="implementation defect"):
        runner.run("DescribeLoadBalancers", [DetailCall("DescribeLoadBalancers")])


def test_first_detail_call_failure_is_failed_and_safe(monkeypatch):
    class Client:
        def describe_load_balancers(self, **params):
            raise EndpointConnectionError(endpoint_url="https://protected-secret")

    runner, _ = detail_runner(Client(), monkeypatch)
    result = runner.run("DescribeLoadBalancers", [DetailCall("DescribeLoadBalancers")])
    assert result.coverage.status is CoverageStatus.FAILED
    assert result.coverage.failures[0].code == "EndpointConnectionError"
    assert "protected-secret" not in result.coverage.model_dump_json()


def test_missing_operation_result_list_is_a_safe_failure():
    runner, _ = operation_runner(FakeClient(pages=[{}]))
    result = runner.run(task())
    assert result.coverage.status is CoverageStatus.INCOMPLETE
    assert result.items == []
    assert result.coverage.failures[0].code == "MalformedResponse"
