"""Expected AWS failures are safe; implementation defects remain visible."""

import pytest
from botocore.exceptions import ClientError, ProfileNotFound

from archdiscov.ingestion.adapter import AdapterContext, IngestionError
from archdiscov.ingestion.aws import create_session
from archdiscov.ingestion.identity import verify_account_identity
from archdiscov.ingestion.region_scope import resolve_region_scope
from archdiscov.ingestion.requests import AwsAccountConfig

ACCOUNT = "123456789012"
SECRET = "never-expose-auth-secret"


def account(**overrides):
    return AwsAccountConfig(
        profile="offline",
        expected_account_id=ACCOUNT,
        all_services=True,
        bootstrap_region="us-east-1",
        **overrides,
    )


class Session:
    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error

    def client(self, *args, **kwargs):
        return self

    def response(self, **kwargs):
        if self.error is not None:
            raise self.error
        return self.value

    get_caller_identity = response
    assume_role = response
    describe_regions = response


def invoke(stage, session):
    if stage == "profile":

        def factory(**kwargs):
            return session.response()

        return create_session(account(), AdapterContext(session_factory=factory))
    if stage == "assume":
        return create_session(
            account(role_arn=f"arn:aws:iam::{ACCOUNT}:role/Inventory"),
            AdapterContext(session_factory=lambda **kwargs: session),
        )
    if stage == "identity":
        return verify_account_identity(session, account())
    return resolve_region_scope(session, account(), partition="aws")


@pytest.mark.parametrize("stage", ["profile", "assume", "identity", "discovery"])
@pytest.mark.parametrize(
    "error_type",
    [
        TypeError,
    ],
)
def test_programming_errors_are_not_account_failures(stage, error_type):
    error = error_type(SECRET)
    with pytest.raises(error_type) as caught:
        invoke(stage, Session(error=error))
    assert caught.value is error


@pytest.mark.parametrize("stage", ["profile", "assume", "identity", "discovery"])
def test_expected_sdk_errors_have_safe_diagnostics(stage):
    error = (
        ProfileNotFound(profile=SECRET)
        if stage == "profile"
        else ClientError({"Error": {"Code": "AccessDenied", "Message": SECRET}}, "Read")
    )
    with pytest.raises(IngestionError) as caught:
        invoke(stage, Session(error=error))
    assert caught.value.code == (
        "region_discovery_failed" if stage == "discovery" else "identity_failed"
    )
    assert caught.value.__cause__ is error
    assert SECRET not in str(caught.value)


@pytest.mark.parametrize("value", [None, [], {}, {"Account": 123}, {"Account": ACCOUNT, "Arn": 3}])
def test_malformed_identity_is_an_expected_failure(value):
    with pytest.raises(IngestionError) as caught:
        invoke("identity", Session(value))
    assert caught.value.code == "identity_failed"


@pytest.mark.parametrize(
    "credentials",
    [
        None,
        [],
        {},
        {"AccessKeyId": "key"},
        {"AccessKeyId": "key", "SecretAccessKey": SECRET, "SessionToken": None},
    ],
)
def test_malformed_assume_role_credentials_are_an_expected_failure(credentials):
    with pytest.raises(IngestionError) as caught:
        invoke("assume", Session({"Credentials": credentials}))
    assert caught.value.code == "identity_failed"
    assert SECRET not in str(caught.value)


@pytest.mark.parametrize("value", [None, [], {"Regions": [None]}, {"Regions": [3]}])
def test_malformed_discovery_is_an_expected_failure(value):
    with pytest.raises(IngestionError) as caught:
        invoke("discovery", Session(value))
    assert caught.value.code == "region_discovery_failed"
