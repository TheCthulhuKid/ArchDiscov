# Recorded resource queries

Step 25.2 provides Python search and inspection services over an existing
`GraphIndex`. They work with AWS schemas `1`–`2`, upgraded AWS schema `3`, and
the offline Azure schema-3 contract. They do not collect live data or generate
analytical conclusions. Directional neighbours are available through `query_neighbours`; paths and the `query` CLI remain later steps.

## Python API

Run from the checkout with the installed environment:

```python
from archdiscov.graph import GraphIndex
from archdiscov.models.query.requests import QueryInspectRequest, QuerySearchRequest
from archdiscov.services import inspect_resource, load_snapshot, search_resources

index = GraphIndex(load_snapshot("tests/fixtures/snapshots/hierarchy.json"))
matches = search_resources(index, QuerySearchRequest(native_type="AWS::EC2::Instance"))
assert len(matches.resources) == 1
details = inspect_resource(index, QueryInspectRequest(resource_id=matches.resources[0].id))
assert details.resource == matches.resources[0]
assert details.evidence
```

Both services also accept request dictionaries. Requests are detached and
revalidated, including previously validated models that have since been mutated.
Construct the graph once and reuse it: queries neither rebuild nor revalidate it.
Returned resources, evidence, coverage, scopes, and limitations cannot mutate
the graph or its input. Resource subtype fields and stored children are retained;
children inside a selected resource are not additional top-level search matches.

## Matching and limits

- Omitted filters impose no restriction. All supplied filters combine with AND.
- `name`, `resource_id`, tag keys, and tag values match exactly and case-sensitively.
  A stable ID is not a physical ID or a display name.
- `native_type` uses the provider-native type, including unknown-resource types.
  AWS is case-sensitive; Azure type matching is case-insensitive.
- `physical_identifier` requires `scope`, `native_type`, and `physical_id`. It uses
  the graph's [qualified identity rules](graph-index.md), including full account/
  partition or subscription/tenant context and Azure ARM canonicalization.
  Multiple regional candidates remain separate matches; none is chosen arbitrarily.
  Logical-only resources can be searched by stable ID, not by physical identity.
- Results are sorted by stable ID before applying `limit_result` (default 100,
  range 1–10,000). `result_limit` truncation is reported only when matching
  resources were actually omitted, not merely when the count equals the limit.
- Unknown search IDs give no matches. Unknown inspection IDs raise `QueryError`
  with `resource_not_found`; invalid requests use `invalid_request`. Public
  messages contain no raw request values. Unexpected implementation errors propagate.

Example tag and qualified-identifier requests:

```python
tagged = search_resources(
    index,
    {
        "tag": {"key": "Team", "value": "payments"},
        "limit_result": 25,
    },
)
physical = search_resources(
    index,
    {
        "physical_identifier": {
            "scope": {"platform": "aws", "account_id": "123456789012"},
            "native_type": "AWS::EC2::Instance",
            "physical_id": "i-web",
        },
    },
)
assert len(physical.resources) == 1
```

## Recorded tags and uncertainty

Tags come only from `attributes.tags`. A null tag-filter value means key-only
matching; an empty string means an exact empty value. Missing/unmapped tag data
does not prove a resource has no tags. Recorded but uncited values may match,
but carry a provenance limitation. A redacted value cannot match as an exact
literal value; its recorded key can still satisfy a key-only query.

An explicit present citation to `/attributes/tags` establishes provenance for
the recorded map, including an explicitly empty map. A citation to the escaped
`/attributes/tags/<key>` establishes that key's provenance, not completeness of
the entire map. Whole-resource citations alone are not treated as tag citations.
Unavailable or redacted citations conservatively retain provenance limitations.
Tag limitations are collected before excluding a candidate by its tag filter,
so an empty response can still explain unknown tag data. Inspection always reports
unavailable tag-map provenance. These are recording signals, not ownership findings.

## Evidence and coverage

Inspection returns all evidence owned by the selected resource, including uncited,
redacted, and unavailable records. The resource retains its exact fact-pointer
citations. Known-null facts remain distinct from unresolved/omitted evidence.

Inspection includes recorded coverage matching the full scope, location, and
canonical service of the resource. Scope-wide location is null; Azure namespace
matching is case-insensitive. Limited outcomes become structured limitations;
successful-zero stays an ordinary successful operation. If no contextual coverage
exists, a provenance limitation identifies that gap without inventing an operation.

Search includes limited coverage from the snapshot as conservative context, even
when there are no matches. These records do not prove which resource was omitted,
nor that a successful scope is exhaustive. When AWS owner evidence explicitly
names an API operation (bare operation or `service:operation` locator), absent
coverage for that exact context becomes `coverage_missing`. File paths and Azure
fixture locators are not guessed to be operation names. Missing-operation
limitations are deduplicated in deterministic candidate/evidence order.

No query declares live completeness, reachability, effective permissions, or
absence of matching infrastructure. Returned data remains subject to the
snapshot's source, lifecycle, redactions, and coverage limitations.
## Neighbours

`query_neighbours(index, request)` returns one oriented step per adjacent edge of a
stored resource. Outgoing steps follow stored source to target; incoming steps are
a reverse lookup of the same stored edge, keeping its source-owned evidence and
asserting no new relationship. `both` returns each direction (a self-loop appears
once per direction). Relationship and attachment-kind filters OR within a list and
AND across lists, and apply to dependencies only. Containment is returned only when
selected, is uncited, and never implies an attachment.

Parallel edges are separate steps. Steps are ordered by edge kind, stored source,
stored target, edge ID, then outgoing before incoming, and the result limit is
applied afterwards. External and unresolved targets stay terminals with structured
limitations; they are never stored resources. Coverage limitations describe the
resource's context: recorded neighbours are not exhaustive.
