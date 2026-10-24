"""README example calls and proposals, and their replies through an in-memory MCP client."""

import asyncio
import json
from pathlib import Path
from typing import Any

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


def readme_example(directory: Path) -> tuple[ToolSession, list[Call], dict]:
    """A README example's session, its tool calls, and the proposal it submits."""
    transcript = directory / "transcript.json"
    turns = json.loads(transcript.read_text(encoding="utf-8"))["turns"]
    [proposal] = [
        call["arguments"]["value"]
        for turn in turns
        for call in turn["response"]["message"]["tool_calls"]
        if call["name"] == "submit_answer"
    ]
    session = ToolSession.from_paths(directory / "snapshot.json")
    return session, transcript_calls(transcript), proposal
