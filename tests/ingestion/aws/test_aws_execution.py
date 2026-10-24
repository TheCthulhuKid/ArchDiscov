import threading
from datetime import UTC, datetime, timedelta

import boto3
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from botocore.stub import Stubber

from archdiscov.ingestion.adapter import AdapterContext, IngestionError
from archdiscov.ingestion.aws import (
    ALLOWED_OPERATIONS,
    create_session,
    sanitize_failure,
    verify_identity,
)
from archdiscov.models.enums import AwsRegion, AwsService, CoverageStatus
from tests.support.aws_execution import (
    ACCOUNT,
    FakeClient,
    FakeSession,
    account,
    operation_runner,
    task,
)

SECRET = "AKIASECRETVALUE/very-secret"


class Clock:
    def __init__(self):
        self.now = datetime(2025, 1, 1, tzinfo=UTC)

    def __call__(self):
        self.now += timedelta(seconds=1)
        return self.now


class Tracker:
    def __init__(self):
        self.lock = threading.Lock()
        self.current = 0
        self.max = 0

    def enter(self):
        with self.lock:
            self.current += 1
            self.max = max(self.max, self.current)

    def exit(self):
        with self.lock:
            self.current -= 1


def client_error(code="Throttling", message=SECRET):
    return ClientError({"Error": {"Code": code, "Message": message}}, "DescribeVpcs")


# --- identity ---------------------------------------------------------------


def test_identity_success_with_stubber():
    sts = boto3.client(
        "sts", region_name="us-east-1", aws_access_key_id="a", aws_secret_access_key="b"
    )
    with Stubber(sts) as stub:
        stub.add_response(
            "get_caller_identity",
            {"Account": ACCOUNT, "UserId": "U", "Arn": "arn:aws:iam::123456789012:user/u"},
        )
        assert verify_identity(FakeSession(sts), account()) == ACCOUNT


def test_identity_mismatch_aborts():
    sts = boto3.client(
        "sts", region_name="us-east-1", aws_access_key_id="a", aws_secret_access_key="b"
    )
    with Stubber(sts) as stub:
        stub.add_response(
            "get_caller_identity",
            {"Account": "999999999999", "UserId": "U", "Arn": "arn:aws:iam::999999999999:user/u"},
        )
        with pytest.raises(IngestionError) as exc:
            verify_identity(FakeSession(sts), account())
    assert exc.value.code == "account_mismatch"


def test_identity_client_error_is_sanitized():
    sts = boto3.client(
        "sts", region_name="us-east-1", aws_access_key_id="a", aws_secret_access_key="b"
    )
    with Stubber(sts) as stub:
        stub.add_client_error("get_caller_identity", "ExpiredToken", SECRET)
        with pytest.raises(IngestionError) as exc:
            verify_identity(FakeSession(sts), account())
    assert exc.value.code == "identity_failed"
    assert SECRET not in exc.value.message


def test_profile_session_construction_and_client_config():
    seen = []

    def factory(**kwargs):
        seen.append(kwargs)
        return FakeSession(FakeClient())

    cfg = account(connect_timeout_seconds=2, read_timeout_seconds=7, max_attempts=5)
    session = create_session(cfg, AdapterContext(session_factory=factory))
    assert seen == [{"profile_name": "dev"}]
    verify_identity(session, cfg)
    botocore_config = session.client_calls[0][1]["config"]
    assert botocore_config.connect_timeout == 2
    assert botocore_config.read_timeout == 7
    assert botocore_config.retries == {"max_attempts": 5, "mode": "standard"}


