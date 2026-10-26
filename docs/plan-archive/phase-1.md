# Phase 1: Models, snapshots, fixtures, and CLI foundation

Archived from [PLAN.md](../../PLAN.md). Steps 1–9 are complete; this file records
how they were planned and verified and is no longer updated.

## Step 1: Establish the package structure and development workflow

**Status:** Implemented. The `src/archdiscov` package separates models, ingestion, services, and CLI presentation. The CLI exposes help/version output through the installed `archdiscov` command and `python -m archdiscov`. Typer, packaging metadata, pytest/Ruff configuration, and development instructions are in place. Packaging tests verify imports and CLI execution from outside the repository. The existing MCP dependency is retained pending the open question below.

Set up an installable application package, a CLI entry point, and clear boundaries between models, ingestion, application services, and presentation.

Start with a small structure; add graph, analysis, and provider modules when their implementations arrive. Fix the enum import so model imports work through the installed package.

Keep Python 3.13 and uv. Add Typer now; add PyYAML when template ingestion begins. Configure pytest and Ruff, and replace the placeholder project description.

**Advice:** Verify imports and CLI execution from outside the repository directory. This catches packaging problems that running files directly can conceal.

**Open question:** The dependencies include `mcp`, but the README describes an internal tool interface and CLI. Is an MCP server intended? If so, treat it as a later interface unless it is a release requirement.

## Step 2: Add a reproducible Docker development environment

**Status:** Implemented and validated on Linux. The development image uses Python 3.13, uv 0.5.29, frozen lockfile installation, and a non-root user with configurable UID/GID. Compose mounts the checkout and a dependency cache; `/opt/venv` keeps container dependencies separate from the host environment. Image builds, Compose configuration, CLI execution, pytest, Ruff, network-isolated tests, live source visibility, writable cache/environment directories, and host file ownership checks passed. Runtime credential setup and Docker Desktop usage are documented; external credentials and other host platforms were not exercised. Devcontainer integration remains deferred.

Create the container files early so subsequent implementation can use a consistent Python and dependency environment:

- `Dockerfile` with a development target using Python 3.13, a pinned uv version, and development dependencies installed from `uv.lock` with frozen resolution.
- `compose.yaml` with a development service for an interactive shell, CLI commands, pytest, and Ruff. The CLI does not need a continuously running application server or exposed ports.
- `.dockerignore` excluding host virtual environments, Git data, IDE files, local credentials, and generated snapshots from the build context.

Bind-mount the source for immediate feedback, and keep the container virtual environment separate from the host `.venv`. Cache dependency downloads and install dependencies in a separate build layer so ordinary source edits do not trigger a complete reinstall. Run as a non-root user and arrange writable cache, environment, and output directories; account for host UID/GID where necessary to avoid root-owned files.

Document commands to build the environment, open a shell, run checks, and execute the CLI once it is available. Explain when changes to `pyproject.toml` or `uv.lock` require dependency synchronization or rebuilding. Maintain the same uv workflow for developers running directly on the host.

Plan an opt-in AWS credential setup for the collection increment: mount the required AWS configuration and credentials at runtime, read-only where supported, and select an explicit profile. Authenticate on the host for SSO workflows where practical. Pass provider credentials at runtime when provider integration arrives. Keep credentials out of images and committed configuration.

**Advice:** Start with one development image and one Compose service. Botocore Stubber and local fixtures cover the planned normal tests; add service containers only when a concrete integration needs them. A production image can be designed when deployment requirements are known.

**Acceptance check:** A clean checkout can build the environment and run the available checks; source edits appear immediately, and generated files have usable host permissions. Dependency installation may require network access, but the normal test suite must not require AWS or provider credentials or network calls.

**Open questions:**

- Which developer environments must be supported: Linux, Windows/WSL, macOS, or all three? This affects mounts and file permissions.
- Is Compose sufficient, or should a `.devcontainer` configuration also be provided for IDE integration?
- Which AWS authentication methods must work in the container: named profiles, SSO, configured assume-role access, or an external credential process? Credential processes may require additional runtime tooling.

## Step 3: Settle the snapshot contract

**Status:** Implemented. Schema `1` requires an explicit snapshot UUID, timezone-aware collection start/end, one discriminated source, and requested regional/account-wide coverage scope. Typed sources distinguish AWS observations, Terraform state, Terraform plans, CloudFormation declarations, and mock inputs; optional source export time and source-specific provenance are retained. Resource identities preserve physical IDs and logical addresses and derive stable, snapshot-independent IDs. Unsupported/missing schema versions are rejected without migration. Three representative JSON examples and contract tests cover round trips, invalid inputs, and identity stability. The README documents the contract and explicit IaC account/region requirements. Operation-level coverage and evidence remain Step 4; snapshot-wide integrity remains Step 5.

Extend `ArchitectureSnapshot` with explicit snapshot identity, timestamps, source information, and coverage. Distinguish live observations, Terraform state, Terraform plans, and CloudFormation declarations using validated fields.

Define how account-wide collection is represented and how source-specific metadata is retained. Establish handling for unsupported schema versions.

