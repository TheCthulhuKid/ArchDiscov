import json
import math
from functools import partial

import pytest

from archdiscov.ingestion.adapter import IngestionError
from archdiscov.ingestion.cloudformation.parameters import load_parameters
from archdiscov.ingestion.cloudformation.parsing import parse_template
from archdiscov.ingestion.strict_json import reject_constant, unique_object
from archdiscov.ingestion.terraform.parsing import load_json


def decode(text, **hooks):
    return json.loads(
        text,
        object_pairs_hook=hooks.get("object_pairs_hook", unique_object),
        parse_constant=hooks.get("parse_constant", reject_constant),
    )


def test_preserves_json_values_and_object_order():
    text = '{"z": [null, true, false, -2, 1e3, "réseau"], "a": {"z": 0}}'
    result = decode(text)
    assert result == json.loads(text)
    assert list(result) == ["z", "a"]
    assert decode('[{}, [], 0, "value"]') == [{}, [], 0, "value"]


@pytest.mark.parametrize(
    "text",
    [
        '{"private-key": 1, "private-key": 2}',
        '[{"nested": {"private-key": 1, "private-key": 2}}]',
        '{"private-key": 1, "private-\\u006bey": 2}',
        "NaN",
        '{"private-key": [Infinity]}',
        "[-Infinity]",
    ],
)
def test_rejections_are_not_syntax_errors_and_do_not_expose_input(text):
    with pytest.raises(ValueError) as caught:
        decode(text)
    assert type(caught.value) is ValueError
    assert str(caught.value) == ""
    assert caught.value.args == ()


@pytest.mark.parametrize(
    ("text", "hook", "primitive"),
    [
        ('{"private-key": 1, "private-key": 2}', "object_pairs_hook", unique_object),
        ("NaN", "parse_constant", reject_constant),
        ("Infinity", "parse_constant", reject_constant),
        ("-Infinity", "parse_constant", reject_constant),
    ],
)
def test_error_factories_raise_the_callers_exception_directly(text, hook, primitive):
    error = IngestionError("invalid_request", "Safe source-specific message")
    with pytest.raises(IngestionError) as caught:
        decode(text, **{hook: partial(primitive, error_factory=lambda: error)})
    assert caught.value is error
    assert error.__cause__ is None
    assert error.__context__ is None
    assert not error.__suppress_context__


def test_syntax_errors_keep_json_decode_location():
    with pytest.raises(json.JSONDecodeError) as caught:
        decode('{\n  "private-key":\n}')
    assert (caught.value.lineno, caught.value.colno) == (3, 1)


@pytest.mark.parametrize(
    ("value", "terraform_message"),
    [
        ('{"private-key": 1, "private-key": 2}', "contains duplicate object keys"),
        ("NaN", "must contain finite JSON values"),
        ("Infinity", "must contain finite JSON values"),
        ("-Infinity", "must contain finite JSON values"),
    ],
)
def test_reader_error_policies_are_preserved(tmp_path, monkeypatch, value, terraform_message):
    def unexpected_yaml_fallback(*args, **kwargs):
        pytest.fail("Strict JSON hook failures must not fall back to YAML")

    monkeypatch.setattr(
        "archdiscov.ingestion.cloudformation.parsing.yaml.load", unexpected_yaml_fallback
    )
    path = tmp_path / "private-path.json"
    path.write_text('{"Resources": {}, "Metadata": {"value": ' + value + "}}")
    for reader, message, suppressed in (
        (lambda _: parse_template(path.read_text()), "Invalid CloudFormation template.", True),
        (load_parameters, "Parameters file must be a bounded UTF-8 JSON object.", True),
        (load_json, f"Terraform JSON {terraform_message}", False),
    ):
        with pytest.raises(IngestionError) as caught:
            reader(path)
        assert caught.value.code == "invalid_request"
        assert str(caught.value) == message
        assert caught.value.__cause__ is None
        assert caught.value.__suppress_context__ is suppressed


def test_float_overflow_validation_stays_with_each_reader(tmp_path):
    text = '{"Resources": {}, "Metadata": {"value": 1e999}}'
    assert math.isinf(decode(text)["Metadata"]["value"])
    path = tmp_path / "overflow.json"
    path.write_text(text)
    assert math.isinf(load_json(path)["Metadata"]["value"])
    with pytest.raises(IngestionError, match="Invalid CloudFormation template"):
        parse_template(text)
    with pytest.raises(IngestionError, match="Parameters file"):
        load_parameters(path)
