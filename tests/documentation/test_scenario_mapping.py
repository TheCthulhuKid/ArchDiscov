"""The acceptance page maps each scenario, provider ones included, to tests that exist."""

import ast
import re

import pytest

from tests.paths import PROJECT_ROOT
from tests.support.markdown import table_rows

ACCEPTANCE = PROJECT_ROOT / "docs" / "acceptance.md"
SCENARIOS = [
    "Public load balancer with a private backend",
    "Public IP with blocked ingress",
    "Missing routing evidence",
    "Database access",
    "Direct and indirect security-group dependencies",
    "Explicit and conditional cross-account trust",
    "Missing ownership tags",
    "Terraform module addresses",
    "Terraform planned deletions",
    "Terraform unknown values",
    "CloudFormation unresolved references",
    "Safe matching of observed and declared resources",
    "README example questions",
    "Coverage limitations",
    "Fabricated and unreturned citations",
    "Unsupported claims",
    "Malicious imported text",
    "Provider errors and tool-call limits",
    "Provider adapters",
    "MCP client answers the README questions",
    "MCP server rejects unsupported claims",
    "MCP server reads only the selected files",
]


def rows():
    return {
        row[0]: re.findall(r"`([^`]+::[^`]+)`", row[1])
        for row in table_rows(ACCEPTANCE, "| Scenario")
    }


def resolve(reference: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """The test function a `path::[Class::]name` reference names, or None if it is gone."""
    path, *scope, name = reference.split("::")
    file = PROJECT_ROOT / path
    if not file.is_file():
        return None
    body = ast.parse(file.read_text(encoding="utf-8")).body
    for class_name in scope:
        body = next(
            (n.body for n in body if isinstance(n, ast.ClassDef) and n.name == class_name), []
        )
    return next(
        (
            n
            for n in body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
        ),
        None,
    )


def test_every_scenario_is_listed_with_tests():
    mapping = rows()
    for scenario in SCENARIOS:
        [tests] = [tests for name, tests in mapping.items() if name.startswith(scenario)]
        assert tests, scenario
    assert len(mapping) == len(SCENARIOS)


@pytest.mark.parametrize(
    "reference",
    [test for tests in rows().values() for test in tests],
    ids=lambda ref: ref.split("/")[-1],
)
def test_each_listed_test_exists_and_asserts_an_outcome(reference):
    function = resolve(reference)
    assert function is not None, f"{reference} no longer exists"
    assert function.name.startswith("test_")
    assert any(isinstance(n, ast.Assert) for n in ast.walk(function))


def test_a_stale_reference_is_detected():
    assert resolve("tests/comparison/test_matching.py::test_names_and_short_ids_never_match")
    assert resolve("tests/comparison/test_matching.py::test_renamed_away") is None
    assert resolve("tests/comparison/test_missing_file.py::test_names") is None
    assert resolve("tests/analysis/test_ownership_analyzer.py::Missing::test_custom_keys") is None
