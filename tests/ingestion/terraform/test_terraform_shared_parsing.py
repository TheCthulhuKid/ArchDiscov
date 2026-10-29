import pytest

from archdiscov.ingestion.adapter import IngestionError
from archdiscov.ingestion.terraform.parsing import parse_export
from archdiscov.ingestion.terraform.plan_parsing import parse_plan
from tests.support.terraform_data import resource


def nested(depth):
    module = {"resources": [resource()]}
    for _ in range(depth):
        module = {"child_modules": [module]}
    return module


@pytest.mark.parametrize(
    "parser,key", [(parse_export, "values"), (parse_plan, "planned_values")], ids=["state", "plan"]
)
@pytest.mark.parametrize(
    "module",
    [
        nested(65),
        {"resources": {}},
        {"resources": [resource(provider_name="registry.terraform.io/other/aws")]},
    ],
    ids=["nesting-limit", "module-shape", "resource-identity"],
)
def test_inventory_depth_shape_and_identity_guards_remain_strict(parser, key, module):
    with pytest.raises(IngestionError) as caught:
        parser({"format_version": "1.0", key: {"root_module": module}})
    assert caught.value.code == "invalid_request"
