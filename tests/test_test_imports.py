"""Test modules share helpers through `tests/support`, never by importing another test module.

A test module's helpers are private to it: importing them makes moving or renaming one break
unrelated tests.
"""

import ast
from pathlib import Path

from tests.paths import PROJECT_ROOT

TESTS = PROJECT_ROOT / "tests"
# Modules that import test modules to check how they are marked, not to use their helpers.
INSPECTING = {"assistant/test_live_opt_in.py"}


def _imported_modules(path: Path) -> list[str]:
    """Every module `path` imports, relative imports resolved, including imported submodules."""
    package = list(path.relative_to(PROJECT_ROOT).with_suffix("").parts[:-1])
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = package[: len(package) - node.level + 1] if node.level else []
            module = ".".join([*base, *([node.module] if node.module else [])])
            found += [module, *(f"{module}.{alias.name}" for alias in node.names)]
    return found


def test_no_test_module_imports_another() -> None:
    imports = {
        path.relative_to(TESTS).as_posix(): sorted(
            module
            for module in _imported_modules(path)
            if module.startswith("tests.") and module.rsplit(".", 1)[-1].startswith("test_")
        )
        for path in TESTS.rglob("*.py")
        if path.relative_to(TESTS).as_posix() not in INSPECTING
    }
    assert {path: modules for path, modules in imports.items() if modules} == {}