def test_assume_role_session_is_verified_not_base_profile():
    class AssumeClient:
        def __init__(self):
            self.params = None

        def assume_role(self, **params):
            self.params = params
            return {
                "Credentials": {"AccessKeyId": "AK", "SecretAccessKey": "SK", "SessionToken": "T"}
            }

    assume = AssumeClient()
    seen = []

    class AssumedClient:
        def get_caller_identity(self):
            return {"Account": "210987654321"}

    def factory(**kwargs):
        seen.append(kwargs)
        if "profile_name" in kwargs:
            return FakeSession(clients={"sts": assume})
        return FakeSession(clients={"sts": AssumedClient()})

    cfg = account(
        expected_account_id="210987654321",
        role_arn="arn:aws:iam::210987654321:role/Reader",
        external_id="ext",
        session_name="sess",
    )
    session = create_session(cfg, AdapterContext(session_factory=factory))
    assert assume.params == {
        "RoleArn": "arn:aws:iam::210987654321:role/Reader",
        "RoleSessionName": "sess",
        "ExternalId": "ext",
    }
    assert seen[1] == {
        "aws_access_key_id": "AK",
        "aws_secret_access_key": "SK",
        "aws_session_token": "T",
        "region_name": "us-east-1",
    }
    assert verify_identity(session, cfg) == "210987654321"


def test_assume_role_failure_is_identity_failed():
    class Failing:
        def assume_role(self, **params):
            raise client_error("AccessDenied")

    def factory(**kwargs):
        return FakeSession(clients={"sts": Failing()})

    cfg = account(role_arn="arn:aws:iam::123456789012:role/R")
    with pytest.raises(IngestionError) as exc:
        create_session(cfg, AdapterContext(session_factory=factory))
    assert exc.value.code == "identity_failed"
    assert SECRET not in str(exc.value)


# --- allowlist ---------------------------------------------------------------


def test_disallowed_operation_rejected_before_any_client_call():
    client = FakeClient(pages=[])
    run, session = operation_runner(client)
    with pytest.raises(IngestionError) as exc:
        run.run(task(operation="DeleteVpc"))
    assert exc.value.code == "operation_not_allowed"
    assert session.client_calls == []
    assert client.calls == 0


def test_run_many_rejects_disallowed_before_running_any_task():
    client = FakeClient(pages=[{"Vpcs": []}])
    run, session = operation_runner(client)
    with pytest.raises(IngestionError):
        run.run_many([task(), task(region=AwsRegion.US_WEST_2, operation="TerminateInstances")])
    assert session.client_calls == []


def test_allowlist_contains_only_read_and_identity_operations():
    from archdiscov.ingestion.operations import IamOperation

    assert all(
        op.startswith(("Describe", "Get", "List", "AssumeRole")) for _, op in ALLOWED_OPERATIONS
    )
    assert {op for service, op in ALLOWED_OPERATIONS if service == AwsService.IAM} == {
        op.value for op in IamOperation
    }


# --- runner -------------------------------------------------------------------


def test_multi_page_success():
    client = FakeClient(
        pages=[{"Vpcs": [{"VpcId": "a"}]}, {"Vpcs": [{"VpcId": "b"}, {"VpcId": "c"}]}]
    )
    run, _ = operation_runner(client)
    result = run.run(task())
    assert [i["VpcId"] for i in result.items] == ["a", "b", "c"]
    assert result.coverage.status == CoverageStatus.SUCCESS
    assert result.coverage.result_count == 3


def test_success_with_zero_results():
    run, _ = operation_runner(FakeClient(pages=[{"Vpcs": []}]))
    result = run.run(task())
    assert result.coverage.status == CoverageStatus.SUCCESS
    assert result.coverage.result_count == 0


def test_first_call_failure_is_failed_and_sanitized():
    run, _ = operation_runner(FakeClient(pages=[client_error()]))
    result = run.run(task())
    assert result.items == []
    assert result.coverage.status == CoverageStatus.FAILED
    assert result.coverage.result_count is None
    dumped = result.coverage.model_dump_json()
    assert SECRET not in dumped
    assert result.coverage.failures[0].code == "Throttling"


def test_failure_after_first_page_is_incomplete_with_retained_items():
    run, _ = operation_runner(FakeClient(pages=[{"Vpcs": [{"VpcId": "a"}]}, client_error()]))
    result = run.run(task())
    assert [i["VpcId"] for i in result.items] == ["a"]
    assert result.coverage.status == CoverageStatus.INCOMPLETE
    assert result.coverage.result_count == 1
    assert SECRET not in result.coverage.model_dump_json()


