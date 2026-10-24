# Whole-architecture request contracts

Steps 17.1–17.7 implement scope/limit contracts, verified-account enabled-region
discovery, shared SDK-call budgets, isolated collector jobs, deterministic account
assembly, and bounded async scheduling. Public async collection/persistence APIs,
synchronous wrappers, cancellation draining, and CLI scope controls are implemented.

## Scope

`AwsAccountConfig(all_services=True, ...)` selects the seven families in
`COLLECTABLE_SERVICES`: EC2/network, IAM, ELBv2, ECS, Lambda, RDS/Aurora, and SQS.
It rejects any explicit `services` argument, including the full supported list.
The validated `services` tuple expands deterministically; serialization retains
the all-services flag instead of duplicating the selection.

Without regions, all-services mode defaults to `RegionScope.ENABLED`; supplied
regions select `RegionScope.EXPLICIT` and bypass discovery. Explicit
enabled scope cannot include regions. Selective callers can also request enabled
scope explicitly. Existing callers default to EC2 and explicit regions; IAM-only
requests still need no regions. `global` and duplicate regions remain invalid.

The CLI mirrors these contracts: `collect --all-services` defaults to enabled-region
discovery; repeatable `--region` values override that default with explicit scope.
`--all-services` conflicts with any explicit `--service`. Selective collection defaults
to EC2 and explicit regions, or opts into discovery with `--region-scope enabled`.
`--region-scope enabled` conflicts with `--region`; `--region-scope explicit` requires
regions unless only IAM is selected. `--bootstrap-region` chooses an endpoint for
authentication/discovery, not a region to collect.

Accounts remain explicitly configured and uniquely identified by their expected
account IDs. No trust traversal or additional-account discovery is implied.
Existing output-path cardinality and no-overwrite checks are unchanged.

All-services covers these seven supported families, not every AWS service or resource
type. Wider scope increases API traffic, runtime, throttling risk, and possible AWS
costs. Concurrency limits are not total-call or cost caps, and overlapping reads do
not provide transactional point-in-time consistency. Use runtime profile/assume-role
credentials; never embed credentials in requests, snapshots, or diagnostics.

## Limits

These strict positive integer fields distinguish SDK-call budgets from scheduling
limits. Call budgets and account/job scheduling limits are enforced now:

| Location | Field | CLI option | Default | Maximum | Boundary |
| --- | --- | --- | --- | --- | --- |
| `AwsCollectionRequest` | `max_in_flight_calls` | `--max-in-flight-calls` | 16 | 64 | SDK calls across the run |
| `AwsAccountConfig` | `max_in_flight_calls` | `--account-max-in-flight-calls` | 4 | 16 | SDK calls for one account |
| `AwsCollectionRequest` | `max_active_accounts` | `--max-active-accounts` | 2 | 16 | Concurrent account workflows |
| `AwsCollectionRequest` | `max_collector_jobs` | `--max-collector-jobs` | 8 | 64 | Active service/region jobs across the run |
| `AwsAccountConfig` | `max_concurrency` | `--max-concurrency` | 4 | 16 | Each runner's local worker pool |

The `collect` help shows each range, read from these fields. A value outside it, like any
other unusable request, is refused before collection with `invalid_request` (exit 2): the
message names the option at fault (for example `--region`), or the details name the request
field that failed (for example `max_concurrency`), and never the value given.

Both call budgets apply simultaneously. An account budget may exceed the
run budget; the run budget remains the ceiling. Existing `max_concurrency` still
controls each runner's local worker pool independently and is unchanged.

`ingestion.concurrency.SdkCallBudget` reserves run-wide and per-account capacity
atomically, so a caller waiting for a busy account cannot consume run-wide slots
needed by another account. `AdapterContext` carries the shared thread-safe budget
through both `OperationRunner` and `DetailRunner`, including their nested pools.
Configured assume-role calls, STS identity checks, and enabled-region discovery
use the same budget, keyed by the configured account before identity is verified.
Each blocking call/page holds capacity through the SDK's internal retries until
it returns or raises. Failure paths release permits; pagination remains sequential
within a request. SDK timeout/retry configuration and coverage counts are unchanged.

Collection services and direct `AwsAdapter.collect` calls bind a budget for the
request; `collect_and_save` shares it across account subrequests and retains all
original request limits. Independent invocations get independent budgets by
default. Low-level runners sharing one `AdapterContext` share its fallback
run ceiling of 16 plus configured account limits. Advanced callers can inject
`AdapterContext(call_budget=SdkCallBudget(...))` to share a stricter ceiling;
request limits can tighten, but never widen, that budget. Account limits must
remain consistent within a shared budget.

Permits are not held during scheduling, normalization, or persistence.
Synchronization-based offline tests exercise
overlapping runner pools, simultaneous run/account ceilings, authentication and
discovery, and permit release after failures without timing benchmarks.

## Isolated collection and account assembly

