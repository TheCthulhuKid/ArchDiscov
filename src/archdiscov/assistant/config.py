"""Provider configuration: bounded, explicit, and free of secret values.

Configuration is a TOML file with one top-level table selected by `kind`. Credentials
are references only: an environment variable name for API-key providers, or an AWS
profile for Bedrock. Values are resolved by `resolve_api_key` when a provider is
constructed, never stored on the configuration, and never accepted as CLI arguments.

`assistant/providers.py` builds a provider from a configuration through the adapter
registered for its kind.
"""

import os
import tomllib
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal, Self

from pydantic import (
    Field,
    HttpUrl,
    SecretStr,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from archdiscov.core.strict_json import FileTooLargeError, read_bounded
from archdiscov.models.enums import AwsRegion
from archdiscov.models.resource_base import NonEmptyString
from archdiscov.models.sanitization import is_sensitive_path
from archdiscov.models.validation_errors import (
    ValidationIssue,
    safe_validation_issues,
    validation_error,
)

from .conversation import Contract
from .provider import ProviderCapability, RequestLimits, require_tool_calling_for_parallel_calls
from .redaction import looks_secret

MAX_CONFIG_BYTES = 65_536

EnvVarName = Annotated[str, Field(pattern=r"^[A-Z_][A-Z0-9_]{0,127}$")]
ModelName = Annotated[NonEmptyString, Field(max_length=256)]
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]", "::1"})


def _reject_secrets(data: Any) -> None:
    """Reject sensitive keys and secret-looking values anywhere, before field validation."""
    if isinstance(data, Mapping):
        for key, value in data.items():
            if isinstance(key, str) and is_sensitive_path(key):
                raise validation_error("provider_config_secret_field", "sensitive field name")
            _reject_secrets(value)
    elif isinstance(data, (list, tuple)):
        for value in data:
            _reject_secrets(value)
    elif isinstance(data, str) and looks_secret(data):
        raise validation_error("provider_config_secret_value", "secret-looking value")


class ProviderKind(StrEnum):
    FAKE = "fake"
    ANTHROPIC = "anthropic"
    OPENAI_COMPATIBLE = "openai_compatible"
    BEDROCK = "bedrock"


class OpenAIProtocol(StrEnum):
    RESPONSES = "responses"
    CHAT_COMPLETIONS = "chat_completions"


class _ProviderConfigBase(Contract):
    """Fields shared by every kind; every bound and default is explicit."""

    model: ModelName
    connect_timeout_seconds: float = Field(default=10.0, gt=0, le=60, strict=True)
    request_timeout_seconds: float = Field(default=60.0, gt=0, le=600, strict=True)
    max_retries: int = Field(default=2, ge=0, le=5, strict=True)
    max_output_tokens: int = Field(default=4_096, ge=1, le=128_000, strict=True)
    max_requests: int = Field(default=8, ge=1, le=50, strict=True)
    max_tool_calls: int = Field(default=32, ge=1, le=200, strict=True)
    max_tool_calls_per_turn: int = Field(default=1, ge=1, le=32, strict=True)
    max_latency_seconds: float = Field(default=300.0, gt=0, le=3_600, strict=True)

    @model_validator(mode="before")
    @classmethod
    def reject_secret_values(cls, data: Any) -> Any:
        _reject_secrets(data)
        return data

    @model_validator(mode="after")
    def validate_limit_order(self) -> Self:
        if self.request_timeout_seconds > self.max_latency_seconds:
            raise validation_error(
                "provider_config_limit_order", "request timeout exceeds max latency"
            )
        if self.max_tool_calls_per_turn > self.max_tool_calls:
            raise validation_error(
                "provider_config_limit_order", "per-turn tool calls exceed the run limit"
            )
        return self

    def request_limits(self) -> RequestLimits:
        """Per-request limits for each provider call; the rest bound the whole run."""
        return RequestLimits(
            max_output_tokens=self.max_output_tokens,
            timeout_seconds=self.request_timeout_seconds,
            max_tool_calls_per_turn=self.max_tool_calls_per_turn,
        )


