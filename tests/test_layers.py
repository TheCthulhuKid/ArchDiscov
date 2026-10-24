"""Imports go one way between layers, so the lower ones stay usable without the upper ones.

`core` imports no other package of archdiscov, `models` only `core`, and `graph` only
`models` and `core` (see `docs/project-layout.md`).
"""

import ast

import pytest

from tests.paths import PROJECT_ROOT

PACKAGE = PROJECT_ROOT / "src" / "archdiscov"
ALLOWED = {
    "core": set(),
    "models": {"core"},
    "graph": {"models", "core"},
}


def _imported_layers(layer: str) -> dict[str, set[str]]:
    """Each module of the layer, and the other archdiscov layers it imports."""
    found = {}
    for path in (PACKAGE / layer).rglob("*.py"):
        parts = path.relative_to(PACKAGE).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        layers = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.level:
                base = list(parts if path.name == "__init__.py" else parts[:-1])
                base = base[: len(base) - (node.level - 1)]
                target = [*base, *(node.module.split(".") if node.module else [])]
            elif isinstance(node, ast.ImportFrom) and node.module:
                target = node.module.split(".")[1:] if node.module.startswith("archdiscov") else []
            elif isinstance(node, ast.Import):
                target = [
                    name
                    for alias in node.names
                    if alias.name.startswith("archdiscov.")
                    for name in alias.name.split(".")[1:2]
                ]
            else:
                continue
            if target and target[0] != layer:
                layers.add(target[0].removesuffix(".py"))
        found[".".join(parts)] = layers
    return found


@pytest.mark.parametrize("layer", list(ALLOWED))
def test_a_lower_layer_imports_only_the_layers_below_it(layer: str) -> None:
    violations = {
        module: sorted(layers - ALLOWED[layer])
        for module, layers in _imported_layers(layer).items()
        if layers - ALLOWED[layer]
    }
    assert violations == {}
