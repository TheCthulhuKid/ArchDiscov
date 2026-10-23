"""Building a provider from its configuration.

`build_provider` looks the configured kind up in `ADAPTERS`, which has an adapter for every
kind, resolves the credential the configuration references, and passes it to that kind's
adapter, so callers never change when a provider is added. Credentials go only to the
adapter: they are never returned, logged, or recorded. A vendor adapter's module is
imported only when that kind is built, so nothing else needs its SDK.
"""

from collections.abc import Callable, Mapping
from pathlib import Path

from pydantic import SecretStr

from .config import (
    AnthropicProviderConfig,
    BedrockProviderConfig,
    FakeProviderConfig,
    OpenAICompatibleProviderConfig,
    ProviderConfig,
    ProviderConfigError,
    ProviderConfigErrorCode,
    ProviderKind,
    load_provider_config,
    resolve_api_key,
)
from .fake import FakeProvider, load_fake_transcript
from .provider import Provider

type Adapter = Callable[[ProviderConfig, SecretStr | None, Path], Provider]


def _fake(config: FakeProviderConfig, _: SecretStr | None, config_dir: Path) -> Provider:
    if config.transcript is None:
        raise ProviderConfigError(ProviderConfigErrorCode.TRANSCRIPT_MISSING, kind=config.kind)
    transcript = load_fake_transcript(config_dir / config.transcript)
    return FakeProvider(transcript, max_retries=config.max_retries)


def _anthropic(config: AnthropicProviderConfig, api_key: SecretStr, _: Path) -> Provider:
    from .anthropic_provider import AnthropicProvider

    return AnthropicProvider(config, api_key)


def _openai(config: OpenAICompatibleProviderConfig, api_key: SecretStr, _: Path) -> Provider:
    from .openai_provider import OpenAIProvider

    return OpenAIProvider(config, api_key)


def _bedrock(config: BedrockProviderConfig, _: SecretStr | None, __: Path) -> Provider:
    from .bedrock_provider import BedrockProvider

    return BedrockProvider(config)


ADAPTERS: Mapping[ProviderKind, Adapter] = {
    ProviderKind.FAKE: _fake,
    ProviderKind.ANTHROPIC: _anthropic,
    ProviderKind.OPENAI_COMPATIBLE: _openai,
    ProviderKind.BEDROCK: _bedrock,
}


def build_provider(
    config: ProviderConfig,
    *,
    config_dir: Path,
    environ: Mapping[str, str] | None = None,
) -> Provider:
    """The provider for a configuration; relative paths in it resolve against `config_dir`."""
    return ADAPTERS[config.kind](config, resolve_api_key(config, environ), config_dir)


def load_provider(
    path: Path, *, environ: Mapping[str, str] | None = None
) -> tuple[ProviderConfig, Provider]:
    """Load a configuration file and build its provider; errors name the file."""
    config = load_provider_config(path)
    try:
        return config, build_provider(config, config_dir=path.parent, environ=environ)
    except ProviderConfigError as error:
        error.path = path
        raise
