import pytest
from pydantic import ValidationError

from archdiscov.ingestion.requests import COLLECTABLE_SERVICES, AwsAccountConfig
from archdiscov.ingestion.scope import RegionScope, ResolvedAwsScope
from archdiscov.models.enums import AwsRegion
from archdiscov.services.snapshots import load_snapshot, validate_snapshot


def account(**overrides):
    return AwsAccountConfig(profile="dev", expected_account_id="123456789012", **overrides)


def test_whole_architecture_defaults_to_enabled_region_discovery():
    cfg = account(all_services=True)
    assert cfg.services == tuple(sorted(COLLECTABLE_SERVICES))
    assert cfg.region_scope is RegionScope.ENABLED
    assert cfg.regions == ()
    assert AwsAccountConfig.model_validate_json(cfg.model_dump_json()) == cfg
    explicit = account(all_services=True, regions=["us-west-2", "us-east-1"])
    assert explicit.region_scope is RegionScope.EXPLICIT
    assert explicit.regions == (AwsRegion.US_WEST_2, AwsRegion.US_EAST_1)
    with pytest.raises(ValidationError, match="all_services cannot be combined"):
        account(all_services=True, services=["ec2"])


def test_resolved_scope_uses_existing_snapshot_metadata():
    cfg = account(all_services=True)
    scope = ResolvedAwsScope.from_account(cfg, (AwsRegion.US_EAST_1, AwsRegion.US_WEST_2))
    assert scope.region_scope is RegionScope.ENABLED
    assert scope.account_wide
    assert scope.regions == (AwsRegion.US_EAST_1, AwsRegion.US_WEST_2)
    snapshot = load_snapshot("tests/fixtures/snapshots/aws.json")
    data = snapshot.model_dump(mode="json")
    data["metadata"]["collection_scope"] = scope.to_provenance()
    validated = validate_snapshot(data)
    assert ResolvedAwsScope.model_validate(validated.metadata["collection_scope"]) == scope
    assert "profile" not in scope.to_provenance()


def test_resolved_scope_requires_regions_and_keeps_explicit_ones():
    with pytest.raises(ValidationError, match="resolved regions are required"):
        ResolvedAwsScope.from_account(account(all_services=True), ())
    iam = ResolvedAwsScope.from_account(account(services=["iam"]), ())
    assert iam.account_wide and not iam.regions
    with pytest.raises(ValueError, match="must match explicit"):
        ResolvedAwsScope.from_account(account(regions=["us-east-1"]), (AwsRegion.US_WEST_2,))
