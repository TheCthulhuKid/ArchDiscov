"""One credential-free, network-free journey through the installed CLI, outside the checkout.

fixture -> ingest -> queries -> each analyzer -> saved results -> findings -> evidence ->
a comparison -> comparison display, through the installed script, one test per stage. Every
stage starts from the same fixtures, written in process once per worker: the ingest stage shows
that the installed `ingest` saves each snapshot fixture byte for byte, so the later stages read
what it would have saved, and the stages run in parallel instead of one long chain. The child
sees no credentials and no checkout: its whole `PYTHONPATH` is the offline guard, which fails
and records any network use. Readable output is left to `tests/cli`, which renders every
command's readable form in process; raw IaC imports are `test_raw_ingestion_acceptance.py`'s,
and no overwrite is `test_offline_acceptance.py`'s.
"""

import socket
import subprocess
import sys

import pytest

from archdiscov.services import (
    load_analysis,
    load_comparison,
    load_snapshot,
    save_snapshot,
)
from tests.paths import PROJECT_ROOT
from tests.support.audit_scenarios import SCENARIOS
from tests.support.comparison_data import journey_snapshot
from tests.support.installed_cli import Workspace, digest
from tests.support.offline_guard import OfflineGuard
from tests.support.security_group_data import IDS

OPTIONS = {"resource_id": "--resource-id", "port": "--port", "max_depth": "--max-depth"}
GROUP = IDS["sg-app"]


@pytest.fixture(scope="module")
def ws(tmp_path_factory) -> Workspace:
    """The journey's fixtures in `fixtures/`: one per analyzer scenario, and a pair to compare."""
    ws = Workspace(tmp_path_factory.mktemp("journey"))
    for kind, scenario in SCENARIOS.items():
        save_snapshot(scenario.build(), ws.path("fixtures", f"{kind.value}.json"))
    save_snapshot(journey_snapshot("aws", 1, seed=1), ws.path("fixtures", "left.json"))
    save_snapshot(journey_snapshot("aws", 2, seed=2), ws.path("fixtures", "right.json"))
    return ws


def snapshot(ws: Workspace, name: str):
    """A stage's input: a snapshot fixture as ingest saves it."""
    return ws.path("fixtures", f"{name}.json")


def test_ingest_saves_each_fixture_unchanged(ws, tmp_path):
    inputs = {path: digest(path) for path in ws.path("fixtures").iterdir()}
    for kind in SCENARIOS:
        fixture = ws.path("fixtures", f"{kind.value}.json")
        output = tmp_path / fixture.name
        saved = ws.json("ingest", fixture, "-o", output)
        assert digest(output) == inputs[fixture]
        assert saved["snapshot_id"] == str(load_snapshot(output).snapshot_id)
    assert {path: digest(path) for path in ws.path("fixtures").iterdir()} == inputs
    assert ws.guard.attempts == []


def test_queries_find_inspect_and_follow_a_resource(ws):
    sg = snapshot(ws, "security_group_impact")
    searched = ws.json("query", "search", "--snapshot", sg, "--id", GROUP)
    assert [item["id"] for item in searched["resources"]] == [GROUP]
    assert ws.json("query", "inspect", GROUP, "--snapshot", sg)["resource"]["id"] == GROUP
    neighbours = ws.json("query", "neighbours", GROUP, "--snapshot", sg, "--direction", "incoming")
    assert neighbours["steps"]
    assert ws.json("query", "paths", IDS["lambda"], "--snapshot", sg, "--depth", "3")["paths"]
    assert ws.guard.attempts == []


@pytest.mark.parametrize("kind", SCENARIOS, ids=lambda kind: kind.value)
def test_each_analyzer_saves_findings_that_cite_evidence(ws, kind):
    scenario = SCENARIOS[kind]
    snapshot_path = snapshot(ws, kind.value)
    result_path = ws.path("results", f"{kind.value}.json")
    options = [arg for name, value in scenario.parameters.items() for arg in (OPTIONS[name], value)]
    saved = ws.json(
        "analyze",
        "--snapshot",
        snapshot_path,
        "--output",
        result_path,
        "--analyzer",
        kind.value,
        *options,
    )
    shown = ws.json("findings", result_path, "--snapshot", snapshot_path)
    assert shown["findings"] == saved["findings"] and shown["findings"]
    assert load_analysis(result_path, snapshot=load_snapshot(snapshot_path)).analyzer == kind

    cited = next(item for item in shown["findings"] if item["evidence_ids"])
    evidence = ws.json("evidence", cited["evidence_ids"][0], "--snapshot", snapshot_path)
    assert evidence["evidence_id"] == cited["evidence_ids"][0]
    assert evidence["resource"]["id"] == evidence["evidence"]["resource_id"]
    assert ws.guard.attempts == []


def test_a_comparison_is_saved_and_shown_as_saved(ws):
    first, second = snapshot(ws, "left"), snapshot(ws, "right")
    sides = ("--left", first, "--right", second)
    output = ws.path("comparisons", "pair.json")
    saved = ws.json("compare", *sides, "--output", output)
    shown = ws.json("comparison", output, *sides)
    for key in ("left", "right", "matched", "unmatched", "ambiguous", "limitations"):
        assert shown[key] == saved[key]
    result = load_comparison(output, left=load_snapshot(first), right=load_snapshot(second))
    assert str(result.left.snapshot_id) == saved["left"]["snapshot_id"]
    # The pair cites evidence on both sides; inspect each in its own snapshot.
    [pair] = result.matched
    [difference] = pair.differences
    for cited, path in (
        (difference.left_evidence_ids, first),
        (difference.right_evidence_ids, second),
    ):
        assert ws.json("evidence", cited[0], "--snapshot", path)["evidence_id"] == cited[0]
    assert ws.guard.attempts == []


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


def test_the_file_watch_logs_opens_under_watched_directories_only(tmp_path):
    guard = OfflineGuard(tmp_path)
    watched, other = tmp_path / "watched", tmp_path / "other"
    for directory in (watched, other):
        directory.mkdir()
        (directory / "a.json").write_text("{}", encoding="utf-8")
    (tmp_path / "link").symlink_to(watched)
    script = (
        "import os, pathlib\n"
        f"pathlib.Path({str(watched / 'a.json')!r}).read_text()\n"
        f"open({str(other / 'a.json')!r}).close()\n"
        f"open({str(tmp_path / 'link' / 'a.json')!r}).close()\n"
        f"os.close(os.open({str(watched / 'b.json')!r}, os.O_WRONLY | os.O_CREAT))\n"
        "import socket\n"
        "try:\n    socket.create_connection(('127.0.0.1', 9))\nexcept OSError:\n    pass\n"
    )

    def run(env):
        subprocess.run([sys.executable, "-c", script], env=env, cwd=tmp_path, check=True)

    run(guard.env)
    assert guard.opened_files == [], "the watch is opt-in"
    run(guard.watching(watched))
    # A file reached through a symbolic link is logged as its real path, and the guard's own
    # logs, which record the refused connection, are not watched.
    real = watched.resolve()
    assert guard.opened_files == [real / "a.json", real / "b.json"]
    assert len(guard.attempts) == 2