`ingestion.collection_jobs` defines an enum-backed `CollectionJob` for one
selected service/region (IAM uses `global`) and a `CollectedJob` carrying its
owned resources, evidence, operation coverage, and any service-local dependencies.
`configuration_services` dispatches a single job without receiving or mutating
account resource trees. Independent collector jobs execute concurrently; dependent
pagination/detail stages and the runners' bounded operation pools are unchanged.

After all jobs finish, `AwsAdapter` assembles copied results in a fixed order:
EC2, ELBv2, account-wide IAM, then regional ECS/Lambda/RDS/SQS. Requested regional
ordering is retained, as is EC2's region/operation-sorted coverage. Assembly rejects
missing, duplicate, or unplanned job results rather than silently claiming complete
scope. SDK-unavailable scopes retain their existing unsupported coverage.

The adapter owns placement and builds relationship indexes after every selected
service's resources and evidence are available. Existing containment and reference
helpers resolve compute/network/IAM/ELB links without inventing unavailable parents
or targets. Assembly and returned snapshot mutations cannot change collected job
inputs; reversed or shuffled results produce the same snapshot with fixed clock
and UUID seams. Retained pages, failed details, successful-zero outcomes, citations,
and redactions are preserved. This is not a new assembly service.

## Bounded async scheduling

`ingestion.async_collection.AsyncCollectionCoordinator` uses asyncio workers and
a bounded thread executor around synchronous boto3 collectors. At most
`max_active_accounts` account workflows are active and `max_collector_jobs`
collector jobs run across the entire request, not per account. The shared SDK-call
budgets remain a separate ceiling over the runners' nested pools. IAM is one
global job per account; dependent discovery/detail stages and pagination are unchanged.

Account preparation verifies STS identity, resolves regions and availability, and
allocates the snapshot ID before dispatching collector jobs. Session factories
are serialized, and each account's Session methods/client construction are
protected across all its jobs. Returned SDK clients execute concurrently under
the shared call budget. A factory returning the same Session object for different
accounts is rejected. Injected clock and UUID factories are serialized too; their
allocation order across accounts is not guaranteed, so deterministic comparisons
should fix these seams. Client objects supplied by custom factories must support
thread-safe calls just as standard boto3 clients do.

The adapter's internal `collect_accounts_async` scheduling boundary retains
request-ordered account results with either a snapshot or a safe `IngestionError`.
Identity/discovery failure schedules no jobs for that account and does not stop
other accounts. API failures remain qualified coverage within collectors;
unexpected collector/assembly/validation failures propagate after sibling work
settles, never becoming successful empty data. Assembly runs only after all
selected jobs finish and receives results in planned order regardless of completion.

Both execution runners translate SDK failures and explicitly detected malformed
responses into sanitized coverage failures (`MalformedResponse` for invalid
response shapes or pagination). Unexpected programming exceptions propagate rather
than being mislabeled as ordinary AWS failures.

`AwsAdapter.collect` invokes this coordinator synchronously and raises the first
request-ordered account error after accounts settle, preserving its existing
all-snapshots-or-error contract. It rejects use inside a running event loop.
The public workflows and CLI use the same coordinator; there is no snapshot
schema change.

## Async workflows and cancellation

`archdiscov.services.collect_snapshots_async` returns independently validated,
detached snapshots in request order, or raises the first ordered account error.
`collect_and_save_async` isolates expected account and persistence failures into
request-ordered `AccountOutcome` entries, with one distinct output path per account.
Native AWS collection runs accounts concurrently under the configured limits.
After collection settles, only complete account snapshots proceed to validation
and immutable publication; saves are bounded by `max_active_accounts` and hold no
SDK-call permits. Coverage failures still produce qualified, inspectable snapshots.
Unexpected collector/assembly errors propagate rather than becoming empty success.

Call the async APIs directly from an existing event loop:

```python
from pathlib import Path
from archdiscov.services import collect_and_save_async, collect_snapshots_async

# Inside your application's async function, using an AwsCollectionRequest:
snapshots = await collect_snapshots_async(request)
outcomes = await collect_and_save_async(request, [Path("architecture.json")])
```

`collect_snapshots` and `collect_and_save` remain synchronous wrappers for CLI and
Python callers. Calling them (or `AwsAdapter.collect`) inside a running loop raises
`IngestionError` with code `invalid_request` and directs callers to the async API;
no nested loop or orphan coroutine is started. Existing sync-only injected adapters
remain supported through settled worker threads. Persistence invokes them serially
per account, preserving compatibility without assuming thread-safe adapter state.
An adapter may instead supply `collect_accounts_async(request, *, context)` returning
ordered `AccountCollection` entries; account identities and result shape are checked
before publication. Existing context/session/clock/UUID seams remain supported.

