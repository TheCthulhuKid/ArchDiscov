import importlib
import subprocess
import sys

import archdiscov.services as services
from tests.paths import PROJECT_ROOT


def test_every_export_resolves_from_its_module() -> None:
    assert sorted(services.__all__) == sorted(services._EXPORTS)
    for name, module in services._EXPORTS.items():
        path = module if "." in module else f"archdiscov.services.{module}"
        expected = getattr(importlib.import_module(path), name)
        assert getattr(services, name) is expected, name
    assert set(services.__all__) <= set(dir(services))


def test_unknown_names_raise_attribute_error() -> None:
    try:
        services.not_a_service  # noqa: B018
    except AttributeError as exc:
        assert "not_a_service" in str(exc)
    else:
        raise AssertionError("expected AttributeError")


def test_query_and_analysis_modules_do_not_load_the_aws_sdk() -> None:
    script = (
        "import sys\n"
        "import archdiscov.services.queries, archdiscov.services.neighbours\n"
        "import archdiscov.services.paths, archdiscov.services.evidence\n"
        "import archdiscov.services.analysis_execution, archdiscov.services.analyses\n"
        "import archdiscov.services.comparison_results, archdiscov.services.snapshots\n"
        "print(','.join(m for m in ('boto3', 'botocore') if m in sys.modules))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True, cwd=PROJECT_ROOT
    )
    assert completed.stdout.strip() == ""


def test_collection_still_loads_the_aws_sdk_on_use() -> None:
    script = (
        "import sys\n"
        "from archdiscov.services import collect_snapshots\n"
        "print('botocore' in sys.modules)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True, cwd=PROJECT_ROOT
    )
    assert completed.stdout.strip() == "True"
