"""Keep the published inventory grants aligned with the executable allowlist."""

import json
import re
from pathlib import Path

from archdiscov.ingestion.operations import ALLOWED_OPERATIONS
from archdiscov.models.enums import AwsService

GUIDE = Path(__file__).parents[1] / "docs" / "aws-permissions.md"


def policies():
    return [
        json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", GUIDE.read_text(), re.S)
    ]


def test_documented_inventory_policy_matches_every_allowlisted_read():
    inventory, _ = policies()
    (statement,) = inventory["Statement"]
    expected = {
        f"{service}:{operation}"
        for service, operation in ALLOWED_OPERATIONS
        if service != AwsService.STS
    }
    assert inventory["Version"] == "2012-10-17"
    assert statement["Effect"] == "Allow"
    assert statement["Resource"] == "*"
    assert set(statement["Action"]) == expected
    assert len(statement["Action"]) == len(expected)


def test_optional_assume_role_grant_is_separate_and_resource_restricted():
    inventory, assumption = policies()
    (statement,) = assumption["Statement"]
    assert statement["Effect"] == "Allow"
    assert statement["Action"] == "sts:AssumeRole"
    assert statement["Resource"] == "arn:aws:iam::123456789012:role/InventoryRead"
    documented = set(inventory["Statement"][0]["Action"]) | {
        statement["Action"],
        "sts:GetCallerIdentity",
    }
    assert documented == {f"{service}:{operation}" for service, operation in ALLOWED_OPERATIONS}
