import json
import threading
from datetime import UTC, datetime, timedelta

import boto3
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from botocore.stub import Stubber

from archdiscov.ingestion.adapter import AdapterContext, IngestionError
from archdiscov.ingestion.iam.execution import IamCall, IamRunner
from archdiscov.ingestion.operations import IamOperation
from archdiscov.ingestion.requests import AwsAccountConfig
from archdiscov.models.enums import AwsRegion, AwsService, CoverageStatus

ACCOUNT = "123456789012"
SECRET = "AKIASECRETVALUE/very-secret"


class Clock:
    def __init__(self):
        self.now = datetime(2025, 1, 1, tzinfo=UTC)
        self.values = []
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            self.now += timedelta(seconds=1)
            self.values.append(self.now)
            return self.now


class Session:
    def __init__(self, client):
        self.client_obj = client
        self.calls = []

    def client(self, name, **kwargs):
        self.calls.append((name, kwargs))
        if isinstance(self.client_obj, Exception):
            raise self.client_obj
        return self.client_obj


def runner(client, **overrides):
    config = {
        "profile": "dev",
        "expected_account_id": ACCOUNT,
        "services": ["iam"],
        "max_concurrency": 1,
    }
    config.update(overrides)
    session = Session(client)
    clock = Clock()
    return (
        IamRunner(session, AwsAccountConfig.model_validate(config), AdapterContext(clock=clock)),
        session,
        clock,
    )


def real_client():
    return boto3.client("iam", aws_access_key_id="a", aws_secret_access_key="b")


def role(name):
    return {
        "Path": "/",
        "RoleName": name,
        "RoleId": "AROA12345678901234567",
        "Arn": f"arn:aws:iam::{ACCOUNT}:role/{name}",
        "CreateDate": datetime(2025, 1, 1, tzinfo=UTC),
    }


def list_roles_call(**params):
    return IamCall(IamOperation.LIST_ROLES, params, "Roles", paginate=True)


def test_real_stubber_pagination_retains_entries_and_exact_paths():
    client = real_client()
    run, session, clock = runner(client)
    call = list_roles_call(PathPrefix="/", MaxItems=2)
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
        result = run.run(IamOperation.LIST_ROLES, [call])
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
    assert result.coverage.completed_at == max(clock.values)
    assert len(session.calls) == 1
    assert session.calls[0][0] == "iam"
    assert "region_name" not in session.calls[0][1]
    assert call.params == {"PathPrefix": "/", "MaxItems": 2}


@pytest.mark.parametrize("roles", [[], [role("retained")]])
def test_failed_real_page_retains_success_including_empty_page(roles):
    client = real_client()
    run, _, _ = runner(client)
    with Stubber(client) as stub:
        stub.add_response("list_roles", {"Roles": roles, "IsTruncated": True, "Marker": "next"}, {})
        stub.add_client_error(
            "list_roles", "AccessDenied", SECRET, expected_params={"Marker": "next"}
        )
        result = run.run(IamOperation.LIST_ROLES, [list_roles_call()])
        stub.assert_no_pending_responses()
    assert result.coverage.status == CoverageStatus.INCOMPLETE
    assert result.coverage.result_count == len(roles)
    assert [entry.value for entry in result.responses] == roles
    assert result.coverage.failures[0].code == "AccessDenied"
    assert SECRET not in result.coverage.model_dump_json()


class ParamClient:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls = []

    def list_role_policies(self, **params):
        self.calls.append(params)
        value = self.outcomes[params["RoleName"]]
        if isinstance(value, Exception):
            raise value
        return {"PolicyNames": value, "IsTruncated": False}


def failure(code="AccessDenied"):
    return ClientError({"Error": {"Code": code, "Message": SECRET}}, "ListRolePolicies")


