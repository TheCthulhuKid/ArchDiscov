from copy import deepcopy

import pytest

from archdiscov.ingestion.adapter import IngestionError
from archdiscov.ingestion.terraform.parsing import parse_export
from archdiscov.ingestion.terraform.plan_parsing import parse_plan
from tests.support.terraform_data import resource


@pytest.fixture(params=["values", "planned_values"])
def parse_inventory(request):
    key = request.param
    parser = parse_export if key == "values" else parse_plan

    def parse(module):
        document = {"format_version": "1.0", key: {"root_module": module}}
        original = deepcopy(document)
        result = parser(document)
        assert document == original
        return result

    return parse


@pytest.mark.parametrize("depth", [64, 65])
def test_inventory_module_depth_boundary(parse_inventory, depth):
    module = {"resources": [resource()]}
    for _ in range(depth):
        module = {"child_modules": [module]}
    if depth == 65:
        with pytest.raises(IngestionError, match="module nesting exceeds supported limits"):
            parse_inventory(module)
    else:
        parsed = parse_inventory(module)
        assert len(parsed.resources) == 1
        assert parsed.resources[0].path.endswith("/child_modules/0" * depth + "/resources/0")


@pytest.mark.parametrize(
    "module",
    [
        {"resources": {}},
        {"child_modules": {}},
        {"child_modules": [None]},
        {"resources": [None]},
        {"resources": [resource()], "child_modules": [{"resources": [resource()]}]},
    ],
)
def test_inventory_shape_and_duplicate_guards_remain_strict(parse_inventory, module):
    with pytest.raises(IngestionError) as caught:
        parse_inventory(module)
    assert caught.value.code == "invalid_request"


@pytest.mark.parametrize(
    "overrides",
    [
        {"mode": None},
        {"address": " "},
        {"type": "aws_vpc\n"},
        {"provider_name": "registry.terraform.io/other/aws"},
    ],
)
def test_inventory_identity_guards_remain_strict(parse_inventory, overrides):
    with pytest.raises(IngestionError) as caught:
        parse_inventory({"resources": [resource(**overrides)]})
    assert caught.value.code == "invalid_request"


def test_inventory_skips_data_before_inspecting_identity_or_values(parse_inventory):
    parsed = parse_inventory(
        {"resources": [{"mode": "data", "provider_name": None, "values": "not-an-object"}]}
    )
    assert parsed.resources == []
    assert len(parsed.skipped_paths) == 1
    assert parsed.skipped_paths[0].endswith("/root_module/resources/0")