Define stable resource identity separately from snapshot identity. Preserve physical identifiers and IaC logical addresses so later comparisons have reliable matching inputs.

**Advice:** Write two or three representative JSON examples first, then refine the models against them. Do not leave fields that affect analysis hidden in generic `metadata`.

**Open questions:**

- Can one snapshot contain multiple source kinds, or should each source produce a separate snapshot? Separate snapshots are the simpler starting point and fit explicit comparison.
- How will account and region be supplied for IaC files that do not identify them?
- Should snapshot timestamps describe collection start/end, source export time, or both? Both are useful when available.

## Step 4: Complete evidence and coverage modeling together

**Status:** Implemented. Evidence requires snapshot/resource context and retains source paths; resource-relative JSON Pointer fact citations identify individual fields or rules without wrapping every field. Operation coverage distinguishes successful zero results, failures, skipped, unsupported, and incomplete collection, with validated scope, timestamps, retained counts, failures, and unresolved-value locations. Shared sanitization covers generic attributes/metadata and evidence values, with explicit redacted, omitted, and unresolved evidence states. The security-group/failed-route-table fixture and model tests demonstrate qualified evidence and missing coverage. README documents the chosen policies and adapter responsibilities; snapshot-wide reference integrity remains step 5.

The current `Evidence` model records source, locator, configuration path, and value. Add the resource and snapshot context required by the README, and decide how individual facts refer to evidence.

Resource-level evidence lists alone are too broad for conclusions such as “this rule permits this port.” Establish a way to associate a specific field or rule with its evidence without immediately wrapping every model field.

Define coverage records by account, region, service, and operation. They must distinguish successful collection with zero results from failed, skipped, unsupported, or incomplete collection. Retain timestamps, failures, and unresolved values.

**Advice:** Design this around one worked example: a security-group rule collected successfully while route-table collection fails. The resulting snapshot must support a qualified conclusion and explain the missing evidence.

**Open questions:**

- Should fact references use normalized model paths, source configuration paths, or both?
- Which sensitive fields must be excluded? Establish a shared sanitization policy before storing generic attributes or evidence values.
- Can evidence values be omitted after sanitization, and how will that limitation be recorded?

## Step 5: Strengthen resource and relationship validation

**Status:** Implemented and verified. A separate snapshot integrity pass indexes all recursive
regional/global resource trees, rejects duplicate IDs with storage locations, and enforces
snapshot account and enclosing-region scope. Names are optional without changing stable IDs;
nested subtype fields survive JSON round trips. Every evidence record belongs to this
snapshot and a stored resource; resource/fact citations resolve to owner-matched evidence,
and fact JSON Pointers resolve to existing JSON-mode values. Supplied coverage resource
references must exist locally. Dependencies require local endpoints, source-region grouping,
and source-owned evidence. The hierarchy fixture and isolated diagnostic tests cover these
rules. All 405 offline tests and Ruff lint/format and whitespace checks passed. Schema remains
`1`; external/cross-account/unresolved relationship representations are explicitly deferred.

Retain the recursive `children` model and discriminated union. Confirm that nested subtype fields survive JSON round trips.

Establish these snapshot-wide rules:

- Every resource is stored once and has a unique identity, matching the snapshot account and enclosing region.
- Attachments and other relationships use dependencies.
- Evidence references resolve within the selected snapshot and belong to the citing resource; fact pointers identify existing values.
- Dependencies are grouped by the source resource’s region, including `global`.
- Both dependency endpoints must exist locally; missing, external, and cross-account targets are rejected for now.

Keep AWS-specific placement decisions in ingestion adapters, consistent with the README’s generic containment model.

The existing `UnknownResource` uses `resource_type="unknown"` plus `aws_resource_type`. A previously unseen discriminator will therefore need explicit normalization into this representation; the union will not automatically accept it.

**Advice:** Separate ordinary field validation from a snapshot integrity pass. Reference checks become much clearer when the full snapshot is available.

**Decisions and deferred work:**

- Resource names may be absent or null; IDs remain independent of names.
- Explicit external/cross-account/unresolved dependency representations are deferred.
- Unresolved-parent placement and unsupported-type normalization belong in ingestion adapters; no parents or attachments are synthesized by snapshot validation.

## Step 6: Implement immutable snapshot persistence

**Status:** Implemented and verified. Public `validate_snapshot`, `load_snapshot`, and
`save_snapshot` services revalidate detached current data, load UTF-8 JSON, and save
sanitized normalized snapshots through synced same-directory temporary files and atomic
no-overwrite hard-link publication. Existing files, directories, and symlinks are conflicts;
publication is the commit point, with best-effort cleanup that cannot report a committed
save as failed. Typed diagnostics distinguish validation, unsupported versions, JSON,
encoding, conflicts, and filesystem failures without exposing raw input values. All five
fixtures round-trip; tests cover mutation, sanitization, concurrent writers, and injected
I/O/cleanup failures. All 462 offline tests, Ruff lint, and formatting checks (excluding
the execution-plan artifacts under `.junie`) passed. Paths select files; loaded models
remain mutable, UUID directory-wide uniqueness is not promised, and CLI ingestion remains
step 8. Parent directories must exist and local filesystems must support atomic hard links.

