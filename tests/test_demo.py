"""Checks that the Acme demo snapshot is complete and readable."""

import json
from pathlib import Path

ACME = Path(__file__).parent.parent / "demo" / "acme"


def test_manifest_operations_point_at_json_files():
    manifest = json.loads((ACME / "manifest.json").read_text())

    assert manifest["operations"]
    for operation in manifest["operations"].values():
        assert operation["status"] == "ok"
        json.loads((ACME / operation["file"]).read_text())
