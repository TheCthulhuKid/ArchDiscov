"""The OpenAI-compatible adapter: built only from its configuration, for either protocol.

Requests go to a mocked API; nothing reaches the network.
"""

import asyncio
import logging
from functools import partial
from pathlib import Path
from typing import Any

import httpx2
import openai
import pytest
from pydantic import SecretStr

from archdiscov.assistant import openai_provider
from archdiscov.assistant.config import (
    OpenAIProtocol,
    ProviderConfigError,
    ProviderConfigErrorCode,
    parse_provider_config,
)
from archdiscov.assistant.conversation import Conversation
from archdiscov.assistant.openai_provider import OpenAIProvider
from archdiscov.assistant.provider import (
    CompletionRequest,
    ProviderCapability,
    ProviderErrorCode,
    ProviderUnavailableError,
    UnsupportedCapabilityError,
    complete,
)
from archdiscov.assistant.providers import load_provider
from archdiscov.services.ask import ask_question
from tests.assistant.test_config import OPENAI
from tests.assistant.test_orchestrator import SNAPSHOT
from tests.assistant.test_provider_contract import SEARCH
from tests.assistant.test_secret_leaks import SECRET, _renderings
from tests.support.mock_api import MockApi

ENDPOINT = "https://gateway.example.com/v1"
QUESTION = "Which databases are reachable?"
PROTOCOLS = list(OpenAIProtocol)
HOSTILE_ENVIRONMENT = {
    "OPENAI_API_KEY": "sk-proj-environmentKEY0123456789abcdef",
    "OPENAI_ADMIN_KEY": "sk-admin-environmentKEY0123456789abcdef",
    "OPENAI_BASE_URL": "https://elsewhere.example.com/v1",
    "OPENAI_ORG_ID": "org-someone-else",
    "OPENAI_PROJECT_ID": "proj-someone-else",
    "OPENAI_CUSTOM_HEADERS": "Authorization: Bearer injected\nX-Injected: 1",
    "OPENAI_LOG": "debug",
    "HTTPS_PROXY": "http://proxy.invalid:3128",
    "ALL_PROXY": "http://proxy.invalid:3128",
}
SDK_HEADERS = {
    "accept",
    "accept-encoding",
    "authorization",
    "connection",
    "content-length",
    "content-type",
    "host",
    "user-agent",
}
# The smallest request each protocol accepts; the mappings (steps 41.3 and 41.4) build more.
PARAMS: dict[OpenAIProtocol, dict[str, Any]] = {
    OpenAIProtocol.CHAT_COMPLETIONS: {
        "model": "gpt-5",
        "messages": [{"role": "user", "content": QUESTION}],
    },
    OpenAIProtocol.RESPONSES: {"model": "gpt-5", "input": QUESTION},
}
PATHS = {
    OpenAIProtocol.CHAT_COMPLETIONS: "/chat/completions",
    OpenAIProtocol.RESPONSES: "/responses",
}


def _rejected() -> httpx2.Response:
    return httpx2.Response(400, json={"error": {"message": "Rejected.", "type": "invalid"}})


def _provider(
    api: MockApi, protocol: OpenAIProtocol = OpenAIProtocol.RESPONSES, **fields: Any
) -> OpenAIProvider:
    config = parse_provider_config(
        {**OPENAI, "endpoint": ENDPOINT, "protocol": protocol.value, **fields}
    )
    return OpenAIProvider(config, SecretStr(SECRET), transport=api, async_transport=api)


def _create(provider: OpenAIProvider, mode: str = "sync") -> None:
    """Send the protocol's smallest request; the mocked API refuses it."""
    params = PARAMS[provider.protocol]
    with pytest.raises(openai.BadRequestError):
        if mode == "sync":
            provider._create(**params)
        else:
            asyncio.run(provider._create_async(**params))


def _request(**limits: Any) -> CompletionRequest:
    return CompletionRequest(
        conversation=Conversation.model_validate({"messages": [{"role": "user", "text": "q"}]}),
        tools=(SEARCH,),
        limits=limits or {},
    )


def _config(tmp_path: Path, protocol: OpenAIProtocol, extra: str = "") -> Path:
    path = tmp_path / "provider.toml"
    path.write_text(
        f'kind = "openai_compatible"\nmodel = "gpt-5"\napi_key_env = "ASK_KEY"\n'
        f'endpoint = "{ENDPOINT}"\nprotocol = "{protocol.value}"\n{extra}',
        encoding="utf-8",
    )
    return path


# ---- construction ------------------------------------------------------------------


@pytest.mark.parametrize("protocol", PROTOCOLS, ids=str)
def test_a_configuration_builds_the_adapter_without_a_request(
    tmp_path: Path, protocol: OpenAIProtocol
) -> None:
    config, provider = load_provider(_config(tmp_path, protocol), environ={"ASK_KEY": SECRET})
    assert isinstance(provider, OpenAIProvider)
    assert (provider.model, provider.base_url, provider.protocol) == (
        config.model,
        ENDPOINT,
        protocol,
    )
    assert provider.capabilities.capabilities == {ProviderCapability.TOOL_CALLING}


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        ("[]", set()),
        ('["tool_calling"]', {ProviderCapability.TOOL_CALLING}),
        (
            '["tool_calling", "parallel_tool_calls"]',
            {ProviderCapability.TOOL_CALLING, ProviderCapability.PARALLEL_TOOL_CALLS},
        ),
    ],
    ids=["none", "tools", "parallel"],
)
def test_the_adapter_declares_the_configured_capabilities(
    tmp_path: Path, declared: str, expected: set
) -> None:
    path = _config(tmp_path, OpenAIProtocol.RESPONSES, f"capabilities = {declared}\n")
    _, provider = load_provider(path, environ={"ASK_KEY": SECRET})
    assert provider.capabilities.capabilities == expected


