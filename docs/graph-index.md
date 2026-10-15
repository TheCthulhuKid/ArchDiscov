# Snapshot graph index foundation

Step 24 implements the complete in-memory index boundary: resource ownership,
lookup, explicit containment, dependency adjacency with terminal targets, and
evidence/coverage indexes. Composed fixture acceptance and reproducible synthetic
profiling verify these pieces together. Traversal, query services, and analyzers
remain subsequent steps; there is no graph CLI command yet.

```python
from archdiscov.graph import GraphIndex, PhysicalResourceKey
from archdiscov.models.enums import ResourceType
from archdiscov.services.snapshots import load_snapshot

snapshot = load_snapshot("tests/fixtures/snapshots/hierarchy.json")
index = GraphIndex(snapshot)
key = PhysicalResourceKey.from_values(index.scope, ResourceType.VPC, "vpc-main")
candidates = index.find_physical(key)
assert len(candidates) == 1
vpc = index.get_resource(candidates[0].id)
assert str(vpc.cidr_blocks[0]) == "10.0.0.0/16"
```

## Ownership and lookup

`GraphIndex(snapshot)` constructs one detached, validated `SnapshotAccess` and
builds private indexes once. It accepts AWS schemas `1`–`2` and cloud schema `3`;
legacy normalization stays within the access boundary, without file migration.
Invalid current model mutations fail construction rather than becoming index data.

- `snapshot_id` identifies the selected snapshot; `scope` returns a detached scope.
- `resource_ids` is an immutable tuple sorted by stable ID.
- `resources()` returns all stored resources in that order, preserving concrete
  classes, typed fields, citations, lifecycle, and stored children. It copies each
  resource tree once: a child is the same returned object as its standalone entry
  within that call. Separate calls return independent copies.
- `get_resource(id)` returns a detached resource or `None` for an unknown ID.
- `physical_key(id)` returns an immutable `PhysicalResourceKey`, or `None` for a
  logical-only resource or an unknown ID.
- `find_physical(key)` returns every candidate in stable-ID order, or `()` when
  none match. It never picks an arbitrary winner.

Returned resources, nested children, dictionaries, identities, and scopes may be
changed by callers without affecting the index. Changing the original snapshot
also cannot affect it. Private borrowed models are internal implementation data,
not a public mutation API. Returned trees retain containment data but do not imply
connectivity; no resources, parents, or attachments are invented.

## Physical identity qualification

Construct lookup keys with `PhysicalResourceKey.from_values(scope, native_type,
physical_id)`. Keys contain the cloud platform, complete scope (AWS account and
partition; Azure subscription and optional tenant), provider-native resource type,
and physical identifier. Their scope representation is an immutable tuple, not a
borrowed mutable model. Direct construction also sorts scope fields, rejects blank
native types/identifiers, and applies the same Azure normalization as the factory.

AWS physical identifiers and native types retain exact spelling. Azure ARM IDs
and native types use the existing lowercase matching rules while returned
resources preserve original source spelling. Unknown resources use their native
type, not the `unknown`/`cloud_unknown` discriminator. Names and logical addresses
are not physical lookup keys.

Matching deliberately does not select a region: region-local AWS identifiers can
have several candidates within an account. Inspect their recorded identities to
disambiguate. Stable-ID lookup retains the existing AWS region/service identity
rules. No cross-scope or cross-platform matches are inferred.

## Explicit containment

Containment is indexed once from `SnapshotAccess.containment()`, which traverses
stored `children` using the shared resource-tree walker. It works identically for
regional and scope-wide trees across schemas `1`–`3`.

- `root_ids` lists parentless stored resources in stable-ID order. Snapshot,
  region, and scope envelopes are not invented graph nodes.
- `parent_id(id)` returns the stored parent ID, or `None` for a root or unknown ID.
- `child_ids(id)` returns direct child IDs in stable-ID order, or `()` for a leaf
  or unknown ID. It does not perform recursive traversal.
- `containment_edges` is an immutable tuple of frozen `ContainmentEdge` values,
  ordered by `(source_id, target_id)`, from parent to child. Reverse lookup uses
  the same edges; it does not assert an additional relationship.

Structural edges have `evidence_ids == ()`: resource and dependency citations do
not prove containment and are not copied onto these edges. Containment remains
distinct from evidence-backed dependencies. An `unresolved_parent` attribute,
physical identifier, or attachment never creates an edge or a missing parent.
Input mutations and changes to returned resource trees cannot change adjacency.

