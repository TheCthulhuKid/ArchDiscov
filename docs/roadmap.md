# Roadmap

What has been delivered, phase by phase, what each capability covers, and what is deliberately
deferred. The active plan is [PLAN.md](../PLAN.md); finished phases are archived in
[plan-archive/](plan-archive/).

## Delivery increments

| Increment | Current status |
| --- | --- |
| 1. Models, snapshots, fixtures, persistence, evidence inspection, and CLI foundation | Complete (steps 1–9) |
| 2. AWS and infrastructure-as-code ingestion | Complete (steps 10–22): supported AWS inventory, offline IaC imports, explicit CLI formats, permissions and offline acceptance |
| Pre-phase-3 cloud boundary bridge (22.1–22.4) | Implemented foundation: scopes/identities/resources, schema `3` and conversion, offline Azure workflows, and common access/compatibility documentation; not live Azure support |
| 3. Graph queries, deterministic findings, and snapshot comparisons | Complete (steps 23–33): result contracts and integrity, graph indexes, queries and the `query` CLI, the five analyzers with immutable result persistence, snapshot comparison, and offline [host and container acceptance](acceptance.md#phase-3-offline-acceptance) |
| 4. LLM-provider adapters, orchestration, validated answers, and evaluations | Complete (steps 34–44); separate from cloud platforms: [provider contracts, configuration, and the fake provider](assistant-providers.md) (step 34), [assistant tools](assistant-tools.md) (step 35), the [tool-calling loop](assistant-orchestration.md) (step 36), [answer validation](assistant-answers.md) (step 37), [answer outcomes, repair, and renderings](assistant-answers.md#outcomes-and-exit-codes) (step 38), the [`ask` command](ask-command.md) (step 39), the [Anthropic adapter](assistant-providers.md#anthropic-adapter) (step 40), the [OpenAI-compatible adapter](assistant-providers.md#openai-compatible-adapter) (step 41), the [Bedrock adapter](assistant-providers.md#bedrock-adapter) (step 42), [evaluation fixtures and commands](assistant-evaluation.md) (step 43), and offline [host and container acceptance](acceptance.md#phase-4-offline-acceptance) (step 44) |
| 5. MCP server over the assistant tools | Complete (steps 45–48); no provider, credential, or network: the [MCP server](mcp-server.md) serves the phase 4 tools (step 45), validates and renders submitted answers as `ask` does (step 46), and runs as `archdiscov mcp` over stdio with the README questions as prompts (step 47), with offline [host and container acceptance](acceptance.md#phase-5-offline-acceptance) (step 48) |
| 6. Complete the AWS network paths | In progress (steps 49–56): NAT gateways, peering, transit gateways, VPC endpoints, prefix lists, and IPv6 are modelled, collected, and imported from Terraform and CloudFormation (steps 49–51); deciding them from cited configuration follows; see [PLAN.md](../PLAN.md) |
| 7. Browser UI for results, evidence, and questions | Planned (steps 57–60); see [PLAN.md](../PLAN.md) |

The bridge prepares phase 3 to build common graph/query and ownership contracts.
Security, network, and trust analyzers are initially planned for AWS; unsupported Azure
analysis must return actionable limitations rather than reuse AWS semantics. No graph,
analyzer, GCP, Terraform HCL parser, Azure authentication, or live Azure collection is
implemented by this bridge. See [boundary validation and extension guidance](cloud-snapshots.md#validation-and-future-extensions).

## Capabilities

| Capability | Status |
| --- | --- |
| Snapshot models, evidence/fact citations, coverage, sanitization, and schema `1`–`3` validation | Implemented; legacy AWS schemas `1`/`2` unchanged |
| Local no-overwrite persistence, snapshot JSON `ingest`, and `evidence` inspection | Implemented |
| Cloud scopes/identities, schema `3`, explicit AWS upgrade, and detached `SnapshotAccess` | Implemented; [contracts and APIs](cloud-snapshots.md) |
| Azure VNet/subnet/VM snapshots | Offline fixture only; no Azure SDK, authentication, or live collector |
| EC2/network, IAM, ALB/NLB, ECS, Lambda, RDS/Aurora, and SQS configuration collection | Implemented |
| Multi-account `collect --all-services`, enabled-region discovery, shared call budgets, async APIs, and cancellation-safe draining | Implemented |
| Offline Terraform `show -json` state import through Python services | Implemented; [mapping and limitations](terraform-state-mapping.md) |
| Offline Terraform `show -json` plan import through Python services | Implemented; [mapping and limitations](terraform-plan-mapping.md) |
| Offline CloudFormation JSON/YAML import through Python services | Implemented; [mapping and limitations](cloudformation-mapping.md) |
| Explicit snapshot/Terraform state/Terraform plan/CloudFormation CLI ingestion | Implemented (step 21); [usage and contracts](raw-source-ingestion.md) |
| Phase 2 permissions, setup, and offline acceptance | [Consolidated permissions](aws-permissions.md) and [acceptance workflow](acceptance.md#phase-2-offline-acceptance) |
| Docker development environment and GitHub Actions pytest/Ruff checks | Implemented |
| Analysis result contracts and `Finding`/`Answer` compatibility | Implemented snapshot-backed integrity checks; no answer generation |
| Analyzer execution, result persistence, and ownership analysis | [Implemented for the ownership analyzer](ownership-analysis.md): `analyze` and `findings` CLI, coverage-qualified, cited, atomic no-overwrite results |
| Security-group impact analysis | [Implemented for AWS](security-group-impact.md): observed attachments and rule references, potential downstream chains, Azure unsupported result |
| Network-path evaluation primitives | [Implemented for AWS](network-path-primitives.md): security-group rules, ordered NACLs, subnet and internet-gateway routes, public addressing, and load-balancer scheme/forwarding return allow/deny/unknown with evidence and blockers over recorded configuration, not live reachability; Azure unsupported result; composed by the database-access and internet-exposure analyzers |
| Cross-account trust analysis | [Implemented for AWS](cross-account-trust.md): classified, cited IAM role trust and SQS queue-policy principals with conditions and `Deny`/`NotPrincipal` context; configured trust, not effective access |
| Database access analysis | [Implemented for AWS](database-access.md): compute sources (EC2, ECS, Lambda) with a recorded potential network path to an RDS instance or cluster port, composed from security groups, NACLs, and routes with per-element decisions, blockers, and citations; network permission, not authentication or live reachability |
| Internet exposure analysis | [Implemented for AWS](internet-exposure.md): candidate exposure of resources with a recorded public IPv4 address and of internet-facing load balancers, composed from routes, NACLs, security groups, listeners, and targets, with per-element decisions, blockers, citations, and observed target health; candidate exposure from recorded configuration, not proof of reachability |
| Analyzer reference and result file contracts | [Five analyzers compared](analyzers.md) (parameters, limits, uncertainty rules) and [analysis and comparison result files](result-contracts.md) |
| Graph resource ownership, lookup, containment, dependencies, and evidence/coverage | [Implemented and verified together across schemas 1–3](graph-index.md#composed-graph-boundary), including external/unresolved terminals and complete-context coverage |
| Resource search, inspection, neighbours, and paths | [Implemented Python services and `archdiscov query` CLI](graph-queries.md) with exact filters, bounds, detached data, citations, and coverage/provenance limitations |
| Snapshot comparison | [Implemented](comparison-results.md): `compare` and `comparison` CLI, identity matching without guessing, field-level differences with citations on both sides, lifecycle, source-kind, and coverage qualification, atomic no-overwrite results verified against both snapshots; recorded differences, not drift detection or proof of change |
| MCP server over the assistant tools | [Implemented](mcp-server.md): `archdiscov mcp` serves the same bounded, read-only tools and the five example questions (as prompts) over stdio to a client such as Claude Code or Claude Desktop, with no provider configuration, credentials, or network; a submitted answer is validated against the tools' results and rendered as `ask` does, though the client's own text is not validated |
| LLM assistant/providers | [Provider-neutral contracts, configuration, and a scripted fake provider](assistant-providers.md), [bounded typed assistant tools](assistant-tools.md), the [tool-calling loop](assistant-orchestration.md), and [structured answers with validation, one repair, conclusions rendered from records, failure explanations, and readable and JSON renderings](assistant-answers.md) behind the [`ask` command](ask-command.md), which answers one question with a configured provider, verified offline through the installed CLI with the five example questions; the fake provider, the [Anthropic adapter](assistant-providers.md#anthropic-adapter), and the [OpenAI-compatible adapter](assistant-providers.md#openai-compatible-adapter) (OpenAI or a compatible server, through the configured Responses or Chat Completions protocol with declared capabilities), and the [Bedrock adapter](assistant-providers.md#bedrock-adapter) (Converse, from its own AWS profile and optional role, with declared capabilities) answer, each adapter also verified through the installed CLI against a loopback mock API; [evaluation](assistant-evaluation.md) runs checked-in README and adversarial suites across configured providers with the `evaluate` command, scoring each answer against deterministic findings and safety checks into an immutable report that `evaluation` shows and compares, external providers only on an explicit opt-in after a stated budget |

## Assumptions and deferred work

- Use Python 3.13, uv, boto3, Pydantic, Typer, PyYAML, and pytest.
- Accounts are explicitly configured; regions are selected explicitly or discovered after identity verification. AWS Organizations discovery is deferred.
- Terraform HCL parsing, additional AWS services, historical monitoring, and automatic remediation are deferred. A local, read-only browser UI is planned as phase 7.
- Saved paths are immutable through the persistence API; loaded models remain mutable. Collection creates a new snapshot, and answers identify the snapshots they analyze.
