"""Offline GraphIndex measurements; run from the root with .venv/bin/python."""

import argparse
import gc
import json
import platform
import statistics
import sys
import tracemalloc
from collections.abc import Callable
from datetime import UTC, datetime
from importlib.metadata import version
from time import perf_counter
from uuid import UUID

from archdiscov.graph import GraphIndex
from archdiscov.models.architecture import (
    CollectionFailure,
    LocalTarget,
    MockSource,
    ResourceIdentity,
    UnknownResource,
    UnresolvedTarget,
)
from archdiscov.models.cloud.contracts import (
    AwsTargetScope,
    CloudCoverage,
    CloudDependency,
    CloudEvidence,
    CloudExternalTarget,
    CloudOperationCoverage,
    CloudSource,
)
from archdiscov.models.cloud.scope import AwsScope
from archdiscov.models.cloud.snapshot import CloudRegion, CloudSnapshot
from archdiscov.models.enums import AttachmentKind, DependencyRelationship

STAMP = datetime(2026, 1, 1, tzinfo=UTC)
SNAPSHOT_ID = UUID("00000000-0000-0000-0000-000000000024")
LOCATION = "eu-west-1"
POINTER = "/attributes/ordinal"


def synthetic_snapshot(shape: str, size: int) -> CloudSnapshot:
    """Size is total nodes: one star or one chain, not real AWS containment."""
    scope = AwsScope(account_id="123456789012")
    nodes = [
        UnknownResource(
            identity=ResourceIdentity(
                account_id=scope.account_id,
                region=LOCATION,
                service="ec2",
                physical_id=f"synthetic-{i:06d}",
            ),
            aws_resource_type="AWS::EC2::SyntheticNode",
            attributes={"ordinal": i, "labels": ["offline", "profile"]},
            evidence_ids=[f"e-{i:06d}"],
            fact_evidence={POINTER: [f"e-{i:06d}"]},
        )
        for i in range(size)
    ]
    if shape == "wide":
        nodes[0].children = nodes[1:]
    else:
        for parent, child in zip(nodes, nodes[1:]):
            parent.children = [child]
    evidence = {
        f"e-{i:06d}": CloudEvidence(
            snapshot_id=SNAPSHOT_ID,
            resource_id=node.id,
            source="mock",
            locator="offline-synthetic",
            configuration_path=f"Nodes[{i}].ordinal",
            value=i,
            scope=scope,
            location=LOCATION,
            service="ec2",
        )
        for i, node in enumerate(nodes)
    }
    dependencies = [
        CloudDependency(
            source_id=node.id,
            target=LocalTarget(resource_id=nodes[(i + 1) % size].id),
            location=LOCATION,
            relationship=DependencyRelationship.ATTACHED_TO,
            attributes={"kind": AttachmentKind.SECURITY_GROUP_REFERENCE},
            evidence_ids=node.evidence_ids,
        )
        for i, node in enumerate(nodes)
    ]
    for target in (
        CloudExternalTarget(scope=AwsTargetScope(), identifier="external-synthetic", service="ec2"),
        UnresolvedTarget(reason="Offline unknown target", source_path="Nodes[0].target"),
    ):
        dependencies.append(
            CloudDependency(
                source_id=nodes[0].id,
                target=target,
                location=LOCATION,
                relationship=DependencyRelationship.ATTACHED_TO,
                evidence_ids=nodes[0].evidence_ids,
            )
        )
    operations = []
    for status in ("success", "incomplete", "failed", "skipped", "unsupported"):
        operations.append(
            CloudOperationCoverage(
                scope=scope,
                location=LOCATION,
                service="ec2",
                operation=f"Synthetic-{status}",
                status=status,
                started_at=STAMP,
                completed_at=STAMP,
                result_count=size if status in ("success", "incomplete") else None,
                reason="Offline synthetic limitation" if status != "success" else None,
                failures=(
                    [CollectionFailure(code="SyntheticFailure", message="Offline failure")]
                    if status == "failed"
                    else []
                ),
            )
        )
    return CloudSnapshot(
        snapshot_id=SNAPSHOT_ID,
        scope=scope,
        collection_started_at=STAMP,
        collection_completed_at=STAMP,
        source=CloudSource(platform="aws", provenance=MockSource(label="offline-profile")),
        regions={LOCATION: CloudRegion(resources=[nodes[0]])},
        evidence=evidence,
        dependencies=dependencies,
        coverage=CloudCoverage(
            scope=scope, locations=[LOCATION], scope_wide=False, operations=operations
        ),
    )


def measure(operation: Callable[[], object], calls: int, repeats: int) -> dict:
    """Warm once; time without tracing; trace one separate call, retaining its result."""
    operation()
    samples = []
    for _ in range(repeats):
        gc.collect()
        start = perf_counter()
        for _ in range(calls):
            operation()
        samples.append((perf_counter() - start) / calls)
    gc.collect()
    tracemalloc.start()
    try:
        result = operation()
        retained, peak = tracemalloc.get_traced_memory()
        del result
    finally:
        tracemalloc.stop()
    return {
        "calls_per_sample": calls,
        "median_us_per_call": round(statistics.median(samples) * 1e6, 3),
        "min_us_per_call": round(min(samples) * 1e6, 3),
        "max_us_per_call": round(max(samples) * 1e6, 3),
        "single_call_retained_bytes": retained,
        "single_call_peak_bytes": peak,
    }


