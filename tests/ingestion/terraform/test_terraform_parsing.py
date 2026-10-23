import json
from copy import deepcopy
from dataclasses import asdict

import pytest

from archdiscov.ingestion.adapter import IngestionError
from archdiscov.ingestion.strict_json import MAX_JSON_FILE_BYTES, FileTooLargeError
from archdiscov.ingestion.terraform import parsing
from archdiscov.ingestion.terraform.parsing import load_export, parse_export
from archdiscov.ingestion.terraform.sensitivity import REDACTED, read_pointer, sanitize_values

RAW_SECRET = "do-not-expose-terraform-value"
AWS_PROVIDER = "registry.terraform.io/hashicorp/aws"


def resource(address="aws_vpc.main", **overrides):
    result = {
        "address": address,
        "mode": "managed",
        "type": "aws_vpc",
        "provider_name": AWS_PROVIDER,
        "values": {"id": "vpc-123", "tags": {"Name": "main"}},
        "sensitive_values": {},
    }
    result.update(overrides)
    return result


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


@pytest.mark.parametrize("version", ["1.0", "1.9"])
def test_accepts_supported_major_with_newer_minor_and_empty_state(version):
    parsed = parse_export(export(format_version=version, terraform_version=None))
    assert parsed.format_version == version
    assert parsed.terraform_version is None
    assert parsed.resources == []
    assert parsed.skipped_paths == []


@pytest.mark.parametrize("version", [None, 1, "0.1", "2.0", "1", "1.9.0", "1.x", RAW_SECRET])
def test_rejects_missing_malformed_or_unsupported_format_version(version):
    document = export(format_version=version)
    if version is None:
        document.pop("format_version")
    with pytest.raises(IngestionError, match="format_version") as exc:
        parse_export(document)
    assert_safe_error(exc.value)


@pytest.mark.parametrize("version", [1, [], "", RAW_SECRET])
def test_rejects_invalid_terraform_version_without_echoing_value(version):
    with pytest.raises(IngestionError, match="Invalid Terraform version") as exc:
        parse_export(export(terraform_version=version))
    assert_safe_error(exc.value)


def test_terraform_version_is_optional():
    document = export()
    document.pop("terraform_version")
    assert parse_export(document).terraform_version is None


@pytest.mark.parametrize("marker", ["planned_values", "resource_changes", "prior_state", "version"])
def test_rejects_plan_and_internal_state_even_with_valid_public_state_fields(marker):
    with pytest.raises(IngestionError, match="not a plan or internal state") as exc:
        parse_export(export(**{marker: RAW_SECRET}))
    assert_safe_error(exc.value)


@pytest.mark.parametrize(
    "document",
    [None, [], RAW_SECRET, 1, {}, {"format_version": "1.0"}],
)
def test_rejects_missing_or_wrong_top_level_shape(document):
    with pytest.raises(IngestionError) as exc:
        parse_export(document)
    assert_safe_error(exc.value)


@pytest.mark.parametrize("values", [None, [], RAW_SECRET, {}, {"root_module": []}])
def test_requires_values_root_module_object(values):
    with pytest.raises(IngestionError, match="values.root_module") as exc:
        parse_export(export(values=values))
    assert_safe_error(exc.value)


@pytest.mark.parametrize(
    "module",
    [
        {"resources": None},
        {"resources": {}},
        {"resources": [RAW_SECRET]},
        {"child_modules": None},
        {"child_modules": {}},
        {"child_modules": [RAW_SECRET]},
    ],
)
def test_rejects_malformed_module_shapes(module):
    with pytest.raises(IngestionError) as exc:
        parse_export(export(module))
    assert_safe_error(exc.value)


@pytest.mark.parametrize(
    "overrides",
    [
        {"mode": None},
        {"mode": RAW_SECRET},
        {"address": None},
        {"address": " "},
        {"address": 1},
        {"type": None},
        {"type": "google_compute_instance"},
        {"type": "aws_"},
        {"provider_name": None},
        {"provider_name": "registry.terraform.io/hashicorp/google"},
        {"provider_name": "registry.terraform.io/example/aws"},
        {"values": None},
        {"values": []},
        {"values": RAW_SECRET},
    ],
)
def test_rejects_invalid_managed_resources_and_non_aws_providers(overrides):
    with pytest.raises(IngestionError) as exc:
        parse_export(export({"resources": [resource(**overrides)]}))
    assert_safe_error(exc.value)


def test_rejects_duplicate_addresses_across_modules():
    document = export(
        {
            "resources": [resource(RAW_SECRET)],
            "child_modules": [{"resources": [resource(RAW_SECRET)]}],
        }
    )
    with pytest.raises(IngestionError, match="duplicate logical addresses") as exc:
        parse_export(document)
    assert_safe_error(exc.value)


def test_rejects_excessive_module_nesting_with_safe_error():
    module = {"resources": [resource(values={"id": RAW_SECRET})]}
    for _ in range(66):
        module = {"child_modules": [module]}
    with pytest.raises(IngestionError, match="nesting exceeds supported limits") as exc:
        parse_export(export(module))
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


@pytest.mark.parametrize(
    "content",
    [
        b'{"value":"' + RAW_SECRET.encode() + b'", "invalid":"\xff"}',
        ('{"value":"' + RAW_SECRET + '",}').encode(),
        b"",
    ],
    ids=["invalid-utf8", "malformed-json", "empty-file"],
)
def test_load_export_reports_safe_decode_errors(tmp_path, content):
    path = tmp_path / f"{RAW_SECRET}.json"
    path.write_bytes(content)
    with pytest.raises(IngestionError) as exc:
        load_export(path)
    assert_safe_error(exc.value)
    assert str(path) not in str(exc.value)


