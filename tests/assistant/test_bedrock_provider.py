"""The Bedrock adapter: built only from its own AWS profile, region, and role.

Requests go to a mocked API through botocore's `before-send` event, or to a loopback server;
nothing reaches AWS. Each test writes its own AWS profile files.
"""

import logging
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import CredentialRetrievalError

from archdiscov.assistant import bedrock_provider
from archdiscov.assistant.bedrock_provider import BedrockProvider
from archdiscov.assistant.config import (
    ProviderConfigError,
    ProviderConfigErrorCode,
    parse_provider_config,
)
from archdiscov.assistant.conversation import Conversation
from archdiscov.assistant.provider import (
    CompletionRequest,
    ProviderAuthenticationError,
    ProviderCapability,
    ProviderErrorCode,
    ProviderUnavailableError,
    UnsupportedCapabilityError,
    complete,
)
from archdiscov.assistant.providers import load_provider
from archdiscov.services.ask import ask_question
from tests.assistant.test_orchestrator import SNAPSHOT
from tests.assistant.test_provider_contract import SEARCH
from tests.assistant.test_secret_leaks import _renderings
from tests.support.ask_examples import write_provider_config
from tests.support.mock_api import AwsResponse, BotocoreApi, mock_api_server

PROFILE = "assistant"
REGION = "eu-west-1"
MODEL = "anthropic.claude-sonnet-5-5-v1:0"
ROLE = "arn:aws:iam::123456789012:role/assistant"
ACCESS_KEY = "AKIAPROFILEKEY000000"
SECRET_KEY = "profileSecretKey0000000000000000000000000"
ROLE_KEY = "ASIAROLEKEY000000000"
ROLE_SECRET = "roleSecretKey00000000000000000000000000000"
QUESTION = "Which databases are reachable?"
# Botocore percent-encodes the model ID in the path.
CONVERSE_PATH = f"/model/{MODEL.replace(':', '%3A')}/converse"
CONVERSE = f"https://bedrock-runtime.{REGION}.amazonaws.com{CONVERSE_PATH}"
ANSWER = {
    "output": {"message": {"role": "assistant", "content": [{"text": "None of them."}]}},
    "stopReason": "end_turn",
    "usage": {"inputTokens": 3, "outputTokens": 1, "totalTokens": 4},
    "metrics": {"latencyMs": 1},
}
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


def _fields(**fields: Any) -> dict[str, Any]:
    return {"kind": "bedrock", "model": MODEL, "profile": PROFILE, "region": REGION, **fields}


def _provider(api: BotocoreApi, **fields: Any) -> BedrockProvider:
    return BedrockProvider(
        parse_provider_config(_fields(**fields)), session_factory=api.session_factory
    )


def _rejected() -> Any:
    return AwsResponse(400, {"message": f"Rejected: {QUESTION}"}, {"x-amzn-ErrorType": "x"})


def _assumed(key: str = ROLE_KEY, token: str | None = "roleToken") -> Any:
    """An `AssumeRole` response with the role's credentials; no token makes it invalid."""
    session_token = "" if token is None else f"<SessionToken>{token}</SessionToken>"
    body = (
        '<AssumeRoleResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">'
        "<AssumeRoleResult><Credentials>"
        f"<AccessKeyId>{key}</AccessKeyId><SecretAccessKey>{ROLE_SECRET}</SecretAccessKey>"
        f"{session_token}<Expiration>2030-01-01T00:00:00Z</Expiration>"
        "</Credentials></AssumeRoleResult></AssumeRoleResponse>"
    )
    return AwsResponse(200, body.encode())


def _credential(request: Any) -> str:
    """The access key ID a request was signed with."""
    authorization = request.headers["Authorization"].decode()
    return authorization.split("Credential=")[1].split("/")[0]


def _request(**limits: Any) -> CompletionRequest:
    return CompletionRequest(
        conversation=Conversation.model_validate(
            {"messages": [{"role": "user", "text": QUESTION}]}
        ),
        tools=(SEARCH,),
        limits=limits or {},
    )


# ---- construction ------------------------------------------------------------------


@pytest.mark.parametrize("role_arn", [None, ROLE], ids=["profile", "role"])
def test_a_configuration_builds_the_adapter_without_a_request(
    tmp_path: Path, monkeypatch, role_arn: str | None
) -> None:
    _aws_files(tmp_path, monkeypatch)
    api = BotocoreApi([])
    monkeypatch.setattr(
        bedrock_provider,
        "BedrockProvider",
        partial(BedrockProvider, session_factory=api.session_factory),
    )
    fields = _fields() if role_arn is None else _fields(role_arn=role_arn)
    config, provider = load_provider(write_provider_config(tmp_path / "provider.toml", fields))
    assert isinstance(provider, BedrockProvider)
    assert (provider.model, provider.region, provider.profile) == (MODEL, REGION, PROFILE)
    assert provider.capabilities.capabilities == {ProviderCapability.TOOL_CALLING}
    assert api.requests == []


@pytest.mark.parametrize(
    ("config", "credentials"),
    [("", True), ("[profile broken\n", True), ("", False)],
    ids=["missing", "malformed-config", "no-files"],
)
def test_a_profile_that_cannot_be_loaded_fails_before_any_request(
    tmp_path: Path, monkeypatch, config: str, credentials: bool
) -> None:
    if credentials:
        _aws_files(tmp_path, monkeypatch, config=config)
    api = BotocoreApi([])
    name = "someone-else" if not config else PROFILE
    with pytest.raises(ProviderConfigError) as raised:
        _provider(api, profile=name)
    error = raised.value
    assert error.code == ProviderConfigErrorCode.PROFILE_UNAVAILABLE and error.profile == name
    assert api.requests == []


