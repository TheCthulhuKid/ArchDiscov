"""Scripted fake-provider turns and a run of the orchestrator over the network snapshot."""

import asyncio
from typing import Any

from archdiscov.assistant.fake import FakeClock, FakeProvider, FakeTranscript
from archdiscov.assistant.orchestrator import RunResult, run_assistant
from archdiscov.assistant.provider import (
    CompletionRequest,
    CompletionResponse,
    ProviderCapabilities,
    ProviderCapability,
)
from archdiscov.assistant.runs import RunLimits
from archdiscov.assistant.tools.registry import ToolRegistry
from archdiscov.assistant.tools.session import ToolSession
from tests.paths import FIXTURES_ROOT

SNAPSHOT = FIXTURES_ROOT / "snapshots" / "network.json"
QUESTION = "Which resources are in the network?"


def text_turn(text: str = "Done.", stop: str = "end", **turn: Any) -> dict:
    return {
        "kind": "respond",
        "response": {"message": {"role": "assistant", "text": text}, "stop_reason": stop},
        **turn,
    }


def calls_turn(*calls: tuple[str, str, Any], text: str | None = None, **turn: Any) -> dict:
    """Tool calls as (id, name, arguments); arguments of None are malformed."""
    message: dict[str, Any] = {
        "role": "assistant",
        "tool_calls": [
            {
                "id": call_id,
                "name": name,
                "arguments": {"kind": "malformed", "reason": "invalid_json"}
                if arguments is None
                else {"kind": "parsed", "value": arguments},
            }
            for call_id, name, arguments in calls
        ],
    }
    if text is not None:
        message["text"] = text
    return {
        "kind": "respond",
        "response": {"message": message, "stop_reason": "tool_use", "usage": {"output_tokens": 5}},
        **turn,
    }


def continued_turn(turn: dict, provider_kind: str = "fake", *blocks: dict) -> dict:
    """The scripted turn with a continuation on its message; one thinking block by default."""
    blocks = blocks or ({"type": "thinking", "thinking": "", "signature": "c2lnbmF0dXJl"},)
    message = {**turn["response"]["message"]}
    message["continuation"] = {"provider_kind": provider_kind, "blocks": list(blocks)}
    return {**turn, "response": {**turn["response"], "message": message}}


def fail_turn(code: str) -> dict:
    return {"kind": "fail", "error": code}


def run_turns(
    turns: list[dict],
    *,
    limits: RunLimits | None = None,
    registry: ToolRegistry | None = None,
    offer_answer_tool: bool = True,
    capabilities: list[str] | None = None,
    max_retries: int = 0,
    question: str = QUESTION,
) -> tuple[RunResult, FakeProvider, ToolSession]:
    data: dict[str, Any] = {"turns": turns}
    if capabilities is not None:
        data["capabilities"] = {"capabilities": capabilities}
    clock = FakeClock()
    provider = FakeProvider(
        FakeTranscript.model_validate(data), clock=clock, max_retries=max_retries
    )
    session = ToolSession.from_paths(SNAPSHOT)
    result = run_assistant(
        question,
        provider=provider,
        session=session,
        provider_kind="fake",
        model="fake",
        limits=limits,
        registry=registry,
        clock=clock.monotonic,
        offer_answer_tool=offer_answer_tool,
    )
    assert provider.remaining_turns == 0
    return result, provider, session


class HangingProvider:
    """Never answers in time; resists the first cancellation, like a call in a thread."""

    def __init__(self, *, answer_first: bool = False) -> None:
        self.answer_first = answer_first
        self.calls = 0
        self.entered = asyncio.Event()
        self.settled = False

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(capabilities={ProviderCapability.TOOL_CALLING})

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        raise AssertionError("the loop calls providers asynchronously")

    async def complete_async(self, request: CompletionRequest) -> CompletionResponse:
        self.calls += 1
        if self.answer_first and self.calls == 1:
            return CompletionResponse.model_validate(
                calls_turn(("c1", "search_resources", {}))["response"]
            )
        self.entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.01)  # finish the work in progress, then stop
            self.settled = True
            raise
        raise AssertionError("unreachable")
