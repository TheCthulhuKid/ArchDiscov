# Snapshot graph index foundation

Steps 24.1–24.2 implement resource ownership, lookup, and explicit containment.
Dependency adjacency, terminal nodes, evidence/coverage indexes, traversal, query
services, and analyzers remain subsequent steps.

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
