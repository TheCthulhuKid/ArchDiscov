"""The OpenAI-compatible adapter over a mocked API, and the requests the OpenAI tests send."""

import asyncio
import json
from typing import Any

import httpx2
from pydantic import SecretStr

from archdiscov.assistant.config import OpenAIProtocol, parse_provider_config
from archdiscov.assistant.conversation import Conversation
from archdiscov.assistant.openai_provider import OpenAIProvider
from archdiscov.assistant.provider import (
    CompletionRequest,
    CompletionResponse,
    RequestLimits,
    complete,
)
from tests.support.mock_api import MockApi, Waits
from tests.support.provider_data import OPENAI, SEARCH_TOOL
from tests.support.secrets import SECRET

ENDPOINT = "https://gateway.example.com/v1"
QUESTION = "Which databases are reachable?"
PARALLEL = ["tool_calling", "parallel_tool_calls"]


def openai_adapter(
    api: MockApi,
    protocol: OpenAIProtocol = OpenAIProtocol.RESPONSES,
    *,
    waits: Waits | None = None,
    **fields: Any,
) -> OpenAIProvider:
    """The adapter over `api`, without retries unless `fields` asks for them."""
    config = parse_provider_config(
        {**OPENAI, "endpoint": ENDPOINT, "protocol": protocol.value, "max_retries": 0, **fields}
    )
    waits = Waits() if waits is None else waits
    return OpenAIProvider(
        config,
        SecretStr(SECRET),
        transport=api,
        async_transport=api,
        sleep=waits.sleep,
        async_sleep=waits.sleep_async,
    )


def openai_request(*messages: dict, tools=(SEARCH_TOOL,), **limits: Any) -> CompletionRequest:
    return CompletionRequest(
        conversation=Conversation.model_validate(
            {"messages": [{"role": "user", "text": QUESTION}, *messages]}
        ),
        tools=tools,
        limits=RequestLimits(**limits),
    )


def responding_api(*bodies: dict) -> MockApi:
    return MockApi([httpx2.Response(200, json=body) for body in bodies])


def send_request(provider: OpenAIProvider, mode: str = "sync", request=None) -> CompletionResponse:
    request = request or openai_request()
    if mode == "sync":
        return complete(provider, request)
    return asyncio.run(provider.complete_async(request))


def sent_body(api: MockApi, index: int = 0) -> dict[str, Any]:
    return json.loads(api.requests[index].content)