@pytest.mark.parametrize("fields", [{}, {"role_arn": ROLE}], ids=["profile", "role"])
def test_only_the_configuration_chooses_the_target_credential_and_retries(
    tmp_path: Path, monkeypatch, fields: dict
) -> None:
    profile_endpoint = (
        "max_attempts = 7\nretry_mode = adaptive\nservices = hostile\n\n"
        "[services hostile]\nbedrock-runtime =\n  endpoint_url = https://profile.example.com\n"
    )
    _aws_files(tmp_path, monkeypatch, config=profile_endpoint)
    for name, value in HOSTILE_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    unavailable = AwsResponse(503, {"message": "busy"}, {"x-amzn-ErrorType": "x"})
    served = [unavailable] if not fields else [_assumed(), unavailable]
    api = BotocoreApi(served)
    with pytest.raises(ProviderUnavailableError):
        complete(_provider(api, **fields), _request())
    # One attempt, whatever the environment and the profile say about retries.
    *assumed, sent = api.requests
    assert sent.url == CONVERSE
    assert _credential(sent) == (ROLE_KEY if fields else ACCESS_KEY)
    for request in assumed:
        assert request.url == f"https://sts.{REGION}.amazonaws.com/"
        assert _credential(request) == ACCESS_KEY
        assert f"RoleArn={ROLE.replace(':', '%3A').replace('/', '%2F')}" in request.body
        assert "RoleSessionName=archdiscov-assistant" in request.body
    body = sent.body.decode()
    # Short values such as `AWS_MAX_ATTEMPTS` occur in any body, as in `maxTokens`.
    assert not any(value in body for value in HOSTILE_ENVIRONMENT.values() if len(value) > 4)


def test_the_role_is_assumed_once_and_an_invalid_answer_is_an_authentication_error(
    tmp_path: Path, monkeypatch
) -> None:
    _aws_files(tmp_path, monkeypatch)
    rejected = AwsResponse(400, {"message": "no"}, {"x-amzn-ErrorType": "x"})
    api = BotocoreApi([_assumed(), rejected, rejected])
    provider = _provider(api, role_arn=ROLE)
    for _ in range(2):
        with pytest.raises(ProviderUnavailableError):
            complete(provider, _request())
    assert [_credential(request) for request in api.requests] == [ACCESS_KEY, ROLE_KEY, ROLE_KEY]

    api = BotocoreApi([_assumed(token=None)])
    with pytest.raises(ProviderAuthenticationError) as raised:
        complete(_provider(api, role_arn=ROLE), _request())
    assert isinstance(raised.value.__cause__, CredentialRetrievalError)
    assert len(api.requests) == 1


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
    for rendering in (*_renderings(raised.value), caplog.text, output.out, output.err):
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
        provider = BedrockProvider(parse_provider_config(_fields(endpoint=endpoint)))
        complete(provider, _request())
    [(path, headers, body)] = api.requests
    assert path == CONVERSE_PATH
    assert headers["Authorization"].startswith(f"AWS4-HMAC-SHA256 Credential={ACCESS_KEY}/")
    assert body["messages"] == [{"role": "user", "content": [{"text": QUESTION}]}]
    assert proxy.requests == []


def test_no_credential_or_request_body_reaches_a_rendering_or_log(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    _aws_files(tmp_path, monkeypatch)
    caplog.set_level(logging.DEBUG)
    for name in bedrock_provider._SDK_LOGGERS:
        logging.getLogger(name).setLevel(logging.DEBUG)
    provider = _provider(BotocoreApi([_assumed(), _rejected()]), role_arn=ROLE)
    with pytest.raises(ProviderUnavailableError):
        complete(provider, _request())
    for name in bedrock_provider._SDK_LOGGERS:
        assert logging.getLogger(name).getEffectiveLevel() == logging.WARNING
    for rendering in (*_renderings(provider), caplog.text):
        for secret in (SECRET_KEY, ROLE_SECRET, "roleToken", QUESTION):
            assert secret not in rendering


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
    tmp_path: Path, monkeypatch, declared: list[str], limits: dict, missing: ProviderCapability
) -> None:
    _aws_files(tmp_path, monkeypatch)
    api = BotocoreApi([])
    with pytest.raises(UnsupportedCapabilityError) as raised:
        complete(_provider(api, capabilities=declared), _request(**limits))
    assert raised.value.capability is missing
    assert api.requests == []


def test_an_ask_run_without_declared_tool_calling_fails_before_any_request(
    tmp_path: Path, monkeypatch
) -> None:
    _aws_files(tmp_path, monkeypatch)
    api = BotocoreApi([])
    monkeypatch.setattr(
        bedrock_provider,
        "BedrockProvider",
        partial(BedrockProvider, session_factory=api.session_factory),
    )
    config = write_provider_config(tmp_path / "provider.toml", _fields(capabilities=[]))
    result = ask_question(QUESTION, snapshot_path=SNAPSHOT, config_path=config)
    outcome = result.answer.outcome
    assert outcome.provider_error == ProviderErrorCode.UNSUPPORTED_CAPABILITY
    assert outcome.exit_code == 6
    assert api.requests == []
