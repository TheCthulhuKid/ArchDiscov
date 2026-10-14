from dataclasses import dataclass
from functools import cache
from typing import Any

from botocore.session import Session

from ..models.enums import AwsRegion, AwsService
from .adapter import AdapterContext, IngestionError
from .execution_common import COLLECTION_ERRORS, client_config, require_allowed, sanitize_failure
from .operations import Ec2Operation
from .requests import AwsAccountConfig, RegionScope
from .scope import ResolvedAwsScope


@cache
def endpoint_regions(service: str, partition: str) -> tuple[str, ...]:
    """Local SDK metadata only: no credentials or endpoint requests."""
    return tuple(Session().get_available_regions(service, partition_name=partition))


@cache
def region_partition(region: AwsRegion) -> str:
    for partition in Session().get_available_partitions():
        if region.value in endpoint_regions("ec2", partition):
            return partition
    raise IngestionError(
        "region_bootstrap_failed", "region has no known EC2 endpoint in this SDK; update botocore"
    )


def partition_bootstrap(partition: str) -> AwsRegion | None:
    defaults = {
        "aws": AwsRegion.US_EAST_1,
        "aws-cn": AwsRegion.CN_NORTH_1,
        "aws-us-gov": AwsRegion.US_GOV_WEST_1,
        "aws-iso": AwsRegion.US_ISO_EAST_1,
        "aws-iso-b": AwsRegion.US_ISOB_EAST_1,
        "aws-iso-e": AwsRegion.EU_ISOE_WEST_1,
        "aws-iso-f": AwsRegion.US_ISOF_EAST_1,
        "aws-eusc": AwsRegion.EUSC_DE_EAST_1,
    }
    region = defaults.get(partition)
    return region if region and region.value in endpoint_regions("ec2", partition) else None


def authentication_region(session: Any, account: AwsAccountConfig) -> str | None:
    """Respect explicit/profile/role context for STS too, before live region discovery."""
    region = account.bootstrap_region or getattr(session, "region_name", None)
    if region:
        return str(region)
    if account.regions:
        return account.regions[0].value
    if account.role_arn:
        bootstrap = partition_bootstrap(account.role_arn.split(":")[1])
        if bootstrap:
            return bootstrap.value
    return None


@dataclass(frozen=True)
class RegionResolution:
    scope: ResolvedAwsScope
    partition: str | None
    bootstrap_region: AwsRegion | None = None
    disabled_regions: tuple[str, ...] = ()

    def diagnostics(self) -> dict:
        return {
            "partition": self.partition,
            "bootstrap_region": self.bootstrap_region.value if self.bootstrap_region else None,
            "operation": Ec2Operation.DESCRIBE_REGIONS.value if self.bootstrap_region else None,
            "disabled_regions": list(self.disabled_regions),
        }


def resolve_region_scope(
    session: Any,
    account: AwsAccountConfig,
    *,
    partition: str | None,
    context: AdapterContext | None = None,
) -> RegionResolution:
    """Called by the adapter only after identity verification; never discovers accounts."""
    role_partition = account.role_arn.split(":")[1] if account.role_arn else None
    if partition and role_partition and partition != role_partition:
        raise IngestionError(
            "region_bootstrap_failed", "identity and configured role partitions differ"
        )
    partition = partition or role_partition
    if account.region_scope is RegionScope.EXPLICIT:
        partitions = {region_partition(region) for region in account.regions}
        if len(partitions) > 1 or (partition and partitions and partitions != {partition}):
            raise IngestionError(
                "region_bootstrap_failed", "requested regions conflict with account partition"
            )
        return RegionResolution(
            ResolvedAwsScope.from_account(account, account.regions),
            partition or next(iter(partitions), None),
        )

    bootstrap = account.bootstrap_region or getattr(session, "region_name", None)
    if bootstrap:
        try:
            bootstrap = AwsRegion(bootstrap)
        except ValueError as error:
            raise IngestionError(
                "region_bootstrap_failed", "configure a supported bootstrap_region"
            ) from error
        bootstrap_partition = region_partition(bootstrap)
        if partition and partition != bootstrap_partition:
            raise IngestionError(
                "region_bootstrap_failed", "bootstrap region conflicts with account partition"
            )
        partition = bootstrap_partition
    elif partition:
        bootstrap = partition_bootstrap(partition)
    if not bootstrap or not partition:
        raise IngestionError(
            "region_bootstrap_failed",
            "cannot resolve an EC2 bootstrap endpoint; configure bootstrap_region",
        )

    operation = Ec2Operation.DESCRIBE_REGIONS
    require_allowed(AwsService.EC2, operation)
    context = context or AdapterContext()
    try:
        client = session.client("ec2", region_name=bootstrap.value, config=client_config(account))
        with context.sdk_calls.permit(account.expected_account_id, account.max_in_flight_calls):
            response = client.describe_regions(AllRegions=True)
    except COLLECTION_ERRORS as error:
        failure = sanitize_failure(error, operation)
        raise IngestionError(
            "region_discovery_failed", f"{failure.message}; enabled-region scope was not collected"
        ) from error
    try:
        if not isinstance(response, dict):
            raise ValueError("invalid region response")
        entries = response["Regions"]
        if not isinstance(entries, list) or not entries:
            raise ValueError("missing regions")
        enabled = set()
        disabled = set()
        seen = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("invalid region entry")
            name, status = entry["RegionName"], entry["OptInStatus"]
            if not isinstance(name, str) or name in seen:
                raise ValueError("invalid region")
            seen.add(name)
            if status == "not-opted-in":
                # Only retain recognized identifiers, never arbitrary response text.
                if name in AwsRegion._value2member_map_:
                    disabled.add(name)
                continue
            if status not in {"opt-in-not-required", "opted-in"}:
                raise ValueError("unknown opt-in status")
            try:
                region = AwsRegion(name)
            except ValueError as error:
                raise IngestionError(
                    "unsupported_region",
                    "an enabled region is unsupported by AwsRegion; update the region contract",
                ) from error
            if region is AwsRegion.GLOBAL or region_partition(region) != partition:
                raise ValueError("region partition mismatch")
            enabled.add(region)
        if not enabled:
            raise ValueError("no enabled regions")
    except IngestionError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise IngestionError(
            "region_discovery_failed",
            "DescribeRegions returned invalid or empty scope; no inventory was collected",
        ) from error
    return RegionResolution(
        ResolvedAwsScope.from_account(account, tuple(sorted(enabled))),
        partition,
        bootstrap,
        tuple(sorted(disabled)),
    )
