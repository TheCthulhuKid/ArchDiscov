"""The Bedrock adapter: built only from its own AWS profile, region, and role.

Requests go to a mocked API through botocore's `before-send` event, or to a loopback server;
nothing reaches AWS. Each test writes its own AWS profile files.
"""

import logging
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from archdiscov.assistant import bedrock_provider
from archdiscov.assistant.bedrock_provider import BedrockProvider
from archdiscov.assistant.config import (
    parse_provider_config,
)
from archdiscov.assistant.conversation import Conversation
from archdiscov.assistant.provider import (
    CompletionRequest,
    ProviderAuthenticationError,
    ProviderCapability,
    ProviderUnavailableError,
    complete,
)
from archdiscov.assistant.providers import load_provider
from tests.support.ask_examples import write_provider_config
from tests.support.bedrock_exchanges import (
    ANSWER,
    MODEL,
    PROFILE,
    QUESTION,
    REGION,
    ROLE,
    ROLE_KEY,
    ROLE_SECRET,
    assumed_role_response,
    bedrock_fields,
    signing_key,
)
from tests.support.mock_api import AwsResponse, BotocoreApi, mock_api_server
from tests.support.provider_data import SEARCH_TOOL
from tests.support.secrets import secret_renderings

ACCESS_KEY = "AKIAPROFILEKEY000000"
SECRET_KEY = "profileSecretKey0000000000000000000000000"
# Botocore percent-encodes the model ID in the path.
CONVERSE_PATH = f"/model/{MODEL.replace(':', '%3A')}/converse"
CONVERSE = f"https://bedrock-runtime.{REGION}.amazonaws.com{CONVERSE_PATH}"
HOSTILE_ENVIRONMENT = {
    "AWS_ACCESS_KEY_ID": "AKIAENVIRONMENT00000",
    "AWS_SECRET_ACCESS_KEY": "environmentSecret000000000000000000000000",
    "AWS_SESSION_TOKEN": "environmentToken",
    "AWS_PROFILE": "someone-else",
    "AWS_DEFAULT_REGION": "us-east-2",
    "AWS_ENDPOINT_URL": "https://elsewhere.example.com",
    "AWS_ENDPOINT_URL_BEDROCK_RUNTIME": "https://bedrock.elsewhere.example.com",
    "AWS_MAX_ATTEMPTS": "9",
    "AWS_RETRY_MODE": "adaptive",
    "AWS_USE_FIPS_ENDPOINT": "true",
    "AWS_USE_DUALSTACK_ENDPOINT": "true",
    "HTTPS_PROXY": "http://proxy.invalid:3128",
    "ALL_PROXY": "http://proxy.invalid:3128",
}


def _aws_files(tmp_path: Path, monkeypatch, *, config: str = "") -> None:
    """The assistant profile's files, with `config` appended to the profile's section."""
    (tmp_path / "aws-config").write_text(
        f"[profile {PROFILE}]\nregion = us-east-2\n{config}", encoding="utf-8"
    )
    (tmp_path / "aws-credentials").write_text(
        f"[{PROFILE}]\naws_access_key_id = {ACCESS_KEY}\naws_secret_access_key = {SECRET_KEY}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "aws-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "aws-credentials"))


def _provider(api: BotocoreApi, **fields: Any) -> BedrockProvider:
    return BedrockProvider(
        parse_provider_config(bedrock_fields(**fields)), session_factory=api.session_factory
    )


def _rejected() -> Any:
    return AwsResponse(400, {"message": f"Rejected: {QUESTION}"}, {"x-amzn-ErrorType": "x"})


def _request(**limits: Any) -> CompletionRequest:
    return CompletionRequest(
        conversation=Conversation.model_validate(
            {"messages": [{"role": "user", "text": QUESTION}]}
        ),
        tools=(SEARCH_TOOL,),
        limits=limits or {},
    )


# ---- construction ------------------------------------------------------------------


def test_a_configuration_builds_the_adapter_without_a_request(tmp_path: Path, monkeypatch) -> None:
    # Even with a role, which is assumed on the first request; a profile that cannot be
    # loaded is the case `test_providers.py` reaches through the configuration.
    _aws_files(tmp_path, monkeypatch)
    api = BotocoreApi([])
    monkeypatch.setattr(
        bedrock_provider,
        "BedrockProvider",
        partial(BedrockProvider, session_factory=api.session_factory),
    )
    fields = bedrock_fields(role_arn=ROLE)
    _config, provider = load_provider(write_provider_config(tmp_path / "provider.toml", fields))
    assert isinstance(provider, BedrockProvider)
    assert (provider.model, provider.region, provider.profile) == (MODEL, REGION, PROFILE)
    assert provider.capabilities.capabilities == {ProviderCapability.TOOL_CALLING}
    assert api.requests == []


