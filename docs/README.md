# Documentation

Start with the [overview](overview.md) for what Archdiscov does, in plain language, and the
[project README](../README.md) for installation and a quick tour. The pages below are the
reference.

## Getting started

| Page | What it covers |
| --- | --- |
| [Overview](overview.md) | The problem, the idea, and the safeguards, without jargon |
| [Testing the CLI locally](local-cli-testing.md) | Running every command offline against fixtures |
| [Development](development.md) | Continuous integration, Docker development, runtime credentials |
| [Project layout](project-layout.md) | Packages, layers, and where tests live |
| [Acceptance](acceptance.md) | The offline acceptance journeys and the scenario-to-test map |
| [Roadmap](roadmap.md) | Delivery increments, what is deferred, and where the plan lives |

## Snapshots and evidence

| Page | What it covers |
| --- | --- |
| [Snapshot model and format](snapshot-format.md) | Hierarchy, schemas, evidence, coverage, sanitization, Python services |
| [Cloud snapshot boundary](cloud-snapshots.md) | Schema 3, cloud scope and identity, persistence |
| [Ingest and evidence commands](cli.md) | The offline workflow, JSON output, and exit codes |
| [Raw-source ingestion](raw-source-ingestion.md) | `ingest`, formats, and offline import |
| [Result file contracts](result-contracts.md) | Saved snapshot, analysis, and comparison files |

## AWS collection

| Page | What it covers |
| --- | --- |
| [AWS collection](aws-collection.md) | Discovery, the allowlist, `collect`, and per-service limits |
| [AWS permissions](aws-permissions.md) | The allowlist, required permissions, and collection boundaries |
| [Collection scope](collection-scope.md) | Whole-architecture requests, regions, and accounts |
| [EC2 network mapping](network-mapping.md) | VPCs, subnets, instances, interfaces, routes, security groups |
| [IAM mapping](iam-mapping.md) | Account-wide roles, policies, and trust |
| [Load balancing mapping](load-balancing-mapping.md) | ALB and NLB listeners, rules, and targets |
| [ECS mapping](ecs-mapping.md) | Clusters, services, and task definitions |
| [Lambda mapping](lambda-mapping.md) | Functions and their network and role configuration |
| [RDS mapping](rds-mapping.md) | RDS instances and Aurora clusters |
| [SQS mapping](sqs-mapping.md) | Queues and their policies |

## Infrastructure as code

| Page | What it covers |
| --- | --- |
| [Infrastructure-as-code import](infrastructure-as-code.md) | The Terraform and CloudFormation Python APIs |
| [Terraform state mapping](terraform-state-mapping.md) | Importing state exports |
| [Terraform plan mapping](terraform-plan-mapping.md) | Importing plans, planned changes, unknown values |
| [CloudFormation mapping](cloudformation-mapping.md) | Importing JSON and YAML templates |

## Graph and queries

| Page | What it covers |
| --- | --- |
| [Graph index](graph-index.md) | The read-only snapshot graph |
| [Graph index profile](graph-index-profile.md) | Offline profiling of index builds |
| [Graph queries](graph-queries.md) | Recorded resource queries |
| [Network-path primitives](network-path-primitives.md) | Route, rule, and reachability decisions |

## Analysis and comparison

| Page | What it covers |
| --- | --- |
| [Analyzers](analyzers.md) | Every analyzer, its parameters, and its limits |
| [Analysis results](analysis-results.md) | Statuses, findings, and integrity rules |
| [Internet exposure](internet-exposure.md) | What the public internet can reach |
| [Database access](database-access.md) | Which sources can reach a database |
| [Security-group impact](security-group-impact.md) | What changing a security group affects |
| [Cross-account trust](cross-account-trust.md) | Which outside accounts are trusted |
| [Ownership analysis](ownership-analysis.md) | Analyzer execution, persistence, and ownership tags |
| [Comparison results](comparison-results.md) | Comparing two snapshots or a plan with observed state |

## Assistant

| Page | What it covers |
| --- | --- |
| [The ask command](ask-command.md) | Asking one question from the CLI |
| [Assistant tools](assistant-tools.md) | The bounded, read-only tools the model can call |
| [Assistant orchestration](assistant-orchestration.md) | The tool-calling loop and its limits |
| [Assistant answers](assistant-answers.md) | Structured answers, validation, and the one repair |
| [Assistant providers](assistant-providers.md) | The fake, Anthropic, OpenAI-compatible, and Bedrock adapters |
| [Assistant evaluation](assistant-evaluation.md) | Evaluation suites and scoring |
| [MCP server](mcp-server.md) | Serving the tools to MCP clients |

## Plan history

Completed phases of the implementation plan are archived in [plan-archive/](plan-archive/);
the active phases are in [PLAN.md](../PLAN.md).
