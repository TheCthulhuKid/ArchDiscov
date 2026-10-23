"""Every third-party package the source imports is a declared dependency.

A package that arrives only through another dependency can disappear when that dependency
changes, without any resolver warning, so a direct import needs a direct declaration.
"""

import ast
import re
import sys
import tomllib
from importlib.metadata import packages_distributions

from tests.paths import PROJECT_ROOT

SOURCE = PROJECT_ROOT / "src" / "archdiscov"


def _imported_top_levels() -> set[str]:
    names = set()
    for path in SOURCE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                names.add(node.module.split(".")[0])
    return {name for name in names if name not in sys.stdlib_module_names} - {"archdiscov"}


def _normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def test_every_imported_package_is_declared() -> None:
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = {
        _normalized(re.split(r"[<>=!~;\[ ]", requirement, maxsplit=1)[0])
        for requirement in project["project"]["dependencies"]
    }
    providers = packages_distributions()
    undeclared = {
        name: providers.get(name, [name])
        for name in _imported_top_levels()
        if not {_normalized(item) for item in providers.get(name, [name])} & declared
    }
    assert undeclared == {}