@pytest.mark.parametrize(
    ("environ", "code"),
    [
        ({}, ProviderConfigErrorCode.CREDENTIAL_MISSING),
        ({"ASK_KEY": " "}, ProviderConfigErrorCode.CREDENTIAL_EMPTY),
    ],
    ids=["missing", "empty"],
)
def test_an_unusable_key_fails_before_the_adapter_exists(
    tmp_path: Path, monkeypatch, environ, code
) -> None:
    def built(*args, **kwargs):
        raise AssertionError("the adapter was built")

    monkeypatch.setattr(openai_provider, "OpenAIProvider", built)
    with pytest.raises(ProviderConfigError) as raised:
        load_provider(_config(tmp_path, OpenAIProtocol.RESPONSES), environ=environ)
    assert raised.value.code == code and raised.value.variable == "ASK_KEY"


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("protocol", PROTOCOLS, ids=str)
def test_only_the_configuration_chooses_the_target_and_credential(
    monkeypatch, protocol: OpenAIProtocol, mode: str
) -> None:
    for name, value in HOSTILE_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    api = MockApi([_rejected()])
    _create(_provider(api, protocol), mode)
    [request] = api.requests
    assert str(request.url) == f"{ENDPOINT}{PATHS[protocol]}"
    assert request.headers["authorization"] == f"Bearer {SECRET}"
    assert {name for name in request.headers if not name.startswith("x-stainless-")} == (
        SDK_HEADERS
    )
    body = request.content.decode()
    assert not any(value in body for value in HOSTILE_ENVIRONMENT.values())


def test_redirects_are_not_followed() -> None:
    elsewhere = httpx2.Response(307, headers={"location": "https://elsewhere.example.com/v1"})
    api = MockApi([elsewhere])
    with pytest.raises(openai.APIStatusError):
        _provider(api)._create(**PARAMS[OpenAIProtocol.RESPONSES])
    assert [request.url.host for request in api.requests] == ["gateway.example.com"]


def test_no_key_or_request_body_reaches_a_rendering_or_log(monkeypatch, caplog) -> None:
    monkeypatch.setenv("OPENAI_LOG", "debug")
    caplog.set_level(logging.DEBUG)
    for name in openai_provider._SDK_LOGGERS:
        logging.getLogger(name).setLevel(logging.DEBUG)
    provider = _provider(MockApi([_rejected()]))
    _create(provider)
    for name in openai_provider._SDK_LOGGERS:
        assert logging.getLogger(name).getEffectiveLevel() == logging.WARNING
    for rendering in (*_renderings(provider), caplog.text):
        assert SECRET not in rendering and QUESTION not in rendering


# ---- capabilities ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("declared", "limits", "missing"),
    [
        ([], {}, ProviderCapability.TOOL_CALLING),
        (["tool_calling"], {"max_tool_calls_per_turn": 2}, ProviderCapability.PARALLEL_TOOL_CALLS),
    ],
    ids=["no-tools", "no-parallel-calls"],
)
def test_an_undeclared_capability_is_refused_before_any_request(
    declared: list[str], limits: dict, missing: ProviderCapability
) -> None:
    api = MockApi([])
    with pytest.raises(UnsupportedCapabilityError) as raised:
        complete(_provider(api, capabilities=declared), _request(**limits))
    assert raised.value.capability is missing
    assert api.requests == []


def test_an_ask_run_without_declared_tool_calling_fails_before_any_request(
    tmp_path: Path, monkeypatch
) -> None:
    api = MockApi([])
    monkeypatch.setenv("ASK_KEY", SECRET)
    monkeypatch.setattr(
        openai_provider,
        "OpenAIProvider",
        partial(OpenAIProvider, transport=api, async_transport=api),
    )
    result = ask_question(
        QUESTION,
        snapshot_path=SNAPSHOT,
        config_path=_config(tmp_path, OpenAIProtocol.CHAT_COMPLETIONS, "capabilities = []\n"),
    )
    outcome = result.answer.outcome
    assert outcome.provider_error == ProviderErrorCode.UNSUPPORTED_CAPABILITY
    assert outcome.exit_code == 6
    assert api.requests == []


# ---- until the mappings ------------------------------------------------------------


@pytest.mark.parametrize("protocol", PROTOCOLS, ids=str)
def test_completing_sends_nothing_until_the_protocol_is_mapped(protocol: OpenAIProtocol) -> None:
    api = MockApi([])
    provider = _provider(api, protocol)
    with pytest.raises(ProviderUnavailableError):
        complete(provider, _request())
    with pytest.raises(ProviderUnavailableError):
        asyncio.run(provider.complete_async(_request()))
    assert api.requests == []
