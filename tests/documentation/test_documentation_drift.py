"""Keep the published analyzers, limits, and commands aligned with the executable definitions."""

import re
from pathlib import Path

import pytest
import typer.main
from annotated_types import Ge, Le

from archdiscov.cli import app
from archdiscov.models.analysis.parameters import AnalyzerParameters
from archdiscov.models.comparison.results import DEFAULT_RESULT_LIMIT, MAX_RESULT_LIMIT
from archdiscov.models.enums import AnalyzerKind
from archdiscov.models.query.parameters import QueryPathLimits
from archdiscov.models.query.requests import QueryNeighboursRequest, QuerySearchRequest
from archdiscov.services import DEFAULT_REGISTRY
from tests.documentation.markdown import DOC_FILES, code_blocks, table_rows
from tests.paths import PROJECT_ROOT

DOCS = PROJECT_ROOT / "docs"
README = PROJECT_ROOT / "README.md"
ANALYZERS = DOCS / "analyzers.md"
PARAMETER_MODELS = {
    model.model_fields["kind"].default: model
    for model in AnalyzerParameters.__args__[0].__args__  # the discriminated union members
}


def number(text: str) -> int:
    return int(text.replace(",", ""))


def bounds(field) -> tuple[int | None, int | None]:
    low = next((m.ge for m in field.metadata if isinstance(m, Ge)), None)
    high = next((m.le for m in field.metadata if isinstance(m, Le)), None)
    return low, high


def range_of(text: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"([\d,]+)–([\d,]+)", text)
    return (number(match.group(1)), number(match.group(2))) if match else None


# ---- analyzers ---------------------------------------------------------------------


def test_every_analyzer_kind_is_registered_and_documented_once():
    rows = table_rows(ANALYZERS, "| Analyzer (`kind`)")
    documented = [row[0].strip("`") for row in rows]
    assert sorted(documented) == sorted(kind.value for kind in AnalyzerKind)
    assert set(PARAMETER_MODELS) == set(AnalyzerKind)
    for kind in AnalyzerKind:
        assert DEFAULT_REGISTRY.get(kind).kind == kind


def test_documented_parameters_match_the_parameter_models():
    rows = table_rows(ANALYZERS, "| Analyzer | Parameter")
    documented: dict[str, dict[str, list[str]]] = {}
    for kind, parameter, option, default, allowed in (row[:5] for row in rows):
        documented.setdefault(kind.strip("`"), {})[parameter.strip("`")] = [
            option,
            default,
            allowed,
        ]
    assert set(documented) == {kind.value for kind in AnalyzerKind}
    for kind, model in PARAMETER_MODELS.items():
        fields = {name: field for name, field in model.model_fields.items() if name != "kind"}
        assert set(documented[kind.value]) == set(fields), kind
        for name, field in fields.items():
            option, default, allowed = documented[kind.value][name]
            flag = "--" + name.replace("_", "-")
            if name == "ownership_keys":
                flag = "--ownership-key"
            assert flag in option, (kind, name)
            if field.is_required():
                assert default == "required", (kind, name)
            elif name == "ownership_keys":
                assert default == ", ".join(f"`{key}`" for key in field.default_factory()), kind
            else:
                assert number(default) == field.default, (kind, name)
            if (low_high := range_of(allowed)) is not None:
                assert low_high == bounds(field), (kind, name)


def test_analyzer_options_are_the_analyze_command_options():
    analyze = typer.main.get_command(app).commands["analyze"]
    options = {
        opt
        for param in analyze.params
        for opt in getattr(param, "opts", [])
        if opt.startswith("--")
    }
    parameter_options = {
        "--ownership-key" if name == "ownership_keys" else "--" + name.replace("_", "-")
        for model in PARAMETER_MODELS.values()
        for name in model.model_fields
        if name != "kind"
    }
    assert parameter_options <= options
    text = ANALYZERS.read_text(encoding="utf-8")
    for option in options - {"--json"}:
        assert option in text, option


# ---- limits ------------------------------------------------------------------------