Cancellation stops scheduling later account, collector, and save work. Running
blocking work settles while the event loop remains responsive, then the API raises
`asyncio.CancelledError`. Repeated cancellation does not abandon those threads or
release SDK-call permits or collector slots before completion. SDK timeouts/retries
bound calls; cancelling an await cannot forcibly interrupt boto3 or custom adapters.
The native coordinator's run-local gate also prevents further SDK calls or pagination
reads in draining collectors; cancellation does not invalidate an injected shared
budget for subsequent runs. Custom async adapters own their cancellation policy;
legacy blocking adapters must finish on their own before cancellation returns.
An account cancelled during collection or assembly is never sent to persistence.

A save already started for a completely collected account is allowed to settle,
including validation, temporary-file cleanup, and atomic hard-link publication.
Successful link creation remains the commit point: committed files are never rolled
back, including files committed while cancellation is draining a save. Cancellation
is therefore **not** a guarantee that no output exists; inspect the explicit output
paths after cancellation. Destinations are never overwritten and no unfinished
account snapshot is published. Files committed by earlier saves remain intact.

## Resolved provenance

`ingestion.scope.ResolvedAwsScope.from_account(account, regions)` validates actual
resolved regions and selected services. Explicit scope must match the configured
region tuple; regional services cannot have an empty resolved region set.

Its `to_provenance()` returns JSON-compatible, credential-free account ID,
services, region-selection mode, and resolved regions for the existing
`snapshot.metadata["collection_scope"]` slot. No profile, external ID, credentials,
or execution-limit details are included. This records scope, **not completeness**;
operation coverage continues to describe failures and unavailable facts.
The AWS adapter attaches this data after STS verification and region resolution.
Schema remains `2`; existing snapshots remain readable.

CLI JSON account results expose `collection_scope` from that metadata. Readable
results show scope lines for selected services, resolved regions, region mode,
and whether account-wide IAM is included. Neither format infers completeness from
scope: failed/incomplete/unsupported operations retain partial snapshots and a
`PARTIAL` summary. Account identity/discovery failures produce account errors instead
of files; other configured accounts can still succeed.

## Enabled-region resolution

`RegionScope.ENABLED` issues the allowlisted EC2 `DescribeRegions(AllRegions=True)`
only after successful STS account verification. Grant **`ec2:DescribeRegions`**
on `"*"` for discovery, even when EC2 inventory is not selected. Explicit regional
scope and explicit IAM-only requests do not need this permission or make this call.

For example, a Python caller can use existing synchronous persistence:

```python
from pathlib import Path
from archdiscov.ingestion.requests import AwsAccountConfig, AwsCollectionRequest
from archdiscov.services import collect_and_save

request = AwsCollectionRequest(
    accounts=(
        AwsAccountConfig(
            profile="dev",
            expected_account_id="123456789012",
            services=("ec2",),
            region_scope="enabled",
            bootstrap_region="us-east-1",
        ),
    )
)
outcomes = collect_and_save(request, [Path("architecture.json")])
```

Bootstrap selection uses `bootstrap_region` first, then the session's profile
region, then a known default endpoint for the verified STS ARN partition (or
configured role partition). China, GovCloud, isolated and sovereign partitions
are not bootstrapped through a commercial endpoint. Defaults must exist in the
installed SDK metadata. Ambiguous or conflicting context raises
`region_bootstrap_failed`; configure a concrete bootstrap region or update the
SDK as appropriate. Configured bootstrap context also applies to authentication;
assumed sessions preserve the selected endpoint region. No credentials are
retained in scope diagnostics.

`opt-in-not-required` and `opted-in` regions are sorted deterministically;
`not-opted-in` regions are excluded. Invalid/empty responses or API failures raise
`region_discovery_failed`, with safe operation/error-code diagnostics. An enabled
region absent from `AwsRegion` raises `unsupported_region`: no silently reduced
scan or empty snapshot is saved. Explicit scope preserves caller order and is
checked against the verified partition. An account-level error prevents that
account's file; `collect_and_save` continues other configured accounts.

`snapshot.metadata["region_resolution"]` retains partition, discovery bootstrap
region/operation, and recognized disabled region identifiers. Discovery is a
scope prerequisite, not resource inventory: it is not included in regional
inventory counts or global coverage. Errors retain their underlying causes for
debugging but public messages omit raw AWS error text and unknown response values.

## Regional service availability

Before dispatch, the adapter checks local SDK endpoint metadata for each selected
regional service in the resolved partition. Missing endpoints produce one
`unsupported` record per inventory operation/region/service, with a reason and
no result count; those collectors are not invoked. Requested regions/services
remain in scope provenance, successful sibling inventories survive, and the
result is partial, never successful zero inventory for an unavailable service.
IAM remains account-wide.

Endpoint metadata is a conservative SDK capability check, **not** a live service
availability guarantee. It may lag AWS launches; update botocore rather than
interpret an unsupported record as proof the service cannot exist there. When
an endpoint is known, existing runners retain API denial/outage/pagination
failures as failed/incomplete coverage rather than guessing availability.