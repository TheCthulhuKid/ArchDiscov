# Acceptance

The offline acceptance journeys that closed each phase, how to run them on the host and in
Docker, and the map from each acceptance scenario to the tests that cover it. None of them
needs credentials or network access.

## Offline increment acceptance

The first increment is exercised by `tests/acceptance/test_offline_acceptance.py`: from outside the
checkout, with credential variables removed, it imports hierarchy and partial-coverage
fixtures using the installed CLI, inspects cited evidence, confirms typed children and
coverage survive persistence, rejects invalid references with safe diagnostics, and
checks that re-importing cannot overwrite saved bytes.

Run the acceptance test directly and in the Docker development environment:

```bash
uv sync --locked --python 3.13
.venv/bin/python -m pytest tests/acceptance/test_offline_acceptance.py -q
docker compose build dev
docker compose run --rm -T dev python -m pytest tests/acceptance/test_offline_acceptance.py -q
docker run --rm --network none \
  --mount "type=bind,src=$PWD,dst=/workspace" \
  archdiscov-dev python -m pytest -q
```

The last command runs the entire normal suite with networking disabled and no credential
mounts. Image builds/installation may need registry access; execution does not. Docker
tests use `/opt/venv`, never the bind-mounted host `.venv`.

For a manual container import and readable evidence inspection, use a fresh destination:

```bash
mkdir -p snapshots
docker compose run --rm -T dev archdiscov ingest \
  tests/fixtures/snapshots/partial-coverage.json --output snapshots/docker-example.json
docker compose run --rm -T dev archdiscov evidence sg-rule \
  --snapshot snapshots/docker-example.json
```