@pytest.mark.parametrize("is_directory", [False, True], ids=["missing-file", "directory"])
def test_load_export_reports_safe_filesystem_errors(tmp_path, is_directory):
    path = tmp_path / RAW_SECRET
    if is_directory:
        path.mkdir()
    with pytest.raises(IngestionError, match="Could not read") as exc:
        load_export(path)
    assert_safe_error(exc.value)
    assert str(path) not in str(exc.value)


@pytest.mark.parametrize("nested", [False, True], ids=["top-level", "nested-values"])
def test_load_export_rejects_duplicate_object_keys(tmp_path, nested):
    duplicate = f'{{"{RAW_SECRET}":1,"{RAW_SECRET}":2}}'
    if nested:
        document = export({"resources": [resource(values={"replace": None})]})
        content = json.dumps(document).replace('{"replace": null}', duplicate)
    else:
        content = duplicate
    path = tmp_path / "state.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(IngestionError, match="duplicate object keys") as exc:
        load_export(path)
    assert_safe_error(exc.value)


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


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_load_export_rejects_non_json_numeric_constants(tmp_path, constant):
    document = export({"resources": [resource(values={"replace": None})]})
    path = tmp_path / "state.json"
    path.write_text(json.dumps(document).replace("null", constant), encoding="utf-8")
    with pytest.raises(IngestionError, match="finite JSON values") as exc:
        load_export(path)
    assert_safe_error(exc.value)


def test_recursive_masks_redact_booleans_dicts_lists_and_escaped_tag_keys():
    values = {
        "id": "vpc-123",
        "tags": {"Name": "main", "team/~name": RAW_SECRET},
        "nested": [{"public": "keep", "private": RAW_SECRET}, RAW_SECRET, "tail"],
        "whole": {"otherwise_safe": RAW_SECRET},
    }
    mask = {
        "id": False,
        "tags": {"team/~name": True},
        "nested": [{"private": True}, True],
        "whole": True,
    }
    before_values, before_mask = deepcopy(values), deepcopy(mask)

    safe, paths = sanitize_values(values, mask)

    assert safe == {
        "id": "vpc-123",
        "tags": {"Name": "main", "team/~name": REDACTED},
        "nested": [{"public": "keep", "private": REDACTED}, REDACTED, "tail"],
        "whole": REDACTED,
    }
    assert paths == ["/nested/0/private", "/nested/1", "/tags/team~1~0name", "/whole"]
    for path in paths:
        assert read_pointer(safe, path) == REDACTED
    assert values == before_values
    assert mask == before_mask
    safe["tags"]["Name"] = "changed"
    safe["nested"][0]["public"] = "changed"
    assert values == before_values


@pytest.mark.parametrize("mask", [False, None, {}])
def test_generic_secret_key_sanitization_applies_even_without_sensitive_mask(mask):
    values = {
        "password": RAW_SECRET,
        "tags": {"api-key": RAW_SECRET, "Name": "safe"},
        "nested": [{"aws_secret_access_key": RAW_SECRET, "id": "safe"}],
        "credentials": {"arbitrary": RAW_SECRET},
    }
    before = deepcopy(values)
    safe, paths = sanitize_values(values, mask)
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


def test_redaction_paths_are_sorted_and_deduplicated():
    safe, paths = sanitize_values({"password": RAW_SECRET}, {"password": True})
    assert safe == {"password": REDACTED}
    assert paths == ["/password"]


@pytest.mark.parametrize(
    ("values", "mask"),
    [
        ({"id": RAW_SECRET}, RAW_SECRET),
        ({"id": RAW_SECRET}, 1),
        ({"id": RAW_SECRET}, 0),
        ({"id": RAW_SECRET}, []),
        ({"id": RAW_SECRET}, {RAW_SECRET: True}),
        ({"id": RAW_SECRET}, {"id": {}}),
        ({"id": RAW_SECRET}, {"id": []}),
        ({"items": [RAW_SECRET]}, {"items": {}}),
        ({"items": [RAW_SECRET]}, {"items": [False, True]}),
        ({"items": {"id": RAW_SECRET}}, {"items": []}),
        ({"items": [RAW_SECRET]}, {"items": [RAW_SECRET]}),
    ],
)
def test_malformed_sensitive_mask_shapes_fail_safely_without_mutating_values(values, mask):
    before_values, before_mask = deepcopy(values), deepcopy(mask)
    with pytest.raises(IngestionError) as exc:
        sanitize_values(values, mask)
    assert_safe_error(exc.value)
    assert values == before_values
    assert mask == before_mask


def test_parse_export_propagates_malformed_sensitive_mask_as_safe_ingestion_error():
    document = export(
        {"resources": [resource(values={"id": RAW_SECRET}, sensitive_values={RAW_SECRET: True})]}
    )
    before = deepcopy(document)
    with pytest.raises(IngestionError) as exc:
        parse_export(document)
    assert_safe_error(exc.value)
    assert document == before


@pytest.mark.parametrize(
    ("pointer", "expected"),
    [
        ("/", "empty-key"),
        ("/tags/team~1~0name", "escaped"),
        ("/items/0/name", "first"),
        ("/items/1/name", "second"),
        ("/~01", "literal-tilde-one"),
    ],
)
def test_read_pointer_decodes_escaped_keys_and_list_indexes(pointer, expected):
    value = {
        "": "empty-key",
        "tags": {"team/~name": "escaped"},
        "items": [{"name": "first"}, {"name": "second"}],
        "~1": "literal-tilde-one",
    }
    before = deepcopy(value)
    assert read_pointer(value, pointer) == expected
    assert value == before


def test_read_pointer_empty_pointer_returns_root():
    value = {"items": ["first"]}
    assert read_pointer(value, "") is value
