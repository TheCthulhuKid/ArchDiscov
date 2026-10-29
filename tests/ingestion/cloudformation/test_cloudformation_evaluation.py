import pytest

from archdiscov.ingestion.cloudformation.evaluation import Reference, evaluate
from archdiscov.ingestion.cloudformation.evaluation_limits import MAX_DEPTH
from archdiscov.ingestion.cloudformation.issue_codes import EvaluationIssueCode


def run(value, **kwargs):
    options = {"parameters": {}, "resources": set(), "pseudo": {}}
    options.update(kwargs)
    return evaluate(value, **options)


def test_if_does_not_evaluate_unselected_sensitive_or_invalid_operations():
    ignored = {
        "secret": {"Fn::Join": ["-", [{"Ref": "Secret"}]]},
        "reference": {"Ref": "R"},
        "invalid": {"Fn::Select": [-1, []]},
        "cycle": {"Condition": "Cycle"},
    }
    result = run(
        {"property": {"Fn::If": ["C", {"answer": ["safe"]}, ignored]}},
        conditions={"C": True, "Cycle": {"Condition": "Cycle"}},
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


def test_eager_intrinsic_arity_is_checked_before_arguments():
    result = run({"Fn::Join": [{"Ref": "R"}]}, resources={"R"})
    assert not result.resolved and result.value is None
    assert [issue.reason for issue in result.issues] == ["invalid_intrinsic"]
    assert not result.references and not result.sources


def test_literals_partial_values_and_json_pointer_paths():
    result = run({"a/b~c": {"Ref": "Missing"}, "null": None, "ok": 3})
    assert result.value == {"null": None, "ok": 3}
    assert not result.resolved and not result.known
    assert result.issues[0].path == "/a~1b~0c"
    assert result.issues[0].reason is EvaluationIssueCode.REFERENCE_UNAVAILABLE
    assert run(None).known
    assert not run({"Ref": "Missing"}).known


def test_unknown_list_omitted_as_a_whole_without_losing_reference_indexes():
    result = run({"items": [1, {"Ref": "R"}], "ok": 2}, resources={"R"})
    assert result.value == {"ok": 2}
    assert result.sources == {"/ok": "/ok"}
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


# Each route reaches its own redaction check.
@pytest.mark.parametrize(
    "expression",
    [
        {"Fn::Join": ["-", [{"Ref": "Secret"}, "suffix"]]},
        {"Fn::Sub": "prefix-${Secret}"},
        {"Fn::ImportValue": {"Ref": "Secret"}},
    ],
    ids=["eager-operand", "substitution", "import-value"],
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


def test_unsupported_imported_or_invalid_values_are_unknown_without_raw_messages():
    for expression, reason in (
        ({"Fn::Unsupported": "secret-source"}, EvaluationIssueCode.UNSUPPORTED_INTRINSIC),
        ({"Fn::ImportValue": "secret-source"}, EvaluationIssueCode.IMPORT_UNAVAILABLE),
        ({"Fn::Select": [-1, ["secret-source"]]}, EvaluationIssueCode.INVALID_INTRINSIC),
    ):
        result = run(expression)
        assert result.value is None and not result.resolved
        assert [issue.reason for issue in result.issues] == [reason]
        assert "secret-source" not in repr(result.issues)


def test_condition_recursion_cycles_and_selected_branches():
    result = run({"Condition": "A"}, conditions={"A": {"Condition": "B"}, "B": {"Condition": "A"}})
    assert not result.resolved and result.issues
    result = run(
        {"Fn::If": ["C", {"Ref": "AWS::NoValue"}, {"Ref": "R"}]},
        resources={"R"},
        conditions={"C": True},
    )
    assert result.resolved and result.value is None and not result.references


def test_expansion_limits_and_non_json_inputs_make_the_value_unknown():
    recursive = []
    recursive.append(recursive)
    deep = 0
    for _ in range(MAX_DEPTH + 1):
        deep = [deep]
    for value, parameters in (
        (recursive, {}),
        (deep, {}),
        (float("nan"), {}),
        ({"Fn::Join": ["x" * 1000, ["a"] * 2000]}, {}),
        ({"Fn::Sub": "${P}" * 1000}, {"P": "x" * 5000}),
        ([{"Fn::Split": [",", {"Ref": "P"}]}] * 20, {"P": "x," * 5000}),
    ):
        result = run(value, parameters=parameters)
        assert not result.resolved and result.value is None and not result.sources
        assert result.issues[-1].reason is EvaluationIssueCode.EVALUATION_LIMIT_OR_INVALID_SHAPE


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


def test_references_and_issues_stop_at_intrinsic_source_boundaries():
    result = run({"property": {"Fn::Join": ["-", ["prefix", {"Ref": "R"}]]}}, resources={"R"})
    assert result.references == [Reference("R", "/property")]
    assert result.issues and all(issue.path == "/property" for issue in result.issues)


def test_condition_in_a_literal_policy_statement_is_not_an_intrinsic():
    statement = {"Effect": "Allow", "Condition": {"StringEquals": {"key": "value"}}}
    result = run(statement)
    assert result.value == statement and result.resolved