def test_non_paginated_call_and_exhausted_retries():
    run, _ = operation_runner(FakeClient(error=EndpointConnectionError(endpoint_url=SECRET)))
    result = run.run(task())
    assert result.coverage.status == CoverageStatus.FAILED
    assert result.coverage.failures[0].code == "EndpointConnectionError"
    assert SECRET not in result.coverage.model_dump_json()


def test_unknown_exception_text_not_leaked():
    failure = sanitize_failure(RuntimeError(SECRET), "DescribeVpcs")
    assert failure.code == "UnexpectedError"
    assert SECRET not in failure.model_dump_json()


def test_unsafe_client_error_code_is_replaced():
    run, _ = operation_runner(FakeClient(pages=[client_error(code=f"Bad {SECRET}")]))
    assert run.run(task()).coverage.failures[0].code == "ClientError"


def test_non_paginated_success_uses_single_call():
    run, _ = operation_runner(FakeClient())
    result = run.run(task())
    assert result.coverage.result_count == 1


def test_skipped_and_unsupported_outcomes():
    run, _ = operation_runner(FakeClient())
    skipped = run.skip(task(), "service not selected")
    unsupported = run.unsupported(task(operation="DescribeSubnets"), "not in region")
    assert skipped.coverage.status == CoverageStatus.SKIPPED
    assert unsupported.coverage.status == CoverageStatus.UNSUPPORTED
    assert skipped.coverage.reason == "service not selected"


def test_duplicate_coverage_record_rejected():
    run, _ = operation_runner(FakeClient(pages=[{"Vpcs": []}]))
    run.run(task())
    with pytest.raises(IngestionError) as exc:
        run.run(task())
    assert exc.value.code == "invalid_request"


def test_bounded_concurrency_and_deterministic_order():
    tracker = Tracker()
    run, _ = operation_runner(FakeClient(delay=0.02, tracker=tracker), max_concurrency=2)
    regions = [AwsRegion.US_WEST_2, AwsRegion.EU_WEST_1, AwsRegion.US_EAST_1, AwsRegion.AP_SOUTH_1]
    results = run.run_many(
        task(region=r, params={}, paginate=False, result_key="Vpcs") for r in regions
    )
    assert 1 <= tracker.max <= 2
    assert [r.coverage.region.value for r in results] == sorted(r.value for r in regions)
    assert all(r.coverage.status == CoverageStatus.SUCCESS for r in results)


def test_concurrency_of_one_is_serial():
    tracker = Tracker()
    run, _ = operation_runner(FakeClient(delay=0.01, tracker=tracker), max_concurrency=1)
    run.run_many(task(region=r, paginate=False) for r in (AwsRegion.US_WEST_2, AwsRegion.US_EAST_1))
    assert tracker.max == 1


def test_stubber_paginated_describe_vpcs():
    ec2 = boto3.client(
        "ec2", region_name="us-east-1", aws_access_key_id="a", aws_secret_access_key="b"
    )
    with Stubber(ec2) as stub:
        stub.add_response("describe_vpcs", {"Vpcs": [{"VpcId": "vpc-1"}], "NextToken": "n"})
        stub.add_response("describe_vpcs", {"Vpcs": [{"VpcId": "vpc-2"}]})
        run, _ = operation_runner(ec2)
        result = run.run(task())
    assert [v["VpcId"] for v in result.items] == ["vpc-1", "vpc-2"]
    assert result.coverage.result_count == 2


def test_stubber_failure_after_first_page_is_incomplete():
    ec2 = boto3.client(
        "ec2", region_name="us-east-1", aws_access_key_id="a", aws_secret_access_key="b"
    )
    with Stubber(ec2) as stub:
        stub.add_response("describe_vpcs", {"Vpcs": [{"VpcId": "vpc-1"}], "NextToken": "n"})
        stub.add_client_error("describe_vpcs", "RequestLimitExceeded", SECRET)
        run, _ = operation_runner(ec2)
        result = run.run(task())
    assert result.coverage.status == CoverageStatus.INCOMPLETE
    assert [v["VpcId"] for v in result.items] == ["vpc-1"]
    assert result.coverage.failures[0].code == "RequestLimitExceeded"
    assert SECRET not in result.coverage.model_dump_json()
