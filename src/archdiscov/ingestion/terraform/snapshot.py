"""Shared fact mapping, citations, and coverage for offline Terraform exports."""

from collections import defaultdict

from ...models.architecture import (
    ArchitectureSnapshot,
    RegionArchitecture,
    ResourceIdentity,
    SnapshotCoverage,
    UnresolvedValue,
)
from ...models.enums import AwsRegion, AwsService, SourceType
from ...models.json_pointer import read_pointer
from ..adapter import IngestionError
from ..import_records import cite_source, import_coverage
from .catalog import service_for_type
from .mapping import map_resource
from .scope import identifier


class SnapshotBuilder:
    def __init__(self, request, context, scope, started):
        self.request = request
        self.context = context
        self.scope = scope
        self.started = started
        self.snapshot_id = context.uuid_factory()
        self.resources = []
        self.evidence = {}
        self.counts = defaultdict(int)
        self.limitations = defaultdict(list)
        self.seen = set()

    def cite(self, resource, path, value, *, redacted=False, unresolved=False):
        return cite_source(
            self.evidence,
            resource,
            snapshot_id=self.snapshot_id,
            source=SourceType.TERRAFORM,
            locator=str(self.request.path),
            path=path,
            value=value,
            redacted=redacted,
            reason="Planned source value is unknown" if unresolved else None,
        )

    def limit(self, resource, path, reason):
        self.limitations[(resource.identity.region, resource.identity.service)].append(
            UnresolvedValue(resource_id=resource.id, configuration_path=path, reason=reason)
        )

    def qualify(self, path, reason):
        if not self.counts:
            region = self.scope.regions[0] if self.scope.regions else AwsRegion.GLOBAL
            self.counts[(region, AwsService.UNKNOWN)] = 0
        for key in self.counts:
            self.limitations[key].append(UnresolvedValue(configuration_path=path, reason=reason))

    def add(self, entry, values_path):
        region = self.scope.regions_by_address[entry.address]
        service = service_for_type(entry.terraform_type) or AwsService.UNKNOWN
        physical_key = "id" if identifier(entry.values.get("id")) else "arn"
        identity = ResourceIdentity(
            account_id=self.scope.account_id,
            region=region,
            service=service,
            physical_id=identifier(entry.values.get(physical_key)),
            logical_address=entry.address,
        )
        resource, pointers, reasons = map_resource(entry.terraform_type, entry.values, identity)
        if resource.id in self.seen:
            raise IngestionError(
                "invalid_request", "Duplicate physical resource identity in export"
            )
        self.seen.add(resource.id)
        self.counts[(region, service)] += 1
        self.cite(resource, entry.path, {"address": entry.address, "type": entry.terraform_type})
        resource.fact_evidence["/identity/logical_address"] = self.cite(
            resource, f"{entry.path}/address", entry.address
        )
        if identity.physical_id is not None:
            resource.fact_evidence["/identity/physical_id"] = self.cite(
                resource, f"{values_path}/{physical_key}", identity.physical_id
            )
        for pointer, source in sorted(pointers.items()):
            path = (
                values_path + source.removeprefix("/values")
                if source.startswith("/values")
                else entry.path + source
            )
            resource.fact_evidence[pointer] = self.cite(
                resource,
                path,
                read_pointer({"type": entry.terraform_type, "values": entry.values}, source),
            )
        for path in entry.redacted_paths:
            self.cite(resource, values_path + path, "[REDACTED]", redacted=True)
            self.limit(
                resource, values_path + path, "Sensitive source value redacted before normalization"
            )
        for reason in reasons:
            self.limit(resource, entry.path, reason)
        self.resources.append(resource)
        return resource

    def finish(self, export, source, operation, placement):
        if export.skipped_paths or not self.counts:
            fallback = (
                self.scope.regions[0] if self.scope.regions else AwsRegion.GLOBAL,
                AwsService.UNKNOWN,
            )
            self.counts.setdefault(fallback, 0)
            self.limitations[fallback].extend(
                UnresolvedValue(
                    configuration_path=path,
                    reason="Data-source entries are not managed architecture resources",
                )
                for path in export.skipped_paths
            )
        regions, globals_, dependencies = placement(self.resources, self.evidence)
        by_id = {resource.id: resource for resource in self.resources}
        for group in dependencies.values():
            for dependency in group:
                if dependency.target.kind == "unresolved":
                    self.limit(
                        by_id[dependency.source_id],
                        dependency.target.source_path,
                        dependency.target.reason,
                    )
        for region in self.scope.regions:
            regions.setdefault(region, RegionArchitecture())
        completed = self.context.clock()
        operations = import_coverage(
            account_id=self.scope.account_id,
            operation=operation,
            counts=self.counts,
            limitations=self.limitations,
            started=self.started,
            completed=completed,
        )
        return ArchitectureSnapshot(
            schema_version="2",
            snapshot_id=self.snapshot_id,
            account_id=self.scope.account_id,
            collection_started_at=self.started,
            collection_completed_at=completed,
            source=source,
            coverage=SnapshotCoverage(
                regions=self.scope.regions,
                account_wide=bool(globals_) or not self.scope.regions,
                operations=operations,
            ),
            regions=regions,
            global_resources=globals_,
            dependencies_by_region=dependencies,
            evidence=self.evidence,
        )
