import pytest
from pydantic import ValidationError

from archdiscov.ingestion.adapter import AdapterContext, IngestionError
from archdiscov.ingestion.requests import AwsAccountConfig, AwsCollectionRequest

ACCOUNT = "123456789012"


def config(**overrides):
    data = {"profile": "dev", "expected_account_id": ACCOUNT, "regions": ["us-east-1"]}
    data.update(overrides)
    return AwsAccountConfig.model_validate(data)


def test_defaults_are_valid():
    cfg = config()
    assert cfg.max_concurrency == 4
    assert cfg.max_attempts == 3
    assert cfg.session_name == "archdiscov"
    assert cfg.role_arn is None


def test_assume_role_fields_accepted():
    cfg = config(
        role_arn="arn:aws:iam::210987654321:role/Reader", external_id="ext", session_name="s"
    )
    assert cfg.external_id == "ext"


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"expected_account_id": "123"}, "expected_account_id"),
        ({"expected_account_id": "12345678901a"}, "expected_account_id"),
        ({"profile": " "}, "profile"),
        ({"regions": ["us-east-1", "us-east-1"]}, "unique"),
        ({"regions": ["mars-north-1"]}, "regions"),
        ({"regions": ["global"]}, "global"),
        ({"regions": []}, "regions are required"),
        ({"max_concurrency": 0}, "max_concurrency"),
        ({"max_concurrency": 1000}, "max_concurrency"),
        ({"connect_timeout_seconds": 0}, "connect_timeout_seconds"),
        ({"read_timeout_seconds": -1}, "read_timeout_seconds"),
        ({"max_attempts": 0}, "max_attempts"),
        ({"services": []}, "at least one service"),
        ({"services": ["s3"]}, "services"),
        ({"services": ["sts"]}, "not collectable"),
        ({"services": ["ec2", "ec2"]}, "unique"),
        ({"role_arn": "not-an-arn"}, "role_arn"),
        ({"external_id": "x"}, "external_id requires role_arn"),
        ({"unknown": 1}, "unknown"),
    ],
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
    with pytest.raises(ValidationError):
        AwsCollectionRequest(accounts=())
    request = AwsCollectionRequest(accounts=(config(), config(expected_account_id="210987654321")))
    assert request.kind == "aws"


def test_models_are_immutable():
    with pytest.raises(ValidationError):
        config().profile = "x"


def test_external_id_not_in_repr():
    assert "secret-ext" not in repr(
        config(role_arn="arn:aws:iam::210987654321:role/R", external_id="secret-ext")
    )


def test_ingestion_error_codes():
    err = IngestionError("account_mismatch", "bad")
    assert (err.code, err.message, str(err)) == ("account_mismatch", "bad", "bad")


def test_context_defaults_provide_seams():
    ctx = AdapterContext()
    assert ctx.clock().tzinfo is not None
    assert ctx.uuid_factory() != ctx.uuid_factory()
