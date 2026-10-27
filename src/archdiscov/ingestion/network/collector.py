"""Run one region's EC2 network reads: the list reads, then each prefix list's entries."""

from dataclasses import dataclass
from typing import Any

from ...models.architecture import Coverage, UnresolvedValue
from ...models.enums import AwsRegion, AwsService, CoverageStatus
from ..adapter import AdapterContext
from ..aws import EC2_OPERATIONS, OperationResult, OperationRunner, OperationTask
from ..detail_execution import DetailCall, DetailResponse, DetailResult, DetailRunner, with_problems
from ..operations import Ec2Operation as Op
from ..requests import AwsAccountConfig

# One call per list at the API's largest page. A longer list is left unrecorded rather than
# paged through, so one large list cannot use up the call budget.
PREFIX_LIST_ENTRY_LIMIT = 100


@dataclass(frozen=True)
class RegionReads:
    items: dict[Op, list[Any]]
    unavailable: dict[Op, str]
    coverage: list[Coverage]
    # The entries response of each list read in full, by prefix list ID.
    prefix_list_entries: dict[str, DetailResponse]


def read_region(
    session: Any, account: AwsAccountConfig, context: AdapterContext, region: AwsRegion
) -> RegionReads:
    results = OperationRunner(session, account, context).run_many(
        OperationTask(region, AwsService.EC2, operation, result_key=key)
        for operation, key in EC2_OPERATIONS
    )
    lists = next(r for r in results if r.coverage.operation == Op.DESCRIBE_MANAGED_PREFIX_LISTS)
    entries = _read_entries(session, account, context, region, lists)
    coverage = [*(r.coverage for r in results), entries.coverage]
    return RegionReads(
        {Op(r.coverage.operation): r.items for r in results},
        {
            Op(record.operation): record.status.value
            for record in coverage
            if record.status is not CoverageStatus.SUCCESS
        },
        coverage,
        {
            response.call.params["PrefixListId"]: response
            for response in entries.responses
            if not response.value.get("NextToken")
        },
    )


def _read_entries(
    session: Any,
    account: AwsAccountConfig,
    context: AdapterContext,
    region: AwsRegion,
    lists: OperationResult,
) -> DetailResult:
    ids = sorted(
        {
            item["PrefixListId"]
            for item in lists.items
            if isinstance(item, dict) and isinstance(item.get("PrefixListId"), str)
        }
    )
    prerequisite = None
    if lists.coverage.status is not CoverageStatus.SUCCESS:
        prerequisite = f"{Op.DESCRIBE_MANAGED_PREFIX_LISTS.value} did not complete"
    runner = DetailRunner(
        session,
        account,
        context,
        service=AwsService.EC2,
        region=region,
        client_name="ec2",
        pagination="ec2",
    )
    operation = Op.GET_MANAGED_PREFIX_LIST_ENTRIES
    result = runner.run(
        operation,
        [
            DetailCall(operation, {"PrefixListId": i, "MaxResults": PREFIX_LIST_ENTRY_LIMIT})
            for i in ids
        ],
        prerequisite=prerequisite,
    )
    reason = (
        f"The read stopped at {PREFIX_LIST_ENTRY_LIMIT} entries and reported more; "
        "none were recorded."
    )
    return with_problems(
        result,
        [
            UnresolvedValue(configuration_path=response.path, reason=reason)
            for response in result.responses
            if response.value.get("NextToken")
        ],
    )
