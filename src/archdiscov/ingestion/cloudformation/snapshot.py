"""Source-owned evidence and qualified per-scope import coverage."""

from collections import defaultdict

from ...models.architecture import (
    ArchitectureSnapshot,
    CloudFormationSource,
    RegionArchitecture,
    SnapshotCoverage,
    UnresolvedValue,
)
from ...models.enums import AwsService, SourceType
from ..import_records import cite_source, import_coverage

IMPORT_OPERATION = "cloudformation.template"


class SnapshotBuilder:
    def __init__(self, request, context):
        self.request = request
        self.context = context
        self.started = context.clock()
        self.snapshot_id = context.uuid_factory()
        self.resources = {}
        self.evidence = {}
        self.limitations = defaultdict(list)
        self.dependencies = defaultdict(list)
        self.omissions = []

    def cite(self, resource, path, value=None, *, reason=None, redacted=False):
        return cite_source(
            self.evidence,
            resource,
            snapshot_id=self.snapshot_id,
            source=SourceType.CLOUDFORMATION,
            locator=str(self.request.path),
            path=path,
            value=value,
            reason=reason,
            redacted=redacted,
        )

    def limit(self, resource, path, reason):
        key = resource.identity.region, resource.identity.service
        limitation = UnresolvedValue(
            resource_id=resource.id, configuration_path=path, reason=reason
        )
        if limitation not in self.limitations[key]:
            self.limitations[key].append(limitation)

    def omit(self, path, reason):
        self.omissions.append(UnresolvedValue(configuration_path=path, reason=reason))

    def finish(self, template, regions, globals_):
        counts = defaultdict(int)
        for resource in self.resources.values():
            counts[(resource.identity.region, resource.identity.service)] += 1
        if self.omissions or not counts:
            counts[(self.request.region, AwsService.UNKNOWN)] += 0
            self.limitations[(self.request.region, AwsService.UNKNOWN)].extend(self.omissions)
        completed = self.context.clock()
        operations = import_coverage(
            account_id=self.request.account_id,
            operation=IMPORT_OPERATION,
            counts=counts,
            limitations=self.limitations,
            started=self.started,
            completed=completed,
        )
        regions.setdefault(self.request.region, RegionArchitecture())
        return ArchitectureSnapshot(
            schema_version="2",
            snapshot_id=self.snapshot_id,
            account_id=self.request.account_id,
            collection_started_at=self.started,
            collection_completed_at=completed,
            source=CloudFormationSource(
                path=str(self.request.path),
                stack_id=self.request.stack_id,
                stack_name=self.request.stack_name,
                template_format_version=template.get("AWSTemplateFormatVersion"),
            ),
            coverage=SnapshotCoverage(
                regions=(self.request.region,), account_wide=bool(globals_), operations=operations
            ),
            regions=regions,
            global_resources=globals_,
            dependencies_by_region=dict(self.dependencies),
            evidence=self.evidence,
        )
