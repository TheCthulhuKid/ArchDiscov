import json
from copy import deepcopy
from dataclasses import asdict

import pytest

from archdiscov.core.strict_json import MAX_JSON_FILE_BYTES, FileTooLargeError
from archdiscov.ingestion.adapter import IngestionError
from archdiscov.ingestion.terraform import parsing
from archdiscov.ingestion.terraform.parsing import load_export, parse_export
from archdiscov.ingestion.terraform.sensitivity import REDACTED, read_pointer, sanitize_values
from tests.support.terraform_data import resource

RAW_SECRET = "do-not-expose-terraform-value"


def export(module=None, **overrides):
    result = {
        "format_version": "1.0",
        "terraform_version": "1.9.8",
        "values": {"root_module": {} if module is None else module},
    }
    result.update(overrides)
    return result


def assert_safe_error(error):
    assert error.code == "invalid_request"
    assert error.message == str(error)
    assert RAW_SECRET not in str(error)
    assert RAW_SECRET not in repr(error)


def test_nested_modules_preserve_addresses_and_exact_resource_pointers():
    counted = "module.network[0].aws_vpc.main[2]"
    quoted = 'module.network[0].module.apps["blue/green~prod"].aws_instance.web["api"]'
    document = export(
        {
            "resources": [resource()],
            "child_modules": [
                {
                    "address": "module.network[0]",
                    "resources": [resource(counted, index=2)],
                    "child_modules": [
                        {
                            "address": 'module.network[0].module.apps["blue/green~prod"]',
                            "resources": [resource(quoted, type="aws_instance", index="api")],
                        }
                    ],
                },
                {"resources": [resource("module.other.aws_vpc.main")]},
            ],
        }
    )
    before = deepcopy(document)

    parsed = parse_export(document)

    assert parsed.format_version == "1.0"
    assert parsed.terraform_version == "1.9.8"
    assert [item.address for item in parsed.resources] == [
        "aws_vpc.main",
        counted,
        quoted,
        "module.other.aws_vpc.main",
    ]
    assert [item.path for item in parsed.resources] == [
        "/values/root_module/resources/0",
        "/values/root_module/child_modules/0/resources/0",
        "/values/root_module/child_modules/0/child_modules/0/resources/0",
        "/values/root_module/child_modules/1/resources/0",
    ]
    assert parsed.resources[2].terraform_type == "aws_instance"
    for item in parsed.resources:
        assert read_pointer(document, item.path)["address"] == item.address
    assert parsed.skipped_paths == []
    assert document == before


def test_accepts_supported_major_with_newer_minor_and_empty_state():
    parsed = parse_export(export(format_version="1.9", terraform_version=None))
    assert parsed.format_version == "1.9"
    assert parsed.terraform_version is None
    assert parsed.resources == []
    assert parsed.skipped_paths == []


def test_rejects_unsupported_format_version_without_echoing_value():
    with pytest.raises(IngestionError, match="format_version") as exc:
        parse_export(export(format_version=RAW_SECRET))
    assert_safe_error(exc.value)


def test_rejects_plan_and_internal_state_even_with_valid_public_state_fields():
    with pytest.raises(IngestionError, match="not a plan or internal state") as exc:
        parse_export(export(planned_values=RAW_SECRET))
    assert_safe_error(exc.value)


def test_data_resources_are_skipped_without_persisting_any_of_their_values():
    data = resource(
        "data.aws_secretsmanager_secret_version.current",
        mode="data",
        values={"secret_string": RAW_SECRET},
        sensitive_values={"invalid-mask": RAW_SECRET},
    )
    document = export(
        {
            "resources": [data, resource()],
            "child_modules": [{"resources": [deepcopy(data)]}],
        }
    )
    parsed = parse_export(document)
    assert [item.address for item in parsed.resources] == ["aws_vpc.main"]
    assert parsed.resources[0].path == "/values/root_module/resources/1"
    assert parsed.skipped_paths == [
        "/values/root_module/resources/0",
        "/values/root_module/child_modules/0/resources/0",
    ]
    assert RAW_SECRET not in json.dumps(asdict(parsed))


