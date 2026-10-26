# Archdiscov implementation plan

This plan follows the four delivery increments in [README.md](README.md) and adds a fifth, a
sixth, and a seventh. It holds the active phases only. Completed phases are archived, one file
per phase, in [docs/plan-archive/](docs/plan-archive/); read them for how an earlier step was
planned and verified, not for current work.

| Phase | Steps | Status | Plan |
| --- | --- | --- | --- |
| 1. Models, snapshots, fixtures, and CLI foundation | 1–9 | Complete | [phase-1.md](docs/plan-archive/phase-1.md) |
| 2. AWS and infrastructure-as-code ingestion | 10–22 | Complete | [phase-2.md](docs/plan-archive/phase-2.md) |
| Cloud-platform foundation before phase 3 | 22.1–22.4 | Complete | [cloud-platform-foundation.md](docs/plan-archive/cloud-platform-foundation.md) |
| 3. Graph queries, findings, and comparisons | 23–33 | Complete | [phase-3.md](docs/plan-archive/phase-3.md) |
| 4. Providers, orchestration, and validated answers | 34–44 | Complete | [phase-4.md](docs/plan-archive/phase-4.md) |
| 5. MCP server over the assistant tools | 45–48 | Complete | [phase-5.md](docs/plan-archive/phase-5.md) |
| 6. Complete the AWS network paths | 49–56 | In progress | [Below](#6-complete-the-aws-network-paths) |
| 7. Browser UI for results, evidence, and questions | 57–60 | Planned | [Below](#7-browser-ui-for-results-evidence-and-questions) |

When a phase completes, move its section to a new file in `docs/plan-archive/` and update this
table.

## Current starting point

The existing code in [src/archdiscov/models/architecture.py](src/archdiscov/models/architecture.py) provides recursive resource models, a discriminated resource union, validated account/region containment, local/external/unresolved dependencies, fact-level evidence citations, and operation coverage. Dedicated resource models cover EC2/network, IAM, ELBv2, ECS, Lambda, RDS/Aurora, and SQS configuration; other types can use `UnknownResource`.

AWS discovery supports the documented regional service slices and account-wide IAM configuration through `collect`; Terraform state exports and plans and CloudFormation templates are imported through `ingest --format`. Phase 3 delivered the graph index, queries, five deterministic analyzers, immutable result persistence, and snapshot comparison. Phase 4 delivered the `ask` command with the fake, Anthropic, OpenAI-compatible, and Bedrock providers, plus evaluation suites. Phase 5 serves the same tools over MCP through `archdiscov mcp`.

## 6. Complete the AWS network paths

**Phase 6 milestone:** Collect or import a VPC whose traffic leaves through a NAT gateway, crosses a peering connection or transit gateway, uses a VPC endpoint or managed prefix list, or travels over IPv6 → the network primitives decide each element from cited configuration instead of stopping at it → `analyze` reports database access across VPCs, internet exposure over IPv6, and internet egress with the same statuses, blockers, and citations as today → `ask` and the MCP server answer questions about them unchanged.

Today a route whose target is a NAT gateway, transit gateway, peering connection, VPC endpoint, or prefix list, a security-group rule that names a prefix list, and every IPv6 element are `unknown`. The route records its target kind (`RouteTargetKind`) but the target itself is never collected, so a path stops there. `docs/network-path-primitives.md` and the internet-exposure and database-access pages list these as unsupported. This phase records those elements and decides them, so the analyzers answer more questions without a new kind of claim. It stays AWS-only, read-only, and offline in tests, and adds no cloud platform, provider, or remediation.

A decision still describes recorded configuration, never live connectivity. A path is decided only when every element on it, in both directions where the return path matters, is recorded and cited. Anything missing, unsupported, or outside the snapshot stays `unknown` with a blocker that names it.

Implement steps 49–56 in order, verifying each before the next. Break each step into substeps when it starts, as steps 34–48 were. Keep this phase planned until step 56 passes.

**Where things go:**
- New resource models in `models/resources/network.py`, joining the supported resource union; new collection in `ingestion/network/`, with its operations in `ingestion/operations.py` and the allowlist.
- Route, prefix-list, and address-family decisions in the existing `services/network_*` primitives; cross-VPC route composition in `services/network_composition.py`.
- The analyzers stay in their modules; a new egress analyzer follows the `Analyzer` protocol, its results in `models/analysis/`, registered in `DEFAULT_REGISTRY`.

**What is reused:**
- `DetailRunner`, `EvidenceBuilder`, coverage and unresolved values, the SDK call budgets, and the sanitizers, as every other collector uses them.
- `NetworkResult`, `all_of`/`any_of`, `cited_decision`, `sole_associate`, and the `missing`/`unsupported` blockers in `services/network_matching.py` and `network_decisions.py`.
- The Terraform and CloudFormation mapping catalogs, the analyzer registry, result persistence, and the phase 3–5 acceptance harnesses.

### Step 49: Settle the contracts for the new network elements
**Status:** Planned.
**Prerequisite:** Step 48.

- Write representative snapshot fixtures first: a NAT gateway with its subnet and Elastic IP, a same-account peering connection between two VPCs, a transit gateway with VPC attachments and a route table, gateway and interface VPC endpoints, a managed prefix list referenced by a route and a security-group rule, and a dual-stack VPC.
- Add resource models for NAT gateways, VPC peering connections, transit gateways with their attachments and route tables, VPC endpoints, and managed prefix lists, and add the IPv6 fields: VPC and subnet IPv6 CIDRs, ENI IPv6 addresses, IPv6 routes, egress-only internet gateways, and IPv6 security-group and NACL entries.
- Record each target as a typed relationship (for example, route to NAT gateway, attachment to transit gateway, endpoint to route table), so the graph can follow it with evidence.
- Keep schema `3` and add the new types and fields as optional, so every existing snapshot and saved result validates unchanged. A snapshot collected before this phase has no coverage for the new operations: its unknowns say coverage was unrecorded, never that the element is absent.

**Advice:** Shape the models from real `Describe*` response shapes, but keep only what a decision needs (state, CIDRs, attachments, owners, and entries); keep everything else out of typed fields.

**Acceptance check:** The new fixtures validate, round-trip through persistence, and keep exact citations; every existing fixture and saved result validates unchanged; model tests reject impossible states (for example, a peering connection whose two sides are the same VPC).

Implement and verify substeps 49.1–49.7 in order. Keep the parent planned until every substep's acceptance checks pass.

**Where things go:**
- Enum members in `models/enums.py`; resource models in `models/resources/network.py` (or a sibling module such as `models/resources/transit.py` if it grows past one readable file), added to `SupportedResource` in `models/architecture.py`.
- Fixtures beside the existing ones in `tests/fixtures/snapshots/`, one file per element family, so a failure names its family; model tests in `tests/models/`.
- `docs/` pages and the README are updated once, in 49.7, because the contracts are not user-visible until collection (step 50) lands.

**What is reused:**
- `Resource`, `Model`, `NonEmptyString`, and the sanitizing validators in `models/resource_base.py`; `Route`, `SecurityGroupRule`, `NaclEntry`, and `PrivateIpAddress` extended in place rather than copied.
- `DependencyRelationship` and `AttachmentKind` for typed relationships (`attributes["kind"]`), and the snapshot graph index's existing edge-and-evidence handling.
- The coverage key and outcome contracts in `models/coverage_key.py` and `models/coverage_outcomes.py`.

#### Step 49.1: Fix the vocabulary and guard compatibility
**Status:** Implemented. `ResourceType` gains the NAT gateway, egress-only internet gateway, peering connection, transit gateway, attachment, transit gateway route table, VPC endpoint, and managed prefix list types (CloudFormation type names); `RouteTargetKind` gains `egress_only_internet_gateway`; `AttachmentKind` gains the kinds step 49.6 records (NAT gateway subnet and interface, peering requester and accepter VPC, transit gateway attachment, VPC attachment, route table association and propagation, endpoint route table, subnet, interface and security group, egress-only gateway attachment, and prefix list reference). No existing value changed and no model uses them yet. `tests/models/test_fixture_compatibility.py` pins the canonical-JSON hash of every existing snapshot fixture and both saved-result fixtures as validated by `load_snapshot` and `load_analysis`, and fails when a snapshot fixture is neither pinned nor listed in `PHASE_SIX_SNAPSHOTS`. The coverage operation names were not declared here: an absent operation already reads as unrecorded coverage by service, and the names belong with the allowlist and permissions page in step 50.
**Prerequisite:** Step 48.

- Add the `ResourceType` members: NAT gateway, VPC peering connection, transit gateway, transit gateway attachment, transit gateway route table, VPC endpoint, managed prefix list, and egress-only internet gateway. Add the `RouteTargetKind` member for an egress-only internet gateway, and `AttachmentKind` members for each typed relationship step 49.6 records.
- Before changing any model, add a compatibility guard: every snapshot fixture and saved-result fixture validates to the same data, as the first test to run after each later substep.

**Advice:** Add enum members only, with no models yet. The guard is the safety net for the remaining substeps, so it must pass here, before anything else changes.

**Acceptance check:** The enums list the new members, the guard passes on the unchanged fixtures, and no existing enum value changes.

#### Step 49.2: NAT gateways, egress-only internet gateways, and peering connections
**Status:** Planned.
**Prerequisite:** Step 49.1.

- Write the fixtures first: a NAT gateway in a public subnet with its Elastic IP, a same-account peering connection between two VPCs, and an egress-only internet gateway on a VPC.
- Add `NatGateway` (state, subnet, VPC, public or private connectivity, public IPs, network interface), `EgressOnlyInternetGateway` (VPC), and `VpcPeeringConnection` (state, requester and accepter VPC, CIDRs, and owner account ID for each side).
- Reject impossible states: a peering connection whose two sides are the same VPC, a public NAT gateway with no public address once `available`, and a malformed owner account.

**Advice:** Record both owner accounts on a peering side even when equal, so the same-account test is a comparison of recorded values, never an assumption.

**Acceptance check:** The fixtures validate and round-trip with exact citations, each impossible state is rejected, and the compatibility guard still passes.

#### Step 49.3: Transit gateways, attachments, and route tables
**Status:** Planned.
**Prerequisite:** Step 49.1.

- Write the fixtures first: a transit gateway with two VPC attachments, one route table with an association, a propagation, and a static and a blackhole route.
- Add `TransitGateway` (state, owner, default association and propagation route table IDs), `TransitGatewayAttachment` (resource type, resource ID and owner, state, association state, associated route table), and `TransitGatewayRouteTable` (associations, propagations, and routes with destination, attachment, type, and state).
- Reject impossible states: a route naming an attachment while marked `blackhole`, a route with neither, and an association to a route table of another transit gateway.

**Advice:** Record the search bound the collector may hit as a field of the route table (such as whether its routes are known to be complete), so step 50 can mark incomplete coverage without a new contract.

**Acceptance check:** The fixtures validate and round-trip with exact citations, each impossible state is rejected, and the compatibility guard still passes.

#### Step 49.4: Endpoints, managed prefix lists, and their references
**Status:** Planned.
**Prerequisite:** Step 49.1.

- Write the fixtures first: a gateway endpoint on route tables, an interface endpoint with ENIs and security groups, and a managed prefix list referenced by a route and by a security-group rule, one customer-owned and one AWS-managed.
- Add `VpcEndpoint` (gateway or interface, service name, state, VPC, route tables, subnets, ENIs, security groups, owner) and `ManagedPrefixList` (address family, owner, state, version, entries with CIDR and description, and whether entries are recorded).
- Let `Route`, `SecurityGroupRule`, and `NaclEntry` name a prefix list through typed fields; `Route.destination` keeps its reported form.
- Reject impossible states: an entry whose CIDR family differs from its list, a gateway endpoint with subnets, and an interface endpoint with route tables.

**Advice:** An AWS-managed list is valid with no recorded entries; its explicit "entries not recorded" state is what lets step 52 answer `unknown` for the right reason.

**Acceptance check:** The fixtures validate and round-trip with exact citations, each impossible state is rejected, and the compatibility guard still passes.

#### Step 49.5: IPv6 fields and the dual-stack fixture
**Status:** Planned.
**Prerequisite:** Step 49.1.

- Write the dual-stack fixture first: a VPC and subnet with IPv6 CIDRs, an ENI with an IPv6 address, an IPv6 default route to an internet gateway and one to an egress-only internet gateway, and IPv6 security-group and NACL entries.
- Add optional `ipv6_cidr_blocks` to `Vpc` and `Subnet`, a list of IPv6 addresses (with any public one) to `NetworkInterface`, and IPv6 destinations and CIDRs to `Route`, `SecurityGroupRule`, and `NaclEntry` through the existing `cidr` field's family, not a second field.
- Reject impossible states: a subnet IPv6 CIDR outside its VPC's, an IPv6 address that is not IPv6, and a rule that sets both a CIDR and a prefix list.

**Advice:** Keep IPv4 fields exactly as they are; add IPv6 beside them, so "no IPv6 recorded" and "no IPv6 collected" stay distinguishable by coverage, not by empty lists.

**Acceptance check:** The dual-stack fixture validates, round-trips, and keeps exact citations; each impossible state is rejected; the compatibility guard still passes.

#### Step 49.6: Typed relationships and graph traversal
**Status:** Planned.
**Prerequisite:** Steps 49.2–49.5.

- Record each target as a typed dependency with evidence: route to NAT gateway, peering connection, transit gateway, endpoint, egress-only internet gateway, and prefix list; attachment to transit gateway and to its VPC; association and propagation to a transit gateway route table; endpoint to route table, subnet, ENI, and security group; NAT gateway to subnet and ENI; rule and route to prefix list.
- Give a target in another account, or absent from the snapshot, a representation that is an external reference, never a resource of this snapshot.
- Extend the graph index so each relationship can be followed in both directions with its evidence, using the existing identity, containment, and dependency handling.

**Advice:** Add the relationships to the fixtures that already exist rather than writing new graph fixtures, so one fixture proves both the model and the traversal. The relationship builders dispatch through per-resource handler tables (`_HANDLERS` in `ingestion/network/relationships.py` and `ingestion/elbv2/relationships.py`, `_GLOBAL_HANDLERS` in `ingestion/iam/relationships.py`); give each new resource type its own handler and table entry rather than a branch in a shared function.

**Acceptance check:** Graph tests follow every new relationship in both directions with exact evidence; an external target is reported as external; and a dangling reference to a missing resource is a cited unresolved dependency.

#### Step 49.7: Verify compatibility and close the step
**Status:** Planned.
**Prerequisite:** Step 49.6.

- Confirm schema `3` is unchanged and every new type and field is optional: a snapshot and a saved result written before this step validate unchanged, and a snapshot with the new types round-trips through persistence.
- Confirm a pre-step-49 snapshot reads as "coverage unrecorded" for the new operations, never as "element absent".
- Document the new resource types, fields, and relationships in the matching `docs/` page, keep the "unsupported" lists in `docs/network-path-primitives.md` accurate until steps 52–53 decide them, and update `README.md`'s capability table only where behaviour changed.
- Run the focused and full offline tests, the acceptance suite, Ruff, and the whitespace check, then review the full change as the Checks section requires.

**Advice:** Do not describe the elements as decided anywhere yet: this step settles contracts, and the primitives still answer `unknown` for them.

**Acceptance check:** Every check passes; all fixtures old and new validate; the docs describe the contracts without claiming decisions; the plan marks step 49 implemented.

### Step 50: Collect the new elements from AWS
**Status:** Planned.
**Prerequisite:** Step 49.

- Collect NAT gateways, VPC peering connections, transit gateways with their attachments, route tables, associations, propagations, and routes, VPC endpoints, and managed prefix lists with their entries, through read-only `Describe*`, `Get*`, and `Search*` calls added to the allowlist and the permissions documentation.
- Read the IPv6 fields already present in the VPC, subnet, ENI, route-table, security-group, and NACL responses, and egress-only internet gateways.
- Bound every new call with the shared call budgets and pagination limits, sanitize values as the existing collectors do, and qualify each operation's coverage, so a denied or failed read leaves its elements `unknown` rather than absent.
- Resolve the recorded route targets to the new resources; a target owned by another account is recorded as external, never as a resource of this snapshot.

**Advice:** Transit gateway routes are the expensive read; search each route table once per collection with a bounded filter, and record when the bound is reached as incomplete coverage.

**Acceptance check:** Botocore Stubber tests cover each operation's success, pagination, denial, malformed responses, and bounds; the whole-architecture collection includes the new elements with coverage; the permissions page and allowlist list exactly the new read actions; no test needs AWS.

### Step 51: Import the new elements from Terraform and CloudFormation
**Status:** Planned.
**Prerequisite:** Step 50.

- Map the Terraform resource types (`aws_nat_gateway`, `aws_vpc_peering_connection`, `aws_ec2_transit_gateway` and its attachment and route-table types, `aws_vpc_endpoint`, `aws_ec2_managed_prefix_list`, `aws_egress_only_internet_gateway`) and the matching CloudFormation types onto the new models, from state, plans, and templates, in place of today's unknown resources.
- Keep the importers' rules: an unknown or planned value is never a decided one, and a reference that cannot be resolved is a cited unresolved dependency.
- Extend comparison so an observed element and its declared counterpart match only by provider identity, as today.

**Advice:** Add one Terraform state, one plan, and one CloudFormation fixture per new type rather than one large template, so a mapping failure points at its type.

**Acceptance check:** Each type imports from each source with exact citations; unknown planned values stay indeterminate; comparison matches observed and declared elements without guessing; existing import fixtures are unchanged.

### Step 52: Decide prefix lists and IPv6 in the primitives
**Status:** Planned.
**Prerequisite:** Step 49.

- Expand a managed prefix list referenced by a security-group rule, NACL, or route into its recorded entries, citing the list. An AWS-managed list whose entries are not recorded stays `unknown`.
- Evaluate each primitive per address family: IPv6 security-group and NACL entries, IPv6 longest-prefix routes, egress-only internet gateways (outbound only), and recorded public IPv6 addresses on ENIs.
- Keep the existing IPv4 behaviour and results unchanged.

**Advice:** Treat a dual-stack resource as two questions, one per address family, rather than one merged answer, so a denial over IPv4 never hides an allow over IPv6.

**Acceptance check:** Primitive tests decide each new case from cited configuration, a partially covering prefix-list or IPv6 entry is `unknown`, unrecorded list entries are `unknown`, and every existing primitive test passes unchanged.

### Step 53: Route through NAT gateways, peering, transit gateways, and endpoints
**Status:** Planned.
**Prerequisite:** Steps 50 and 52.

- Extend `RouteReach` and route evaluation: a NAT gateway route reaches the internet outbound only, and only when the NAT gateway is available, has a public address, and its own subnet routes to an attached internet gateway; a NAT gateway never allows inbound traffic.
- A peering route reaches the peer VPC's CIDR only when the connection is active and both sides' route tables route to each other.
- A transit gateway route reaches a destination only when the source attachment's associated transit gateway route table routes to the destination's attachment, and the return path does too.
- A gateway endpoint route reaches its AWS service's prefix list; an interface endpoint is an ENI with security groups.
- Add a composed route between two subnets in `network_composition.py` that follows local, peering, and transit gateway routes in both directions, citing every hop.
- A hop owned by another account, or outside the snapshot, is `unknown` with a blocker naming it; it is never an allow.

**Advice:** Bound the hops: a path crosses at most one transit gateway, or one peering connection, plus local routes. A deeper chain is `unknown` and says so, rather than searching the graph.

**Acceptance check:** Each route kind decides `allow`, `deny`, and `unknown` cases from fixtures with every hop cited; blackhole, pending, and deleted states deny or are `unknown` as recorded; a cross-account hop is `unknown`; existing local and internet-gateway route results are unchanged.

### Step 54: Use the new paths in the existing analyzers
**Status:** Planned.
**Prerequisite:** Step 53.

- Database access: sources in another VPC of the same snapshot are evaluated through peering or a transit gateway, instead of being unsupported; a source behind another account's hop stays an `unknown` candidate.
- Internet exposure: recorded public IPv6 addresses are candidates over IPv6, and security-group rules that name prefix lists are decided from their entries.
- Security-group impact: rules that reference a prefix list name it with its entries.
- Update the analyzers' vocabulary, limitations, and the analyzer pages; keep each result contract additive, so saved results from earlier versions load unchanged.

**Advice:** Re-run the phase 3 acceptance fixtures before changing any analyzer: results that do not involve a new element must not change, and a test should prove it.

**Acceptance check:** New fixtures produce cited cross-VPC database-access findings and IPv6 exposure findings; every existing analyzer fixture gives the same result as before; the assistant tools return the new findings unchanged, since they read the analyzer results.

### Step 55: Add the internet egress analyzer
**Status:** Planned.
**Prerequisite:** Step 54.

- Add an `internet_egress` analyzer: which compute resources (EC2, ECS tasks, Lambda functions in a VPC) can open a connection to the internet, through an internet gateway with a public address, a NAT gateway, or an egress-only gateway over IPv6, composed from security groups, NACLs, and routes, with the same statuses, blockers, and citations as internet exposure.
- Register it in `DEFAULT_REGISTRY`, add its result models, document it in `docs/analyzers.md` and its own page, and offer it to the assistant tools.

**Advice:** Report destinations as the whole internet or as the recorded prefix-list or CIDR a rule allows; do not attempt per-destination reachability.

**Acceptance check:** Fixtures with a public subnet, a private subnet behind a NAT gateway, a subnet with no route out, and an IPv6-only egress path each give the expected cited result; the analyzer reference, drift tests, and tool catalog include it.

### Step 56: Close phase 6 with acceptance and documentation
**Status:** Planned.
**Prerequisite:** Step 55.

- Add offline acceptance journeys through the installed CLI: collect (Stubber-backed) or import the new fixtures, analyze them, and ask about them through `ask` with fake transcripts and through the MCP server, with cited answers that state their limitations.
- Update the README (capabilities, delivery table with a phase 6 row, permissions, acceptance scenarios), `docs/network-path-primitives.md`'s supported table, and every page that lists these elements as unsupported.
- Run focused and full offline tests, the acceptance suite, Ruff, whitespace checks, and the suite in Docker with networking disabled.

**Advice:** Keep the scenario table honest: list what is still `unknown` after this phase (another account's hops, deeper transit chains, Network Firewall, Gateway Load Balancer endpoints) next to what is now decided.

**Completion criterion:** Every route target kind that `RouteTargetKind` records is either decided from cited configuration or `unknown` with a named blocker; database access across VPCs, internet exposure over IPv6, and internet egress are answered offline with citations; existing results are unchanged; no test needs AWS or the network.

### Phase 6 decisions and open questions

**Advice:** Decide an element only from recorded, cited configuration. Each new kind of element widens what the analyzers can say, so keep the `unknown` path the default.

**Open questions (settle before the named step):**
- Should snapshots of several accounts be composed, so a peering connection or transit gateway that crosses accounts can be decided (step 53)? Collection writes one snapshot per account. The plan assumes not in this phase: a cross-account hop stays `unknown`. Composing snapshots would need a multi-snapshot graph and its own phase.
- Is the internet egress question added to the README example questions, the assistant prompts, and the evaluation suites (step 55)? Adding it changes the phase 4 and 5 fixtures; the plan assumes the analyzer is available to the tools without becoming a sixth README question.
- Are AWS Network Firewall and Gateway Load Balancer endpoints in scope (step 53)? The plan assumes not: a route to either stays `unknown`.
- How many transit gateway routes may one collection read before coverage is marked incomplete (step 50)?

## 7. Browser UI for results, evidence, and questions

**Phase 7 milestone:** A user starts `archdiscov serve` over a directory of snapshots and saved results → opens a local browser page → browses snapshots, findings, comparisons, and the evidence behind every fact → asks one question with a configured provider and sees the same validated, rendered answer `ask` prints → nothing on any page states a fact the renderers did not.

The web UI is a second presentation over the existing services, as the MCP server is a second front end over the tool session. Its distinct value over an MCP client is browsing saved findings, comparisons, and evidence without a model in the loop, so the pages are results first and the question form second. The server is local, single-user, and read-only: it binds to loopback only, serves files chosen from its configured directory and never from a browser-supplied path, keeps provider configuration and keys on the server, runs one question at a time within the `ask` limits, and writes nothing except a transcript when asked to. Text from the cloud is untrusted: every template autoescapes, and quoted text is shown as `ask` shows it. The pages are server-rendered HTML with no JavaScript build chain, so the repository stays Python-only and the tests run offline through an in-process HTTP client and the scripted fake provider. This phase adds no analyzer, collector, cloud platform, provider, or remediation; the README's deferral of a web UI is lifted by this phase and nowhere else.

Implement steps 57–60 in order, verifying each before the next. Break each step into substeps when it starts, as steps 34–48 were. Keep this phase planned until step 60 passes. Phase 7 touches only new modules and may start before step 56 if the schedule needs it, but its evidence and route pages are easier to finish once phase 6 has settled the network contracts.

**Where things go:**
- The server lives in a new `src/archdiscov/web/` package: an application factory built from a workspace directory and options, route modules by page family (snapshots and results, evidence and queries, ask), Jinja2 templates under `web/templates/`, and one stylesheet under `web/static/`. Only `web/` imports the web framework, the template engine, and the ASGI server, and only when the server is built, as `mcp_server.py` does with `mcp`.
- The command lives in `cli_serve.py`, which stays routing and presentation: it parses options, builds the application, and runs the ASGI server.
- Logic a page needs that today lives only in a `cli_*_output` module (naming, quoting, ordering, summaries) moves into `services/` or `assistant/` so both presentations share it; `web/` never imports a `cli_*` module.

**What is reused:**
- `load_snapshot`, `load_analysis`, `load_comparison`, `GraphIndex`, and the detached `SnapshotAccess` for browsing; the query services (`services/queries.py` and the search, inspection, neighbour, and path services) with their exact filters and bounds; the evidence services behind the `evidence` command.
- `ToolSession.from_paths`, `answer_selected`, `AskCancelled`, `RunLimits`, `load_provider_config`, and `resolve_api_key` for questions; `render_answer`, `render_failure`, and `RenderedAnswer` for what the page shows; the `ask` pre-run diagnostics and outcome codes, mapped to HTTP statuses.
- The sanitizers and the quoting of untrusted text; the no-overwrite persistence for transcripts.
- The import-isolation test, the installed-CLI harness, the offline guard, and the Docker image and compose file for acceptance.

### Step 57: Serve the workspace and browse snapshots and results
**Status:** Planned.
**Prerequisite:** Step 56 (may start after step 49 if phase 6 stalls; see above).

- Add `archdiscov serve WORKSPACE` with `--host` (default `127.0.0.1`), `--port` (default stated in the docs, `0` for an ephemeral port), and `--config PATH` for the provider configuration step 59 uses. Refuse a non-loopback host with a fixed error; a later step may add authentication, this one does not.
- Build the application from the workspace: list the snapshot files, analysis results, and comparison results in the directory once at start (and again on a `refresh` action), validate each with the existing loaders, and give each a stable opaque key. Every page addresses a file by its key; a path, a `..`, or an unknown key is a `404`, never a filesystem read.
- Pages: an index of snapshots (platform, account, region, collection time, resource count, coverage summary) with the saved results that name each one; a snapshot page with resource counts by type, coverage by operation, and limitations; an analysis page listing findings with status, blockers, and citations; a comparison page listing matched, differing, unmatched, and ambiguous entries with both sides' citations. A file that fails validation appears with its diagnostic and no pages.
- Every page has a JSON twin at the same path with an `Accept: application/json` header or a `.json` suffix, returning the loaded model's JSON, so the HTML never carries a fact the JSON lacks.
- Import the web framework, template engine, and ASGI server only when the server is built; extend the import-isolation test so `ask`, `mcp`, and every other command never import them.

**Advice:** Choose the opaque key from the file's validated content (for example the snapshot ID or result ID), not from its name, so two directories holding the same files address them alike and a renamed file keeps its links.

**Acceptance check:** In-process HTTP client tests walk every page over the committed fixtures and compare each JSON twin to the loader's output; a traversal path, an unknown key, and an invalid file give the documented statuses; a non-loopback host is refused before binding; no command other than `serve` imports the web dependencies.

### Step 58: Drill down to resources, evidence, and queries
**Status:** Planned.
**Prerequisite:** Step 57.

- Add a resource page: the inspection the `query` services give (type, identity, containment, fields with their fact citations, dependencies to local, external, and unresolved targets, coverage for its operations), with each citation linking to an evidence page that shows the recorded evidence entry (source operation, path, time, and the sanitized recorded value) exactly as the `evidence` command does.
- Add neighbours and paths pages over the directional neighbour and bounded path services with the same bounds and limitation reporting; the limitations are shown, never silently dropped.
- Add a search form over the exact-filter search service, with the service's own limits on results and no free-text matching the service does not offer.
- Link every citation on the analysis, comparison, and (in step 59) answer pages to its evidence page, so a reader reaches the receipt behind any claim in one click.
- Render imported text (names, tags, descriptions, policy text) through autoescaping and the same quoting the renderers use, so a resource named like an instruction reads as a quoted value on every page.

**Advice:** Keep the pages thin over the query services: if a page needs a filter, bound, or ordering the service lacks, add it to the service so the `query` CLI and the assistant tools gain it too.

**Acceptance check:** Tests open a finding, follow a citation to its evidence page, and compare the shown value to the evidence record; neighbours, paths, and search give the services' results and limitations; an unresolved or external target is shown as such; a resource name holding HTML and an instruction-like string renders escaped and quoted.

### Step 59: Ask a question from the browser
**Status:** Planned.
**Prerequisite:** Step 58.

- Add a question form on a snapshot page (and on a comparison pair) that posts one question. The server builds a `ToolSession` from the selected files, the provider from the configuration given at start, and runs `answer_selected` with `RunLimits.from_config`; the browser never names a configuration, a key, or a file path.
- Run one question at a time: a second question while one runs gets `409` with a fixed message. Cancel the run when the client disconnects, through the existing cancellation, and explain the cancelled run as `ask` does.
- Show the `RenderedAnswer`: the validated conclusions rendered from records, the acknowledged limitations, the retrieved summary, and the model's explanation in a clearly labelled unverified box; a rejected, insufficient, provider-failed, or cancelled run shows `render_failure`. Each citation links to its evidence page from step 58. The JSON twin returns the `ask --json` rendering unchanged.
- Save a transcript only when `serve` was started with `--transcripts DIR`, through the no-overwrite persistence, and show the saved path on the answer page.
- Reuse the `ask` pre-run diagnostics (missing configuration, provider profile unavailable, result not belonging to the selected snapshots) as `4xx` responses with the same codes and messages.

**Advice:** Do not add chat, memory, or follow-up questions: each submission is one `ask` run, as the CLI and the MCP server are, and the answer page says so.

**Acceptance check:** With the scripted fake provider, each of the five README questions asked from the browser renders the same conclusions, limitations, and citations as `ask` prints for the same transcript; a rejected and a repaired transcript show their outcomes; a concurrent question gets `409`; a disconnect cancels the run and leaves no transcript; the JSON twin equals the `ask --json` output; no key or configuration content appears in any response.

### Step 60: Close phase 7 with acceptance and documentation
**Status:** Planned.
**Prerequisite:** Step 59.

- Add offline acceptance journeys through the installed CLI: start `archdiscov serve` on an ephemeral port over a workspace of committed fixtures, walk the index, a snapshot, a finding, a citation, and an evidence page with an HTTP client, ask a README question with the fake provider, and check the rendered answer and its JSON twin; stop the server cleanly.
- Run the same journey in the Docker image with networking disabled, binding inside the container and connecting from inside it.
- Write `docs/web-ui.md`: what the pages show, the workspace layout, the loopback-only and read-only boundaries, what a transcript is written for, how untrusted text is rendered, and what the UI does not do (no chat, no analysis on demand, no authentication, no remote binding). Add the capability row and a phase 7 delivery row to the README, remove the web UI from the README's deferred list, and link the page from `docs/project-layout.md`.
- Run focused and full offline tests, the acceptance suite, Ruff, whitespace checks, and the import-isolation test, then review the full change as the Checks section requires.

**Advice:** Keep the documentation's limits honest, as the MCP page does: the UI shows what the renderers produce and links every claim to evidence, but it is a local tool for one user and makes no claim about hosting, access control, or multi-user use.

**Completion criterion:** Every page is rendered from loaded models or the existing renderers and has a JSON twin equal to the service output; every citation reaches its evidence record; a question from the browser gives the `ask` result unchanged; the server binds only to loopback, reads only its workspace, and writes only requested transcripts; the suite stays offline.

### Phase 7 decisions and open questions

**Advice:** Treat the UI as presentation only. If a page needs a fact, a filter, or a summary the services do not give, add it to the service and its tests first, so the CLI, the assistant tools, and the page all agree.

**Decisions made in this plan:**
- Server-rendered HTML with Starlette, Jinja2, and uvicorn as regular dependencies imported only by `web/`; FastAPI is not needed because the JSON twins return existing models and no OpenAPI schema is served. Revisit only if another client needs a documented HTTP API.
- No JavaScript in this phase. Progressive enhancement (for example a vendored htmx file for partial updates, or streamed progress for a running question) is a later step and must not change what a page shows.
- No authentication, and non-loopback binding is refused. Opening the server beyond loopback needs a token or a reverse proxy and its own step with a security review.
- Saved results only: the server does not run analyzers, comparisons, or collection. Running an analyzer from the browser would make the server write to the workspace and is left to a later phase if wanted.

**Open questions (settle before the named step):**
- Is the workspace one flat directory, or the `snapshots/`, `analyses/`, and `comparisons/` layout the CLI examples use (step 57)? The plan assumes one directory scanned by file type, with subdirectories allowed one level deep.
- Does the index page need a coverage summary per snapshot, or is the snapshot page enough (step 57)? The plan assumes a one-line summary (operations complete, limited, and failed) on the index.
- Should the question form offer the five README questions as presets, as the MCP server offers them as prompts (step 59)? The plan assumes yes, since they are the questions the evaluation suites verify.
- Should the comparison pages be part of step 57 or deferred to a later step to keep 57 small (step 57)? The plan assumes they are in 57, because the loader and model already exist and the page is a list.