class _EndpointConfig(_ProviderConfigBase):
    @field_validator("endpoint", check_fields=False)
    @classmethod
    def validate_endpoint(cls, endpoint: HttpUrl | None) -> HttpUrl | None:
        """HTTPS (or loopback HTTP) without user info, query, or fragment, which can hold keys."""
        if endpoint is None:
            return endpoint
        if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
            raise validation_error("provider_config_endpoint_invalid", "credentials or query")
        if endpoint.scheme != "https" and endpoint.host not in _LOOPBACK_HOSTS:
            raise validation_error("provider_config_endpoint_invalid", "non-loopback http")
        return endpoint


class FakeProviderConfig(_ProviderConfigBase):
    """The scripted fake provider; it needs no credentials or network.

    Building it needs `transcript`, a fake transcript file; a relative path is resolved
    against the configuration file's directory.
    """

    kind: Literal[ProviderKind.FAKE] = ProviderKind.FAKE
    model: ModelName = "fake"
    transcript: Annotated[NonEmptyString, Field(max_length=4_096)] | None = None


class AnthropicProviderConfig(_EndpointConfig):
    kind: Literal[ProviderKind.ANTHROPIC] = ProviderKind.ANTHROPIC
    api_key_env: EnvVarName
    endpoint: HttpUrl | None = None


class _DeclaredCapabilitiesConfig(_EndpointConfig):
    """A provider that cannot be asked what its model supports, so `capabilities` declares
    it: tool calling by default, and parallel calls only when declared."""

    capabilities: frozenset[ProviderCapability] = frozenset({ProviderCapability.TOOL_CALLING})

    @field_validator("capabilities")
    @classmethod
    def validate_capabilities(
        cls, capabilities: frozenset[ProviderCapability]
    ) -> frozenset[ProviderCapability]:
        require_tool_calling_for_parallel_calls(capabilities)
        return capabilities


class OpenAICompatibleProviderConfig(_DeclaredCapabilitiesConfig):
    """Base URL, key variable, protocol, and capabilities are explicit; nothing is guessed.

    Parallel calls are declared, never assumed, because some servers reject
    `parallel_tool_calls`. Likewise `encrypted_reasoning` asks a Responses server for the
    encrypted reasoning a stateless reasoning model needs back, only when declared, because
    servers without it may reject the request.
    """

    kind: Literal[ProviderKind.OPENAI_COMPATIBLE] = ProviderKind.OPENAI_COMPATIBLE
    api_key_env: EnvVarName
    endpoint: HttpUrl
    protocol: OpenAIProtocol
    encrypted_reasoning: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def require_responses_for_encrypted_reasoning(self) -> Self:
        if self.encrypted_reasoning and self.protocol is not OpenAIProtocol.RESPONSES:
            raise validation_error(
                "provider_config_reasoning_protocol", "encrypted reasoning needs responses"
            )
        return self


class BedrockProviderConfig(_DeclaredCapabilitiesConfig):
    """Uses its own AWS profile and optional role, separate from collection credentials.

    Bedrock models differ in tool support, so `capabilities` declares it. `endpoint`
    replaces the regional endpoint, such as for a VPC interface endpoint.
    """

    kind: Literal[ProviderKind.BEDROCK] = ProviderKind.BEDROCK
    profile: NonEmptyString
    region: AwsRegion
    endpoint: HttpUrl | None = None
    role_arn: str | None = Field(default=None, pattern=r"^arn:aws[a-z-]*:iam::[0-9]{12}:role/\S+$")
    session_name: NonEmptyString = "archdiscov-assistant"

    @field_validator("region")
    @classmethod
    def require_concrete_region(cls, region: AwsRegion) -> AwsRegion:
        if region is AwsRegion.GLOBAL:
            raise validation_error("provider_config_region_invalid", "global is not a region")
        return region


ProviderConfig = Annotated[
    FakeProviderConfig
    | AnthropicProviderConfig
    | OpenAICompatibleProviderConfig
    | BedrockProviderConfig,
    Field(discriminator="kind"),
]
_PROVIDER_CONFIG: TypeAdapter[ProviderConfig] = TypeAdapter(ProviderConfig)
_DIAGNOSTIC_FIELDS = frozenset(
    field
    for model in (
        FakeProviderConfig,
        AnthropicProviderConfig,
        OpenAICompatibleProviderConfig,
        BedrockProviderConfig,
    )
    for field in model.model_fields
)


