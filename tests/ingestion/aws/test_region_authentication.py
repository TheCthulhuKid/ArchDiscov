import pytest

from archdiscov.ingestion.adapter import AdapterContext
from archdiscov.ingestion.aws import create_session, verify_identity
from archdiscov.ingestion.identity import verify_account_identity
from archdiscov.ingestion.requests import AwsAccountConfig

ACCOUNT = "123456789012"


class Session:
    def __init__(self, region=None):
        self.region_name = region
        self.clients = []

    def client(self, name, **kwargs):
        self.clients.append((name, kwargs))
        return self

    def assume_role(self, **params):
        return {
            "Credentials": {
                "AccessKeyId": "transient",
                "SecretAccessKey": "transient",
                "SessionToken": "transient",
            }
        }

    def get_caller_identity(self):
        return {"Account": ACCOUNT, "Arn": f"arn:aws-cn:sts::{ACCOUNT}:assumed-role/Reader/session"}


@pytest.mark.parametrize("profile_region", [None, "cn-northwest-1"])
def test_assumed_sessions_preserve_partition_endpoint_context(profile_region):
    seen = []
    base = Session(profile_region)

    def factory(**kwargs):
        seen.append(kwargs)
        return base if "profile_name" in kwargs else Session(kwargs.get("region_name"))

    cfg = AwsAccountConfig(
        profile="china",
        expected_account_id=ACCOUNT,
        all_services=True,
        role_arn=f"arn:aws-cn:iam::{ACCOUNT}:role/Reader",
    )
    session = create_session(cfg, AdapterContext(session_factory=factory))
    expected = profile_region or "cn-north-1"
    assert base.clients[0][1]["region_name"] == expected
    assert seen[1]["region_name"] == expected
    identity = verify_account_identity(session, cfg)
    assert identity.partition == "aws-cn"
    assert identity.account_id == ACCOUNT
    assert session.clients[0][1]["region_name"] == expected


def test_explicit_bootstrap_is_applied_to_profile_session_and_sts():
    seen = []

    def factory(**kwargs):
        seen.append(kwargs)
        return Session(kwargs.get("region_name"))

    cfg = AwsAccountConfig(
        profile="china",
        expected_account_id=ACCOUNT,
        all_services=True,
        bootstrap_region="cn-north-1",
    )
    session = create_session(cfg, AdapterContext(session_factory=factory))
    assert seen == [{"profile_name": "china", "region_name": "cn-north-1"}]
    assert verify_identity(session, cfg) == ACCOUNT
    assert session.clients[0][1]["region_name"] == "cn-north-1"
