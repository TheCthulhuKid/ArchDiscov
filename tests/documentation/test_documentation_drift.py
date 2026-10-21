"""Keep the published analyzers, limits, and commands aligned with the executable definitions."""

import re
import typing
from pathlib import Path

import pytest
import typer.main
from annotated_types import Ge, Gt, Le

from archdiscov.assistant import answer_validation
from archdiscov.assistant import answers as assistant_answers
from archdiscov.assistant import config as assistant_config
from archdiscov.assistant import conversation as assistant_conversation
from archdiscov.assistant import fake as assistant_fake
from archdiscov.assistant import instructions as assistant_instructions
from archdiscov.assistant import runs as assistant_runs
from archdiscov.assistant.provider import (
    ProviderError,
    ProviderErrorCode,
    RequestLimits,
    UnsupportedCapabilityError,
)
from archdiscov.assistant.tools import contracts as tool_contracts
from archdiscov.assistant.tools.catalog import default_registry
from archdiscov.cli import app
from archdiscov.models.analysis.parameters import AnalyzerParameters
from archdiscov.models.comparison.results import DEFAULT_RESULT_LIMIT, MAX_RESULT_LIMIT
from archdiscov.models.enums import AnalysisStatus, AnalyzerKind
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


# ---- assistant providers -----------------------------------------------------------

PROVIDERS_DOC = DOCS / "assistant-providers.md"
CONFIG_MODELS = {
    model.model_fields["kind"].default.value: model
    for model in assistant_config.ProviderConfig.__origin__.__args__
}


def decimal(text: str) -> float:
    return float(text.replace(",", ""))


def documented_range(text: str) -> tuple[bool, float, float] | None:
    """(exclusive lower bound, low, high) of `1–50` or `>0–60`, ignoring trailing notes."""
    match = re.match(r"(>?)([\d.,]+)–([\d.,]+)", text)
    if match is None:
        return None
    return bool(match.group(1)), decimal(match.group(2)), decimal(match.group(3))


def field_range(field) -> tuple[bool, float, float]:
    metadata = field.metadata
    gt = next((m.gt for m in metadata if isinstance(m, Gt)), None)
    ge = next((m.ge for m in metadata if isinstance(m, Ge)), None)
    le = next(m.le for m in metadata if isinstance(m, Le))
    return gt is not None, float(gt if gt is not None else ge), float(le)


def check_default(documented: str, field, where) -> None:
    if field.is_required():
        assert documented == "required", where
    elif field.default is None:
        assert documented == "none", where
    elif isinstance(field.default, (int, float)):
        assert decimal(documented) == field.default, where
    else:
        assert documented == f"`{field.default}`", where


def test_documented_provider_configuration_matches_the_models():
    rows = table_rows(PROVIDERS_DOC, "| Field | Kinds")
    covered = set()
    for name, kinds, default, allowed in (row[:4] for row in rows):
        name = name.strip("`")
        selected = (
            list(CONFIG_MODELS)
            if kinds == "all"
            else [kind.strip().strip("`") for kind in kinds.split(",")]
        )
        for kind in selected:
            assert (kind, name) not in covered, (kind, name)
            covered.add((kind, name))
            field = CONFIG_MODELS[kind].model_fields[name]
            if name == "kind":
                continue
            check_default(default, field, (kind, name))
            if (documented := documented_range(allowed)) is not None:
                assert documented == field_range(field), (kind, name)
    expected = {
        (kind, name) for kind, model in CONFIG_MODELS.items() for name in model.model_fields
    }
    assert covered == expected
    kind_row = next(row for row in rows if row[0] == "`kind`")
    assert kind_row[3] == ", ".join(f"`{kind}`" for kind in assistant_config.ProviderKind)
    assert set(CONFIG_MODELS) == {kind.value for kind in assistant_config.ProviderKind}


def test_documented_request_limits_match_the_model():
    rows = table_rows(PROVIDERS_DOC, "| Request limit")
    assert [row[0].strip("`") for row in rows] == list(RequestLimits.model_fields)
    for name, default, allowed in (row[:3] for row in rows):
        field = RequestLimits.model_fields[name.strip("`")]
        check_default(default, field, name)
        assert documented_range(allowed) == field_range(field), name


def test_documented_provider_errors_match_the_hierarchy():
    rows = table_rows(PROVIDERS_DOC, "| Code | Error")
    errors = {
        error.code: error
        for error in [UnsupportedCapabilityError, *ProviderError.__subclasses__()]
        if "code" in vars(error)
    }
    assert [row[0].strip("`") for row in rows] == [code.value for code in ProviderErrorCode]
    for code, name, retryable, message in (row[:4] for row in rows):
        error = errors[ProviderErrorCode(code.strip("`"))]
        assert name.strip("`") == error.__name__
        assert retryable == ("yes" if error.retryable else "no"), code
        assert message == error.message, code


