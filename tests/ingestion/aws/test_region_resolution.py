import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber
from pydantic import ValidationError

from archdiscov.ingestion.adapter import IngestionError
from archdiscov.ingestion.region_scope import resolve_region_scope
from archdiscov.ingestion.requests import AwsAccountConfig
from archdiscov.models.enums import AwsRegion

ACCOUNT = "123456789012"


def account(**overrides):
    return AwsAccountConfig(
        profile="dev", expected_account_id=ACCOUNT, region_scope="enabled", **overrides
    )


def entry(name, status="opt-in-not-required"):
    return {"RegionName": name, "OptInStatus": status}


class Session:
    def __init__(self, response=None, region=None):
        self.response = response
        self.region_name = region
        self.calls = []

    def client(self, name, **kwargs):
        self.calls.append((name, kwargs))
        return self

    def describe_regions(self, **params):
        assert params == {"AllRegions": True}
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_enabled_regions_are_sorted_and_disabled_regions_are_excluded():
    session = Session(
        {
            "Regions": [
                entry("us-west-2"),
                entry("ap-east-1", "not-opted-in"),
                entry("us-east-1"),
                entry("af-south-1", "opted-in"),
            ]
        }
    )
    resolved = resolve_region_scope(session, account(), partition="aws")
    assert resolved.scope.regions == (
        AwsRegion.AF_SOUTH_1,
        AwsRegion.US_EAST_1,
        AwsRegion.US_WEST_2,
    )
    assert resolved.disabled_regions == ("ap-east-1",)
    assert resolved.bootstrap_region is AwsRegion.US_EAST_1
    assert resolved.diagnostics()["operation"] == "DescribeRegions"
    assert session.calls[0][1]["region_name"] == "us-east-1"


@pytest.mark.parametrize(
    ("partition", "region"),
    [
        ("aws", "us-east-1"),
        ("aws-cn", "cn-north-1"),
        ("aws-us-gov", "us-gov-west-1"),
        ("aws-iso", "us-iso-east-1"),
    ],
)
def test_partition_specific_bootstrap_with_real_stubber(partition, region):
    client = boto3.client(
        "ec2", region_name=region, aws_access_key_id="test", aws_secret_access_key="test"
    )
    session = Session()
    session.client = lambda name, **kwargs: client
    with Stubber(client) as stub:
        stub.add_response("describe_regions", {"Regions": [entry(region)]}, {"AllRegions": True})
        resolved = resolve_region_scope(session, account(), partition=partition)
        stub.assert_no_pending_responses()
    assert resolved.partition == partition
    assert resolved.bootstrap_region.value == region
    assert tuple(r.value for r in resolved.scope.regions) == (region,)


@pytest.mark.parametrize("context", ["explicit", "profile", "role"])
def test_bootstrap_context_does_not_assume_commercial_partition(context):
    session = Session({"Regions": [entry("cn-north-1")]})
    kwargs = {}
    if context == "explicit":
        kwargs["bootstrap_region"] = "cn-north-1"
        session.region_name = "us-east-1"  # explicit bootstrap takes precedence
    elif context == "profile":
        session.region_name = "cn-north-1"
    else:
        kwargs["role_arn"] = f"arn:aws-cn:iam::{ACCOUNT}:role/Reader"
    resolved = resolve_region_scope(session, account(**kwargs), partition=None)
    assert resolved.partition == "aws-cn"
    assert session.calls[0][1]["region_name"] == "cn-north-1"


def test_explicit_scope_bypasses_discovery_and_retains_order():
    session = Session(AssertionError("must not call discovery"))
    cfg = AwsAccountConfig(
        profile="dev", expected_account_id=ACCOUNT, regions=["us-west-2", "us-east-1"]
    )
    resolved = resolve_region_scope(session, cfg, partition="aws")
    assert resolved.scope.regions == cfg.regions
    assert resolved.bootstrap_region is None
    assert session.calls == []


def test_iam_only_explicit_scope_needs_no_bootstrap():
    cfg = AwsAccountConfig(profile="dev", expected_account_id=ACCOUNT, services=["iam"])
    assert resolve_region_scope(Session(), cfg, partition=None).scope.regions == ()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"partition": None},
        {"partition": "unknown"},
        {"partition": "aws-cn", "bootstrap_region": "us-east-1"},
        {"partition": "aws", "profile_region": "global"},
        {"partition": "aws", "profile_region": "secret-invalid-region"},
    ],
)
def test_ambiguous_or_conflicting_bootstrap_is_actionable_before_discovery(kwargs):
    kwargs = dict(kwargs)
    partition = kwargs.pop("partition")
    session = Session(region=kwargs.pop("profile_region", None))
    with pytest.raises(IngestionError) as exc:
        resolve_region_scope(session, account(**kwargs), partition=partition)
    assert exc.value.code == "region_bootstrap_failed"
    assert "secret-invalid-region" not in str(exc.value)
    assert session.calls == []


@pytest.mark.parametrize(
    "regions",
    [
        [],
        [entry("us-east-1", "unknown")],
        [entry("us-east-1"), entry("us-east-1")],
        [entry("us-east-1", "not-opted-in")],
        [{}],
        [None],
        [entry("cn-north-1")],
    ],
)
def test_invalid_discovery_never_returns_reduced_or_empty_scope(regions):
    with pytest.raises(IngestionError) as exc:
        resolve_region_scope(Session({"Regions": regions}), account(), partition="aws")
    assert exc.value.code == "region_discovery_failed"


def test_unsupported_enabled_region_rejects_entire_scope_without_raw_diagnostics():
    response = {"Regions": [entry("us-east-1"), entry("secret-new-region")]}
    with pytest.raises(IngestionError) as exc:
        resolve_region_scope(Session(response), account(), partition="aws")
    assert exc.value.code == "unsupported_region"
    assert "update the region contract" in str(exc.value)
    assert "secret-new-region" not in str(exc.value)


def test_disabled_unknown_region_does_not_block_enabled_scope_or_leak_text():
    resolved = resolve_region_scope(
        Session({"Regions": [entry("us-east-1"), entry("secret-disabled", "not-opted-in")]}),
        account(),
        partition="aws",
    )
    assert resolved.scope.regions == (AwsRegion.US_EAST_1,)
    assert "secret-disabled" not in str(resolved.diagnostics())


def test_discovery_denial_uses_safe_error_and_cause_with_stubber():
    client = boto3.client(
        "ec2", region_name="us-east-1", aws_access_key_id="a", aws_secret_access_key="b"
    )
    session = Session()
    session.client = lambda name, **kwargs: client
    with Stubber(client) as stub:
        stub.add_client_error(
            "describe_regions",
            "UnauthorizedOperation",
            "secret exception text",
            expected_params={"AllRegions": True},
        )
        with pytest.raises(IngestionError) as exc:
            resolve_region_scope(session, account(), partition="aws")
    assert exc.value.code == "region_discovery_failed"
    assert "UnauthorizedOperation" in exc.value.message
    assert "secret exception text" not in exc.value.message
    assert isinstance(exc.value.__cause__, ClientError)


def test_bootstrap_global_is_rejected_by_request():
    with pytest.raises(ValidationError, match="bootstrap_region must name a concrete"):
        account(bootstrap_region="global")
