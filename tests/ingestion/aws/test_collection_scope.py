import pytest
from pydantic import ValidationError

from archdiscov.ingestion.requests import (
    COLLECTABLE_SERVICES,
    AwsAccountConfig,
    AwsCollectionRequest,
)
from archdiscov.ingestion.scope import RegionScope, ResolvedAwsScope
from archdiscov.models.enums import AwsRegion, AwsService
from archdiscov.services.snapshots import load_snapshot, validate_snapshot


def account(**overrides):
    return AwsAccountConfig(profile="dev", expected_account_id="123456789012", **overrides)


def test_whole_architecture_defaults_to_enabled_region_discovery():
    cfg = account(all_services=True)
    assert set(cfg.services) == COLLECTABLE_SERVICES
    assert len(cfg.services) == 7
    assert cfg.services == tuple(sorted(COLLECTABLE_SERVICES))
    assert cfg.region_scope is RegionScope.ENABLED
    assert cfg.regions == ()
    assert AwsAccountConfig.model_validate_json(cfg.model_dump_json()) == cfg


def test_explicit_regions_override_whole_architecture_discovery():
    cfg = account(all_services=True, regions=["us-west-2", "us-east-1"])
    assert cfg.region_scope is RegionScope.EXPLICIT
    assert cfg.regions == (AwsRegion.US_WEST_2, AwsRegion.US_EAST_1)


@pytest.mark.parametrize("services", [["ec2"], ["iam"], sorted(COLLECTABLE_SERVICES)])
def test_all_services_rejects_any_explicit_selection(services):
    with pytest.raises(ValidationError, match="all_services cannot be combined"):
        account(all_services=True, services=services)


def test_selective_scope_remains_compatible():
    cfg = account(regions=["us-east-1"])
    assert cfg.services == (AwsService.EC2,)
    assert cfg.region_scope is RegionScope.EXPLICIT
    assert not cfg.all_services
    iam = account(services=["iam"])
    assert iam.regions == ()
    assert iam.region_scope is RegionScope.EXPLICIT
    discovered = account(services=["sqs"], region_scope="enabled")
    assert discovered.regions == ()
    with pytest.raises(ValidationError, match="regions are required"):
        account()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"all_services": True, "region_scope": "explicit"}, "regions are required"),
        ({"region_scope": "enabled", "regions": ["us-east-1"]}, "cannot include explicit"),
        ({"region_scope": "unknown"}, "region_scope"),
        ({"all_services": "true"}, "all_services"),
    ],
)
def test_invalid_scope_rejected(overrides, message):
    with pytest.raises(ValidationError, match=message):
        account(**overrides)


def test_execution_limit_defaults_and_independent_budgets():
    cfg = account(regions=["us-east-1"])
    request = AwsCollectionRequest(accounts=(cfg,))
    assert cfg.max_in_flight_calls == 4
    assert request.max_in_flight_calls == 16
    assert request.max_active_accounts == 2
    assert request.max_collector_jobs == 8
    limited = AwsCollectionRequest(
        accounts=(cfg,), max_in_flight_calls=1, max_active_accounts=1, max_collector_jobs=1
    )
    assert limited.accounts[0].max_in_flight_calls == 4
    assert AwsCollectionRequest.model_validate_json(limited.model_dump_json()) == limited


def test_execution_limit_upper_boundaries():
    cfg = account(services=["iam"], max_in_flight_calls=16)
    request = AwsCollectionRequest(
        accounts=(cfg,), max_in_flight_calls=64, max_active_accounts=16, max_collector_jobs=64
    )
    assert request.max_in_flight_calls == request.max_collector_jobs == 64
    assert request.max_active_accounts == cfg.max_in_flight_calls == 16


@pytest.mark.parametrize(
    "field", ["max_in_flight_calls", "max_active_accounts", "max_collector_jobs"]
)
@pytest.mark.parametrize("value", [0, True, 1000])
def test_invalid_run_limits_rejected(field, value):
    with pytest.raises(ValidationError, match=field):
        AwsCollectionRequest(accounts=(account(services=["iam"]),), **{field: value})


@pytest.mark.parametrize("value", [0, -1, True, "2", 1.5, 17])
def test_invalid_account_call_limit_rejected(value):
    with pytest.raises(ValidationError, match="max_in_flight_calls"):
        account(services=["iam"], max_in_flight_calls=value)


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
    reloaded = type(validated).model_validate_json(validated.model_dump_json())
    assert reloaded.schema_version == snapshot.schema_version
    assert ResolvedAwsScope.model_validate(reloaded.metadata["collection_scope"]) == scope
    assert "profile" not in scope.to_provenance()


def test_resolved_scope_rejects_invalid_or_unresolved_regions():
    with pytest.raises(ValidationError, match="resolved regions are required"):
        ResolvedAwsScope.from_account(account(all_services=True), ())
    for regions in [(AwsRegion.GLOBAL,), (AwsRegion.US_EAST_1, AwsRegion.US_EAST_1)]:
        with pytest.raises(ValidationError):
            ResolvedAwsScope.from_account(account(all_services=True), regions)
    iam = ResolvedAwsScope.from_account(account(services=["iam"]), ())
    assert iam.account_wide and not iam.regions


def test_explicit_resolved_scope_cannot_change_requested_regions():
    cfg = account(regions=["us-east-1"])
    with pytest.raises(ValueError, match="must match explicit"):
        ResolvedAwsScope.from_account(cfg, (AwsRegion.US_WEST_2,))


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"account_id": "123"}, "account_id"),
        ({"services": []}, "at least one service"),
        ({"services": ["sts"]}, "not collectable"),
        ({"services": ["iam", "iam"]}, "unique"),
        ({"regions": ["mars-north-1"]}, "regions"),
        ({"profile": "dev"}, "profile"),
    ],
)
def test_resolved_provenance_rejects_invalid_scope(overrides, message):
    data = ResolvedAwsScope.from_account(account(services=["iam"]), ()).to_provenance()
    data.update(overrides)
    with pytest.raises(ValidationError, match=message):
        ResolvedAwsScope.model_validate(data)


def test_whole_architecture_preserves_account_isolation():
    first = account(all_services=True)
    second = AwsAccountConfig(
        profile="other", expected_account_id="210987654321", all_services=True
    )
    request = AwsCollectionRequest(accounts=(first, second))
    assert [cfg.profile for cfg in request.accounts] == ["dev", "other"]
    with pytest.raises(ValidationError, match="expected_account_id values must be unique"):
        AwsCollectionRequest(accounts=(first, first))
