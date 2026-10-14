"""Complete-snapshot adapter for local Terraform show-state exports."""

from collections import defaultdict

from ...models.architecture import (
    ArchitectureSnapshot,
    Coverage,
    Evidence,
    RegionArchitecture,
    ResourceIdentity,
    SnapshotCoverage,
    TerraformStateSource,
    UnresolvedValue,
)
from ...models.enums import AwsRegion, AwsService, CoverageStatus, SourceType
from ..adapter import AdapterContext, IngestionError
from ..requests import SourceRequest
from .catalog import service_for_type
from .mapping import map_resource
from .parsing import load_export
from .relationships import place_resources
from .requests import TerraformStateRequest
from .scope import identifier, resolve_scope
from .sensitivity import read_pointer

IMPORT_OPERATION = "terraform.show.state"


class TerraformStateAdapter:
    def collect(
        self, request: SourceRequest, *, context: AdapterContext
    ) -> list[ArchitectureSnapshot]:
        if not isinstance(request, TerraformStateRequest):
            raise IngestionError("invalid_request", "Expected a TerraformStateRequest")
        started = context.clock()
        snapshot_id = context.uuid_factory()
        export = load_export(request.path)
        scope = resolve_scope(export, request)
        resources = []
        evidence = {}
        counts = defaultdict(int)
        limitations = defaultdict(list)
        seen: set[str] = set()

        def cite(resource, path, value, *, redacted=False):
            evidence_id = f"terraform:{len(evidence)}"
            evidence[evidence_id] = Evidence(
                snapshot_id=snapshot_id,
                resource_id=resource.id,
                source=SourceType.TERRAFORM,
                locator=str(request.path),
                configuration_path=path,
                value=value,
                redacted_paths=[""] if redacted else [],
            )
            resource.evidence_ids.append(evidence_id)
            return [evidence_id]

        for entry in export.resources:
            region = scope.regions_by_address[entry.address]
            service = service_for_type(entry.terraform_type) or AwsService.UNKNOWN
            physical_key = "id" if identifier(entry.values.get("id")) else "arn"
            identity = ResourceIdentity(
                account_id=scope.account_id,
                region=region,
                service=service,
                physical_id=identifier(entry.values.get(physical_key)),
                logical_address=entry.address,
            )
            resource, pointers, reasons = map_resource(entry.terraform_type, entry.values, identity)
            if resource.id in seen:
                raise IngestionError(
                    "invalid_request", "Duplicate physical resource identity in export"
                )
            seen.add(resource.id)
            key = (region, service)
            counts[key] += 1
            cite(resource, entry.path, {"address": entry.address, "type": entry.terraform_type})
            resource.fact_evidence["/identity/logical_address"] = cite(
                resource, f"{entry.path}/address", entry.address
            )
            if identity.physical_id is not None:
                resource.fact_evidence["/identity/physical_id"] = cite(
                    resource, f"{entry.path}/values/{physical_key}", identity.physical_id
                )
            for pointer, source_pointer in sorted(pointers.items()):
                resource.fact_evidence[pointer] = cite(
                    resource,
                    f"{entry.path}{source_pointer}",
                    read_pointer(
                        {"type": entry.terraform_type, "values": entry.values}, source_pointer
                    ),
                )
            for path in entry.redacted_paths:
                cite(resource, f"{entry.path}/values{path}", "[REDACTED]", redacted=True)
                limitations[key].append(
                    UnresolvedValue(
                        resource_id=resource.id,
                        configuration_path=f"{entry.path}/values{path}",
                        reason="Sensitive source value redacted before normalization",
                    )
                )
            for reason in reasons:
                limitations[key].append(
                    UnresolvedValue(
                        resource_id=resource.id, configuration_path=entry.path, reason=reason
                    )
                )
            resources.append(resource)

        if export.skipped_paths or not counts:
            fallback = (scope.regions[0] if scope.regions else AwsRegion.GLOBAL, AwsService.UNKNOWN)
            counts.setdefault(fallback, 0)
            for path in export.skipped_paths:
                limitations[fallback].append(
                    UnresolvedValue(
                        configuration_path=path,
                        reason="Data-source entries are not managed architecture resources",
                    )
                )
        regions, globals_, dependencies = place_resources(resources, evidence)
        by_id = {resource.id: resource for resource in resources}
        for group in dependencies.values():
            for dependency in group:
                if dependency.target.kind != "unresolved":
                    continue
                source = by_id[dependency.source_id]
                limitations[(source.identity.region, source.identity.service)].append(
                    UnresolvedValue(
                        resource_id=source.id,
                        configuration_path=dependency.target.source_path,
                        reason=dependency.target.reason,
                    )
                )
        for region in scope.regions:
            regions.setdefault(region, RegionArchitecture())
        completed = context.clock()
        operations = [
            Coverage(
                account_id=scope.account_id,
                region=region,
                service=service,
                operation=IMPORT_OPERATION,
                status=CoverageStatus.INCOMPLETE
                if limitations[(region, service)]
                else CoverageStatus.SUCCESS,
                result_count=count,
                unresolved_values=limitations[(region, service)],
                started_at=started,
                completed_at=completed,
            )
            for (region, service), count in sorted(counts.items())
        ]
        return [
            ArchitectureSnapshot(
                schema_version="2",
                snapshot_id=snapshot_id,
                account_id=scope.account_id,
                collection_started_at=started,
                collection_completed_at=completed,
                source=TerraformStateSource(
                    path=str(request.path),
                    format_version=export.format_version,
                    terraform_version=export.terraform_version,
                    workspace=request.workspace,
                ),
                coverage=SnapshotCoverage(
                    regions=scope.regions,
                    account_wide=bool(globals_) or not scope.regions,
                    operations=operations,
                ),
                regions=regions,
                global_resources=globals_,
                dependencies_by_region=dependencies,
                evidence=evidence,
            )
        ]
