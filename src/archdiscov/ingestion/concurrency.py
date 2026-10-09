import asyncio
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from threading import Condition, Event


class SdkCallBudget:
    """Reserve run and account capacity atomically for one blocking SDK call."""

    def __init__(self, max_in_flight_calls: int = 16) -> None:
        if type(max_in_flight_calls) is not int or max_in_flight_calls < 1:
            raise ValueError("SDK-call limit must be a positive integer")
        self._limit = max_in_flight_calls
        self._condition = Condition()
        self._active = 0
        self._accounts: dict[str, int] = {}
        self._account_limits: dict[str, int] = {}

    def restrict(self, max_in_flight_calls: int) -> None:
        """An injected/shared budget may tighten, but never bypass, a request limit."""
        if type(max_in_flight_calls) is not int or max_in_flight_calls < 1:
            raise ValueError("SDK-call limit must be a positive integer")
        with self._condition:
            self._limit = min(self._limit, max_in_flight_calls)

    def pages[T](self, pages: Iterable[T], account_id: str, limit: int) -> Iterator[T]:
        """Guard lazy paginator reads, releasing capacity before the caller processes a page."""
        iterator = iter(pages)
        while True:
            try:
                with self.permit(account_id, limit):
                    page = next(iterator)
            except StopIteration:
                return
            yield page

    @contextmanager
    def permit(self, account_id: str, limit: int) -> Iterator[None]:
        if type(limit) is not int or limit < 1:
            raise ValueError("account SDK-call limit must be a positive integer")
        with self._condition:
            previous = self._account_limits.setdefault(account_id, limit)
            if previous != limit:
                raise ValueError("account SDK-call limit must be consistent within a run")
            self._condition.wait_for(
                lambda: self._active < self._limit and self._accounts.get(account_id, 0) < limit
            )
            self._active += 1
            self._accounts[account_id] = self._accounts.get(account_id, 0) + 1
        try:
            yield
        finally:
            with self._condition:
                self._active -= 1
                self._accounts[account_id] -= 1
                self._condition.notify_all()


class CancellableSdkCalls:
    """Run-local cancellation gate; never invalidate the caller's shared budget."""

    def __init__(self, budget: SdkCallBudget) -> None:
        self._budget = budget
        self._cancelled = Event()

    def cancel(self) -> None:
        self._cancelled.set()

    def restrict(self, limit: int) -> None:
        self._budget.restrict(limit)

    def pages[T](self, pages: Iterable[T], account_id: str, limit: int) -> Iterator[T]:
        return SdkCallBudget.pages(self, pages, account_id, limit)

    @contextmanager
    def permit(self, account_id: str, limit: int) -> Iterator[None]:
        if self._cancelled.is_set():
            raise asyncio.CancelledError
        with self._budget.permit(account_id, limit):
            if self._cancelled.is_set():
                raise asyncio.CancelledError
            yield
