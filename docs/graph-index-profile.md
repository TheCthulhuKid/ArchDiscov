# GraphIndex offline profiling

This is a measurement harness for step 24.5, not a performance acceptance test
or an optimization proposal. It uses public constructors and `GraphIndex` APIs;
no AWS calls, credentials, snapshot files, random inputs, or production changes
are needed. The harness imports only the standard library and the installed
application (whose existing dependencies include Pydantic).

## Reproduce

From the repository root, using the installed project environment:

```sh
.venv/bin/python scripts/profile_graph_index.py --repeats 3 --lookup-rounds 3
.venv/bin/ruff check scripts/profile_graph_index.py
.venv/bin/ruff format --check scripts/profile_graph_index.py
```

The script has an explicit `main()` and prints JSON to stdout, including runtime
metadata, case sizes, timings, and traced allocation measurements. Redirect it
to a location of your choice to retain raw results. Small smoke configuration:

```sh
.venv/bin/python scripts/profile_graph_index.py --wide 4 --deep 4 --repeats 1 --lookup-rounds 1
```

`--wide` and `--deep` accept lists of sizes, defaulting to `100 1000` and `16 64`.
Size means **total resources**, including the root. Sizes must be at least two;
deep cases are capped at 64 to stay within the exercised recursive model paths.
The harness does not raise Python's recursion limit. Larger wide sizes are
allowed but have not been characterized here. Repeats and lookup rounds must be
positive; neither is a timing threshold.

## Deterministic cases and boundaries

Each case is a schema-3 AWS `CloudSnapshot`, account `123456789012`, region
`eu-west-1`, with fixed snapshot UUID, timestamps, physical IDs, and mock
provenance. `UnknownResource` preserves the deliberately artificial native type
`AWS::EC2::SyntheticNode`: these are storage shapes, **not claims about legal AWS
containment or real network connectivity**.

- Wide: one root with `N-1` direct children, depth two.
- Deep: one chain of `N` resources, depth `N` (counting root as one).
- Every node has a small attribute payload, one owned evidence record, and an
  explicit `/attributes/ordinal` fact citation.
- Each node has one cited local dependency to the next node, closing a directed
  ring. The root also has one external and one unresolved dependency. Thus there
  are `N+2` dependencies and two terminal targets. Local edges carry a recognized
  attachment kind for filtered adjacency measurements.
- Five fixed coverage records exercise success, incomplete, failed, skipped, and
  unsupported outcomes, retaining the required counts, reasons, or failure.

Basic untimed assertions check resource, containment, dependency, evidence,
coverage, and terminal counts, plus a cited leaf and its empty child adjacency.
These are harness sanity checks, not the composed acceptance suite. Coverage
cardinality is fixed, so these results do not measure scaling with coverage size.
Azure, legacy-schema upgrades, large evidence payloads, high-degree dependency
graphs, and concurrent callers are outside this measurement.

## Measurement method

Input generation and initial snapshot validation happen before measurement.
Construction measures `GraphIndex(snapshot)`, **including its own detached
serialization/validation and all index building**, not just dictionary insertion.
It does not include imports or process startup.

Each operation gets one untimed warm-up. Before each timing sample, garbage
collection runs outside the timer; normal GC remains enabled during measurement.
Construction samples contain one invocation. Lookup samples contain
`--lookup-rounds` invocations of the named workload. `perf_counter` samples are
normalized by those invocations; JSON reports median/min/max microseconds.
Timed results are discarded immediately, so disposal costs are included.

A `_sweep` workload invokes its API once per listed resource, evidence ID, edge,
terminal, or coverage key and discards each result before the next. Its reported
time is **per complete sweep**, not per individual API call. JSON's
`api_calls_per_workload` supplies that denominator. `id_tuples` reads two
properties; `misses` performs four missing-ID calls. Small fixed workloads are
particularly susceptible to loop, dispatch, clock, GC, and scheduling noise.

Memory is measured separately with `tracemalloc`, with no timing instrumentation
overlap: one additional workload invocation, starting tracing after inputs and
the lookup index already exist. The returned result is retained until the current
and peak traced byte counts are read. Construction therefore retains its new
index; sweeps return `None` and retain no result collection. Peaks include Python
allocation/cache effects across a sweep and are **not** summed result sizes,
total process RSS, or all native allocations. Tracing itself adds overhead.

Workloads are separated intentionally:

- `scalar.*`: immutable ID tuples, containment adjacency IDs, physical keys,
  owner/fact citation IDs, coverage keys, and missing IDs. No successful resource
  model retrieval is hidden in this group.
- `detached.*`: dependency adjacency (including filtered outgoing), edge and
  terminal retrieval, evidence/fact records, coverage records, leaf/root resource
  retrieval, and full `resources()` listing. These include defensive copying;
  successful `get_coverage` is **not** a scalar dictionary-only benchmark.
- Root retrieval copies a complete subtree, while leaf retrieval copies one node.
  `resources()` returns all nodes with the stored tree structure. Full listings
  are reported per call, not divided by node count or described as ID lookups.

