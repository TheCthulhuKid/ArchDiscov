"""One credential-free, network-free journey through the installed CLI, outside the checkout.

fixture -> ingest -> queries -> each analyzer -> saved results -> findings -> evidence ->
comparisons -> comparison display, through both entry points, checking JSON and readable
output at each stage. The child sees no credentials and no checkout: its whole `PYTHONPATH` is
the offline guard, which fails and records any network use.
"""

import hashlib
import json
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from archdiscov.models.enums import AnalyzerKind
from archdiscov.services import (
    load_analysis,
    load_comparison,
    load_snapshot,
    save_snapshot,
)
from tests.acceptance.test_comparison_journeys import build
from tests.paths import FIXTURES_ROOT, PROJECT_ROOT
from tests.support.audit_scenarios import SCENARIOS
from tests.support.installed_cli import ENTRY_POINTS, run_installed
from tests.support.offline_guard import OfflineGuard
from tests.support.security_group_data import IDS

# The journey runs both entry points once per module: keep it on one xdist worker.
pytestmark = pytest.mark.xdist_group("installed-journey")

CONTEXT = ("--account-id", "123456789012", "--region", "eu-west-1")
OPTIONS = {"resource_id": "--resource-id", "port": "--port", "max_depth": "--max-depth"}
GROUP = IDS["sg-app"]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Workspace:
    """A directory outside the checkout and an installed CLI that cannot reach the network."""

    def __init__(self, entry_point: str, root: Path) -> None:
        self.entry_point = entry_point
        self.root = root
        self.guard = OfflineGuard(root)
        for name in ("fixtures", "snapshots", "results", "comparisons"):
            (root / name).mkdir()

    def path(self, *parts: str) -> Path:
        return self.root.joinpath(*parts)

    def run(self, *arguments: object, expect: int = 0):
        return run_installed(
            self.entry_point,
            self.root,
            *map(str, arguments),
            expect=expect,
            env=self.guard.env,
        )

    def json(self, *arguments: object, expect: int = 0):
        data = json.loads(self.run(*arguments, "--json", expect=expect).stdout)
        return data

    def both(self, *arguments: object) -> tuple[dict, str]:
        """The JSON result and the readable output of one read-only command."""
        return self.json(*arguments), self.run(*arguments).stdout


