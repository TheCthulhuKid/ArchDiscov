# Development

How to work on Archdiscov: continuous integration, the source layout, the Docker development
environment, and runtime credentials. Installation and the everyday checks are in the
[README](../README.md#installation); placement rules are in the [project layout](project-layout.md).

## Continuous integration

[GitHub Actions](../.github/workflows/ci.yml) runs the offline unit and integration tests, the
offline acceptance journeys (`tests/acceptance`, including the installed-CLI journey), and
Ruff lint/format checks in separate jobs on every push and pull request, with manual
runs available too. CI uses Python 3.13, uv 0.5.29, and locked development dependencies;
the test jobs use eight pytest-xdist workers (`--dist worksteal`, and `--dist loadgroup` for
acceptance so each module-scoped journey, such as the installed `ask` and MCP journeys, stays
on one worker) to limit nested concurrency. The unit-test job reports line coverage of `archdiscov` without gating on it,
and the quality job audits the locked dependencies for known vulnerabilities with
`pip-audit`. It requires no AWS/provider secrets. Superseded runs are cancelled automatically.

The source package separates shared models, ingestion adapters, public application services, and CLI presentation:

```text
src/archdiscov/
├── models/
│   ├── analysis/ # Finding, parameter, and reference contracts
│   ├── cloud/    # Shared scope, identity, and schema-3 snapshot contracts
│   ├── query/    # Query requests, results, edges, and limitations
│   └── resources/ # Provider/service-specific resource models
├── graph/        # Read-only resource, edge, evidence, and coverage indexes
├── ingestion/    # Allowlisted AWS collectors and offline Terraform/CloudFormation adapters
├── services/     # Sync/async collection, persistence, and evidence inspection
├── cli.py        # Typer presentation and entry point
└── __main__.py   # python -m archdiscov
```

Model imports use the installed package, for example `from archdiscov.models.architecture import ArchitectureSnapshot` and `from archdiscov.models.enums import AwsRegion`. CLI commands call application services; discovery and analysis logic belong outside the CLI.

Tests are grouped by component under `tests/models`, `tests/analysis`, `tests/query`,
`tests/graph`, `tests/ingestion`, `tests/services`, `tests/cli`, and `tests/acceptance`.
Shared fakes/data builders live in `tests/support`; committed input files remain in
`tests/fixtures`. See [the layout guide](project-layout.md) for placement rules,
updated model import paths, and focused test commands.

Packaging tests execute both CLI entry points and model imports from a temporary directory outside the repository.

## Docker development

Install Docker with the Compose plugin and BuildKit support. On Linux/WSL, match the container user's IDs to your host user before building:

```bash
export DEV_UID=$(id -u)
export DEV_GID=$(id -g)
```

Alternatively, copy `.env.example` to `.env` and set those values there. Use non-root IDs. Docker Desktop on macOS/Windows can generally use the defaults; on Windows, run these examples from a WSL shell with Docker Desktop integration enabled. Keeping the checkout in the WSL filesystem usually improves bind-mount performance. A devcontainer configuration is deferred.

Build the development image and start an interactive shell:

```bash
docker compose build dev
docker compose run --rm dev
```

Run commands without opening a shell:

```bash
docker compose run --rm dev archdiscov --help
docker compose run --rm dev archdiscov --version
docker compose run --rm dev python -m pytest
docker compose run --rm dev ruff check .
docker compose run --rm dev ruff format --check .
```

The image uses Python 3.13 and uv 0.5.29. Dependencies, including development tools, are installed from `uv.lock` with frozen resolution. The checkout is mounted at `/workspace`; the editable package reflects source edits immediately. The container uses `/opt/venv`, independent of the host `.venv`, and its `python` resolves to that environment. Source commands and generated snapshots run as the configured non-root user, so files written under `/workspace` remain accessible on the host.

BuildKit caches dependency downloads and the dependency-installation layer. Compose also retains a named uv cache for commands run inside the container. Rebuild after changing `pyproject.toml`, `uv.lock`, the Dockerfile, or user IDs. Run `uv lock` on the host when intentionally changing dependencies, then rebuild so the image matches the committed lockfile. For a temporary container-side dependency refresh, run `uv sync --locked --group dev` inside the development shell; rebuild afterward to make the change available to subsequent containers.

Dependency synchronization can require network access to rebuild the editable package: the runtime cache is separate from BuildKit's build cache. Normal CLI and test commands use the installed environment directly. If changing user IDs makes an existing cache volume unwritable, remove the development cache with `docker compose down --volumes` before rebuilding; this removes the named cache, while the bind-mounted checkout remains on the host.

Normal tests require no network calls. To enforce network isolation when checking the built image, use Docker directly with the same source mount:

```bash
docker run --rm --network none --mount "type=bind,src=$PWD,dst=/workspace" archdiscov-dev python -m pytest
```

## Optional runtime credentials

The default development service supplies no AWS or provider credentials. For live AWS collection, mount your AWS configuration explicitly and choose a profile:

```bash
docker compose run --rm \
  --volume "$HOME/.aws:/home/dev/.aws:ro" \
  --env AWS_PROFILE=development \
  dev bash
```

For SSO, authenticate on the host first with `aws sso login --profile development`. The read-only mount exposes the host configuration and cached tokens; refresh them on the host when needed. Configured assume-role profiles can use the mounted source profiles. Profiles using `credential_process` need the referenced executable and any supporting files inside the container; host-only executables do not automatically become available. Use `archdiscov collect` for the seven supported service families described in
[AWS collection](aws-collection.md).

For LLM providers (phase 4, separate from cloud platforms), forward a variable already set in your host environment, for example `docker compose run --rm --env ANTHROPIC_API_KEY dev bash` (or `--env OPENAI_API_KEY`), and name it in the provider configuration's `api_key_env`; `ask` reads the key only from that variable. Credentials belong in runtime configuration and are excluded from the image build context. Keep snapshots in the ignored `snapshots/` directory; committed test fixtures belong under `tests/`.