## Recorded run

Executed with the full command above (three repeats, three lookup rounds), plus
the small smoke command. Environment reported by the actual run:

- CPython `3.13.2`, Clang `19.1.6` (build Feb 5, 2025).
- Executable `/home/mat/projects/archdiscov/.venv/bin/python`.
- `Linux-6.6.87.2-microsoft-standard-WSL2-x86_64-with-glibc2.39`;
  machine/processor reported as `x86_64` (no more specific CPU model reported).
- Installed `archdiscov 0.1.0`, `pydantic 2.13.5`.
- Python recursion limit `1000`; GC enabled. No recursion settings changed.

Construction times and separate traced allocation counts:

| Case | Median ms | Min–max ms | Retained bytes | Peak bytes |
| --- | ---: | ---: | ---: | ---: |
| Wide 100 | 11.825 | 11.108–12.397 | 996,550 | 1,353,927 |
| Wide 1000 | 116.267 | 114.623–117.375 | 9,349,296 | 13,014,419 |
| Deep 16 | 3.259 | 3.055–3.321 | 211,941 | 260,721 |
| Deep 64 | 27.138 | 24.703–33.763 | 643,511 | 905,785 |

Median **microseconds per complete named workload**, rounded to three decimals:

| Workload | Wide 100 | Wide 1000 | Deep 16 | Deep 64 |
| --- | ---: | ---: | ---: | ---: |
| `scalar.id_tuples` | 3.266 | 2.551 | 2.312 | 2.142 |
| `scalar.parent_sweep` | 7.688 | 54.158 | 3.571 | 6.019 |
| `scalar.child_sweep` | 8.028 | 53.171 | 4.047 | 6.597 |
| `scalar.physical_key_sweep` | 8.028 | 56.233 | 4.863 | 6.733 |
| `scalar.owner_citation_sweep` | 12.145 | 82.257 | 5.067 | 13.942 |
| `scalar.fact_citation_sweep` | 19.663 | 173.020 | 5.713 | 14.759 |
| `scalar.coverage_keys` | 2.925 | 2.211 | 1.938 | 2.074 |
| `scalar.misses` | 4.592 | 4.150 | 3.537 | 3.775 |
| `detached.outgoing_sweep` | 1,577.557 | 15,875.684 | 313.398 | 1,063.977 |
| `detached.incoming_sweep` | 1,577.591 | 15,873.200 | 255.248 | 1,016.131 |
| `detached.filtered_outgoing_sweep` | 1,499.313 | 17,097.233 | 290.139 | 1,066.799 |
| `detached.edge_sweep` | 1,514.282 | 14,755.885 | 319.486 | 1,008.309 |
| `detached.terminal_sweep` | 50.008 | 47.865 | 50.941 | 48.289 |
| `detached.evidence_sweep` | 1,974.046 | 19,658.249 | 385.865 | 1,484.256 |
| `detached.fact_evidence_sweep` | 2,075.865 | 20,998.990 | 358.422 | 1,403.220 |
| `detached.coverage_sweep` | 176.286 | 177.341 | 167.989 | 158.536 |
| `detached.resource_leaf` | 34.869 | 38.120 | 34.448 | 34.754 |
| `detached.resource_root` | 2,273.106 | 22,600.158 | 436.670 | 1,695.943 |
| `detached.resources_all` | 1,997.825 | 22,382.384 | 649.445 | 1,574.916 |

Selected one-workload traced peaks in bytes (full JSON includes every workload):

| Workload | Wide 100 | Wide 1000 | Deep 16 | Deep 64 |
| --- | ---: | ---: | ---: | ---: |
| Parent/child/physical-key/owner-ID sweep (each) | 48 | 48 | 48 | 48 |
| Fact-ID sweep | 104 | 104 | 104 | 104 |
| Outgoing sweep | 14,536 | 100,888 | 6,536 | 11,080 |
| Coverage sweep | 5,275 | 5,275 | 5,275 | 5,275 |
| Leaf resource | 3,048 | 3,048 | 3,048 | 3,048 |
| Root resource | 327,376 | 3,196,872 | 52,184 | 209,016 |
| All resources | 328,120 | 3,197,616 | 52,928 | 209,760 |

In this run, widening 100 to 1000 nodes increased construction time about 9.8×
and traced construction peak about 9.6×. Increasing chain depth 16 to 64 increased
construction time about 8.3×; this includes recursive model serialization and
validation and does not isolate their individual contributions. These few points
do not establish asymptotic complexity or justify a production optimization.
Root/full-model retrieval is materially different from scalar ID lookup and
must not be used to claim dictionary lookup latency. Fixed coverage and leaf
payload costs do not measure dependence on payload/cardinality growth.

There are no timing assertions. The executable harness and Ruff checks validate
these measurements; the separate composed fixture tests verify graph correctness
and isolation. Step 24.5's integrated verification passed all 120 focused graph
tests and the complete 2,690-test offline suite using `.venv/bin/python`.