# Snapshot graph index foundation

Steps 24.1–24.3 implement resource ownership, lookup, explicit containment,
and dependency adjacency with terminal targets. Evidence/coverage indexes,
traversal, query services, and analyzers remain subsequent steps.

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
records. Evidence lookup and traversal remain later steps.

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
