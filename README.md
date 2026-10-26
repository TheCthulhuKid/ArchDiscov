# Archdiscov: Evidence-backed cloud architecture snapshots

Archdiscov is a Python CLI and reusable core for collecting AWS architecture configuration,
saving validated evidence-backed snapshots, and inspecting their facts and limitations.
It supports explicitly configured accounts, enabled-region discovery, and bounded async
collection across seven service families, with one immutable snapshot file per account.
Terraform state and plan exports and CloudFormation templates import offline into the same
snapshots. Deterministic analyzers, snapshot comparison, and an assistant that answers only
from cited records work over those files, from the CLI or from an MCP client.

The cloud-neutral snapshot boundary also supports offline Azure fixture ingestion and
evidence inspection; it does not provide live Azure collection or analysis.

New to the project? The [overview](docs/overview.md) explains it in plain language, and the
[documentation index](docs/README.md) lists every reference page.

## Status

Phases 1–5 are complete: snapshots and evidence, AWS and infrastructure-as-code ingestion,
graph queries, five analyzers and comparison, the `ask` assistant with four providers, and
the MCP server. Phase 6, which follows network paths through NAT gateways, peering, transit
gateways, VPC endpoints, prefix lists, and IPv6, is in progress, and phase 7 adds a local
browser UI. See the [roadmap](docs/roadmap.md) for what each capability covers and what is
deferred, and [PLAN.md](PLAN.md) for the active plan.

## Installation

Use Python 3.13 and uv. From the repository root, install the package and development
dependencies from the committed lockfile:

```bash
uv sync --locked --python 3.13
uv run --locked archdiscov --help
```

Run the same checks as CI through the project virtual environment:

```bash
.venv/bin/python -m pytest -n 4 --dist worksteal
.venv/bin/ruff check .
.venv/bin/ruff format --check --exclude .
```

Tests use pytest-xdist workers; reduce the count on constrained machines, or use `-n 0` to run
serially. They need neither AWS or provider credentials nor network access. See
[development](docs/development.md) for continuous integration, the Docker development
environment, and runtime credentials, and the [project layout](docs/project-layout.md) for
where code and tests go.

## Quick tour

Every step below runs offline against the committed fixtures. [Testing the CLI
locally](docs/local-cli-testing.md) walks through the full set, including `ask` with the fake
provider.

```bash
mkdir -p snapshots results
uv run --locked archdiscov ingest tests/fixtures/snapshots/network.json \
  --output snapshots/network.json
uv run --locked archdiscov evidence sg-rule --snapshot snapshots/network.json
uv run --locked archdiscov query search --snapshot snapshots/network.json --limit 5
uv run --locked archdiscov analyze --snapshot snapshots/network.json \
  --output results/exposure.json --analyzer internet_exposure --max-depth 3
uv run --locked archdiscov findings results/exposure.json --snapshot snapshots/network.json
```

Saved files are never overwritten; choose fresh paths on a rerun. Live AWS collection
(`archdiscov collect`) is read-only and needs the permissions in
[AWS permissions](docs/aws-permissions.md); see [AWS collection](docs/aws-collection.md).

## Commands

| Command | Purpose |
| --- | --- |
| `collect` | Read configuration from one or more AWS accounts into one snapshot per account ([AWS collection](docs/aws-collection.md)). |
| `ingest`, `evidence` | Import a snapshot, Terraform export, or CloudFormation template, then inspect a cited evidence record ([ingest and evidence](docs/cli.md), [raw-source ingestion](docs/raw-source-ingestion.md)). |
| `query search`, `inspect`, `neighbours`, `paths` | Read recorded resources and structure with exact filters and bounded results ([graph queries](docs/graph-queries.md)). |
| `analyze`, `findings` | Run one of the five analyzers and save an immutable result, then show it after verifying it against its snapshot ([analyzer reference](docs/analyzers.md)). |
| `compare`, `comparison` | Compare two selected snapshots and save an immutable result, then show it after verifying it against both ([comparison results](docs/comparison-results.md)). |
| `ask` | Answer one question from the selected snapshots with a configured provider, as a validated answer cited from tool results or an explained failure, with an optional transcript ([the ask command](docs/ask-command.md)). |
| `mcp` | Serve the selected snapshots' read-only assistant tools over stdio to an MCP client, which submits answers for the validation and rendering `ask` applies ([the MCP server](docs/mcp-server.md)). |
| `evaluate`, `evaluation` | Run a suite of questions with each configured provider after stating the budget, and save an immutable scored report; then show a report or compare two ([evaluation](docs/assistant-evaluation.md)). |

