"""Serving a selection over MCP: verified before anything is served, then served until the
client closes.

Serving runs over in-memory streams in place of stdio, so no subprocess or network is used;
the command's real stdio session is tested with the command.
"""

import asyncio
import json
import logging
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from archdiscov.assistant.mcp_limits import ServerLimits
from archdiscov.assistant.mcp_server import PRESENT_ANSWER
from archdiscov.assistant.tools.session import ToolSession
from archdiscov.services import mcp as mcp_service
from tests.assistant.test_mcp_server import _example
from tests.paths import PROJECT_ROOT
from tests.support.evaluation_suites import README_SUITE

README = README_SUITE.parent

# Each selection fails while it is verified, before a server is built or `mcp` imported.
PRE_SERVE_SCRIPT = f"""
import sys
from pathlib import Path

from archdiscov.assistant.tools.session import ToolSessionError
from archdiscov.services.mcp import serve_selection
from archdiscov.services.snapshot_errors import SnapshotIOError, SnapshotValidationError

readme = Path({str(README)!r})
invalid = Path(sys.argv[1])
invalid.write_text("{{}}", encoding="utf-8")
cases = [
    (SnapshotIOError, dict(snapshot_path=readme / "missing.json")),
    (SnapshotValidationError, dict(snapshot_path=invalid)),
    (
        ToolSessionError,
        dict(
            snapshot_path=readme / "ownership" / "snapshot.json",
            comparison_path=readme / "internet_exposure" / "snapshot.json",
            comparison_result_paths=[readme / "ownership_compared" / "comparison-result.json"],
        ),
    ),
]
for error, selection in cases:
    try:
        serve_selection(**selection)
    except error:
        pass
    else:
        raise AssertionError(error)
assert not any(name == "mcp" or name.startswith("mcp.") for name in sys.modules)
"""


def test_an_unusable_selection_fails_before_anything_is_served(tmp_path: Path) -> None:
    completed = subprocess.run(
        [sys.executable, "-c", PRE_SERVE_SCRIPT, str(tmp_path / "invalid.json")],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.fixture
def verbose_mcp_logger():
    """The `mcp` logger at DEBUG, as a client's environment might set it; restored after."""
    logger = logging.getLogger("mcp")
    level = logger.level
    logger.setLevel(logging.DEBUG)
    yield
    logger.setLevel(level)


def test_a_session_is_served_until_the_client_closes(verbose_mcp_logger) -> None:
    session, calls, proposal = _example(README / "ownership")

    async def scenario() -> tuple[list[str], int]:
        async with create_client_server_memory_streams() as (client_streams, server_streams):

            @asynccontextmanager
            async def transport():
                yield server_streams

            serving = asyncio.create_task(mcp_service.serve_session(session, transport=transport))
            read, write = client_streams
            async with ClientSession(read, write) as client:
                await client.initialize()
                level = logging.getLogger("mcp").getEffectiveLevel()
                for name, arguments in calls:
                    assert not (await client.call_tool(name, arguments)).is_error
                result = await client.call_tool("submit_answer", proposal)
            await write.aclose()
            # Serving returns once the client has closed its side.
            await asyncio.wait_for(serving, 5)
        return [item.text for item in result.content], level

    texts, level = asyncio.run(scenario())
    present, readable, data = texts
    assert present == PRESENT_ANSWER
    assert readable.startswith("Answer: answered, exit code 0")
    assert json.loads(data)["answer"]["outcome"]["status"] == "answered"
    assert level == logging.WARNING


def test_serve_selection_serves_the_verified_selection_with_its_limits(monkeypatch) -> None:
    served = []

    async def recording(session: ToolSession, limits: ServerLimits | None = None) -> None:
        served.append((session, limits))

    monkeypatch.setattr(mcp_service, "serve_session", recording)
    limits = ServerLimits(max_tool_calls=5)
    case = README / "ownership_compared"
    mcp_service.serve_selection(
        case / "snapshot.json",
        comparison_path=case / "comparison.json",
        comparison_result_paths=[case / "comparison-result.json"],
        limits=limits,
    )
    [(session, given)] = served
    assert given == limits
    assert session.comparison_index is not None and len(session.comparisons) == 1


def test_an_interrupt_reaches_the_caller(monkeypatch) -> None:
    async def interrupted(session: ToolSession, limits: ServerLimits | None = None) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(mcp_service, "serve_session", interrupted)
    with pytest.raises(KeyboardInterrupt):
        mcp_service.serve_selection(README / "ownership" / "snapshot.json")
