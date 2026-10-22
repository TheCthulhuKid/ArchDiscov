"""Building the Anthropic adapter: only the configuration chooses its target and credential.

Requests go to a mock transport; nothing reaches the network.
"""

import asyncio
import logging
from pathlib import Path

import anthropic
import httpx2
import pytest
from pydantic import SecretStr

from archdiscov.assistant import anthropic_provider
from archdiscov.assistant.anthropic_provider import DEFAULT_BASE_URL, AnthropicProvider
from archdiscov.assistant.config import (
    ProviderConfigError,
    ProviderConfigErrorCode,
    parse_provider_config,
)
from archdiscov.assistant.provider import (
    CompletionRequest,
    ProviderCapability,
    ProviderUnavailableError,
    complete,
)
from archdiscov.assistant.providers import load_provider
from tests.assistant.test_config import ANTHROPIC
from tests.assistant.test_secret_leaks import SECRET, _renderings

ENDPOINT = "https://gateway.example.com/anthropic"
QUESTION = "Which databases are reachable?"
MESSAGE = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "claude-sonnet-5-5",
    "content": [{"type": "text", "text": "None."}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 3, "output_tokens": 1},
}
HOSTILE_ENVIRONMENT = {
    "ANTHROPIC_API_KEY": "sk-ant-api03-environmentKEY0123456789abcdef",
    "ANTHROPIC_AUTH_TOKEN": "environment-token",
    "ANTHROPIC_BASE_URL": "https://elsewhere.example.com",
    "ANTHROPIC_CUSTOM_HEADERS": "Authorization: Bearer injected\nX-Injected: 1",
    "ANTHROPIC_PROFILE": "someone-else",
    "HTTPS_PROXY": "http://proxy.invalid:3128",
    "ALL_PROXY": "http://proxy.invalid:3128",
}
SDK_HEADERS = {
    "accept",
    "accept-encoding",
    "anthropic-version",
    "authorization",
    "connection",
    "content-length",
    "content-type",
    "host",
    "user-agent",
}


class Recorder(httpx2.MockTransport):
    """A mock API that answers every request with `response` and keeps what it was sent."""

    def __init__(self, response: httpx2.Response | None = None) -> None:
        self.requests: list[httpx2.Request] = []
        super().__init__(self._handle)
        self.response = response or httpx2.Response(200, json=MESSAGE)

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self.response


def _provider(recorder: Recorder, endpoint: str | None = ENDPOINT) -> AnthropicProvider:
    data = ANTHROPIC if endpoint is None else {**ANTHROPIC, "endpoint": endpoint}
    return AnthropicProvider(
        parse_provider_config(data),
        SecretStr(SECRET),
        transport=recorder,
        async_transport=recorder,
    )


def _send(provider: AnthropicProvider, mode: str) -> None:
    params = {
        "model": provider.model,
        "max_tokens": 16,
        "messages": [{"role": "user", "content": QUESTION}],
    }
    if mode == "sync":
        provider._create(**params)
    else:
        asyncio.run(provider._create_async(**params))


def _config(tmp_path: Path, extra: str = "") -> Path:
    path = tmp_path / "provider.toml"
    path.write_text(
        f'kind = "anthropic"\nmodel = "claude-sonnet-5-5"\napi_key_env = "ASK_KEY"\n{extra}',
        encoding="utf-8",
    )
    return path


def test_a_configuration_builds_the_adapter_without_a_request(tmp_path: Path) -> None:
    config, provider = load_provider(
        _config(tmp_path, f'endpoint = "{ENDPOINT}"\n'), environ={"ASK_KEY": SECRET}
    )
    assert isinstance(provider, AnthropicProvider)
    assert (provider.model, provider.base_url) == (config.model, ENDPOINT)
    assert provider.capabilities.capabilities == {
        ProviderCapability.TOOL_CALLING,
        ProviderCapability.PARALLEL_TOOL_CALLS,
    }
    _, default = load_provider(_config(tmp_path), environ={"ASK_KEY": SECRET})
    assert default.base_url == DEFAULT_BASE_URL


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

    monkeypatch.setattr(anthropic_provider, "AnthropicProvider", built)
    with pytest.raises(ProviderConfigError) as raised:
        load_provider(_config(tmp_path), environ=environ)
    assert raised.value.code == code and raised.value.variable == "ASK_KEY"


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("endpoint", [ENDPOINT, None], ids=["endpoint", "default"])
def test_only_the_configuration_chooses_the_target_and_credential(
    monkeypatch, mode: str, endpoint: str | None
) -> None:
    for name, value in HOSTILE_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    recorder = Recorder()
    _send(_provider(recorder, endpoint), mode)
    [request] = recorder.requests
    assert str(request.url) == f"{endpoint or DEFAULT_BASE_URL}/v1/messages"
    assert request.headers["authorization"] == f"Bearer {SECRET}"
    assert {name for name in request.headers if not name.startswith("x-stainless-")} == (
        SDK_HEADERS
    )


def test_redirects_are_not_followed() -> None:
    elsewhere = httpx2.Response(307, headers={"location": "https://elsewhere.example.com/v1"})
    recorder = Recorder(elsewhere)
    with pytest.raises(anthropic.APIStatusError):
        _send(_provider(recorder), "sync")
    assert [request.url.host for request in recorder.requests] == ["gateway.example.com"]


def test_requests_wait_for_the_mapping_and_are_never_sent() -> None:
    recorder = Recorder()
    provider = _provider(recorder)
    request = CompletionRequest.model_validate(
        {"conversation": {"messages": [{"role": "user", "text": QUESTION}]}}
    )
    with pytest.raises(ProviderUnavailableError):
        complete(provider, request)
    with pytest.raises(ProviderUnavailableError):
        asyncio.run(provider.complete_async(request))
    assert recorder.requests == []


def test_no_key_or_request_body_reaches_a_rendering_or_log(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    for name in anthropic_provider._SDK_LOGGERS:
        logging.getLogger(name).setLevel(logging.DEBUG)
    recorder = Recorder(httpx2.Response(400, json={"type": "error", "error": {"type": "x"}}))
    provider = _provider(recorder)
    with pytest.raises(anthropic.BadRequestError) as raised:
        _send(provider, "sync")
    for name in anthropic_provider._SDK_LOGGERS:
        assert logging.getLogger(name).getEffectiveLevel() == logging.WARNING
    for rendering in (*_renderings(provider), caplog.text):
        assert SECRET not in rendering and QUESTION not in rendering
    assert SECRET not in "".join(_renderings(raised.value))
