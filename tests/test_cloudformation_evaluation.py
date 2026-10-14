import json

import pytest

from archdiscov.ingestion.cloudformation.evaluation import Reference, evaluate
from archdiscov.ingestion.cloudformation.intrinsics import InvalidIntrinsic, evaluate_pure
from archdiscov.ingestion.cloudformation.issue_codes import EvaluationIssueCode


def run(value, **kwargs):
    options = {"parameters": {}, "resources": set(), "pseudo": {}}
    options.update(kwargs)
    return evaluate(value, **options)


def test_issue_codes_preserve_persisted_strings():
    assert {code.value for code in EvaluationIssueCode} == {
        "redacted_input",
        "invalid_intrinsic",
        "resource_value_unavailable",
        "reference_unavailable",
        "condition_unavailable",
        "condition_cycle",
        "invalid_condition",
        "import_unavailable",
        "unsupported_intrinsic",
        "mapping_unavailable",
        "evaluation_limit_or_invalid_shape",
    }
    for code in EvaluationIssueCode:
        assert isinstance(code, str)
        assert str(code) == code.value
        assert json.loads(json.dumps({"reason": code})) == {"reason": code.value}
    assert run({"Ref": "Missing"}).issues[0].reason is EvaluationIssueCode.REFERENCE_UNAVAILABLE
    assert (
        run({"Fn::ImportValue": "export"}).issues[0].reason
        is EvaluationIssueCode.IMPORT_UNAVAILABLE
    )


@pytest.mark.parametrize(
    ("name", "values", "expected"),
    [
        ("Fn::Join", ["-", []], ""),
        ("Fn::Join", ["-", ["a", "b"]], "a-b"),
        ("Fn::Split", [",", "a,,b,"], ["a", "", "b", ""]),
        ("Fn::Select", ["01", ["a", "b"]], "b"),
        ("Fn::Equals", [True, 1], False),
        ("Fn::Equals", [1, 1.0], False),
        ("Fn::Equals", [None, None], True),
        ("Fn::Not", [False], True),
        ("Fn::And", [True] * 10, True),
        ("Fn::Or", [False] * 9 + [True], True),
    ],
)
def test_pure_operations_without_evaluator_context(name, values, expected):
    assert evaluate_pure(name, values) == expected


@pytest.mark.parametrize(
    ("name", "values"),
    [
        ("Fn::Select", [True, ["a", "b"]]),
        ("Fn::Select", ["0000000000", ["a"]]),
        ("Fn::Join", ["-", [1]]),
        ("Fn::Split", ["", "abc"]),
        ("Fn::Equals", [[], []]),
        ("Fn::And", [True, 1]),
        ("Fn::Or", [False, "true"]),
        ("Fn::Not", [1]),
    ],
)
def test_pure_operations_reject_invalid_operand_types(name, values):
    with pytest.raises(InvalidIntrinsic):
        evaluate_pure(name, values)


@pytest.mark.parametrize(
    ("limit", "expression"),
    [
        ("MAX_BYTES", {"Fn::Join": ["é", ["a", "b", "c"]]}),
        ("MAX_NODES", {"Fn::Split": [",", "a,b,c,d,e"]}),
    ],
)
def test_pure_expansion_limits_invalidate_evaluation_without_sources(
    monkeypatch, limit, expression
):
    monkeypatch.setattr(f"archdiscov.ingestion.cloudformation.intrinsics.{limit}", 4)
    result = run({"property": expression})
    assert result.value is None and not result.resolved and not result.sources
    assert result.issues[-1].reason is EvaluationIssueCode.EVALUATION_LIMIT_OR_INVALID_SHAPE


@pytest.mark.parametrize("selected", [True, False])
def test_if_does_not_evaluate_unselected_sensitive_or_invalid_operations(selected):
    ignored = {
        "secret": {"Fn::Join": ["-", [{"Ref": "Secret"}]]},
        "reference": {"Ref": "R"},
        "invalid": {"Fn::Select": [-1, []]},
        "cycle": {"Condition": "Cycle"},
    }
    branches = [{"answer": ["safe"]}, ignored] if selected else [ignored, {"answer": ["safe"]}]
    result = run(
        {"property": {"Fn::If": ["C", *branches]}},
        conditions={"C": selected, "Cycle": {"Condition": "Cycle"}},
        parameters={"Secret": "[REDACTED]"},
        resources={"R"},
    )
    assert result.value == {"property": {"answer": ["safe"]}}
    assert result.resolved
    assert not result.issues and not result.references and not result.redacted_paths
    assert result.sources == {"/property/answer/0": "/property"}


def test_mapping_results_are_literals_with_intrinsic_boundary_provenance():
    result = run(
        {"property": {"Fn::FindInMap": ["M", "top", "key"]}},
        mappings={"M": {"top": {"key": {"Ref": "Missing", "secret": "[REDACTED]"}}}},
    )
    assert result.value == {"property": {"Ref": "Missing", "secret": "[REDACTED]"}}
    assert result.resolved and not result.issues and not result.references
    assert result.redacted_paths == ["/property"]
    assert result.sources == {"/property/Ref": "/property", "/property/secret": "/property"}


