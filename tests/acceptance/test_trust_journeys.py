"""Credential-free fixture -> analyze -> save -> findings -> evidence journeys."""

from archdiscov.services import load_analysis, load_snapshot, save_snapshot, upgrade_snapshot
from tests.support.installed_cli import run_installed_json
from tests.support.trust_data import OTHER, build, role_arn, statement


def aws_snapshot():
    snapshot = build(
        {
            "web": [
                statement({"AWS": OTHER}, condition=True),
                statement({"AWS": role_arn("api")}),
                statement({"AWS": "*"}, effect="Deny"),
            ],
            "api": [statement({"Service": "ec2.amazonaws.com"})],
        },
        {"work": [statement({"Service": "sns.amazonaws.com"}, condition=True)]},
    )
    return upgrade_snapshot(snapshot)


def analyze_arguments(snapshot_path, result_path, *extra):
    return [
        "analyze",
        "--snapshot",
        str(snapshot_path),
        "--output",
        str(result_path),
        "--analyzer",
        "cross_account_trust",
        *extra,
        "--json",
    ]


def test_installed_trust_journey(tmp_path):
    snapshot = aws_snapshot()
    snapshot_path = save_snapshot(snapshot, tmp_path / "snapshot.json")
    result_path = tmp_path / "result.json"

    saved = run_installed_json(
        tmp_path,
        *analyze_arguments(snapshot_path, result_path, "--result-limit", "50"),
    )
    shown = run_installed_json(
        tmp_path,
        "findings",
        str(result_path),
        "--snapshot",
        str(snapshot_path),
        "--json",
    )
    assert shown["findings"] == saved["findings"]
    result = load_analysis(result_path, snapshot=load_snapshot(snapshot_path))
    assert result.parameters.result_limit == 50

    trust = [f["trust"] for f in shown["findings"] if f["trust"]]
    classifications = {item["classification"] for item in trust}
    assert {"same_account", "service", "external_account", "wildcard"} <= classifications
    assert any(item["conditional"] for item in trust)
    assert any(item["effect"] == "Deny" for item in trust)
    assert {item["source"] for item in trust} == {"role_trust_policy", "resource_policy"}

    cited = next(
        f
        for f in shown["findings"]
        if f["trust"] and f["trust"]["classification"] == "same_account"
    )
    statement_id = cited["evidence_ids"][1]
    inspected = run_installed_json(
        tmp_path,
        "evidence",
        statement_id,
        "--snapshot",
        str(snapshot_path),
        "--json",
    )
    assert inspected["resource"]["id"] == cited["resources"][0]["resource_id"]
    assert inspected["evidence"]["value"]["Effect"] == "Allow"
