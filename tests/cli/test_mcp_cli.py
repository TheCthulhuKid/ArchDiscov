"""The `mcp` command: diagnostics before serving, and a real stdio session.

Diagnostics and exit codes before serving run in process. A stdio session needs the
command's own standard input and output, so the protocol tests run the command in a
subprocess, with the `mcp` package's stdio client or raw JSON-RPC lines; nothing reaches a
network.
"""

import asyncio
import json
import os
import signal
import subprocess
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters
from typer.testing import CliRunner

from archdiscov.assistant.mcp_limits import DEFAULT_MAX_TOOL_CALLS, ServerLimits
from archdiscov.assistant.mcp_server import PRESENT_ANSWER
from archdiscov.assistant.questions import README_QUESTIONS
from archdiscov.cli import app
from archdiscov.services import mcp as mcp_service
from tests.support.evaluation_suites import README_SUITE
from tests.support.installed_cli import cli_command
from tests.support.mcp_clients import readme_example

runner = CliRunner()
README = README_SUITE.parent
OWNERSHIP = README / "ownership"
SNAPSHOT = OWNERSHIP / "snapshot.json"
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


# ---- before serving ----------------------------------------------------------------


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
        (lambda tmp: ["--snapshot", _invalid(tmp)], "invalid_snapshot", 2),
        (lambda tmp: _unselected(), "result_snapshot_unselected", 2),
    ],
    ids=["missing", "invalid", "unselected"],
)
def test_pre_serve_failures_are_readable_diagnostics_on_standard_error(
    tmp_path: Path, arguments, code: str, exit_code: int
) -> None:
    result = _invoke(*arguments(tmp_path))
    assert result.exit_code == exit_code, result.output
    assert result.stdout == ""
    assert result.stderr.startswith(f"Error [{code}]: ")


def _invalid(directory: Path) -> Path:
    path = directory / "invalid.json"
    path.write_text("{}", encoding="utf-8")
    return path


@pytest.mark.parametrize("value", ["0", "2001", "many"])
def test_an_out_of_range_cap_is_a_usage_error(value: str) -> None:
    result = _invoke("--snapshot", SNAPSHOT, "--max-tool-calls", value)
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


# ---- a real stdio session ----------------------------------------------------------


def _command(*options: object) -> list[str]:
    return [*cli_command("module"), "mcp", "--snapshot", str(SNAPSHOT), *map(str, options)]


def test_a_stdio_client_lists_prompts_and_answers_a_readme_example(tmp_path: Path) -> None:
    _, calls, proposal = readme_example(OWNERSHIP)
    command, *arguments = _command()
    parameters = StdioServerParameters(command=command, args=arguments, cwd=str(tmp_path))

    async def session() -> dict:
        async with Client(parameters) as client:
            tools = (await client.list_tools()).tools
            prompts = (await client.list_prompts()).prompts
            questions = [await client.get_prompt(prompt.name) for prompt in prompts]
            for name, call_arguments in calls:
                assert not (await client.call_tool(name, call_arguments)).is_error
            answer = await client.call_tool("submit_answer", proposal)
        return {"tools": tools, "prompts": prompts, "questions": questions, "answer": answer}

    seen = asyncio.run(session())
    assert seen["tools"][-1].name == "submit_answer"
    assert [prompt.name for prompt in seen["prompts"]] == [kind.value for kind in README_QUESTIONS]
    assert [result.messages[0].content.text for result in seen["questions"]] == list(
        README_QUESTIONS.values()
    )
    answer = seen["answer"]
    assert not answer.is_error and answer.content[0].text == PRESENT_ANSWER


def _start(tmp_path: Path) -> subprocess.Popen:
    return subprocess.Popen(
        _command(),
        cwd=tmp_path,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _send(process: subprocess.Popen, message: dict) -> None:
    process.stdin.write(json.dumps(message) + "\n")
    process.stdin.flush()


def test_a_disconnect_exits_0_with_only_protocol_messages_on_standard_output(
    tmp_path: Path,
) -> None:
    """Each request is answered before the next; `mcp` cancels requests still in flight when
    the client closes its side, so a client waits for its answers before disconnecting."""
    process = _start(tmp_path)
    requests = [
        INITIALIZE,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "prompts/list"},
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "run_analyzer", "arguments": {"kind": "ownership"}},
        },
    ]
    lines = []
    for request in requests:
        _send(process, request)
        lines.append(process.stdout.readline())
        if request is INITIALIZE:
            _send(process, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    stdout, stderr = process.communicate(timeout=30)
    assert process.returncode == 0, stderr
    messages = [json.loads(line) for line in [*lines, *stdout.splitlines()]]
    assert all(message["jsonrpc"] == "2.0" for message in messages)
    assert [message["id"] for message in messages] == [1, 2, 3, 4]
    assert all("result" in message for message in messages)
    assert stderr == ""


@pytest.mark.skipif(os.name != "posix", reason="sends SIGINT to the child")
def test_an_interrupt_of_a_waiting_server_exits_130(tmp_path: Path) -> None:
    process = _start(tmp_path)
    _send(process, INITIALIZE)
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
