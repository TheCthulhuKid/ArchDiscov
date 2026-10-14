from dataclasses import dataclass
from pathlib import Path

from ..ingestion.adapter import AdapterContext, IngestionError, SnapshotAdapter
from ..ingestion.async_runtime import run_sync
from ..ingestion.aws import AwsAdapter
from ..ingestion.cloudformation import CloudFormationAdapter, CloudFormationRequest
from ..ingestion.requests import AwsCollectionRequest, SourceRequest
from ..ingestion.terraform import (
    TerraformPlanAdapter,
    TerraformPlanRequest,
    TerraformStateAdapter,
    TerraformStateRequest,
)
from ..models.architecture import ArchitectureSnapshot
from .snapshots import SnapshotError


def select_adapter(request: SourceRequest) -> SnapshotAdapter:
    if isinstance(request, AwsCollectionRequest):
        return AwsAdapter()
    if isinstance(request, CloudFormationRequest):
        return CloudFormationAdapter()
    if isinstance(request, TerraformPlanRequest):
        return TerraformPlanAdapter()
    if isinstance(request, TerraformStateRequest):
        return TerraformStateAdapter()
    raise IngestionError("invalid_request", f"no adapter for request kind {request.kind!r}")


def collect_snapshots(
    request: SourceRequest,
    *,
    context: AdapterContext | None = None,
    adapter: SnapshotAdapter | None = None,
) -> list[ArchitectureSnapshot]:
    """Collect with an adapter and return independently validated snapshots; saving is separate."""
    from .async_ingestion import collect_snapshots_async

    return run_sync(lambda: collect_snapshots_async(request, context=context, adapter=adapter))


@dataclass
class AccountOutcome:
    """Result for one configured account; failures never affect other accounts."""

    account_id: str
    snapshot: ArchitectureSnapshot | None = None
    path: Path | None = None
    error: SnapshotError | IngestionError | None = None

    @property
    def complete(self) -> bool:
        """True only when every recorded operation succeeded."""
        return self.snapshot is not None and all(
            op.status == "success" for op in self.snapshot.coverage.operations
        )


def collect_and_save(
    request: AwsCollectionRequest,
    outputs: list[Path],
    *,
    context: AdapterContext | None = None,
    adapter: SnapshotAdapter | None = None,
) -> list[AccountOutcome]:
    """Collect each account into its own new file; existing files are never overwritten."""
    from .async_ingestion import collect_and_save_async

    return run_sync(
        lambda: collect_and_save_async(request, outputs, context=context, adapter=adapter)
    )
