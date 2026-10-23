from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

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

SECRET = "sk-ant-api03-SECRETvalue0123456789abcdefXYZ"
ANTHROPIC = {"kind": "anthropic", "model": "claude-sonnet-5-5", "api_key_env": "ANTHROPIC_API_KEY"}
OPENAI = {
    "kind": "openai_compatible",
    "model": "gpt-5",
    "api_key_env": "OPENAI_API_KEY",
    "endpoint": "https://api.openai.com/v1",
    "protocol": "responses",
}
BEDROCK = {
    "kind": "bedrock",
    "model": "anthropic.claude-3-5-sonnet-20240620-v1:0",
    "profile": "assistant",
    "region": "eu-west-1",
}


def _write(tmp_path: Path, text: str | bytes) -> Path:
    path = tmp_path / "provider.toml"
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
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
model = "llama3.1"
api_key_env = "LOCAL_LLM_KEY"
endpoint = "http://localhost:11434/v1"
protocol = "chat_completions"
""",
        )
    )
    assert isinstance(openai, OpenAICompatibleProviderConfig)
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
    assert parse_provider_config(BEDROCK).endpoint is None
    # Bedrock models differ in tool support; only tool calling is assumed unless declared.
    assert parse_provider_config(BEDROCK).capabilities == {ProviderCapability.TOOL_CALLING}


def test_defaults_are_explicit() -> None:
    assert FakeProviderConfig().model_dump(mode="json") == {
        "kind": "fake",
        "model": "fake",
        "connect_timeout_seconds": 10.0,
        "request_timeout_seconds": 60.0,
        "max_retries": 2,
        "max_output_tokens": 4_096,
        "max_requests": 8,
        "max_tool_calls": 32,
        "max_tool_calls_per_turn": 1,
        "max_latency_seconds": 300.0,
        "transcript": None,
    }
    assert FakeProviderConfig().request_limits() == RequestLimits()


@pytest.mark.parametrize(
    "model",
    [
        "claude-sonnet-5-5",
        "claude-opus-5-5",
        "gpt-4o-2024-08-06",
        "anthropic.claude-3-5-sonnet-20240620-v1:0",
        "us.anthropic.claude-3-7-sonnet-20250219-v1:0",
        "meta-llama/Meta-Llama-3.1-70B-Instruct",
        "llama3.1:8b",
    ],
)
def test_real_model_names_are_not_mistaken_for_secrets(model: str) -> None:
    assert parse_provider_config({**ANTHROPIC, "model": model}).model == model


@pytest.mark.parametrize(
    "data,code",
    [
        ({**ANTHROPIC, "api_key": SECRET}, "provider_config_secret_field"),
        ({**ANTHROPIC, "api_key": "ANTHROPIC_API_KEY"}, "provider_config_secret_field"),
        ({**ANTHROPIC, "password": "x"}, "provider_config_secret_field"),
        ({**ANTHROPIC, "headers": {"Authorization": "x"}}, "provider_config_secret_field"),
        ({**BEDROCK, "aws_secret_access_key": "x"}, "provider_config_secret_field"),
        ({**ANTHROPIC, "model": SECRET}, "provider_config_secret_value"),
        ({**ANTHROPIC, "api_key_env": "AKIAIOSFODNN7EXAMPLE"}, "provider_config_secret_value"),
        ({**ANTHROPIC, "model": "Bearer abc"}, "provider_config_secret_value"),
        ({**ANTHROPIC, "model": "-----BEGIN RSA PRIVATE KEY-----"}, "provider_config_secret_value"),
        (
            {**ANTHROPIC, "model": "aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3z"},
            "provider_config_secret_value",
        ),
        ({**OPENAI, "notes": [f"key {SECRET}"]}, "provider_config_secret_value"),
        ({**ANTHROPIC, "unknown": 1}, "extra_forbidden"),
        ({**ANTHROPIC, "api_key_env": "lowercase"}, "string_pattern_mismatch"),
        ({"kind": SECRET}, "union_tag_invalid"),
        ({"kind": "gemini", "model": "x"}, "union_tag_invalid"),
        ({"model": "x"}, "union_tag_not_found"),
        ({k: v for k, v in OPENAI.items() if k != "protocol"}, "missing"),
        ({**OPENAI, "protocol": "assistants"}, "enum"),
        ({k: v for k, v in OPENAI.items() if k != "endpoint"}, "missing"),
        ({k: v for k, v in ANTHROPIC.items() if k != "api_key_env"}, "missing"),
        ({**OPENAI, "endpoint": "http://api.example.com/v1"}, "provider_config_endpoint_invalid"),
        (
            {**OPENAI, "endpoint": "https://user:pw@api.example.com/v1"},
            "provider_config_endpoint_invalid",
        ),
        (
            {**OPENAI, "endpoint": "https://api.example.com/v1?key=x"},
            "provider_config_endpoint_invalid",
        ),
        (
            {**OPENAI, "endpoint": "https://api.example.com/v1#x"},
            "provider_config_endpoint_invalid",
        ),
        ({**OPENAI, "endpoint": "ftp://api.example.com"}, "url_scheme"),
        (
            {**OPENAI, "capabilities": ["parallel_tool_calls"]},
            "provider_parallel_calls_without_tools",
        ),
        ({**OPENAI, "capabilities": ["function_calling"]}, "enum"),
        ({**OPENAI, "capabilities": "tool_calling"}, "frozen_set_type"),
        (
            {**OPENAI, "protocol": "chat_completions", "encrypted_reasoning": True},
            "provider_config_reasoning_protocol",
        ),
        ({**OPENAI, "encrypted_reasoning": "true"}, "bool_type"),
        ({**BEDROCK, "region": "global"}, "provider_config_region_invalid"),
        (
            {**BEDROCK, "capabilities": ["parallel_tool_calls"]},
            "provider_parallel_calls_without_tools",
        ),
        ({**BEDROCK, "endpoint": "http://bedrock.example.com"}, "provider_config_endpoint_invalid"),
        ({**BEDROCK, "region": "mars-1"}, "enum"),
        ({**BEDROCK, "role_arn": "role/x"}, "string_pattern_mismatch"),
        ({k: v for k, v in BEDROCK.items() if k != "profile"}, "missing"),
        (
            {**ANTHROPIC, "request_timeout_seconds": 120, "max_latency_seconds": 60},
            "provider_config_limit_order",
        ),
        (
            {**ANTHROPIC, "max_tool_calls_per_turn": 8, "max_tool_calls": 4},
            "provider_config_limit_order",
        ),
        ({**ANTHROPIC, "max_retries": "2"}, "int_type"),
        ({**ANTHROPIC, "max_retries": True}, "int_type"),
    ],
)
def test_invalid_configuration_fails_safely(data: dict, code: str) -> None:
    assert code in _invalid_types(data)


@pytest.mark.parametrize(
    "field,low,high",
    [
        ("connect_timeout_seconds", 0, 61),
        ("request_timeout_seconds", 0, 601),
        ("max_retries", -1, 6),
        ("max_output_tokens", 0, 128_001),
        ("max_requests", 0, 51),
        ("max_tool_calls", 0, 201),
        ("max_tool_calls_per_turn", 0, 33),
        ("max_latency_seconds", 0, 3_601),
    ],
)
def test_every_bound_is_enforced(field: str, low: float, high: float) -> None:
    for value in (low, high):
        types = _invalid_types({**ANTHROPIC, field: value})
        assert types & {"greater_than", "greater_than_equal", "less_than_equal"}


@pytest.mark.parametrize(
    "content,code",
    [
        ("kind = \n", ProviderConfigErrorCode.MALFORMED),
        (b'kind = "fake"\n# \xff\xfe\n', ProviderConfigErrorCode.MALFORMED),
        ("#" * (MAX_CONFIG_BYTES + 1), ProviderConfigErrorCode.TOO_LARGE),
        (
            f'kind = "anthropic"\nmodel = "m"\napi_key = "{SECRET}"\n',
            ProviderConfigErrorCode.INVALID,
        ),
    ],
)
def test_bad_files_fail_with_path_and_no_content(
    tmp_path: Path, content: str | bytes, code: ProviderConfigErrorCode
) -> None:
    path = _write(tmp_path, content)
    with pytest.raises(ProviderConfigError) as raised:
        load_provider_config(path)
    error = raised.value
    assert error.code == code
    assert error.path == path
    assert SECRET not in str(error) + repr(error.details)


def test_file_at_the_size_limit_loads(tmp_path: Path) -> None:
    header = 'kind = "fake"\n'
    path = _write(tmp_path, header + "#" * (MAX_CONFIG_BYTES - len(header)))
    assert load_provider_config(path) == FakeProviderConfig()


def test_missing_file_is_unreadable(tmp_path: Path) -> None:
    with pytest.raises(ProviderConfigError) as raised:
        load_provider_config(tmp_path / "missing.toml")
    assert raised.value.code == ProviderConfigErrorCode.UNREADABLE


@pytest.mark.parametrize(
    "environ,code",
    [
        ({}, ProviderConfigErrorCode.CREDENTIAL_MISSING),
        ({"ANTHROPIC_API_KEY": ""}, ProviderConfigErrorCode.CREDENTIAL_EMPTY),
        ({"ANTHROPIC_API_KEY": " \t"}, ProviderConfigErrorCode.CREDENTIAL_EMPTY),
    ],
)
def test_missing_or_empty_credentials_fail_before_any_request(
    environ: dict, code: ProviderConfigErrorCode
) -> None:
    config = parse_provider_config(ANTHROPIC)
    with pytest.raises(ProviderConfigError) as raised:
        resolve_api_key(config, environ)
    assert raised.value.code == code
    assert raised.value.variable == "ANTHROPIC_API_KEY"


def test_resolved_secret_never_appears_in_dumps_or_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    config = parse_provider_config(ANTHROPIC)
    secret = resolve_api_key(config)
    assert isinstance(secret, SecretStr)
    assert secret.get_secret_value() == SECRET
    for rendered in (
        repr(secret),
        str(secret),
        repr(config),
        str(config),
        config.model_dump_json(),
        str(config.model_dump()),
    ):
        assert SECRET not in rendered
    assert resolve_api_key(parse_provider_config(OPENAI), {"OPENAI_API_KEY": "k"}) is not None


def test_kinds_without_api_keys_resolve_nothing() -> None:
    assert resolve_api_key(FakeProviderConfig(), {}) is None
    assert resolve_api_key(parse_provider_config(BEDROCK), {}) is None


def test_configurations_are_frozen() -> None:
    config = parse_provider_config(ANTHROPIC)
    with pytest.raises(ValidationError):
        config.api_key_env = "OTHER"
