"""Complete-snapshot adapter for proposed Terraform configuration."""

from ...models.architecture import ArchitectureSnapshot, ResourceLifecycle, TerraformPlanSource
from ...models.enums import LifecycleAction, LifecycleValueScope
from ..adapter import AdapterContext, IngestionError
from ..requests import SourceRequest
from .parsing import StateResource, load_json
from .plan_parsing import parse_plan
from .plan_relationships import configuration_dependencies, place_plan_resources
from .requests import TerraformPlanRequest
from .scope import _context, resolve_scope
from .snapshot import SnapshotBuilder

IMPORT_OPERATION = "terraform.show.plan"


def _validate_before_context(export, scope):
    for entry in export.resources:
        if not entry.before_values:
            continue
        before = StateResource(
            entry.address, entry.terraform_type, entry.before_values, entry.path, []
        )
        accounts, regions = _context(before)
        if accounts - {scope.account_id}:
            raise IngestionError(
                "invalid_request",
                "Supply one account_id; missing, conflicting, or mixed-account context",
            )
        if len(regions) > 1:
            raise IngestionError("invalid_request", "Conflicting Terraform resource region context")


class TerraformPlanAdapter:
    def collect(
        self, request: SourceRequest, *, context: AdapterContext
    ) -> list[ArchitectureSnapshot]:
        if not isinstance(request, TerraformPlanRequest):
            raise IngestionError("invalid_request", "Expected a TerraformPlanRequest")
        started = context.clock()
        data = load_json(request.path)
        export = parse_plan(data)
        scope = resolve_scope(export, request)
        _validate_before_context(export, scope)
        builder = SnapshotBuilder(request, context, scope, started)
        for entry in export.resources:
            resource = builder.add(entry, entry.values_path)
            resource.lifecycle = ResourceLifecycle(
                actions=entry.actions,
                value_scope=LifecycleValueScope.BEFORE
                if entry.actions == [LifecycleAction.DELETE]
                else LifecycleValueScope.AFTER,
            )
            if entry.actions_path is not None:
                resource.fact_evidence["/lifecycle/actions"] = builder.cite(
                    resource, entry.actions_path, entry.actions
                )
            for pointer in entry.unknown_paths:
                path = f"{entry.path}/change/after_unknown{pointer}"
                builder.cite(resource, path, None, unresolved=True)
                builder.limit(resource, path, "Planned source value is unknown")
        _qualify_plan(data, builder)
        configuration_links = configuration_dependencies(data, builder)

        def place(resources, evidence):
            regions, globals_, links = place_plan_resources(resources, evidence)
            for region, group in configuration_links.items():
                links.setdefault(region, []).extend(group)
            return regions, globals_, links

        return [
            builder.finish(
                export,
                TerraformPlanSource(
                    path=str(request.path),
                    format_version=export.format_version,
                    terraform_version=export.terraform_version,
                    workspace=request.workspace,
                ),
                IMPORT_OPERATION,
                place,
            )
        ]


def _qualify_plan(data, builder):
    for key in ("complete", "errored"):
        if key in data and not isinstance(data[key], bool):
            raise IngestionError("invalid_request", "Terraform plan status flags must be booleans")
    if data.get("errored", False):
        raise IngestionError("invalid_request", "An errored Terraform plan cannot be imported")
    deferred = data.get("deferred_changes", [])
    if not isinstance(deferred, list):
        raise IngestionError("invalid_request", "Terraform deferred_changes must be a list")
    if data.get("complete") is False:
        builder.qualify("/complete", "Terraform marked this plan as incomplete")
    if deferred:
        builder.qualify(
            "/deferred_changes", "Deferred changes are unavailable in this planned inventory"
        )