def journey(entry_point: str, root: Path) -> dict:
    """Run the whole journey; return the identifiers that must not depend on the entry point."""
    ws = Workspace(entry_point, root)
    summary: dict = {"snapshots": {}, "findings": {}, "comparisons": {}}

    # -- fixtures: generated AWS snapshots, and the raw IaC inputs copied out of the checkout
    for kind, scenario in SCENARIOS.items():
        save_snapshot(scenario.build(), ws.path("fixtures", f"{kind.value}.json"))
    shutil.copy(FIXTURES_ROOT / "terraform" / "plan.json", ws.path("fixtures", "plan.json"))
    shutil.copy(FIXTURES_ROOT / "cloudformation" / "template.yaml", ws.path("fixtures", "t.yaml"))
    left, right = build("aws", 1, seed=1), build("aws", 2, seed=2)
    save_snapshot(left, ws.path("fixtures", "left.json"))
    save_snapshot(right, ws.path("fixtures", "right.json"))
    inputs = {path: digest(path) for path in ws.path("fixtures").iterdir()}

    # -- ingest: JSON and readable runs agree, and a snapshot is never overwritten
    for kind in SCENARIOS:
        name, fixture = kind.value, ws.path("fixtures", f"{kind.value}.json")
        saved = ws.json("ingest", fixture, "-o", ws.path("snapshots", f"{name}.json"))
        readable = ws.run(
            "ingest", fixture, "-o", ws.path("snapshots", f"{name}-readable.json")
        ).stdout
        assert "Saved snapshot" in readable
        assert digest(ws.path("snapshots", f"{name}.json")) == digest(
            ws.path("snapshots", f"{name}-readable.json")
        )
        summary["snapshots"][name] = saved["snapshot_id"]
    for name in ("left", "right"):
        ws.json(
            "ingest",
            ws.path("fixtures", f"{name}.json"),
            "-o",
            ws.path("snapshots", f"{name}.json"),
        )
    ws.json(
        "ingest",
        ws.path("fixtures", "plan.json"),
        "-o",
        ws.path("snapshots", "plan.json"),
        "--format",
        "terraform-plan",
        *CONTEXT,
    )
    ws.json(
        "ingest",
        ws.path("fixtures", "t.yaml"),
        "-o",
        ws.path("snapshots", "template.json"),
        "--format",
        "cloudformation",
        "--stack-name",
        "demo",
        *CONTEXT,
    )
    ws.json(
        "ingest",
        ws.path("fixtures", "left.json"),
        "-o",
        ws.path("snapshots", "left.json"),
        expect=3,
    )

    # -- queries over the security-group snapshot
    sg = ws.path("snapshots", "security_group_impact.json")
    searched, text = ws.both("query", "search", "--snapshot", sg, "--id", GROUP)
    assert [item["id"] for item in searched["resources"]] == [GROUP] and GROUP in text
    inspected, text = ws.both("query", "inspect", GROUP, "--snapshot", sg)
    assert inspected["resource"]["id"] == GROUP and GROUP in text
    neighbours, text = ws.both(
        "query", "neighbours", GROUP, "--snapshot", sg, "--direction", "incoming"
    )
    assert neighbours["steps"] and GROUP in text
    paths, text = ws.both("query", "paths", IDS["lambda"], "--snapshot", sg, "--depth", "3")
    assert paths["paths"] and IDS["lambda"] in text

    # -- analyzers: save, show, verify, and refuse to overwrite
    for kind, scenario in SCENARIOS.items():
        snapshot_path = ws.path("snapshots", f"{kind.value}.json")
        result_path = ws.path("results", f"{kind.value}.json")
        options = [
            arg for name, value in scenario.parameters.items() for arg in (OPTIONS[name], value)
        ]
        arguments = ("--snapshot", snapshot_path, "--output", result_path, "--analyzer", kind.value)
        saved = ws.json("analyze", *arguments, *options)
        readable = ws.run(
            "analyze",
            *arguments[:3],
            ws.path("results", f"{kind.value}-readable.json"),
            *arguments[4:],
            *options,
        ).stdout
        assert "Saved analysis" in readable
        original = digest(result_path)
        ws.json("analyze", *arguments, *options, expect=3)
        assert digest(result_path) == original

        shown, text = ws.both("findings", result_path, "--snapshot", snapshot_path)
        assert shown["findings"] == saved["findings"] and shown["findings"]
        assert f"Findings: {len(shown['findings'])}" in text
        loaded = load_analysis(result_path, snapshot=load_snapshot(snapshot_path))
        assert loaded.analyzer == kind
        summary["findings"][kind.value] = sorted(item["id"] for item in shown["findings"])

        cited = next(item for item in shown["findings"] if item["evidence_ids"])
        evidence, text = ws.both("evidence", cited["evidence_ids"][0], "--snapshot", snapshot_path)
        assert evidence["evidence_id"] == cited["evidence_ids"][0]
        assert evidence["resource"]["id"] == evidence["evidence"]["resource_id"]

    # -- comparisons: observed pair, observed against a plan, and against a template
    pairs = {
        "pair": ("left", "right"),
        "plan": ("security_group_impact", "plan"),
        "template": ("security_group_impact", "template"),
    }
    for name, (first, second) in pairs.items():
        sides = (
            "--left",
            ws.path("snapshots", f"{first}.json"),
            "--right",
            ws.path("snapshots", f"{second}.json"),
        )
        output = ws.path("comparisons", f"{name}.json")
        saved = ws.json("compare", *sides, "--output", output)
        readable = ws.run(
            "compare", *sides, "--output", ws.path("comparisons", f"{name}-readable.json")
        ).stdout
        assert "Saved comparison" in readable and "not verified drift" in readable
        original = digest(output)
        ws.json("compare", *sides, "--output", output, expect=3)
        assert digest(output) == original
        shown, text = ws.both("comparison", output, *sides)
        for key in ("left", "right", "matched", "unmatched", "ambiguous", "limitations"):
            assert shown[key] == saved[key]
        assert f"Matched: {len(shown['matched'])}" in text
        result = load_comparison(
            output,
            left=load_snapshot(sides[1]),
            right=load_snapshot(sides[3]),
        )
        assert str(result.left.snapshot_id) == saved["left"]["snapshot_id"]
        summary["comparisons"][name] = {
            "matched": sorted(entry.id for entry in result.matched),
            "unmatched": sorted(entry.id for entry in result.unmatched),
        }

    # the observed pair cites evidence on both sides; inspect each in its own snapshot
    [pair] = json.loads(ws.path("comparisons", "pair.json").read_text())["matched"]
    [difference] = pair["differences"]
    for side, name in (("left", "left"), ("right", "right")):
        cited = difference[f"{side}_evidence_ids"][0]
        found = ws.json("evidence", cited, "--snapshot", ws.path("snapshots", f"{name}.json"))
        assert found["evidence_id"] == cited

    # nothing was modified, and nothing reached for the network
    assert {path: digest(path) for path in ws.path("fixtures").iterdir()} == inputs
    assert ws.guard.attempts == []
    return summary