```python
subnet_id = next(
    child_id
    for child_id in index.child_ids(vpc.id)
    if index.get_resource(child_id).resource_type == ResourceType.SUBNET
)
assert index.parent_id(subnet_id) == vpc.id
assert index.parent_id(vpc.id) is None
assert all(edge.evidence_ids == () for edge in index.containment_edges)
```

## Dependencies and terminal targets

`dependency_edges` returns all recorded directed dependencies as a tuple of frozen
`DependencyEdge` values. Each edge retains its source ID, full typed target,
source location (`None` for scope-wide), relationship, attributes, and original
source-owned evidence IDs as a tuple. Dependencies are separate from containment:
configuration fields, child placement, and matching names never create edges.

- `outgoing_edges(resource_id)` and `incoming_edges(target_id)` return recorded
  edges, optionally filtered by `relationship=DependencyRelationship.…` and/or
  `attachment_kind=AttachmentKind.…`. Reverse lookup returns the original edge,
  not a reversed assertion. Unknown IDs and unmatched filters return `()`.
- `attachment_kind` recognizes existing enum values in `attributes["kind"]`.
  Absent, custom, or non-string details remain in attributes and yield `None`;
  they are still available through unfiltered and relationship-only lookups.
- `get_dependency_edge(edge_id)` returns an edge or `None`. Edge IDs are
  snapshot-local handles derived from SHA-256 of the full normalized dependency
  JSON (sorted object keys), plus a duplicate occurrence number. Ordering follows
  that canonical JSON, not input list order. Distinct parallel edges and even
  identical stored occurrences remain separate; their citations are never merged.
- For local targets, `edge.target_id` is the stored resource ID. External and
  unresolved targets instead have an edge-specific terminal ID. `terminal_ids`
  lists those IDs in sorted order; `get_terminal(id)` returns a `TerminalTarget`
  retaining the originating `edge_id` and complete typed target, or `None`.

External terminals keep their own platform/scope, identifier, location, service,
and native type, including unknown context; source scope is never substituted.
Unresolved terminals preserve their reason and source path. Repeated references
to the same external identifier have distinct terminals tied to their own edges.
Terminals are not resources: `get_resource(terminal_id)` returns `None`, incoming
lookup returns the originating edge, and outgoing lookup is always empty.
Dependency cycles and self-edges are indexed without traversal or inference.

The outer edge/terminal records are frozen, but their nested target models and
attribute dictionaries are mutable **detached copies**. Every public lookup
returns independent copies; changing these values or the original snapshot cannot
change any index state. Do not use mutated returned copies as authoritative edge
records. Traversal remains a later step.

```python
from archdiscov.models.enums import AttachmentKind, DependencyRelationship

network_index = GraphIndex(load_snapshot("tests/fixtures/snapshots/network.json"))
routes = network_index.outgoing_edges(
    "123456789012/eu-west-1/ec2/physical/rtb-main",
    relationship=DependencyRelationship.ATTACHED_TO,
    attachment_kind=AttachmentKind.ROUTE_TARGET,
)
assert len(routes) == 2
for edge in routes:
    assert edge in network_index.incoming_edges(edge.target_id)
    assert edge.evidence_ids
    if edge.target.kind != "local":
        terminal = network_index.get_terminal(edge.target_id)
        assert terminal.edge_id == edge.id
        assert network_index.get_resource(terminal.id) is None
        assert network_index.outgoing_edges(terminal.id) == ()
```

## Evidence and exact fact citations

- `evidence_ids` lists all stored evidence IDs in sorted order, including uncited
  records. `get_evidence(id)` returns a detached `CloudEvidence` or `None`.
- `resource_evidence_ids(resource_id)` returns all evidence owned by that stored
  resource, in evidence-ID order. This is not restricted to its broad
  `resource.evidence_ids` citations. `evidence_for_resource(id)` returns those
  records as a tuple.
- `fact_evidence_ids(resource_id, pointer)` returns the explicitly stored
  citations for that exact resource-relative JSON Pointer, in original citation
  order. `evidence_for_fact(id, pointer)` returns the corresponding records.

Fact lookups do not expand parent/child pointers, decode source configuration
paths, or infer citations from resource-level evidence. Use the exact escaped
pointer stored in `fact_evidence` (for example `/attributes/a~1b~0c` for key
`a/b~c`). Unknown resources, uncited facts, and unmatched pointers return `()`.
An empty lookup means no explicit citation, not that a fact is absent or false.

Values, source paths, redaction paths, limitations, and value statuses are retained
unchanged after snapshot validation. A present `null` remains distinguishable
from unresolved/omitted evidence. All returned records, scopes, and nested values
are detached; caller mutations cannot alter subsequent lookups.

