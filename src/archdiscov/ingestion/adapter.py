from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

from ..models.architecture import ArchitectureSnapshot
from .concurrency import CancellableSdkCalls, SdkCallBudget
from .requests import SourceRequest

IngestionErrorCode = Literal[
    "account_mismatch",
    "identity_failed",
    "operation_not_allowed",
    "invalid_request",
    "region_discovery_failed",
    "region_bootstrap_failed",
    "unsupported_region",
]


class IngestionError(Exception):
    """A request-level failure; per-operation failures are coverage records instead."""

    def __init__(self, code: IngestionErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class AdapterContext:
    """Injected seams so adapters run offline and deterministically."""

    clock: Callable[[], datetime] = _utc_now
    uuid_factory: Callable[[], UUID] = uuid4
    session_factory: Callable[..., Any] | None = field(default=None)
    call_budget: SdkCallBudget | CancellableSdkCalls | None = field(default=None, repr=False)
    _default_budget: SdkCallBudget = field(
        default_factory=SdkCallBudget, init=False, repr=False, compare=False
    )

    @property
    def sdk_calls(self) -> SdkCallBudget | CancellableSdkCalls:
        return self.call_budget if self.call_budget is not None else self._default_budget

    def with_call_budget(self, limit: int) -> "AdapterContext":
        """Bind a run once; account subrequests retain the same budget."""
        if self.call_budget is not None:
            self.call_budget.restrict(limit)
            return self
        return replace(self, call_budget=SdkCallBudget(limit))


class SnapshotAdapter(Protocol):
    """An adapter returns a complete snapshot, or raises IngestionError."""

    def collect(
        self, request: SourceRequest, *, context: AdapterContext
    ) -> list[ArchitectureSnapshot]: ...