@pytest.fixture(scope="module")
def summaries(tmp_path_factory):
    return {
        entry_point: journey(entry_point, tmp_path_factory.mktemp(entry_point))
        for entry_point in ENTRY_POINTS
    }


@pytest.mark.parametrize("entry_point", ENTRY_POINTS)
def test_the_journey_runs_through_every_stage(summaries, entry_point):
    summary = summaries[entry_point]
    assert set(summary["findings"]) == {kind.value for kind in AnalyzerKind}
    assert set(summary["comparisons"]) == {"pair", "plan", "template"}
    assert summary["comparisons"]["pair"]["matched"]


def test_identifiers_do_not_depend_on_the_entry_point(summaries):
    assert summaries["script"] == summaries["module"]


def test_the_offline_guard_blocks_network_use(tmp_path):
    guard = OfflineGuard(tmp_path)
    script = (
        "import socket\n"
        "for call in (lambda: socket.getaddrinfo('example.com', 80),\n"
        "             lambda: socket.create_connection(('127.0.0.1', 9))):\n"
        "    try:\n        call()\n    except OSError:\n        print('blocked')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], env=guard.env, capture_output=True, text=True, cwd=tmp_path
    )
    assert result.stdout.split() == ["blocked", "blocked"]
    assert len(guard.attempts) == 2
    assert str(PROJECT_ROOT) not in guard.env["PYTHONPATH"]


def test_the_offline_guard_opens_only_the_allowed_loopback_port(tmp_path):
    guard = OfflineGuard(tmp_path)
    with socket.create_server(("127.0.0.1", 0)) as server:
        port = server.getsockname()[1]
        script = (
            "import socket\n"
            "def raw():\n"
            f"    with socket.socket() as s:\n        s.connect(('127.0.0.1', {port}))\n"
            "for call in (\n"
            f"    lambda: socket.create_connection(('127.0.0.1', {port})).close(),\n"
            "    raw,\n"
            f"    lambda: socket.create_connection(('127.0.0.1', {port + 1})),\n"
            f"    lambda: socket.create_connection(('localhost', {port})),\n"
            "    lambda: socket.getaddrinfo('example.com', 443),\n"
            "):\n"
            "    try:\n        call()\n        print('open')\n"
            "    except OSError:\n        print('blocked')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            env=guard.allowing(port),
            capture_output=True,
            text=True,
            cwd=tmp_path,
        )
    assert result.stdout.split() == ["open", "open", "blocked", "blocked", "blocked"]
    assert len(guard.attempts) == 3
