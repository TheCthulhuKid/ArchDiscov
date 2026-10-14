"""Event-loop-safe collection and immutable persistence workflows."""

import asyncio
from pathlib import Path

from ..ingestion.adapter import AdapterContext, IngestionError, SnapshotAdapter
from ..ingestion.async_collection import AccountCollection, ordered_workers
from ..ingestion.async_runtime import await_settled
from ..ingestion.requests import AwsCollectionRequest, SourceRequest
from ..models.architecture import ArchitectureSnapshot
from .ingestion import AccountOutcome, select_adapter
from .snapshots import SnapshotError, _save_validated_snapshot, validate_snapshot


async def _accounts(
    request: AwsCollectionRequest, selected: SnapshotAdapter, context: AdapterContext
) -> list[AccountCollection]:
    native = getattr(selected, "collect_accounts_async", None)
    if native is not None:
        results = await native(request, context=context)
    else:
        # Legacy adapters retain serial invocation: they need not be thread-safe.
        async def collect(account) -> AccountCollection:
            subrequest = request.model_copy(update={"accounts": (account,)})
            try:
                snapshots = await await_settled(
                    asyncio.to_thread(selected.collect, subrequest, context=context)
                )
            except IngestionError as error:
                return AccountCollection(account.expected_account_id, error=error)
            if len(snapshots) != 1:
                raise IngestionError(
                    "invalid_request", "adapter must return one snapshot per account"
                )
            return AccountCollection(account.expected_account_id, snapshot=snapshots[0])

        results = await ordered_workers(request.accounts, 1, collect)
    expected = [account.expected_account_id for account in request.accounts]
    if [result.account_id for result in results] != expected or any(
        (result.snapshot is None) == (result.error is None)
        or (result.snapshot is not None and result.snapshot.account_id != result.account_id)
        for result in results
    ):
        raise IngestionError("invalid_request", "adapter returned invalid account outcomes")
    return results


async def collect_snapshots_async(
    request: SourceRequest,
    *,
    context: AdapterContext | None = None,
    adapter: SnapshotAdapter | None = None,
) -> list[ArchitectureSnapshot]:
    """Return detached valid snapshots, or raise the first ordered account error."""
    selected = adapter or select_adapter(request)
    context = context or AdapterContext()
    if isinstance(request, AwsCollectionRequest):
        context = context.with_call_budget(request.max_in_flight_calls)
    if (
        isinstance(request, AwsCollectionRequest)
        and getattr(selected, "collect_accounts_async", None) is not None
    ):
        results = await _accounts(request, selected, context)
        for result in results:
            if result.error is not None:
                raise result.error
        snapshots = [result.snapshot for result in results]
    else:
        snapshots = await await_settled(
            asyncio.to_thread(selected.collect, request, context=context)
        )
    return await await_settled(
        asyncio.to_thread(lambda: [validate_snapshot(snapshot) for snapshot in snapshots])
    )


async def collect_and_save_async(
    request: AwsCollectionRequest,
    outputs: list[Path],
    *,
    context: AdapterContext | None = None,
    adapter: SnapshotAdapter | None = None,
) -> list[AccountOutcome]:
    """Collect concurrently, then validate and publish each completed account independently."""
    outputs = [Path(path) for path in outputs]
    if len(outputs) != len(request.accounts):
        raise IngestionError("invalid_request", "exactly one output path per account is required")
    if len({path.resolve() for path in outputs}) != len(outputs):
        raise IngestionError("invalid_request", "output paths must be distinct")
    context = (context or AdapterContext()).with_call_budget(request.max_in_flight_calls)
    results = await _accounts(request, adapter or select_adapter(request), context)

    def publish(result: AccountCollection, path: Path) -> AccountOutcome:
        outcome = AccountOutcome(result.account_id, error=result.error)
        if result.error is not None:
            return outcome
        try:
            outcome.snapshot = validate_snapshot(result.snapshot)
            outcome.path = _save_validated_snapshot(outcome.snapshot, path)
        except SnapshotError as error:
            error.path = path
            outcome.error = error
        return outcome

    async def save(item: tuple[AccountCollection, Path]) -> AccountOutcome:
        return await await_settled(asyncio.to_thread(publish, *item))

    return await ordered_workers(
        list(zip(results, outputs, strict=True)), request.max_active_accounts, save
    )
