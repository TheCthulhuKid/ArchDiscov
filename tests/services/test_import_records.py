from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from archdiscov.ingestion.import_records import cite_source, import_coverage
from archdiscov.models.architecture import ResourceIdentity, UnresolvedValue, Vpc
from archdiscov.models.enums import AwsRegion, AwsService, SourceType

ACCOUNT = "123456789012"
STARTED = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize("source", [SourceType.TERRAFORM, SourceType.CLOUDFORMATION])
def test_source_citations_keep_ids_ownership_and_value_status(source):
    resource = Vpc(
        identity=ResourceIdentity(
            account_id=ACCOUNT,
            region=AwsRegion.US_EAST_1,
            service=AwsService.EC2,
            logical_address="Network",
        )
    )
    evidence = {}
    snapshot_id = UUID(int=1)

    def cite(value, **kwargs):
        return cite_source(
            evidence,
            resource,
            snapshot_id=snapshot_id,
            source=source,
            locator="template.json",
            path="/value",
            value=value,
            **kwargs,
        )[0]

    present = cite(None)
    redacted = cite("[REDACTED]", redacted=True)
    unknown = cite(None, reason="Source value unavailable")
    assert resource.evidence_ids == [f"{source.value}:{index}" for index in range(3)]
    assert all(
        e.resource_id == resource.id
        and e.snapshot_id == snapshot_id
        and e.source == source
        and e.locator == "template.json"
        and e.configuration_path == "/value"
        for e in evidence.values()
    )
    assert evidence[present].value_status == "present"
    assert evidence[redacted].value_status == "redacted"
    assert evidence[redacted].redacted_paths == [""]
    assert evidence[unknown].value_status == "unresolved"
    assert evidence[unknown].limitation == "Source value unavailable"


def test_import_coverage_orders_scopes_without_mutating_caller_limitations():
    regional = AwsRegion.US_EAST_1, AwsService.EC2
    global_ = AwsRegion.GLOBAL, AwsService.IAM
    limitation = UnresolvedValue(configuration_path="/missing", reason="Unavailable fact")
    counts = {regional: 0, global_: 2}
    limitations = {global_: [limitation, limitation]}
    completed = STARTED + timedelta(seconds=1)
    coverage = import_coverage(
        account_id=ACCOUNT,
        operation="source.import",
        counts=counts,
        limitations=limitations,
        started=STARTED,
        completed=completed,
    )
    assert [(c.region, c.service) for c in coverage] == sorted(counts)
    by_region = {c.region: c for c in coverage}
    assert by_region[AwsRegion.US_EAST_1].status == "success"
    assert by_region[AwsRegion.US_EAST_1].result_count == 0
    assert by_region[AwsRegion.GLOBAL].status == "incomplete"
    assert by_region[AwsRegion.GLOBAL].result_count == 2
    assert by_region[AwsRegion.GLOBAL].unresolved_values == [limitation, limitation]
    assert all(c.started_at == STARTED and c.completed_at == completed for c in coverage)
    assert limitations == {global_: [limitation, limitation]}