def test_documented_bounds_match_the_constants():
    rows = table_rows(PROVIDERS_DOC, "| Bound | Value")
    assert {row[0].strip("`") for row in rows} == {
        name for name in vars(assistant_conversation) if name.startswith("MAX_")
    }
    for name, value in (row[:2] for row in rows):
        assert number(value) == getattr(assistant_conversation, name.strip("`")), name
    text = PROVIDERS_DOC.read_text(encoding="utf-8")
    assert assistant_config.MAX_CONFIG_BYTES == 64 * 1024 and "64 KiB" in text
    assert assistant_fake.MAX_TRANSCRIPT_BYTES == 1024 * 1024 and "1 MiB" in text
    for code in assistant_config.ProviderConfigErrorCode:
        assert f"`{code.value}`" in text, code


def test_the_readme_links_the_provider_contracts_without_claiming_real_providers():
    text = README.read_text(encoding="utf-8")
    assert "docs/assistant-providers.md" in text
    for claim in ("Anthropic adapter is implemented", "OpenAI-compatible adapter is implemented"):
        assert claim not in text
    provider_doc = PROVIDERS_DOC.read_text(encoding="utf-8")
    assert "the fake is the only provider" in provider_doc


# ---- assistant tools ---------------------------------------------------------------

TOOLS_DOC = DOCS / "assistant-tools.md"


def test_documented_tools_match_the_registry():
    rows = table_rows(TOOLS_DOC, "| Tool | Inputs")
    declarations = default_registry().declarations()
    assert [row[0].strip("`") for row in rows] == [item.name for item in declarations]
    for (name, inputs, _returns), declaration in zip(
        (row[:3] for row in rows), declarations, strict=True
    ):
        schema = declaration.input_schema
        expected = [
            f"**`{field}`**" if field in schema.get("required", []) else f"`{field}`"
            for field in schema.get("properties", {})
        ]
        assert inputs == (", ".join(expected) or "none"), name


def test_documented_tool_limits_match_the_model():
    rows = table_rows(TOOLS_DOC, "| Limit | Default")
    fields = tool_contracts.ToolLimits.model_fields
    assert [row[0].strip("`") for row in rows] == list(fields)
    for name, default, allowed in (row[:3] for row in rows):
        field = fields[name.strip("`")]
        check_default(default, field, name)
        assert documented_range(allowed) == field_range(field), name


def test_documented_truncation_reasons_and_errors_match_the_code():
    reasons = table_rows(TOOLS_DOC, "| Truncation reason")
    assert [row[0].strip("`") for row in reasons] == [
        reason.value for reason in tool_contracts.TruncationReason
    ]
    errors = table_rows(TOOLS_DOC, "| Code | Message")
    assert {row[0].strip("`"): row[1] for row in errors} == {
        code.value: message for code, message in tool_contracts.TOOL_ERROR_MESSAGES.items()
    }
    assert [row[0].strip("`") for row in errors] == [
        code.value for code in tool_contracts.ToolErrorCode
    ]


def test_documented_untrusted_text_and_run_record_rules_match_the_code():
    text = TOOLS_DOC.read_text(encoding="utf-8")
    assert f"truncated to\n{tool_contracts.MAX_UNTRUSTED_TEXT_CHARS} characters" in text
    fields = ", ".join(f"`{name}`" for name in tool_contracts.ID_FIELDS)
    assert fields.replace(", `resource_ids`", ", and `resource_ids`") in " ".join(text.split())


def test_the_readme_links_the_tools_without_claiming_orchestration():
    text = README.read_text(encoding="utf-8")
    assert "docs/assistant-tools.md" in text
    for claim in ("orchestration is implemented", "`ask` command is implemented"):
        assert claim not in text
    assert "No command uses them yet" in TOOLS_DOC.read_text(encoding="utf-8")


# ---- assistant orchestration -------------------------------------------------------

ORCHESTRATION_DOC = DOCS / "assistant-orchestration.md"
_LIMIT_OUTCOMES = {
    "max_provider_requests": "provider_requests",
    "max_tool_calls_per_turn": "tool_calls_per_turn",
    "max_tool_calls": "tool_calls",
    "max_total_output_chars": "total_output",
    "request_timeout_seconds": "request_deadline",
    "overall_timeout_seconds": "overall_deadline",
}