def test_only_the_configuration_chooses_the_target_credential_and_retries(
    tmp_path: Path, monkeypatch
) -> None:
    profile_endpoint = (
        "max_attempts = 7\nretry_mode = adaptive\nservices = hostile\n\n"
        "[services hostile]\nbedrock-runtime =\n  endpoint_url = https://profile.example.com\n"
    )
    _aws_files(tmp_path, monkeypatch, config=profile_endpoint)
    for name, value in HOSTILE_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    unavailable = AwsResponse(503, {"message": "busy"}, {"x-amzn-ErrorType": "x"})
    api = BotocoreApi([assumed_role_response(), unavailable])
    with pytest.raises(ProviderUnavailableError):
        complete(_provider(api, role_arn=ROLE), _request())
    # One attempt, whatever the environment and the profile say about retries, and the role
    # assumed with the profile's own key.
    assumed, sent = api.requests
    assert sent.url == CONVERSE
    assert signing_key(sent) == ROLE_KEY
    assert assumed.url == f"https://sts.{REGION}.amazonaws.com/"
    assert signing_key(assumed) == ACCESS_KEY
    assert f"RoleArn={ROLE.replace(':', '%3A').replace('/', '%2F')}" in assumed.body
    assert "RoleSessionName=archdiscov-assistant" in assumed.body
    body = sent.body.decode()
    # Short values such as `AWS_MAX_ATTEMPTS` occur in any body, as in `maxTokens`.
    assert not any(value in body for value in HOSTILE_ENVIRONMENT.values() if len(value) > 4)


def test_the_role_is_assumed_once_and_no_credential_or_request_reaches_a_rendering_or_log(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    _aws_files(tmp_path, monkeypatch)
    caplog.set_level(logging.DEBUG)
    # Building the adapter quiets these again; the contract checks that for every adapter.
    for name in bedrock_provider._SDK_LOGGERS:
        logging.getLogger(name).setLevel(logging.DEBUG)
    api = BotocoreApi([assumed_role_response(), _rejected(), _rejected()])
    provider = _provider(api, role_arn=ROLE)
    for _ in range(2):
        with pytest.raises(ProviderUnavailableError):
            complete(provider, _request())
    assert [signing_key(request) for request in api.requests] == [ACCESS_KEY, ROLE_KEY, ROLE_KEY]
    for rendering in (*secret_renderings(provider), caplog.text):
        for secret in (SECRET_KEY, ROLE_SECRET, "roleToken", QUESTION):
            assert secret not in rendering


def test_a_role_sts_rejects_is_an_authentication_error_without_its_message(
    tmp_path: Path, monkeypatch, caplog, capsys
) -> None:
    _aws_files(tmp_path, monkeypatch)
    message = f"User is not authorized to perform sts:AssumeRole on resource: {ROLE}"
    body = (
        '<ErrorResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/"><Error>'
        f"<Type>Sender</Type><Code>AccessDenied</Code><Message>{message}</Message>"
        "</Error><RequestId>sts-request-id</RequestId></ErrorResponse>"
    )
    api = BotocoreApi([AwsResponse(403, body.encode())])
    with pytest.raises(ProviderAuthenticationError) as raised:
        complete(_provider(api, role_arn=ROLE), _request())
    assert len(api.requests) == 1
    output = capsys.readouterr()
    for rendering in (*secret_renderings(raised.value), caplog.text, output.out, output.err):
        assert ROLE not in rendering and "sts-request-id" not in rendering


def test_a_profile_without_credentials_fails_as_an_authentication_error(
    tmp_path: Path, monkeypatch
) -> None:
    _aws_files(tmp_path, monkeypatch)
    (tmp_path / "aws-credentials").write_text("", encoding="utf-8")
    api = BotocoreApi([])
    with pytest.raises(ProviderAuthenticationError):
        complete(_provider(api), _request())
    assert api.requests == []


def test_a_configured_endpoint_is_used_and_proxy_variables_are_not(
    tmp_path: Path, monkeypatch
) -> None:
    _aws_files(tmp_path, monkeypatch)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    with mock_api_server([{}]) as proxy, mock_api_server([ANSWER]) as api:
        # Were proxy variables honoured, the request would go to the other server.
        for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY"):
            monkeypatch.setenv(name, f"http://127.0.0.1:{proxy.port}")
        endpoint = f"http://127.0.0.1:{api.port}"
        provider = BedrockProvider(parse_provider_config(bedrock_fields(endpoint=endpoint)))
        complete(provider, _request())
    [(path, headers, body)] = api.requests
    assert path == CONVERSE_PATH
    assert headers["Authorization"].startswith(f"AWS4-HMAC-SHA256 Credential={ACCESS_KEY}/")
    assert body["messages"] == [{"role": "user", "content": [{"text": QUESTION}]}]
    assert proxy.requests == []
