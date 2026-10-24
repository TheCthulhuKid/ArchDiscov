"""Cited AWS resources and evidence for building snapshots to compare."""

from uuid import UUID

from archdiscov.services import upgrade_snapshot, validate_snapshot
from tests.comparison.snapshots import ACCOUNT, REGION, aws_resource, aws_snapshot


def rid(name, kind="physical"):
    return f"{ACCOUNT}/{REGION}/ec2/{kind}/{name}"


def vpc(physical="vpc-1", *, attributes=None, evidence=(), fact_evidence=None, children=(), **kw):
    data = aws_resource(physical=physical, **kw)
    if attributes is not None:
        data["attributes"] = attributes
    data["evidence_ids"] = list(evidence)
    data["fact_evidence"] = fact_evidence or {}
    data["children"] = list(children)
    return data


def evidence(seed, resource, path, *, status="present", value=None):
    data = {
        "snapshot_id": str(UUID(int=seed)),
        "resource_id": rid(resource),
        "source": "aws",
        "locator": "test",
        "configuration_path": path,
    }
    if status == "present":
        return {**data, "value": value}
    return {**data, "value_status": status, "limitation": "not available"}


def journey_snapshot(source, value, *, seed, schema="2", **options):
    resource, cited = cited_vpc("vpc-1", value, seed=seed, **options.pop("resource", {}))
    snapshot = aws_snapshot([resource], source, seed=seed, evidence=cited, **options)
    if schema == "3":
        return upgrade_snapshot(snapshot)
    if schema == "1":
        return validate_snapshot({**snapshot.model_dump(mode="json"), "schema_version": "1"})
    return snapshot


def cited_vpc(physical, value, *, seed, **extra):
    """A VPC whose `a` attribute is cited, so a difference has evidence on its side."""
    resource = {
        **aws_resource(physical=physical, **extra.pop("identity", {})),
        "attributes": {"a": value},
        "evidence_ids": ["cite"],
        "fact_evidence": {"/attributes/a": ["cite"]},
        **extra,
    }
    return resource, {"cite": evidence(seed, physical, "a")}
