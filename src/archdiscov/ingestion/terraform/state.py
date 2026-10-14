"""Complete-snapshot adapter for local Terraform show-state exports."""

from ...models.architecture import ArchitectureSnapshot, TerraformStateSource
from ..adapter import AdapterContext, IngestionError
from ..requests import SourceRequest
from .parsing import load_export
from .relationships import place_resources
from .requests import TerraformStateRequest
from .scope import resolve_scope
from .snapshot import SnapshotBuilder

IMPORT_OPERATION = "terraform.show.state"


class TerraformStateAdapter:
    def collect(
        self, request: SourceRequest, *, context: AdapterContext
    ) -> list[ArchitectureSnapshot]:
        if not isinstance(request, TerraformStateRequest):
            raise IngestionError("invalid_request", "Expected a TerraformStateRequest")
        started = context.clock()
        export = load_export(request.path)
        scope = resolve_scope(export, request)
        builder = SnapshotBuilder(request, context, scope, started)
        for entry in export.resources:
            builder.add(entry, f"{entry.path}/values")
        return [
            builder.finish(
                export,
                TerraformStateSource(
                    path=str(request.path),
                    format_version=export.format_version,
                    terraform_version=export.terraform_version,
                    workspace=request.workspace,
                ),
                IMPORT_OPERATION,
                place_resources,
            )
        ]
