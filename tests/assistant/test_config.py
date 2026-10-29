from pathlib import Path

import pytest
from pydantic import SecretStr

from archdiscov.assistant.config import (
    MAX_CONFIG_BYTES,
    AnthropicProviderConfig,
    BedrockProviderConfig,
    FakeProviderConfig,
    OpenAICompatibleProviderConfig,
    OpenAIProtocol,
    ProviderConfigError,
    ProviderConfigErrorCode,
    load_provider_config,
    parse_provider_config,
    resolve_api_key,
)
from archdiscov.assistant.provider import ProviderCapability, RequestLimits
from archdiscov.models.enums import AwsRegion
from tests.support.provider_data import ANTHROPIC, BEDROCK, OPENAI

SECRET = "sk-ant-api03-SECRETvalue0123456789abcdefXYZ"


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "provider.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _invalid_types(data: dict) -> set[str]:
    with pytest.raises(ProviderConfigError) as raised:
        parse_provider_config(data)
    error = raised.value
    assert error.code == ProviderConfigErrorCode.INVALID
    rendered = str(error) + repr(error.details)
    assert SECRET not in rendered
    assert all(issue.message == "Invalid provider configuration field." for issue in error.details)
    return {issue.type for issue in error.details}


def test_each_kind_loads_from_toml(tmp_path: Path) -> None:
    fake = load_provider_config(_write(tmp_path, 'kind = "fake"\n'))
    assert fake == FakeProviderConfig()

    anthropic = load_provider_config(
        _write(
            tmp_path,
            """
# Gateway base URL and a key referenced by name.
kind = "anthropic"
model = "claude-sonnet-5-5"
api_key_env = "ANTHROPIC_API_KEY"
endpoint = "https://gateway.example.com/anthropic"
max_tool_calls_per_turn = 4
request_timeout_seconds = 30
""",
        )
    )
    assert isinstance(anthropic, AnthropicProviderConfig)
    assert str(anthropic.endpoint) == "https://gateway.example.com/anthropic"
    assert anthropic.request_limits() == RequestLimits(
        max_output_tokens=4_096, timeout_seconds=30.0, max_tool_calls_per_turn=4
    )

    openai = load_provider_config(
        _write(
            tmp_path,
            """
kind = "openai_compatible"
model = "meta-llama/Meta-Llama-3.1-70B-Instruct"
api_key_env = "LOCAL_LLM_KEY"
endpoint = "http://localhost:11434/v1"
protocol = "chat_completions"
""",
        )
    )
    assert isinstance(openai, OpenAICompatibleProviderConfig)
    # Real model names, like this one and the others here, are not mistaken for secrets.
    assert openai.model == "meta-llama/Meta-Llama-3.1-70B-Instruct"
    assert openai.protocol is OpenAIProtocol.CHAT_COMPLETIONS
    # A compatible server cannot be asked; only tool calling is assumed unless declared.
    assert openai.capabilities == {ProviderCapability.TOOL_CALLING}
    assert openai.encrypted_reasoning is False

    bedrock = load_provider_config(
        _write(
            tmp_path,
            """
kind = "bedrock"
model = "anthropic.claude-3-5-sonnet-20240620-v1:0"
profile = "assistant"
region = "eu-west-1"
role_arn = "arn:aws:iam::123456789012:role/ArchdiscovAssistant"
endpoint = "https://vpce-0a1b2c.bedrock-runtime.eu-west-1.vpce.amazonaws.com"
capabilities = ["tool_calling", "parallel_tool_calls"]
""",
        )
    )
    assert isinstance(bedrock, BedrockProviderConfig)
    assert bedrock.region is AwsRegion.EU_WEST_1
    assert bedrock.capabilities == set(ProviderCapability) - {ProviderCapability.STRUCTURED_OUTPUT}
    # Bedrock models differ in tool support; only tool calling is assumed unless declared.
    assert parse_provider_config(BEDROCK).capabilities == {ProviderCapability.TOOL_CALLING}


@pytest.mark.parametrize(
    "data,code",
    [
        ({**ANTHROPIC, "api_key": SECRET}, "provider_config_secret_field"),
        ({**ANTHROPIC, "model": SECRET}, "provider_config_secret_value"),
        ({**OPENAI, "endpoint": "http://api.example.com/v1"}, "provider_config_endpoint_invalid"),
        (
            {**OPENAI, "capabilities": ["parallel_tool_calls"]},
            "provider_parallel_calls_without_tools",
        ),
        (
            {**OPENAI, "protocol": "chat_completions", "encrypted_reasoning": True},
            "provider_config_reasoning_protocol",
        ),
        ({**BEDROCK, "region": "global"}, "provider_config_region_invalid"),
        (
            {**ANTHROPIC, "request_timeout_seconds": 120, "max_latency_seconds": 60},
            "provider_config_limit_order",
        ),
    ],
)
def test_invalid_configuration_fails_safely(data: dict, code: str) -> None:
    assert code in _invalid_types(data)


@pytest.mark.parametrize(
    "content,code",
    [
        ("kind = \n", ProviderConfigErrorCode.MALFORMED),
        ("#" * (MAX_CONFIG_BYTES + 1), ProviderConfigErrorCode.TOO_LARGE),
        ('kind = "anthropic"\nmodel = "m"\n', ProviderConfigErrorCode.INVALID),
    ],
    ids=["malformed", "too-large", "invalid"],
)
def test_bad_files_fail_with_their_path(
    tmp_path: Path, content: str, code: ProviderConfigErrorCode
) -> None:
    path = _write(tmp_path, content)
    with pytest.raises(ProviderConfigError) as raised:
        load_provider_config(path)
    assert raised.value.code == code
    assert raised.value.path == path


def test_api_keys_resolve_from_the_environment_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = parse_provider_config(ANTHROPIC)
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    secret = resolve_api_key(config)
    assert isinstance(secret, SecretStr)
    assert secret.get_secret_value() == SECRET
    for environ, code in (
        ({}, ProviderConfigErrorCode.CREDENTIAL_MISSING),
        ({"ANTHROPIC_API_KEY": " \t"}, ProviderConfigErrorCode.CREDENTIAL_EMPTY),
    ):
        with pytest.raises(ProviderConfigError) as raised:
            resolve_api_key(config, environ)
        assert raised.value.code == code
        assert raised.value.variable == "ANTHROPIC_API_KEY"
    assert resolve_api_key(parse_provider_config(BEDROCK), {}) is None