@pytest.mark.parametrize(
    ("outcomes", "status", "count", "failures"),
    [
        ({"a": failure(), "b": failure()}, CoverageStatus.FAILED, None, 2),
        ({"a": [], "b": []}, CoverageStatus.SUCCESS, 0, 0),
        ({"a": [], "b": failure()}, CoverageStatus.INCOMPLETE, 0, 1),
        ({"a": ["policy"], "b": failure()}, CoverageStatus.INCOMPLETE, 1, 1),
    ],
)
def test_multiple_params_are_one_aggregate(outcomes, status, count, failures):
    client = ParamClient(outcomes)
    run, _, clock = runner(client)
    calls = [
        IamCall(IamOperation.LIST_ROLE_POLICIES, {"RoleName": name}, "PolicyNames", True)
        for name in ["b", "a"]
    ]
    result = run.run(IamOperation.LIST_ROLE_POLICIES, calls)
    assert client.calls == [{"RoleName": "a"}, {"RoleName": "b"}]
    assert result.coverage.status == status
    assert result.coverage.result_count == count
    assert len(result.coverage.failures) == failures
    assert result.coverage.started_at == min(clock.values)
    assert result.coverage.completed_at == max(clock.values)
    if result.responses:
        assert result.responses[0].call is calls[1]
        assert result.responses[0].path == 'Request[{"RoleName":"a"}].Page[0].PolicyNames[0]'


@pytest.mark.parametrize("prerequisite", [None, "role listing incomplete"])
def test_no_calls_do_not_create_client(prerequisite):
    run, session, _ = runner(RuntimeError(SECRET))
    result = run.run(IamOperation.GET_ROLE, [], prerequisite=prerequisite)
    assert result.responses == []
    assert session.calls == []
    assert result.coverage.status == (
        CoverageStatus.SKIPPED if prerequisite else CoverageStatus.SUCCESS
    )
    assert result.coverage.reason == prerequisite
    assert result.coverage.result_count == (None if prerequisite else 0)


@pytest.mark.parametrize("policies", [[], ["policy"]])
def test_upstream_partial_marks_successful_calls_incomplete(policies):
    run, _, _ = runner(ParamClient({"a": policies}))
    result = run.run(
        IamOperation.LIST_ROLE_POLICIES,
        [IamCall(IamOperation.LIST_ROLE_POLICIES, {"RoleName": "a"}, "PolicyNames")],
        prerequisite="role listing incomplete",
    )
    assert result.coverage.status == CoverageStatus.INCOMPLETE
    assert result.coverage.result_count == len(policies)
    assert result.coverage.reason == "role listing incomplete"


def test_upstream_partial_does_not_turn_all_failed_into_incomplete():
    run, _, _ = runner(ParamClient({"a": failure()}))
    result = run.run(
        IamOperation.LIST_ROLE_POLICIES,
        [IamCall(IamOperation.LIST_ROLE_POLICIES, {"RoleName": "a"}, "PolicyNames")],
        prerequisite="role listing incomplete",
    )
    assert result.coverage.status == CoverageStatus.FAILED
    assert result.coverage.result_count is None


@pytest.mark.parametrize("result_key", [None, "Role"])
def test_successful_detail_response_counts_once(result_key):
    client = real_client()
    run, _, _ = runner(client)
    call = IamCall(IamOperation.GET_ROLE, {"RoleName": "one"}, result_key)
    response = {"Role": role("one")}
    with Stubber(client) as stub:
        stub.add_response("get_role", response, {"RoleName": "one"})
        result = run.run(IamOperation.GET_ROLE, [call])
        stub.assert_no_pending_responses()
    assert result.coverage.result_count == 1
    assert result.responses[0].value == (response if result_key is None else response["Role"])
    assert result.responses[0].path == (
        'Request[{"RoleName":"one"}].Page[0]' + (".Role" if result_key else "")
    )


def test_sdk_config_and_shared_client_across_operations():
    client = real_client()
    run, session, _ = runner(
        client, connect_timeout_seconds=2, read_timeout_seconds=7, max_attempts=5
    )
    with Stubber(client) as stub:
        stub.add_response("list_roles", {"Roles": [], "IsTruncated": False}, {})
        stub.add_response("get_role", {"Role": role("one")}, {"RoleName": "one"})
        run.run(IamOperation.LIST_ROLES, [list_roles_call()])
        run.run(IamOperation.GET_ROLE, [IamCall(IamOperation.GET_ROLE, {"RoleName": "one"})])
        stub.assert_no_pending_responses()
    assert len(session.calls) == 1
    assert set(session.calls[0][1]) == {"config"}
    config = session.calls[0][1]["config"]
    assert config.connect_timeout == 2
    assert config.read_timeout == 7
    assert config.retries == {"max_attempts": 5, "mode": "standard"}