def test_parse_sanitizes_values_before_storing_resources_without_mutating_input():
    values = {"id": "vpc-123", "password": RAW_SECRET, "tags": {"Name": RAW_SECRET}}
    document = export(
        {"resources": [resource(values=values, sensitive_values={"tags": {"Name": True}})]}
    )
    before = deepcopy(document)
    parsed = parse_export(document)
    item = parsed.resources[0]
    assert item.values == {"id": "vpc-123", "password": REDACTED, "tags": {"Name": REDACTED}}
    assert item.redacted_paths == ["/password", "/tags/Name"]
    assert RAW_SECRET not in json.dumps(asdict(parsed))
    assert document == before


def test_load_export_reads_utf8_public_state(tmp_path):
    document = export({"resources": [resource(values={"tags": {"Name": "réseau"}})]})
    path = tmp_path / "state.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    assert load_export(path) == parse_export(document)


def test_load_export_reports_safe_decode_and_filesystem_errors(tmp_path):
    malformed = tmp_path / f"{RAW_SECRET}.json"
    malformed.write_text('{"value":"' + RAW_SECRET + '",}', encoding="utf-8")
    for path in (malformed, tmp_path / f"missing-{RAW_SECRET}"):
        with pytest.raises(IngestionError) as exc:
            load_export(path)
        assert_safe_error(exc.value)
        assert str(path) not in str(exc.value)


def test_load_export_refuses_an_oversized_export(tmp_path, monkeypatch):
    def too_large(path):
        raise FileTooLargeError

    monkeypatch.setattr(parsing, "read_bounded", too_large)
    path = tmp_path / "state.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(IngestionError, match="Terraform export exceeds") as exc:
        load_export(path)
    assert str(MAX_JSON_FILE_BYTES) in str(exc.value)
    assert_safe_error(exc.value)


def test_recursive_masks_redact_booleans_dicts_and_lists():
    values = {
        "id": "vpc-123",
        "password": RAW_SECRET,
        "tags": {"Name": "main", "team": RAW_SECRET},
        "nested": [{"public": "keep", "private": RAW_SECRET}, RAW_SECRET, "tail"],
        "whole": {"otherwise_safe": RAW_SECRET},
    }
    mask = {
        "id": False,
        "password": True,
        "tags": {"team": True},
        "nested": [{"private": True}, True],
        "whole": True,
    }
    before_values, before_mask = deepcopy(values), deepcopy(mask)

    safe, paths = sanitize_values(values, mask)

    assert safe == {
        "id": "vpc-123",
        "password": REDACTED,
        "tags": {"Name": "main", "team": REDACTED},
        "nested": [{"public": "keep", "private": REDACTED}, REDACTED, "tail"],
        "whole": REDACTED,
    }
    assert paths == ["/nested/0/private", "/nested/1", "/password", "/tags/team", "/whole"]
    assert values == before_values
    assert mask == before_mask


def test_generic_secret_key_sanitization_applies_even_without_sensitive_mask():
    values = {
        "password": RAW_SECRET,
        "tags": {"api-key": RAW_SECRET, "Name": "safe"},
        "nested": [{"aws_secret_access_key": RAW_SECRET, "id": "safe"}],
        "credentials": {"arbitrary": RAW_SECRET},
    }
    before = deepcopy(values)
    safe, paths = sanitize_values(values, {})
    assert safe == {
        "password": REDACTED,
        "tags": {"api-key": REDACTED, "Name": "safe"},
        "nested": [{"aws_secret_access_key": REDACTED, "id": "safe"}],
        "credentials": REDACTED,
    }
    assert paths == [
        "/credentials",
        "/nested/0/aws_secret_access_key",
        "/password",
        "/tags/api-key",
    ]
    assert values == before
    assert RAW_SECRET not in json.dumps(safe)


def test_whole_resource_sensitive_mask_discards_all_attributes_including_identity():
    values = {"id": RAW_SECRET, "tags": {"Name": RAW_SECRET}}
    before = deepcopy(values)
    assert sanitize_values(values, True) == ({}, [""])
    assert values == before
    parsed = parse_export(export({"resources": [resource(values=values, sensitive_values=True)]}))
    assert parsed.resources[0].values == {}
    assert parsed.resources[0].redacted_paths == [""]
    assert RAW_SECRET not in json.dumps(asdict(parsed))


def test_parse_export_propagates_malformed_sensitive_mask_as_safe_ingestion_error():
    document = export(
        {"resources": [resource(values={"id": RAW_SECRET}, sensitive_values={RAW_SECRET: True})]}
    )
    before = deepcopy(document)
    with pytest.raises(IngestionError) as exc:
        parse_export(document)
    assert_safe_error(exc.value)
    assert document == before