## Complete-context operation coverage

`CoverageKey`, exported from `archdiscov.graph`, is a frozen, hashable lookup key
shared with analysis integrity validation. Build it using
`CoverageKey.from_values(scope, location, service, operation)` or
`CoverageKey.from_record(record)`. It includes the platform and complete immutable
scope (AWS account/partition or Azure subscription/tenant), location, canonical
service key, and exact operation name. Azure service casing is folded using the
existing context rule; AWS service casing and operation names remain exact. Both
direct construction and factories canonicalize service casing and scope ordering.
Scope-wide operations use `location=None`, not the legacy `global` sentinel.

- `coverage_keys` lists recorded keys ordered by location (scope-wide first),
  canonical service, then operation. Each graph owns a single snapshot scope.
- `get_coverage(key)` returns a detached `CloudOperationCoverage` or `None` when
  no record matches the complete context.
- `coverage_records()` returns all recorded outcomes in key order, retaining
  timestamps, counts, reasons, failures, and unresolved values.

Successful-zero, failed, incomplete, skipped, and unsupported outcomes remain
distinct. Absent records never become successful-zero results, and these lookups
do not infer completeness or qualify analyzer conclusions. Legacy snapshots are
normalized through `SnapshotAccess`; the graph API stays the same for schemas
`1`–`3` and does not migrate saved files.

```python
from archdiscov.graph import CoverageKey

partial = GraphIndex(load_snapshot("tests/fixtures/snapshots/partial-coverage.json"))
owner_id = partial.get_evidence("sg-rule").resource_id
assert partial.fact_evidence_ids(owner_id, "/attributes/ingress/0") == ("sg-rule",)
assert partial.evidence_for_fact(owner_id, "/attributes/ingress/0")[0].value["FromPort"] == 443
key = CoverageKey.from_values(partial.scope, "eu-west-1", "ec2", "DescribeInstances")
assert partial.get_coverage(key).result_count == 0
missing = CoverageKey.from_values(partial.scope, "eu-west-1", "ec2", "DescribeNetworkInterfaces")
assert partial.get_coverage(missing) is None
```

## Composed graph boundary

Build one index for a selected snapshot and reuse it across lookups. Containment,
dependency, fact, and coverage indexes refer to that same detached snapshot, but
they answer different questions. For example, this joins a recorded network route
to its source-owned evidence and local target without inferring a network path:

```python
route = next(edge for edge in routes if edge.target.kind == "local")
route_table = network_index.get_resource(route.source_id)
gateway = network_index.get_resource(route.target_id)
assert route_table.resource_type == ResourceType.ROUTE_TABLE
assert gateway.resource_type == ResourceType.INTERNET_GATEWAY
assert network_index.parent_id(route_table.id) == network_index.parent_id(gateway.id)
assert all(
    network_index.get_evidence(evidence_id).resource_id == route_table.id
    for evidence_id in route.evidence_ids
)
# No operation coverage was supplied by this small fixture.
assert network_index.coverage_records() == ()
```

Sharing a parent does not establish the route; the stored dependency does. Missing
coverage does not establish completeness, and neither a route nor its citation is
a reachability conclusion. Caller-selected snapshots are never merged by the index.

Ordering is deterministic, not source-list ordering: IDs, direct children and roots
are sorted, dependencies use their canonical records and occurrence numbers, and
coverage uses complete-context keys. Fact citation order and child order **inside
returned resource models** deliberately retain the stored order. Reordering a
snapshot's storage lists does not change adjacency or edge handles; changing an
ordered fact or a dependency's citation list can change its canonical edge handle.

`tests/test_graph_acceptance.py` checks the composed boundary against exact fixture
inventories and normalized records, including resource/physical lookup, containment,
dependency/evidence joins, terminals, coverage, reordered storage and mutation
isolation. It covers AWS hierarchy, network, IAM, load-balancing, dependency-target,
partial-coverage and Azure snapshots, plus explicit AWS schema-3 upgrades. The
IAM/load-balancing fixtures intentionally contain no dependencies: the index must
not manufacture them from configuration. Schemas `1`–`3` are represented.

Run the focused offline checks with:

```bash
.venv/bin/python -m pytest tests/test_graph_*.py -q
```

See [profiling the graph index](graph-index-profile.md) for larger/deeper synthetic
inputs, construction and repeated-lookup measurements, reproduction instructions,
and limitations. Measurements are diagnostic observations, not performance gates
or promises of constant-time detached model copying. No graph server, new dependency,
or speculative optimization is introduced.