@pytest.mark.parametrize(
    ("name", "valid"),
    [
        ("Fn::Join", ["-", ["a", "b"]]),
        ("Fn::Split", [",", "a,b"]),
        ("Fn::Select", [0, ["a"]]),
        ("Fn::FindInMap", ["M", "top", "key"]),
        ("Fn::Equals", [1, 1]),
        ("Fn::Not", [True]),
        ("Fn::And", [True, False]),
        ("Fn::Or", [True, False]),
    ],
)
def test_eager_intrinsic_arity_is_checked_before_arguments(name, valid):
    for arguments in (valid[:-1], valid + [None] * 10, {"Ref": "R"}):
        result = run({name: arguments}, resources={"R"})
        assert not result.resolved and result.value is None
        assert [issue.reason for issue in result.issues] == ["invalid_intrinsic"]
        assert not result.references and not result.sources


def test_literals_partial_values_and_json_pointer_paths():
    result = run({"a/b~c": {"Ref": "Missing"}, "null": None, "ok": 3})
    assert result.value == {"null": None, "ok": 3}
    assert not result.resolved and not result.known
    assert result.issues[0].path == "/a~1b~0c"
    assert run(None).known
    assert not run({"Ref": "Missing"}).known


def test_unknown_list_omitted_as_a_whole_without_losing_reference_indexes():
    result = run({"items": [1, {"Ref": "R"}], "ok": 2}, resources={"R"})
    assert result.value == {"ok": 2}
    assert result.references == [Reference("R", "/items/1")]
    assert result.issues[0].path == "/items/1"


def test_resource_ref_and_getatt_never_invent_physical_values():
    result = run({"id": {"Ref": "R"}, "arn": {"Fn::GetAtt": ["R", "Arn"]}}, resources={"R"})
    assert result.value == {}
    assert result.references == [Reference("R", "/id"), Reference("R", "/arn", "Arn")]
    assert len(result.issues) == 2


def test_parameters_pseudo_and_no_value():
    result = run(
        {
            "p": {"Ref": "P"},
            "region": {"Ref": "AWS::Region"},
            "drop": {"Ref": "AWS::NoValue"},
            "list": [1, {"Ref": "AWS::NoValue"}, 2],
        },
        parameters={"P": "value"},
        pseudo={"AWS::Region": "us-east-1"},
    )
    assert result.value == {"p": "value", "region": "us-east-1", "list": [1, 2]}
    assert result.resolved and not result.issues


def test_pure_intrinsics_and_conditions():
    result = run(
        {
            "join": {"Fn::Join": ["-", ["a", {"Ref": "P"}]]},
            "split": {"Fn::Split": [",", "a,b"]},
            "select": {"Fn::Select": ["1", ["a", "b"]]},
            "map": {"Fn::FindInMap": ["M", "top", "key"]},
            "if": {"Fn::If": ["Enabled", "yes", {"Ref": "Missing"}]},
            "bool": {"Fn::And": [{"Condition": "Enabled"}, {"Fn::Not": [False]}]},
        },
        parameters={"P": "b"},
        mappings={"M": {"top": {"key": "mapped"}}},
        conditions={"Enabled": {"Fn::Equals": ["x", "x"]}},
    )
    assert result.value == {
        "join": "a-b",
        "split": ["a", "b"],
        "select": "b",
        "map": "mapped",
        "if": "yes",
        "bool": True,
    }
    assert result.resolved and not result.issues
    assert run({"Condition": "C"}, conditions={"C": {"Fn::Or": [False, True]}}).value is True


def test_substitution_explicit_variables_escapes_and_local_references():
    assert (
        run(
            {"Fn::Sub": ["${P}-${AWS::Region}-${V}-${!literal}", {"V": "v"}]},
            parameters={"P": "p"},
            pseudo={"AWS::Region": "region"},
        ).value
        == "p-region-v-${literal}"
    )
    result = run({"x": {"Fn::Sub": "${R}/${R.Arn}"}}, resources={"R"})
    assert result.value == {}
    assert result.references == [Reference("R", "/x"), Reference("R", "/x", "Arn")]
    assert not result.resolved
    assert run({"Fn::Sub": ["${R}", {"R": "override"}]}, resources={"R"}).value == "override"


@pytest.mark.parametrize(
    "expression",
    [
        {"Fn::Join": ["-", [{"Ref": "Secret"}, "suffix"]]},
        {"Fn::Sub": "prefix-${Secret}"},
        {"Fn::Sub": ["${X}", {"X": {"Ref": "Secret"}}]},
        {"Fn::Split": [",", {"Ref": "Secret"}]},
        {"Fn::Equals": [{"Ref": "Secret"}, "x"]},
        {"Fn::ImportValue": {"Ref": "Secret"}},
    ],
)
def test_redacted_inputs_cannot_be_derived_or_exposed(expression):
    result = run({"identifier": expression}, parameters={"Secret": "[REDACTED]"})
    assert result.value == {}
    assert not result.resolved
    assert "/identifier" in result.redacted_paths
    assert any(issue.reason == "redacted_input" for issue in result.issues)
    assert not result.references