def test_documented_run_limits_match_the_model():
    rows = table_rows(ORCHESTRATION_DOC, "| Limit | Default | Allowed | Ends the run with")
    fields = assistant_runs.RunLimits.model_fields
    assert [row[0].strip("`") for row in rows] == list(fields)
    documented_kinds = set()
    for name, default, allowed, ends in (row[:4] for row in rows):
        name = name.strip("`")
        check_default(default, fields[name], name)
        assert documented_range(allowed) == field_range(fields[name]), name
        if name in _LIMIT_OUTCOMES:
            assert ends == f"`{_LIMIT_OUTCOMES[name]}`", name
            documented_kinds.add(ends.strip("`"))
        else:
            assert ends.startswith("none"), name
    assert documented_kinds == {kind.value for kind in assistant_runs.RunLimitKind}


def test_documented_outcomes_match_the_union():
    rows = table_rows(ORCHESTRATION_DOC, "| Outcome (`kind`)")
    members = typing.get_args(typing.get_args(assistant_runs.RunOutcome)[0])
    assert [row[0].strip("`") for row in rows] == [
        member.model_fields["kind"].default for member in members
    ]
    for (_kind, fields, _when), member in zip((row[:3] for row in rows), members, strict=True):
        expected = [f"`{name}`" for name in member.model_fields if name != "kind"]
        assert fields == (", ".join(expected) or "none"), member.__name__
    reasons = table_rows(ORCHESTRATION_DOC, "| Malformed output (`reason`)")
    assert [row[0].strip("`") for row in reasons] == [
        reason.value for reason in assistant_runs.MalformedOutputReason
    ]
    stops = typing.get_args(assistant_runs.Answered.model_fields["stop_reason"].annotation)
    answered = rows[0][2]
    assert all(f"`{stop.value}`" in answered for stop in stops)


def test_documented_instructions_and_transcript_bounds_match_the_code():
    text = " ".join(ORCHESTRATION_DOC.read_text(encoding="utf-8").split())
    assert f"version `{assistant_instructions.INSTRUCTIONS_VERSION}`" in text
    assert f"{assistant_runs.MAX_TRANSCRIPT_TEXT_CHARS:,} characters" in text
    assert "answers are not yet validated" in text


def test_the_readme_links_the_loop_without_claiming_ask_or_validation():
    text = README.read_text(encoding="utf-8")
    assert "docs/assistant-orchestration.md" in text
    for claim in (
        "`ask` command is implemented",
        "answer validation is implemented",
        "answers are validated",
    ):
        assert claim not in text


# ---- assistant answers -------------------------------------------------------------

ANSWERS_DOC = DOCS / "assistant-answers.md"


def test_documented_answer_bounds_match_the_constants():
    rows = table_rows(ANSWERS_DOC, "| Bound | Value")
    modules = (assistant_answers, answer_validation)
    expected = {
        name: getattr(module, name)
        for module in modules
        for name in vars(module)
        if name.startswith(("MAX_", "MIN_")) and isinstance(getattr(module, name), int)
    }
    assert {row[0].strip("`"): number(row[1]) for row in rows} == expected


def test_documented_rejections_match_the_codes():
    rows = table_rows(ANSWERS_DOC, "| Code | Check | Message")
    assert [row[0].strip("`") for row in rows] == [
        code.value for code in answer_validation.RejectionCode
    ]
    for code, check, message in (row[:3] for row in rows):
        assert check in {"citations", "claims"}, code
        assert (
            message
            == answer_validation.REJECTION_MESSAGES[
                answer_validation.RejectionCode(code.strip("`"))
            ]
        )


def test_documented_proposal_vocabulary_matches_the_contract():
    kinds = table_rows(ANSWERS_DOC, "| Conclusion kind")
    assert [row[0].strip("`") for row in kinds] == [
        kind.value for kind in assistant_answers.ConclusionKind
    ]
    fields = table_rows(ANSWERS_DOC, "| Field | Holds")
    documented = {name.strip("`") for row in fields for name in row[0].split(", ")}
    assert documented == set(assistant_answers.AnswerProposal.model_fields)
    reason = next(row[1] for row in fields if row[0] == "`insufficient_reason`")
    assert all(f"`{item.value}`" in reason for item in assistant_answers.InsufficientReason)
    status = next(row[1] for row in fields if row[0] == "`status`")
    assert all(f"`{item.value}`" in status for item in AnalysisStatus)
    text = ANSWERS_DOC.read_text(encoding="utf-8")
    assert f"Version `{assistant_instructions.INSTRUCTIONS_VERSION}`" in text


def test_the_readme_links_answers_without_claiming_rendering_or_ask():
    text = README.read_text(encoding="utf-8")
    assert "docs/assistant-answers.md" in text
    for claim in ("answers are rendered", "repair is implemented", "`ask` command is implemented"):
        assert claim not in text
    assert "rendering answers, the single repair attempt, the `ask` command" in " ".join(
        ANSWERS_DOC.read_text(encoding="utf-8").split()
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
