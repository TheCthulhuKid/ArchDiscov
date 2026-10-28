"""Run one region's EC2 network reads: the list reads, then the per-item detail reads.

The detail reads are each prefix list's entries and each transit gateway route table's
associations, propagations, and routes.
"""

from dataclasses import dataclass
from typing import Any

from ...models.architecture import Coverage, UnresolvedValue
from ...models.enums import AwsRegion, AwsService, CoverageStatus
from ..adapter import AdapterContext
from ..aws import EC2_OPERATIONS, OperationResult, OperationRunner, OperationTask
from ..detail_execution import DetailCall, DetailResponse, DetailResult, DetailRunner, with_problems
from ..operations import Ec2Operation as Op
from ..requests import AwsAccountConfig
from .transit import TransitGatewayTableReads

# One call per list at the API's largest page. A longer list is left unrecorded rather than
# paged through, so one large list cannot use up the call budget.
PREFIX_LIST_ENTRY_LIMIT = 100
# One route search per table at the API's largest result count, never repeated to read past
# it: a table with more routes keeps those returned and records them as incomplete.
TRANSIT_GATEWAY_ROUTE_LIMIT = 1000
_ROUTE_TYPES = [{"Name": "type", "Values": ["static", "propagated"]}]
_TABLE_ID = "TransitGatewayRouteTableId"


@dataclass(frozen=True)
class RegionReads:
    items: dict[Op, list[Any]]
    unavailable: dict[Op, str]
    coverage: list[Coverage]
    # The entries response of each list read in full, by prefix list ID.
    prefix_list_entries: dict[str, DetailResponse]
    transit_gateway_tables: dict[str, TransitGatewayTableReads]


def read_region(
    session: Any, account: AwsAccountConfig, context: AdapterContext, region: AwsRegion
) -> RegionReads:
    results = OperationRunner(session, account, context).run_many(
        OperationTask(region, AwsService.EC2, operation, result_key=key)
        for operation, key in EC2_OPERATIONS
    )
    by_operation = {Op(r.coverage.operation): r for r in results}
    runner = DetailRunner(
        session,
        account,
        context,
        service=AwsService.EC2,
        region=region,
        client_name="ec2",
        pagination="ec2",
    )
    entries = _read_entries(runner, by_operation[Op.DESCRIBE_MANAGED_PREFIX_LISTS])
    tables, table_results = _read_route_tables(
        runner, by_operation[Op.DESCRIBE_TRANSIT_GATEWAY_ROUTE_TABLES]
    )
    coverage = [*(r.coverage for r in results), entries.coverage]
    coverage.extend(result.coverage for result in table_results)
    return RegionReads(
        {operation: result.items for operation, result in by_operation.items()},
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
        tables,
    )


def _ids(listed: OperationResult, key: str) -> list[str]:
    return sorted(
        {
            item[key]
            for item in listed.items
            if isinstance(item, dict) and isinstance(item.get(key), str)
        }
    )


def _prerequisite(listed: OperationResult) -> str | None:
    if listed.coverage.status is CoverageStatus.SUCCESS:
        return None
    return f"{listed.coverage.operation} did not complete"


def _read_entries(runner: DetailRunner, lists: OperationResult) -> DetailResult:
    operation = Op.GET_MANAGED_PREFIX_LIST_ENTRIES
    result = runner.run(
        operation,
        [
            DetailCall(operation, {"PrefixListId": i, "MaxResults": PREFIX_LIST_ENTRY_LIMIT})
            for i in _ids(lists, "PrefixListId")
        ],
        prerequisite=_prerequisite(lists),
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


def _read_route_tables(
    runner: DetailRunner, tables: OperationResult
) -> tuple[dict[str, TransitGatewayTableReads], list[DetailResult]]:
    """Each table's detail reads, keeping only those that completed, plus their results."""
    ids = _ids(tables, _TABLE_ID)
    prerequisite = _prerequisite(tables)
    pages = {}
    results = []
    for name, operation in (
        ("associations", Op.GET_TRANSIT_GATEWAY_ROUTE_TABLE_ASSOCIATIONS),
        ("propagations", Op.GET_TRANSIT_GATEWAY_ROUTE_TABLE_PROPAGATIONS),
    ):
        calls = [DetailCall(operation, {_TABLE_ID: i}, paginate=True) for i in ids]
        result = runner.run(operation, calls, prerequisite=prerequisite)
        results.append(result)
        pages[name] = _complete_pages(result.responses)
    search = _search_routes(runner, ids, prerequisite)
    results.append(search)
    routes = {response.call.params[_TABLE_ID]: response for response in search.responses}
    reads = {
        i: TransitGatewayTableReads(
            pages["associations"].get(i), pages["propagations"].get(i), routes.get(i)
        )
        for i in ids
    }
    return reads, results


def _complete_pages(responses: list[DetailResponse]) -> dict[str, list[DetailResponse]]:
    """Each table's pages, for tables whose last page ended the read (no further token)."""
    pages: dict[str, list[DetailResponse]] = {}
    for response in responses:
        pages.setdefault(response.call.params[_TABLE_ID], []).append(response)
    return {i: table for i, table in pages.items() if not table[-1].value.get("NextToken")}


def _search_routes(runner: DetailRunner, ids: list[str], prerequisite: str | None) -> DetailResult:
    operation = Op.SEARCH_TRANSIT_GATEWAY_ROUTES
    params = {"Filters": _ROUTE_TYPES, "MaxResults": TRANSIT_GATEWAY_ROUTE_LIMIT}
    result = runner.run(
        operation,
        [DetailCall(operation, {_TABLE_ID: i, **params}) for i in ids],
        prerequisite=prerequisite,
    )
    reason = (
        f"The route search stopped at {TRANSIT_GATEWAY_ROUTE_LIMIT} routes and reported more; "
        "the routes returned are recorded as incomplete."
    )
    return with_problems(
        result,
        [
            UnresolvedValue(configuration_path=response.path, reason=reason)
            for response in result.responses
            if response.value.get("AdditionalRoutesAvailable")
        ],
    )
