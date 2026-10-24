"""README example calls and proposals, and their replies through an in-memory MCP client."""

import asyncio
import json
import subprocess
import threading
from itertools import count
from pathlib import Path
from typing import Any, Self

from mcp import Client

from archdiscov.assistant.mcp_server import build_server
from archdiscov.assistant.tools.session import ToolSession

type Call = tuple[str, dict[str, Any] | None]


def transcript_calls(path: Path) -> list[Call]:
    turns = json.loads(path.read_text(encoding="utf-8"))["turns"]
    return [
        (call["name"], call["arguments"]["value"])
        for turn in turns
        for call in turn["response"]["message"]["tool_calls"]
        if call["name"] != "submit_answer"
    ]


def mcp_replies(session: ToolSession, calls: list[Call]) -> list[tuple[bool, list[str]]]:
    """Each call's error flag and every text item, through one connection, in order."""

    async def run() -> list[tuple[bool, list[str]]]:
        async with Client(build_server(session)) as client:
            results = [await client.call_tool(name, arguments) for name, arguments in calls]
        return [(result.is_error, [item.text for item in result.content]) for result in results]

    return asyncio.run(run())


def submitted_proposals(path: Path) -> list[dict]:
    """The proposals a fake transcript submits, in order."""
    turns = json.loads(path.read_text(encoding="utf-8"))["turns"]
    return [
        call["arguments"]["value"]
        for turn in turns
        for call in turn["response"]["message"]["tool_calls"]
        if call["name"] == "submit_answer"
    ]


def readme_example(directory: Path) -> tuple[ToolSession, list[Call], dict]:
    """A README example's session, its tool calls, and the proposal it submits."""
    transcript = directory / "transcript.json"
    [proposal] = submitted_proposals(transcript)
    session = ToolSession.from_paths(directory / "snapshot.json")
    return session, transcript_calls(transcript), proposal


class StdioSession:
    """A real `archdiscov mcp` process, driven with raw JSON-RPC lines.

    Each request waits for its reply, so the server never has a request in flight when the
    client disconnects. Unlike the `mcp` package's client, it shows the process's exit code,
    standard error, and any standard output beyond the replies.
    """

    TIMEOUT_SECONDS = 60

    def __init__(self, command: list[str], cwd: Path, env: dict[str, str]) -> None:
        self.process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        # A server that stops answering fails the test instead of stalling the suite.
        self._watchdog = threading.Timer(self.TIMEOUT_SECONDS, self.process.kill)
        self._watchdog.daemon = True
        self._watchdog.start()
        self._ids = count(1)
        self.exit_code: int | None = None
        self.stderr = ""
        self.extra_output = ""

    def __enter__(self) -> Self:
        self.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "acceptance", "version": "1"},
            },
        )
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self

    def __exit__(self, *_: object) -> None:
        self.disconnect()

    def _send(self, message: dict) -> None:
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def request(self, method: str, params: dict | None = None) -> dict:
        """The result of one request; every reply must be that request's own, without error."""
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": next(self._ids), "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)
        reply = json.loads(self.process.stdout.readline())
        assert reply["jsonrpc"] == "2.0" and reply["id"] == message["id"], reply
        assert "error" not in reply, reply
        return reply["result"]

    def tool(self, name: str, arguments: dict | None = None) -> tuple[bool, list[str]]:
        """A tool call's error flag and every text item."""
        result = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        return result["isError"], [item["text"] for item in result["content"]]

    def disconnect(self) -> None:
        """Close the server's input and record how it ended."""
        if self.exit_code is not None:
            return
        self.extra_output, self.stderr = self.process.communicate(timeout=self.TIMEOUT_SECONDS)
        self.exit_code = self.process.returncode
        self._watchdog.cancel()