def profile_case(shape: str, size: int, repeats: int, rounds: int) -> dict:
    snapshot = synthetic_snapshot(shape, size)
    construction = measure(lambda: GraphIndex(snapshot), 1, repeats)
    index = GraphIndex(snapshot)
    ids = index.resource_ids
    root = index.root_ids[0]
    leaf = next(resource_id for resource_id in reversed(ids) if not index.child_ids(resource_id))
    evidence_ids = index.evidence_ids
    keys = index.coverage_keys
    edges = index.dependency_edges
    terminals = index.terminal_ids
    assert len(ids) == size
    assert len(index.containment_edges) == size - 1
    assert len(edges) == size + 2
    assert len(evidence_ids) == size and len(keys) == 5 and len(terminals) == 2
    assert index.fact_evidence_ids(leaf, POINTER)
    assert index.child_ids(leaf) == ()

    # Sweeps discard each result immediately: no accumulation of detached trees.
    def sweep(operation, values):
        def run():
            for value in values:
                operation(value)

        return run

    workloads = {
        "scalar.id_tuples": (lambda: (index.resource_ids, index.root_ids), 2),
        "scalar.parent_sweep": (sweep(index.parent_id, ids), size),
        "scalar.child_sweep": (sweep(index.child_ids, ids), size),
        "scalar.physical_key_sweep": (sweep(index.physical_key, ids), size),
        "scalar.owner_citation_sweep": (sweep(index.resource_evidence_ids, ids), size),
        "scalar.fact_citation_sweep": (
            sweep(lambda rid: index.fact_evidence_ids(rid, POINTER), ids),
            size,
        ),
        "scalar.coverage_keys": (lambda: index.coverage_keys, 1),
        "scalar.misses": (
            lambda: (
                index.parent_id("missing"),
                index.child_ids("missing"),
                index.fact_evidence_ids("missing", POINTER),
                index.get_resource("missing"),
            ),
            4,
        ),
        "detached.outgoing_sweep": (sweep(index.outgoing_edges, ids), size),
        "detached.incoming_sweep": (sweep(index.incoming_edges, ids), size),
        "detached.filtered_outgoing_sweep": (
            sweep(
                lambda rid: index.outgoing_edges(
                    rid,
                    relationship=DependencyRelationship.ATTACHED_TO,
                    attachment_kind=AttachmentKind.SECURITY_GROUP_REFERENCE,
                ),
                ids,
            ),
            size,
        ),
        "detached.edge_sweep": (
            sweep(index.get_dependency_edge, [e.id for e in edges]),
            len(edges),
        ),
        "detached.terminal_sweep": (sweep(index.get_terminal, terminals), len(terminals)),
        "detached.evidence_sweep": (sweep(index.get_evidence, evidence_ids), size),
        "detached.fact_evidence_sweep": (
            sweep(lambda rid: index.evidence_for_fact(rid, POINTER), ids),
            size,
        ),
        "detached.coverage_sweep": (sweep(index.get_coverage, keys), len(keys)),
        "detached.resource_leaf": (lambda: index.get_resource(leaf), 1),
        "detached.resource_root": (lambda: index.get_resource(root), 1),
        "detached.resources_all": (index.resources, 1),
    }
    measurements = {}
    for name, (operation, api_calls) in workloads.items():
        row = measure(operation, rounds, repeats)
        row["api_calls_per_workload"] = api_calls
        measurements[name] = row
    return {
        "shape": shape,
        "nodes": size,
        "depth": 2 if shape == "wide" else size,
        "containment_edges": size - 1,
        "dependencies": len(edges),
        "evidence": len(evidence_ids),
        "coverage_records": len(keys),
        "construction": construction,
        "workloads": measurements,
    }


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wide", type=positive_int, nargs="+", default=[100, 1000])
    parser.add_argument("--deep", type=positive_int, nargs="+", default=[16, 64])
    parser.add_argument("--repeats", type=positive_int, default=3)
    parser.add_argument("--lookup-rounds", type=positive_int, default=3)
    args = parser.parse_args()
    if min(args.wide + args.deep) < 2 or max(args.deep) > 64:
        parser.error("sizes must be >= 2; deep sizes must be <= 64 (model recursion safety)")
    report = {
        "environment": {
            "python": sys.version,
            "executable": sys.executable,
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "pydantic": version("pydantic"),
            "archdiscov": version("archdiscov"),
            "recursion_limit": sys.getrecursionlimit(),
            "gc_enabled": gc.isenabled(),
        },
        "repeats": args.repeats,
        "lookup_rounds": args.lookup_rounds,
        "cases": [
            profile_case(shape, size, args.repeats, args.lookup_rounds)
            for shape, sizes in (("wide", args.wide), ("deep", args.deep))
            for size in sizes
        ],
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