def test_documented_query_limits_match_the_query_models():
    rows = table_rows(DOCS / "graph-queries.md", "| Query | Limit")
    path_limits = QueryPathLimits.model_fields
    expected = {
        "--limit": [
            (QuerySearchRequest.model_fields["limit_result"]),
            (QueryNeighboursRequest.model_fields["limit_result"]),
            (path_limits["limit_result"]),
        ],
        "--depth": [path_limits["depth"]],
        "--work-limit": [path_limits["work_limit"]],
    }
    seen = {option: 0 for option in expected}
    for _query, _limit, option, default, allowed in (row[:5] for row in rows):
        option = option.strip("`")
        field = expected[option][seen[option]]
        seen[option] += 1
        assert number(default) == field.default
        assert range_of(allowed) == bounds(field)
    assert seen == {option: len(fields) for option, fields in expected.items()}


def test_documented_comparison_limit_matches_the_model():
    text = (DOCS / "comparison-results.md").read_text(encoding="utf-8")
    assert f"default {DEFAULT_RESULT_LIMIT:,}".replace(",", "") in text.replace(",", "")
    assert f"1 to {MAX_RESULT_LIMIT}" in text.replace(",", "")
    rows = table_rows(DOCS / "result-contracts.md", "| Limit")
    comparison = next(row for row in rows if "comparison" in row[1])
    assert number(comparison[2]) == DEFAULT_RESULT_LIMIT
    assert range_of(comparison[3]) == (1, MAX_RESULT_LIMIT)
    analysis = next(row for row in rows if "analyzer" in row[1])
    assert (
        number(analysis[2])
        == PARAMETER_MODELS[AnalyzerKind.CROSS_ACCOUNT_TRUST].model_fields["result_limit"].default
    )


# ---- commands ----------------------------------------------------------------------


def command_options():
    """Long options of every command, keyed by its words (`analyze`, `query paths`, ...)."""
    commands = {}
    for name, command in typer.main.get_command(app).commands.items():
        subcommands = getattr(command, "commands", None)
        for words, leaf in (
            [(name, command)]
            if subcommands is None
            else [(f"{name} {sub}", item) for sub, item in subcommands.items()]
        ):
            commands[words] = {
                opt for p in leaf.params for opt in getattr(p, "opts", []) if opt.startswith("--")
            }
    return commands


def documented_invocations(path: Path):
    """(command words, long options) of each `archdiscov ...` usage in fenced blocks."""
    names = command_options()
    for block in code_blocks(path):
        invocation = None
        for line in block.splitlines() + [""]:
            if invocation is not None and (line.startswith((" ", "\t")) and line.strip()):
                invocation += " " + line.strip()
                continue
            if invocation is not None:
                yield from _parse(invocation, names)
                invocation = None
            if line.startswith("archdiscov "):
                invocation = line.rstrip("\\").strip()


def _parse(invocation: str, names):
    words = invocation.split()[1:]
    for count in (2, 1):
        key = " ".join(words[:count])
        if key in names:
            yield key, set(re.findall(r"--[a-z][a-z-]*", invocation))
            return


@pytest.mark.parametrize("document", DOC_FILES, ids=lambda path: path.name)
def test_documented_command_options_exist(document):
    names = command_options()
    for command, options in documented_invocations(document):
        assert options <= names[command] | {"--help"}, (
            document.name,
            command,
            options - names[command],
        )


def test_every_analysis_query_and_comparison_option_is_documented():
    names = command_options()
    documented = "\n".join(
        block for path in DOC_FILES for block in code_blocks(path) if "archdiscov" in block
    )
    for command in (
        "analyze",
        "findings",
        "compare",
        "comparison",
        "query search",
        "query inspect",
        "query neighbours",
        "query paths",
    ):
        for option in names[command] - {"--json"}:
            assert option in documented, (command, option)


# ---- status text -------------------------------------------------------------------


def test_the_readme_describes_every_analyzer_and_links_the_reference():
    text = README.read_text(encoding="utf-8")
    assert "docs/analyzers.md" in text and "docs/result-contracts.md" in text
    for kind in AnalyzerKind:
        assert kind.value in text or kind.value.replace("_", "-") in text, kind


def test_implemented_commands_are_not_listed_as_planned():
    text = README.read_text(encoding="utf-8")
    assert "#### Planned command extensions" not in text
    for stale in (
        "other analyzers, comparisons, and provider interfaces remain planned",
        "comparisons and the assistant remain later steps",
        "Deterministic findings (planned)",
        "no analyzer or CLI uses them yet",
        "other analyzers and comparisons remain planned",
    ):
        assert stale not in text, stale
    for path in DOC_FILES:
        assert "No other analyzer is implemented" not in path.read_text(encoding="utf-8")
