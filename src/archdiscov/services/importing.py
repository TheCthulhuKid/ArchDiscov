"""Explicit-format offline imports using complete-snapshot adapters and immutable storage."""

from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from ..ingestion.adapter import AdapterContext, IngestionError
from ..models.architecture import ArchitectureSnapshot
from ..models.enums import AwsRegion, CoverageStatus
from .import_requests import ImportOptions, IngestFormat, source_request
from .ingestion import collect_snapshots
from .snapshots import load_snapshot, save_snapshot


@dataclass(frozen=True)
class IngestResult:
    snapshot: ArchitectureSnapshot
    path: Path

    @property
    def complete(self) -> bool:
        """Coverage success is not a claim of full mapping or effective architecture."""
        return all(
            operation.status == CoverageStatus.SUCCESS
            for operation in self.snapshot.coverage.operations
        )


def ingest_snapshot(
    input_path: str | Path,
    output: str | Path,
    *,
    source_format: IngestFormat | str = IngestFormat.SNAPSHOT,
    account_id: str | None = None,
    region: AwsRegion | str | None = None,
    workspace: str | None = None,
    stack_name: str | None = None,
    stack_id: str | None = None,
    parameters_file: str | Path | None = None,
    sensitive_paths: tuple[str, ...] = (),
    context: AdapterContext | None = None,
) -> IngestResult:
    """Import one explicit source, validate, and publish only to a new destination.

    Snapshot JSON remains the default. Context options never override context in
    stored snapshots; raw adapters enforce their own inference and conflict rules.
    """
    try:
        selected = IngestFormat(source_format)
        options = ImportOptions(
            account_id=account_id,
            region=region,
            workspace=workspace,
            stack_name=stack_name,
            stack_id=stack_id,
            parameters_file=Path(parameters_file) if parameters_file is not None else None,
            sensitive_paths=sensitive_paths,
        )
        request = source_request(selected, Path(input_path), options)
    except (ValidationError, ValueError):
        raise IngestionError(
            "invalid_request", "Invalid source format or import context."
        ) from None
    if request is None:
        snapshot = load_snapshot(input_path)
    else:
        snapshots = collect_snapshots(request, context=context)
        if len(snapshots) != 1:
            raise IngestionError("invalid_request", "Import adapter must return one snapshot.")
        snapshot = snapshots[0]
    destination = save_snapshot(snapshot, output)
    return IngestResult(snapshot=snapshot, path=destination)
