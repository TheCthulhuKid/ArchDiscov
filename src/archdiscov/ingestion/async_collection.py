"""Bounded scheduling of isolated jobs; the adapter still owns account assembly."""

import asyncio
import threading
from collections.abc import Awaitable, Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from functools import partial
from typing import Any

from ..models.architecture import ArchitectureSnapshot
from .adapter import AdapterContext, IngestionError
from .async_runtime import await_settled
from .concurrency import CancellableSdkCalls
from .requests import AwsCollectionRequest


@dataclass(frozen=True)
class AccountCollection:
    account_id: str
    snapshot: ArchitectureSnapshot | None = None
    error: IngestionError | None = None


class PreparedSession:
    """Serialize Session access, not the calls made by its thread-safe clients."""

    def __init__(self, session: Any) -> None:
        self._session = session
        self._lock = threading.RLock()

    def __getattr__(self, name: str) -> Any:
        with self._lock:
            value = getattr(self._session, name)
        if not callable(value):
            return value

        def invoke(*args: Any, **kwargs: Any) -> Any:
            with self._lock:
                return value(*args, **kwargs)

        return invoke


async def ordered_workers[T, R](
    items: Sequence[T], limit: int, work: Callable[[T], Awaitable[R]]
) -> list[R]:
    """Bound task creation and retain input order even when workers finish out of order."""
    results: list[Any] = [None] * len(items)
    pending = iter(enumerate(items))

    async def worker() -> None:
        for index, item in pending:
            try:
                results[index] = await work(item)
            except Exception as error:  # noqa: BLE001 - re-raised in input order below
                results[index] = error

    workers = [asyncio.create_task(worker()) for _ in range(min(limit, len(items)))]
    try:
        await asyncio.gather(*workers)
    except asyncio.CancelledError:
        for task in workers:
            task.cancel()
        await await_settled(asyncio.gather(*workers, return_exceptions=True))
        raise
    for result in results:
        if isinstance(result, Exception):
            raise result
    return results


class AsyncCollectionCoordinator:
    def __init__(
        self, request: AwsCollectionRequest, context: AdapterContext, factory: Callable
    ) -> None:
        self.request = request
        self._factory_lock = threading.Lock()
        self._sessions: dict[int, tuple[Any, str, PreparedSession]] = {}
        self._jobs = asyncio.Semaphore(request.max_collector_jobs)
        self._seams_lock = threading.Lock()
        bounded_context = context.with_call_budget(request.max_in_flight_calls)
        self._sdk_calls = CancellableSdkCalls(bounded_context.sdk_calls)
        self.context = replace(
            bounded_context,
            call_budget=self._sdk_calls,
            clock=self._serialized(context.clock),
            uuid_factory=self._serialized(context.uuid_factory),
        )
        self._factory = factory

    def _serialized(self, function: Callable) -> Callable:
        def invoke(*args: Any, **kwargs: Any) -> Any:
            with self._seams_lock:
                return function(*args, **kwargs)

        return invoke

    def account_context(self, account_id: str) -> AdapterContext:
        def factory(**kwargs: Any) -> PreparedSession:
            with self._factory_lock:
                session = self._factory(**kwargs)
                key = id(session)
                if key not in self._sessions:
                    self._sessions[key] = (session, account_id, PreparedSession(session))
                _, owner, prepared = self._sessions[key]
                if owner != account_id:
                    raise IngestionError(
                        "invalid_request", "session factory reused a session across accounts"
                    )
                return prepared

        return replace(self.context, session_factory=factory)

    async def blocking(self, function: Callable, *args: Any) -> Any:
        return await await_settled(
            asyncio.get_running_loop().run_in_executor(self._pool, partial(function, *args)),
            on_cancel=self._sdk_calls.cancel,
        )

    async def collect_jobs[T, R](self, jobs: Sequence[T], function: Callable[[T], R]) -> list[R]:
        async def collect(job: T) -> R:
            async with self._jobs:
                return await self.blocking(function, job)

        return await ordered_workers(jobs, self.request.max_collector_jobs, collect)

    async def run(self, collect_account: Callable) -> list[AccountCollection]:
        async def collect(account: Any) -> AccountCollection:
            context = self.account_context(account.expected_account_id)
            try:
                snapshot = await collect_account(account, context, self)
            except IngestionError as error:
                return AccountCollection(account.expected_account_id, error=error)
            return AccountCollection(account.expected_account_id, snapshot=snapshot)

        with ThreadPoolExecutor(
            max_workers=self.request.max_active_accounts + self.request.max_collector_jobs
        ) as self._pool:
            return await ordered_workers(
                self.request.accounts, self.request.max_active_accounts, collect
            )
