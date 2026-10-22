"""Building a provider from its configuration.

`build_provider` looks the configured kind up in `ADAPTERS`, resolves the credential the
configuration references, and passes it to that kind's adapter, so steps 40-42 add a
provider by registering an adapter here, without changing callers. A kind without an
adapter fails before any credential is read or any request is made. Credentials go only to
the adapter: they are never returned, logged, or recorded.
"""

from collections.abc import Callable, Mapping
from pathlib import Path

from pydantic import SecretStr

from .config import (
    FakeProviderConfig,
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


ADAPTERS: Mapping[ProviderKind, Adapter] = {ProviderKind.FAKE: _fake}


def build_provider(
    config: ProviderConfig,
    *,
    config_dir: Path,
    environ: Mapping[str, str] | None = None,
) -> Provider:
    """The provider for a configuration; relative paths in it resolve against `config_dir`."""
    adapter = ADAPTERS.get(config.kind)
    if adapter is None:
        raise ProviderConfigError(ProviderConfigErrorCode.ADAPTER_UNAVAILABLE, kind=config.kind)
    return adapter(config, resolve_api_key(config, environ), config_dir)


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