Choose another output path if it already exists. Saved files are written as the configured
non-root user. Snapshot schemas, source formats, fixture purposes, and Python APIs are in the
[snapshot model and format](snapshot-format.md); command and error behavior is in
[ingest and evidence commands](cli.md). AWS, Terraform, and CloudFormation adapters
and raw-source CLI ingestion use these contracts today. Queries, analyzers, and comparisons
are described in the [analyzer reference](analyzers.md), and the assistant in [assistant behavior](../README.md#assistant).

## AWS collection acceptance

`tests/acceptance/test_whole_architecture_acceptance.py` exercises populated multi-account,
multi-region collection through the CLI using injected AWS clients, without credentials
or network calls. It covers all seven families, IAM once per account, partial results,
scope discovery/overrides, evidence inspection, redaction, failure isolation, and no overwrite.
Runner and async workflow tests additionally cover pagination, bounded concurrency,
deterministic assembly, and cancellation. Run the collection journey with:

```bash
.venv/bin/python -m pytest tests/acceptance/test_whole_architecture_acceptance.py -q
```

GitHub Actions runs the full offline suite and quality checks, not live account scans or
live provider calls; the live provider tests and the
[live evaluation](assistant-evaluation.md#verification) are deselected by default and
run only with `-m live` and their variables.
Real-AWS checks require explicit runtime credentials and remain opt-in.

## Phase 2 offline acceptance

The phase closes with complementary journeys rather than live-AWS requirements:

- `tests/acceptance/test_phase_two_acceptance.py`: real Botocore Stubber requests through
  collection, validated persistence and exact evidence inspection; a denied read
  remains partial while successful zero inventory stays distinguishable. Explicit
  local/external/unresolved targets retain their identifiers, reasons and citations.
- `tests/acceptance/test_whole_architecture_acceptance.py`: populated seven-family collection
  across accounts/regions, with discovery, isolated failures, redaction and immutable files.
- `tests/acceptance/test_raw_ingestion_acceptance.py`: the installed `archdiscov` script outside
  the checkout imports every source format without saving a protected value, inspects a cited
  fact, and refuses malformed input without creating output. Each format's mapping and context
  and parameter failures are proven by the ingestion and import-service tests, no overwrite once
  in the ingest stage of `test_installed_end_to_end.py`, and the readable summaries in
  `tests/cli`.
- `tests/acceptance/test_offline_acceptance.py` and the fixture persistence suite preserve
  historical schema `1`, typed children, citations and qualified coverage.
- `tests/ingestion/aws/test_permissions_documentation.py` prevents the published inventory policy
  from drifting from the executable allowlist, including the separate STS rules.

Run the full acceptance and regression suite without any credential mounts:

```bash
.venv/bin/python -m pytest -q
docker compose build dev
docker run --rm --network none \
  --mount "type=bind,src=$PWD,dst=/workspace" \
  archdiscov-dev /opt/venv/bin/python -m pytest -q
```

The host uses `.venv/bin/python`; Docker uses `/opt/venv/bin/python` and no host
virtual environment. Installation/builds may need network access, test execution
does not. Do not mount AWS credentials for these checks. Live smoke collection is
opt-in: authenticate a runtime profile and deliberately choose scope and a fresh
output using [the setup guide](aws-permissions.md). It is not part of pytest
or CI and is not needed to pass acceptance.

Completeness remains relative to selected supported inventory and operation coverage,
not all AWS services or a transactionally consistent view. IaC exports/declarations
are not deployed observations; complex mappings, unknown expressions and unfetched
nested templates stay explicit limitations in the linked format guides. Graph
queries, analyzers, and comparisons are phase 3 (see [analyzers](analyzers.md)); providers are phase 4;
the MCP server is phase 5.

## Phase 3 offline acceptance

Phase 3 closes with offline checks that need no credentials or network, on the host with
`.venv/bin/python` and in Docker with `/opt/venv/bin/python`:

- `tests/acceptance/test_phase_three_audit.py` and `test_phase_three_comparison_audit.py`: every
  analyzer on schemas `1`–`3` (limited and unrecorded coverage on the current schema) and the
  Azure fixture, and representative pairs of snapshot source kinds (the same kind, two AWS
  kinds, a planned side, and both clouds), against the completion criteria (cited evidence and paths, coverage qualification, no
  unqualified conclusion under partial coverage, immutable and verified results).
- `tests/acceptance/test_installed_end_to_end.py`: one journey outside the checkout through the
  installed `archdiscov` script, one test per stage (ingest, queries, each analyzer with its
  findings and evidence, each comparison), with an offline guard that fails any network use.
  Every stage starts from the same fixtures, and the ingest stage shows that `ingest` saves each
  snapshot fixture unchanged, so the stages run in parallel. The AWS-only analyzers' unsupported
  result on the Azure fixture is checked once per analyzer in `test_cloud_workflows.py`.
- `tests/acceptance/test_readme_scenarios.py` and `tests/documentation`: the scenarios below,
  and the documentation, command, limit, link, and scenario-mapping checks.

```bash
.venv/bin/python -m pytest tests/acceptance tests/documentation -q
docker compose build dev
docker run --rm --network none \
  --mount "type=bind,src=$PWD,dst=/workspace" \
  archdiscov-dev /opt/venv/bin/python -m pytest -n 8 --dist loadgroup -q
```

`--dist loadgroup` keeps each module-scoped journey (the installed `ask`, MCP, and evaluation
journeys) on one worker; `-n 8` can be dropped to run serially. The last verified run passed all 4,303 tests on the host and in Docker with networking
disabled. Image builds need registry access; test execution does not. The offline Azure fixtures
validate the generic mechanics (schemas, queries, comparison) and the explicit unsupported
results of the AWS-only analyzers; they are not Azure discovery or analysis.

## Phase 4 offline acceptance

Phase 4 closes with offline checks that need no credentials or network, on the host with
`.venv/bin/python` and in Docker with `/opt/venv/bin/python`:

- `tests/acceptance/test_installed_ask.py`: four journeys outside the checkout through the
  installed `archdiscov` script. In the first, each README example question is asked over the
  checked-in fixtures with a fake transcript and answered from the findings the tools
  returned, cited evidence is recorded in the answered snapshot and inspected through the CLI,
  and answers short of `supported` state their limitations. In the second, a fabricated or
  unreturned citation is rejected after the one repair (exit 5), a repaired proposal answers,
  and imported instructions appear only as quoted data. The third covers refusals before a run
  and provider errors and limits, and the fourth each provider adapter against a loopback mock
  API, described below. Readable output is checked once; `tests/cli` renders every outcome.
- `tests/acceptance/test_installed_evaluation.py`: the README and adversarial evaluation
  suites, described below.
- `tests/documentation`: the documented tools, limits, provider options, answer and
  evaluation contracts, commands, exit codes, links, and the scenario mapping below.

```bash
.venv/bin/python -m pytest tests/acceptance tests/documentation -n 8 --dist loadgroup -q
docker compose build dev
docker run --rm --network none \
  --mount "type=bind,src=$PWD,dst=/workspace" \
  archdiscov-dev /opt/venv/bin/python -m pytest -n 8 --dist loadgroup -q
```

The last verified run passed all 7,030 tests (52 skipped) on the host and in Docker with
networking disabled. Real-provider checks stay opt-in: the live provider tests and the live
evaluation run only with `-m live` and their variables, never in CI.

## Phase 5 offline acceptance

Phase 5 closes with offline checks that need no credentials, no provider, and no network, on
the host with `.venv/bin/python` and in Docker with `/opt/venv/bin/python`:

- `tests/acceptance/test_phase_five_acceptance.py`: two journeys outside the checkout through
  the installed `archdiscov` script, with no connection allowed at all. In the first, an MCP
  stdio client starts `archdiscov mcp` over each checked-in README case's files, lists the
  tools and the five prompts, answers each example question from its prompt through the tools and `submit_answer`,
  and retrieves cited evidence through a tool; each accepted answer equals what `ask` gives for
  the same files. In the second, the adversarial suite's unsupported claims are rejected with
  their codes and corrected in the same session, imported instructions appear only as quoted
  data, a missing or invalid snapshot fails before serving, and a session that calls every tool
  opens no file beyond the three selected, with other snapshots and results beside them.
- `tests/cli/test_mcp_cli.py`, `tests/services/test_mcp_service.py`, and
  `tests/assistant/test_mcp_server.py`: the command's diagnostics and exit codes, a real stdio
  session, and the server's tools, limits, prompts, and answer parity with `ask`.
- `tests/documentation`: the documented options, exit codes, limits, tools, prompts, and the
  scenario mapping below.

```bash
.venv/bin/python -m pytest tests/acceptance/test_phase_five_acceptance.py tests/documentation -n 8 --dist loadgroup -q
docker compose build dev
docker run --rm --network none \
  --mount "type=bind,src=$PWD,dst=/workspace" \
  archdiscov-dev /opt/venv/bin/python -m pytest -n 8 --dist loadgroup -q
```

The last verified run passed all 7,553 tests (52 skipped) on the host and in Docker with
networking disabled.

The test client is the stdio client from the `mcp` package. Real MCP clients such as Claude
Code and Claude Desktop never run in CI, and the [client setup](mcp-server.md#client-setup)
steps cannot be exercised offline.

## Acceptance scenarios

Normal tests require neither credentials nor network access. Each scenario runs offline
through pytest and is covered by the tests below, which assert its qualified outcome (blockers,
limitations, planned lifecycle, an indeterminate difference, or a rejected or qualified answer),
not only that a command ran:

| Scenario | Tests |
| --- | --- |
| Public load balancer with a private backend | `tests/analysis/test_exposure_load_balancers.py::test_a_public_load_balancer_exposes_its_listener_and_reaches_a_private_backend` |
| Public IP with blocked ingress | `tests/analysis/test_exposure_addresses.py::test_a_public_address_with_blocked_ingress_is_a_cited_denial` |
| Missing routing evidence | `tests/analysis/test_exposure_load_balancers.py::test_a_missing_default_route_denies_and_missing_route_records_are_unknown`<br>`tests/analysis/test_exposure_addresses.py::test_missing_route_or_acl_records_are_unknown` |
| Database access through security-group references, CIDRs, and denied rules, without claiming authentication | `tests/analysis/test_database_access.py::test_allowed_sources_are_supported_findings_naming_the_source_and_database`<br>`tests/analysis/test_database_access.py::test_denied_sources_are_supported_conclusions_with_cited_blockers` |
| Direct and indirect security-group dependencies | `tests/analysis/test_security_group_impact.py::test_every_attaching_service_is_a_cited_observed_direct_attachment`<br>`tests/analysis/test_security_group_impact.py::test_downstream_chains_are_potential_cited_and_separate_from_observed` |
| Explicit and conditional cross-account trust | `tests/analysis/test_cross_account_trust.py::test_explicit_cross_account_trust_is_cited_with_its_statement_and_a_terminal_limit`<br>`tests/analysis/test_cross_account_trust.py::test_conditions_effect_and_not_principal_context_are_kept` |
| Missing ownership tags and configurable ownership keys | `tests/analysis/test_ownership_analyzer.py::TestOwnershipSignals::test_missing_tags_with_cited_map_are_a_supported_signal`<br>`tests/analysis/test_ownership_analyzer.py::TestOwnershipSignals::test_custom_keys_replace_the_defaults` |
| Terraform module addresses | `tests/ingestion/terraform/test_terraform_parsing.py::test_nested_modules_preserve_addresses_and_exact_resource_pointers`<br>`tests/acceptance/test_readme_scenarios.py::test_module_addresses_survive_import_and_a_planned_resource_stays_planned` |
| Terraform planned deletions | `tests/comparison/test_qualification.py::test_a_planned_deletion_qualifies_the_pair_and_is_not_an_observed_absence`<br>`tests/acceptance/test_readme_scenarios.py::test_a_planned_deletion_is_a_planned_lifecycle_never_an_observed_absence` |
| Terraform unknown values | `tests/ingestion/terraform/test_terraform_plan.py::test_missing_null_and_unknown_are_distinct`<br>`tests/acceptance/test_readme_scenarios.py::test_an_unknown_planned_value_is_indeterminate_not_a_change` |
| CloudFormation unresolved references | `tests/ingestion/cloudformation/test_cloudformation_import.py::test_conditions_local_getatt_and_conditional_missing_target`<br>`tests/acceptance/test_readme_scenarios.py::test_cloudformation_unresolved_references_are_indeterminate_between_two_templates` |
| Safe matching of observed and declared resources | `tests/comparison/test_matching.py::test_names_and_short_ids_never_match`<br>`tests/comparison/test_matching.py::test_logical_addresses_never_match_across_iac_contexts_or_observations`<br>`tests/acceptance/test_readme_scenarios.py::test_observed_and_declared_resources_are_matched_only_by_identity` |
| README example questions answered from tool-returned findings and evidence | `tests/acceptance/test_installed_ask.py::test_every_readme_question_is_answered_from_tool_returned_findings`<br>`tests/acceptance/test_installed_ask.py::test_cited_evidence_is_inspected_in_the_answered_snapshot` |
| Coverage limitations stated in answers | `tests/acceptance/test_installed_ask.py::test_answers_short_of_supported_state_their_limitations`<br>`tests/acceptance/test_phase_five_acceptance.py::test_answers_short_of_supported_state_their_limitations` |
| Fabricated and unreturned citations rejected, and one repair | `tests/acceptance/test_installed_ask.py::test_fabricated_and_unreturned_citations_are_rejected_and_repairs_answer`<br>`tests/assistant/test_answer_suite.py::test_each_rejection_is_reached_in_isolation` |
| Unsupported claims | `tests/services/test_evaluation_suites.py::test_the_adversarial_suite_expects_each_safe_outcome` |
| Malicious imported text in names and tags | `tests/acceptance/test_installed_ask.py::test_imported_instructions_are_only_quoted_data`<br>`tests/services/test_evaluation_suites.py::test_injected_names_and_tags_are_only_quoted_data`<br>`tests/acceptance/test_phase_five_acceptance.py::test_imported_instructions_are_only_quoted_data` |
| Provider errors and tool-call limits | `tests/acceptance/test_installed_ask.py::test_provider_errors_and_limits_are_answer_outcomes`<br>`tests/services/test_evaluation_suites.py::test_the_adversarial_suite_expects_each_safe_outcome` |
| Provider adapters answer as the fake provider does | `tests/acceptance/test_installed_ask.py::test_each_adapter_answers_through_a_loopback_api`<br>`tests/acceptance/test_installed_evaluation.py::test_the_adapter_reaches_the_fakes_records_through_a_loopback_api` |
| MCP client answers the README questions from their prompts | `tests/acceptance/test_phase_five_acceptance.py::test_every_readme_question_is_answered_from_its_prompt`<br>`tests/acceptance/test_phase_five_acceptance.py::test_every_accepted_answer_equals_what_ask_gives`<br>`tests/acceptance/test_phase_five_acceptance.py::test_cited_evidence_is_retrieved_through_a_tool` |
| MCP server rejects unsupported claims and accepts the corrected proposal | `tests/acceptance/test_phase_five_acceptance.py::test_each_unsupported_claim_is_rejected_with_its_code`<br>`tests/acceptance/test_phase_five_acceptance.py::test_a_corrected_proposal_is_accepted_in_the_same_session` |
| MCP server reads only the selected files and never the network | `tests/acceptance/test_phase_five_acceptance.py::test_the_server_opens_only_the_files_it_was_given`<br>`tests/acceptance/test_phase_five_acceptance.py::test_the_server_exits_cleanly_and_nothing_reaches_the_network` |

`tests/acceptance/test_installed_ask.py` runs the installed `ask` command outside the checkout
through the `archdiscov` script, with the offline guard and no credentials: it answers the
README suite's cases, which include the five README example questions, with fake-provider
transcripts, answers one through each provider
adapter against a loopback mock API, and checks that a missing configuration, an unknown
snapshot, a Bedrock profile that cannot be loaded, a missing key, a provider error, a run
limit, and an existing transcript path fail safely with their
[documented exit codes](ask-command.md#exit-codes).
`tests/acceptance/test_installed_evaluation.py` evaluates the checked-in README suite and an
adversarial suite (fabricated citations, unsupported claims, malicious imported text,
provider errors, tool-call and request limits, and a repair) the same way: every case
passes with the fake provider, the README suite reaches the same records through the
Anthropic adapter against a loopback mock API, an existing report path and an external
provider without `--allow-external` are refused, and two reports are compared
([evaluation](assistant-evaluation.md)). External-provider and real-AWS tests remain
opt-in.