Saved results follow the [result file contracts](docs/result-contracts.md). Commands provide
readable output and machine-readable JSON, and call public application services rather than
containing discovery or analysis logic. Snapshots themselves are described in the
[snapshot model and format](docs/snapshot-format.md).

## Analyzers

The five analyzers run through `archdiscov analyze` over a saved snapshot and save immutable
results; the [analyzer reference](docs/analyzers.md) lists their parameters, limits, and
uncertainty rules.

| Analysis | Behavior and limits |
| --- | --- |
| [Internet exposure](docs/internet-exposure.md) | Trace supported public ingress through addressing, routes, security groups, NACLs, listeners, and targets. Report candidate exposure and blockers; unresolved path elements produce an uncertain result. |
| [Database access](docs/database-access.md) | Identify compute resources with a supported potential network path to the selected database and port. Distinguish network permission from successful database authentication. |
| [Security-group impact](docs/security-group-impact.md) | List direct attachments and rule references, then trace downstream dependencies with paths and configurable depth. Label indirect impact as potential. |
| [Cross-account trust](docs/cross-account-trust.md) | Extract role trust principals and supported resource-policy relationships, retaining conditions and distinguishing named accounts from wildcard or unresolved principals. Trust permission alone does not prove effective access. |
| [Ownership](docs/ownership-analysis.md) | Report resources missing configured ownership tags. Default to `Owner` or `Team`; this is an ownership signal, not proof that nobody owns the resource. |

Findings and answers share one status vocabulary (`AnalysisStatus` in
`archdiscov.models.enums`); there are no severity scores:

| Status | Meaning and required limitations |
| --- | --- |
| `supported` | The stated conclusion is supported within its scope; scope caveats may still be recorded |
| `partial` | Some supported conclusions are available, but a nonempty limitations list must explain the gaps |
| `insufficient_evidence` | No definitive conclusion is justified; a nonempty limitations list explains why, and citations may be empty |

Archdiscov does not attempt comprehensive IAM policy evaluation, organization-policy
evaluation, application behavior analysis, or complete network simulation. A decision
describes recorded configuration, never live connectivity.

## Assistant

The [`ask` command](docs/ask-command.md) answers one question from the selected snapshots
with a configured provider:

- [Providers and configuration](docs/assistant-providers.md): a provider-neutral interface for messages, tool calls, and results, with the scripted fake, Anthropic, OpenAI-compatible (Responses or Chat Completions, with declared capabilities), and Bedrock Converse adapters. Each configuration file sets one provider's model, endpoint, timeouts, retries, and request, tool-call, and output limits; credentials come only from a named environment variable or AWS profile at run time and never appear in output.
- [Tools and limits](docs/assistant-tools.md): typed, bounded, read-only tools for resource search and inspection, relationships, deterministic findings, coverage, evidence, and saved results, in a [bounded tool-calling loop](docs/assistant-orchestration.md). Resource names, tags, and imported text are quoted data, never instructions.
- [Answers and failures](docs/assistant-answers.md): a status (`supported`, `partial`, or `insufficient_evidence`) and conclusions rendered from the findings, evidence, and resources the tools returned, qualified by coverage, with snapshot times and planned sources labelled. An invalid proposal gets one repair; otherwise the failure is explained, with a stable exit code.
- [Evaluation](docs/assistant-evaluation.md): the `evaluate` and `evaluation` commands score README and adversarial suites across configured providers against deterministic findings and safety checks.

`ask` keeps no conversation memory, makes no changes, and does not evaluate effective access
or prove reachability; see [what is not supported](docs/ask-command.md#what-is-not-supported).

The [MCP server](docs/mcp-server.md) (`archdiscov mcp`) lets a user's own MCP client, such as
Claude Code or Claude Desktop, be the model. It serves the same bounded, read-only tools over
stdio for snapshots chosen when it starts, offers the five example questions below as prompts,
and needs no provider configuration, credentials, or network. A client's `submit_answer` call is
validated against the IDs the tools returned and rendered exactly as `ask` renders an answer.
The server cannot stop a client from stating unvalidated claims in its own text: treat only the
answer the server returned as validated.

The `ask` command answers these example questions:

- What receives traffic from the public internet?
- Which services can access this database?
- What would be affected if this security group changed?
- Where are the cross-account trust relationships?
- Which resources appear to have no clear owner?

Every factual conclusion cites discovered resources or configuration evidence and states relevant limitations.

## Acceptance

Each phase closed with offline acceptance journeys on the host and in Docker with networking
disabled. [Acceptance](docs/acceptance.md) describes them, how to run them, and which tests
cover each acceptance scenario. Real-AWS and real-provider checks stay opt-in and never run in
CI.