Define application services for loading, validating, and saving local JSON snapshots. Use atomic writes and avoid silently replacing an existing snapshot.

Make immutability an explicit storage rule. Pydantic models alone do not ensure that nested dictionaries and lists cannot change.

Handle malformed JSON, invalid model data, and unsupported schema versions with actionable errors. Preserve normalized facts and sanitized evidence across save/load operations.

**Advice:** Begin with explicit file paths. A snapshot registry, database, and migration framework can wait until actual usage requires them.

**Open question:** Should users select snapshots by path or by ID from a configured directory? Paths are sufficient for the first increment.

## Step 7: Create a small fixture set and meaningful model tests

**Status:** Implemented. The five small fixtures cover typed hierarchy, VPC-contained and
global unknown resources, source-evidenced dependencies, logical-only declarations, and
partial collection. Explicit before/after serialization assertions preserve subtype fields,
identities, citations, failure diagnostics, zero-result outcomes, and each incomplete-coverage
limitation. Rejection tests isolate missing failures and reversed timestamps and verify
schema-version diagnostics. Existing integrity and persistence coverage is retained;
unresolved relationships remain deferred. All 464 offline tests, Ruff lint/format checks
(excluding `.junie` artifacts), and whitespace checks passed. Production models and services
are unchanged.

Build fixtures for:

- A VPC → subnet → EC2 hierarchy.
- An account-wide resource and a VPC resource represented through `UnknownResource`.
- A dependency backed by evidence.
- Partial coverage; an unresolved-relationship fixture is deferred until an explicit representation is designed.
- A declared resource with a logical address but no physical identifier.

Test nested serialization, invalid resource discriminators, duplicate identities, evidence integrity, unresolved-reference handling, and schema-version rejection.

**Advice:** Keep fixtures small and understandable by inspection. Each should demonstrate a contract or limitation. These fixtures will become inputs for later analyzer and provider tests.

## Step 8: Deliver the first usable CLI workflow

**Status:** Implemented and verified. Thin Typer `ingest INPUT --output PATH` and
`evidence ID --snapshot PATH` commands use reusable persistence and evidence-inspection
services. Both support readable output and `--json`, including safe structured service
errors and documented exit codes. Ingestion preserves normalized snapshot data and refuses
existing destinations; inspection retains owner/subtype context, provenance, fact citations,
sanitized values, redactions, unavailable-value limitations, and same-region/service coverage
outcomes without claiming completeness. Tests cover all fixture imports, both installed entry
points outside the repository, malformed inputs, missing evidence, sanitization, limitations,
and no-overwrite behavior. All 525 offline tests, Ruff lint/format checks (excluding `.junie`),
and whitespace checks passed. Raw AWS/IaC adapters remain later work; Docker acceptance and
the remaining increment-closing contracts remain step 9. CLI usage errors retain Typer's
readable diagnostics even when `--json` is supplied.

Implement `ingest` for the project’s own snapshot JSON and `evidence` for retrieving a cited record. Support readable output and JSON output, with consistent exit codes.

Keep Typer commands thin: argument parsing and output formatting should call reusable application services.

Evidence output should show the selected snapshot, associated resource, source operation/file, configuration path, sanitized value, and relevant limitations.

**Advice:** Complete this workflow before adding more commands. It gives you an immediately reviewable result without AWS credentials or an LLM.

**Open question:** Should failed validation produce structured JSON errors when JSON output is requested? The recommended default is yes.

## Step 9: Close the increment with acceptance checks and documentation

**Status:** Implemented and verified. Minimal snapshot-scoped `Finding` and `Answer`
models define evidence/finding references, limitations, and `supported`, `partial`, and
`insufficient_evidence` statuses without analyzer-specific fields or provider-authored
conclusions. Shape/status validation is implemented; analyzer semantics and snapshot/tool
citation validation remain later increments. Installed-CLI acceptance tests run outside
the checkout without credential variables, preserve typed children and partial coverage,
inspect evidence, reject invalid references with safe diagnostics, and prove no overwrite.
Locked uv installation and the Docker development image build passed; all 554 tests
passed on the host and in Docker with networking disabled, and Compose acceptance passed.
Ruff lint/format checks (excluding `.junie` artifacts), documentation examples, and whitespace
checks passed. README documents installation, snapshot/fixture/API contracts, commands,
and reproducible host/container acceptance. The first offline increment is complete;
raw ingestion adapters, analyzers, and providers remain future work. Host verification
uses the local `.venv/bin/python`; Docker retains its separate `/opt/venv` environment.

Demonstrate that a user can install the project, import a fixture, inspect its evidence, and receive clear errors for invalid references. Verify the workflow both directly with uv and through the Docker development environment. Confirm that saved snapshots preserve typed children and coverage information.

Document installation, the snapshot contract, fixture usage, and supported commands. Define minimal `Finding` and `Answer` contracts now, particularly their evidence references, limitations, and statuses; defer analyzer-specific details.

**Completion criterion:** The offline workflow works, normal tests require no credentials or network access, and the snapshot/evidence contracts are stable enough for ingestion adapters to target.
