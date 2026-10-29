"""Expected AWS failures are safe; implementation defects remain visible."""

import pytest
from botocore.exceptions import ProfileNotFound

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
def test_programming_errors_are_not_account_failures(stage):
    error = TypeError(SECRET)
    with pytest.raises(TypeError) as caught:
        invoke(stage, Session(error=error))
    assert caught.value is error


def test_missing_profile_has_a_safe_diagnostic():
    error = ProfileNotFound(profile=SECRET)
    with pytest.raises(IngestionError) as caught:
        invoke("profile", Session(error=error))
    assert caught.value.code == "identity_failed"
    assert caught.value.__cause__ is error
    assert SECRET not in str(caught.value)


def test_identity_of_another_account_is_an_expected_failure():
    value = {"Account": ACCOUNT, "Arn": "arn:aws:iam::000000000000:user/x"}
    with pytest.raises(IngestionError) as caught:
        invoke("identity", Session(value))
    assert caught.value.code == "identity_failed"


def test_malformed_assume_role_credentials_are_an_expected_failure():
    credentials = {"AccessKeyId": "key", "SecretAccessKey": SECRET, "SessionToken": None}
    with pytest.raises(IngestionError) as caught:
        invoke("assume", Session({"Credentials": credentials}))
    assert caught.value.code == "identity_failed"
    assert SECRET not in str(caught.value)
