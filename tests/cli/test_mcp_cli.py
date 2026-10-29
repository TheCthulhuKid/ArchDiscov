"""The `mcp` command: diagnostics before serving, the selection and cap it serves, and an
interrupt.

Diagnostics and exit codes before serving run in process. An interrupt of a waiting server
needs the command's own standard input, so that test runs the command in a subprocess, driven
with a raw JSON-RPC line; nothing reaches a network. The installed server answering over stdio
is `tests/acceptance/test_phase_five_acceptance.py`'s to show.
"""

import json
import os
import signal
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from archdiscov.assistant.mcp_limits import DEFAULT_MAX_TOOL_CALLS, ServerLimits
from archdiscov.cli import app
from archdiscov.services import mcp as mcp_service
from tests.support.evaluation_suites import README_SUITE
from tests.support.installed_cli import CLI

runner = CliRunner()
README = README_SUITE.parent
SNAPSHOT = README / "ownership" / "snapshot.json"
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    },
}


def _invoke(*arguments):
    return runner.invoke(app, ["mcp", *map(str, arguments)])


def _unselected() -> list:
    case = README / "ownership_compared"
    return [
        "--snapshot",
        SNAPSHOT,
        "--compare",
        README / "internet_exposure" / "snapshot.json",
        "--comparison-result",
        case / "comparison-result.json",
    ]


@pytest.mark.parametrize(
    ("arguments", "code", "exit_code"),
    [
        (lambda tmp: ["--snapshot", tmp / "missing.json"], "snapshot_io_error", 1),
        (lambda tmp: _unselected(), "result_snapshot_unselected", 2),
    ],
    ids=["missing", "unselected"],
)
def test_pre_serve_failures_are_readable_diagnostics_on_standard_error(
    tmp_path: Path, arguments, code: str, exit_code: int
) -> None:
    result = _invoke(*arguments(tmp_path))
    assert result.exit_code == exit_code, result.output
    assert result.stdout == ""
    assert result.stderr.startswith(f"Error [{code}]: ")


def test_an_out_of_range_cap_is_a_usage_error() -> None:
    result = _invoke("--snapshot", SNAPSHOT, "--max-tool-calls", "0")
    assert result.exit_code == 2
    assert result.stdout == ""


def test_the_selection_and_cap_reach_the_service(monkeypatch) -> None:
    served = []

    async def recording(session, limits=None) -> None:
        served.append((session, limits))

    monkeypatch.setattr(mcp_service, "serve_session", recording)
    assert _invoke("--snapshot", SNAPSHOT).exit_code == 0
    assert _invoke("--snapshot", SNAPSHOT, "--max-tool-calls", "7").exit_code == 0
    [(session, default), (_, capped)] = served
    assert default == ServerLimits(max_tool_calls=DEFAULT_MAX_TOOL_CALLS)
    assert capped == ServerLimits(max_tool_calls=7)
    assert session.index.snapshot_id


def test_an_interrupt_exits_130(monkeypatch) -> None:
    async def interrupted(session, limits=None) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(mcp_service, "serve_session", interrupted)
    result = _invoke("--snapshot", SNAPSHOT)
    assert result.exit_code == 130 and result.stdout == ""


@pytest.mark.skipif(os.name != "posix", reason="sends SIGINT to the child")
def test_an_interrupt_of_a_waiting_server_exits_130(tmp_path: Path) -> None:
    process = subprocess.Popen(
        [*CLI, "mcp", "--snapshot", str(SNAPSHOT)],
        cwd=tmp_path,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    process.stdin.write(json.dumps(INITIALIZE) + "\n")
    process.stdin.flush()
    assert json.loads(process.stdout.readline())["id"] == 1
    # The server is now waiting for the client's next line, and standard input stays open:
    # closing it would end the wait by itself.
    process.send_signal(signal.SIGINT)
    try:
        returncode = process.wait(timeout=30)
    finally:
        process.kill()
        process.communicate()
    assert returncode == 130
