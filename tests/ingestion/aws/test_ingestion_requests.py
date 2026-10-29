import pytest
from pydantic import ValidationError

from archdiscov.ingestion.requests import AwsAccountConfig, AwsCollectionRequest

ACCOUNT = "123456789012"


def config(**overrides):
    data = {"profile": "dev", "expected_account_id": ACCOUNT, "regions": ["us-east-1"]}
    data.update(overrides)
    return AwsAccountConfig.model_validate(data)


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"regions": ["us-east-1", "us-east-1"]}, "unique"),
        ({"services": ["sts"]}, "not collectable"),
    ],
    ids=["region-validator", "service-validator"],
)
def test_invalid_config_rejected(overrides, fragment):
    with pytest.raises(ValidationError) as exc:
        config(**overrides)
    assert fragment in str(exc.value)


def test_regions_optional_for_account_wide_services_only():
    assert config(regions=[], services=["iam"]).regions == ()
    with pytest.raises(ValidationError, match="regions are required"):
        config(regions=[], services=["iam", "ec2"])


def test_request_requires_unique_accounts():
    with pytest.raises(ValidationError, match="unique"):
        AwsCollectionRequest(accounts=(config(), config(profile="other")))
    request = AwsCollectionRequest(accounts=(config(), config(expected_account_id="210987654321")))
    assert request.kind == "aws"


def test_external_id_not_in_repr():
    assert "secret-ext" not in repr(
        config(role_arn="arn:aws:iam::210987654321:role/R", external_id="secret-ext")
    )