@pytest.mark.parametrize(
    "error",
    [RuntimeError(SECRET), EndpointConnectionError(endpoint_url=SECRET), failure(SECRET)],
)
def test_client_creation_failure_is_safe(error):
    run, session, _ = runner(error)
    result = run.run(IamOperation.LIST_ROLES, [list_roles_call()])
    assert result.coverage.status == CoverageStatus.FAILED
    assert result.coverage.result_count is None
    assert SECRET not in result.coverage.model_dump_json()
    assert len(session.calls) == 1


def test_all_calls_allowlisted_before_any_client_access():
    run, session, _ = runner(RuntimeError("must not create client"))
    with pytest.raises(IngestionError) as exc:
        run.run(IamOperation.GET_ROLE, [IamCall(IamOperation.GET_ROLE), IamCall("DeleteRole")])
    assert exc.value.code == "operation_not_allowed"
    assert session.calls == []


def test_denied_operation_with_no_calls_is_rejected():
    run, session, _ = runner(None)
    with pytest.raises(IngestionError) as exc:
        run.run("DeleteRole", [])
    assert exc.value.code == "operation_not_allowed"
    assert session.calls == []


@pytest.mark.parametrize(
    "calls",
    [
        [IamCall(IamOperation.GET_ROLE)],
        [list_roles_call(PathPrefix="/", MaxItems=2), list_roles_call(MaxItems=2, PathPrefix="/")],
        [list_roles_call(MaxItems=object())],
    ],
)
def test_invalid_calls_rejected_before_client_access(calls):
    run, session, _ = runner(None)
    with pytest.raises(IngestionError) as exc:
        run.run(IamOperation.LIST_ROLES, calls)
    assert exc.value.code == "invalid_request"
    assert session.calls == []


def test_bounded_concurrency_and_deterministic_response_order():
    class ConcurrentClient:
        def __init__(self):
            self.barrier = threading.Barrier(3)
            self.lock = threading.Lock()
            self.active = 0
            self.maximum = 0

        def get_role(self, **params):
            with self.lock:
                self.active += 1
                self.maximum = max(self.maximum, self.active)
            try:
                self.barrier.wait(timeout=5)
                return {"Role": role(params["RoleName"])}
            finally:
                with self.lock:
                    self.active -= 1

    client = ConcurrentClient()
    run, session, _ = runner(client, max_concurrency=3)
    calls = [
        IamCall(IamOperation.GET_ROLE, {"RoleName": str(i)}, "Role") for i in reversed(range(9))
    ]
    result = run.run(IamOperation.GET_ROLE, calls)
    assert client.maximum == 3
    assert len(session.calls) == 1
    assert result.coverage.status == CoverageStatus.SUCCESS
    assert result.coverage.result_count == 9
    assert [item.value["RoleName"] for item in result.responses] == [str(i) for i in range(9)]
    assert [
        json.loads(item.path.split("Request[")[1].split("].Page")[0]) for item in result.responses
    ] == [{"RoleName": str(i)} for i in range(9)]


@pytest.mark.parametrize("marker", [None, "repeated"])
def test_missing_or_repeated_marker_stops_with_retained_results(marker):
    class BrokenPaginationClient:
        def list_roles(self, **params):
            response = {"Roles": [role("one")], "IsTruncated": True}
            if marker is not None:
                response["Marker"] = marker
            return response

    run, _, _ = runner(BrokenPaginationClient())
    result = run.run(IamOperation.LIST_ROLES, [list_roles_call()])
    assert result.coverage.status == CoverageStatus.INCOMPLETE
    assert result.coverage.result_count == (1 if marker is None else 2)
    assert len(result.coverage.failures) == 1