class ProviderConfigErrorCode(StrEnum):
    UNREADABLE = "provider_config_unreadable"
    TOO_LARGE = "provider_config_too_large"
    MALFORMED = "provider_config_malformed"
    INVALID = "provider_config_invalid"
    CREDENTIAL_MISSING = "provider_credential_missing"
    CREDENTIAL_EMPTY = "provider_credential_empty"
    TRANSCRIPT_MISSING = "provider_transcript_missing"
    PROFILE_UNAVAILABLE = "provider_profile_unavailable"


class ProviderConfigError(Exception):
    """A configuration or credential failure with a fixed message.

    Diagnostics name the file path, the referenced variable or AWS profile name, and the
    provider kind, never file contents, field values, or resolved credentials.
    """

    messages: ClassVar[Mapping[ProviderConfigErrorCode, str]] = {
        ProviderConfigErrorCode.UNREADABLE: "Provider configuration file could not be read.",
        ProviderConfigErrorCode.TOO_LARGE: (
            f"Provider configuration file exceeds {MAX_CONFIG_BYTES} bytes."
        ),
        ProviderConfigErrorCode.MALFORMED: "Provider configuration file is not valid TOML.",
        ProviderConfigErrorCode.INVALID: "Provider configuration validation failed.",
        ProviderConfigErrorCode.CREDENTIAL_MISSING: (
            "Configured credential environment variable is not set."
        ),
        ProviderConfigErrorCode.CREDENTIAL_EMPTY: (
            "Configured credential environment variable is empty."
        ),
        ProviderConfigErrorCode.TRANSCRIPT_MISSING: (
            "Fake provider configuration names no transcript file."
        ),
        ProviderConfigErrorCode.PROFILE_UNAVAILABLE: (
            "Configured AWS profile could not be loaded."
        ),
    }

    def __init__(
        self,
        code: ProviderConfigErrorCode,
        *,
        path: Path | None = None,
        variable: str | None = None,
        profile: str | None = None,
        kind: ProviderKind | None = None,
        details: tuple[ValidationIssue, ...] = (),
    ) -> None:
        self.code = ProviderConfigErrorCode(code)
        self.message = self.messages[self.code]
        super().__init__(self.message)
        self.path = path
        self.variable = variable
        self.profile = profile
        self.kind = kind
        self.details = details


def parse_provider_config(data: Mapping[str, Any]) -> ProviderConfig:
    """Validate configuration data, reporting only codes and known field names."""
    try:
        return _PROVIDER_CONFIG.validate_python(data)
    except ValidationError as exc:
        raise ProviderConfigError(
            ProviderConfigErrorCode.INVALID,
            details=safe_validation_issues(exc, _DIAGNOSTIC_FIELDS, "provider configuration"),
        ) from exc


def load_provider_config(path: Path) -> ProviderConfig:
    """Read a bounded TOML file and validate it."""
    try:
        content = read_bounded(path, MAX_CONFIG_BYTES)
    except FileTooLargeError as exc:
        raise ProviderConfigError(ProviderConfigErrorCode.TOO_LARGE, path=path) from exc
    except OSError as exc:
        raise ProviderConfigError(ProviderConfigErrorCode.UNREADABLE, path=path) from exc
    try:
        data = tomllib.loads(content.decode())
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, RecursionError) as exc:
        raise ProviderConfigError(ProviderConfigErrorCode.MALFORMED, path=path) from exc
    try:
        return parse_provider_config(data)
    except ProviderConfigError as exc:
        exc.path = path
        raise


def resolve_api_key(
    config: ProviderConfig, environ: Mapping[str, str] | None = None
) -> SecretStr | None:
    """Read the referenced key at provider construction, before any request.

    Returns None for kinds without an API key. The value is wrapped so `repr` and `str`
    never show it.
    """
    variable = getattr(config, "api_key_env", None)
    if variable is None:
        return None
    value = (os.environ if environ is None else environ).get(variable)
    if value is None:
        raise ProviderConfigError(ProviderConfigErrorCode.CREDENTIAL_MISSING, variable=variable)
    if not value.strip():
        raise ProviderConfigError(ProviderConfigErrorCode.CREDENTIAL_EMPTY, variable=variable)
    return SecretStr(value)
