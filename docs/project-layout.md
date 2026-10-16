# Project layout

Group files by responsibility rather than letting every new contract or test add
another entry at the directory root. This is an organization change: snapshot wire
formats, stable IDs, CLI behavior, and test coverage are unchanged.

## Models

| Location under `src/archdiscov/models/` | Responsibility |
| --- | --- |
| `analysis/` | Analysis parameters, references, and result contracts |
| `cloud/` | Cloud scope, identity, context, namespaces, and schema-3 contracts |
| `query/` | Query parameters, requests, edges, limitations, and results |
| `resources/` | AWS service-specific and Azure resource/value models, including unknown resources |
| Root modules | Shared base/enums, legacy architecture, coverage keys/outcomes, integrity, sanitization, and small utilities |

Use focused imports, not eager package-wide re-exports. In particular, keep package
`__init__.py` files lightweight so recursive resource models do not acquire import
cycles. Existing central imports such as `models.architecture`, `models.enums`, and
`models.resource_base` remain in place.

Grouped modules have new Python import paths; old flat module paths are not aliases:

```python
from archdiscov.models.analysis.results import AnalysisResult
from archdiscov.models.cloud.snapshot import CloudSnapshot
from archdiscov.models.query.requests import QuerySearchRequest
from archdiscov.models.resources.network import SecurityGroup
```

New contracts belong with their domain. Split a module only when it has a distinct
responsibility; avoid a subpackage for each class or a single catch-all model module.

## Tests

| Location | Responsibility |
| --- | --- |
| `tests/models/` | Shared snapshot, cloud, resource, and primitive model contracts |
| `tests/analysis/` | Analysis contracts, integrity, and diagnostics |
| `tests/query/` | Query contracts and, later, query services |
| `tests/graph/` | Graph indexes, composed acceptance, and profiling regressions |
| `tests/ingestion/aws/` | Shared AWS execution/orchestration and service-specific collector subpackages |
| `tests/ingestion/terraform/` | State/plan parsing, mappings, and imports |
| `tests/ingestion/cloudformation/` | Template parsing, evaluation, mappings, and imports |
| `tests/services/` | Persistence, evidence, import coordination, and shared validation |
| `tests/cli/` | Command parsing and presentation |
| `tests/acceptance/` | Installed-package and end-to-end offline journeys |
| `tests/support/` | Reusable fake clients and API-data builders, not test cases |
| `tests/fixtures/` | Committed snapshot/source/result input files; unchanged locations |

Tests use `tests.paths.FIXTURES_ROOT` and `PROJECT_ROOT` instead of assuming how
many directories separate a test from its inputs. Python package markers retain
unambiguous imports and pytest discovery. Existing cross-test helper imports remain
supported; put new reusable helpers under `tests/support/`.

Run the entire suite as before, or select a directory for focused work:

```bash
.venv/bin/python -m pytest -n 4 --dist worksteal
.venv/bin/python -m pytest tests/query -q
.venv/bin/python -m pytest tests/graph tests/services -q
.venv/bin/python -m pytest tests/ingestion/aws/iam -q
```

No tests were removed or weakened for this reorganization. CI continues to discover
the complete suite recursively; there is no need to enumerate each directory.