def test_direct_redacted_values_are_preserved():
    result = run({"value": {"Ref": "P"}}, parameters={"P": "[REDACTED]"})
    assert result.value == {"value": "[REDACTED]"}
    assert result.redacted_paths == ["/value"]
    assert result.resolved


@pytest.mark.parametrize(
    "expression",
    [
        {"Fn::Select": [-1, ["a"]]},
        {"Fn::Select": [9, ["a"]]},
        {"Fn::Select": [True, ["a"]]},
        {"Fn::Join": ["-", [1]]},
        {"Fn::Split": ["", "a"]},
        {"Fn::Equals": [1]},
        {"Fn::And": [True]},
        {"Fn::Not": ["true"]},
        {"Fn::If": ["Missing", 1, 2]},
        {"Fn::FindInMap": ["M", "x", "y"]},
        {"Fn::Sub": "${Missing}"},
        {"Fn::Sub": "${unclosed"},
        {"Fn::GetAtt": ["R"]},
        {"Fn::GetAZs": ""},
        {"Fn::Base64": "text"},
        {"Fn::Unsupported": "secret-source"},
        {"Ref": "P", "extra": "secret-source"},
        {"Fn::ImportValue": "external-export"},
        {"Fn::Sub": "${AWS::NoValue}"},
        {"Fn::Join": ["-", {"Ref": "AWS::NoValue"}]},
        {"Fn::Equals": [{}, {}]},
    ],
)
def test_unsupported_or_invalid_expressions_are_unknown_without_raw_messages(expression):
    result = run(expression)
    assert result.value is None and not result.resolved
    assert result.issues
    assert "secret-source" not in repr(result.issues)
    assert "external-export" not in repr(result.issues)


def test_condition_recursion_cycles_and_selected_branches():
    result = run({"Condition": "A"}, conditions={"A": {"Condition": "B"}, "B": {"Condition": "A"}})
    assert not result.resolved and result.issues
    result = run(
        {"Fn::If": ["C", {"Ref": "AWS::NoValue"}, {"Ref": "R"}]},
        resources={"R"},
        conditions={"C": True},
    )
    assert result.resolved and result.value is None and not result.references


def test_evaluation_limits_and_non_json_inputs():
    recursive = []
    recursive.append(recursive)
    deep = 0
    for _ in range(100):
        deep = [deep]
    for value in (recursive, deep, float("nan"), {1: "x"}, {"x": object()}, [0] * 60_000):
        result = run(value)
        assert not result.resolved and result.issues


def test_sources_preserve_compacted_list_indexes_and_intrinsic_boundaries():
    result = run(
        {
            "tags": [{"Ref": "AWS::NoValue"}, {"Key": "name", "Value": {"Fn::Sub": "${P}"}}],
            "branch": {"Fn::If": ["C", [{"x": 1}], []]},
        },
        parameters={"P": "value"},
        conditions={"C": True},
    )
    assert result.value == {"tags": [{"Key": "name", "Value": "value"}], "branch": [{"x": 1}]}
    assert result.sources == {
        "/tags/0/Key": "/tags/1/Key",
        "/tags/0/Value": "/tags/1/Value",
        "/branch/0/x": "/branch",
    }


def test_unknown_nested_list_fields_omit_whole_list_and_sources():
    result = run({"items": [{"x": {"Ref": "Missing"}, "ok": 1}], "ok": 2})
    assert result.value == {"ok": 2}
    assert result.sources == {"/ok": "/ok"}


def test_substitution_expansion_is_bounded_even_for_cached_variables():
    result = run({"Fn::Sub": "${P}" * 1000}, parameters={"P": "x" * 5000})
    assert not result.resolved and result.value is None and not result.sources


def test_generated_collections_have_an_aggregate_budget():
    result = run([{"Fn::Split": [",", {"Ref": "P"}]}] * 20, parameters={"P": "x," * 5000})
    assert not result.resolved and result.value is None and not result.sources


@pytest.mark.parametrize(
    "expression",
    [
        {"Fn::Join": ["-", ["prefix", {"Ref": "R"}]]},
        {"Fn::If": ["C", {"nested": {"Ref": "R"}}, {}]},
    ],
)
def test_references_and_issues_stop_at_intrinsic_source_boundaries(expression):
    result = run({"property": expression}, resources={"R"}, conditions={"C": True})
    assert result.references == [Reference("R", "/property")]
    assert result.issues and all(issue.path == "/property" for issue in result.issues)


def test_condition_in_a_literal_policy_statement_is_not_an_intrinsic():
    statement = {"Effect": "Allow", "Condition": {"StringEquals": {"key": "value"}}}
    result = run(statement)
    assert result.value == statement and result.resolved
