"""The Anthropic Messages API adapter: the only assistant module that imports the SDK.

The client is built only from the configuration. The SDK would otherwise read the
environment for its credential, base URL, extra headers, proxies, tracing, and logging,
so the adapter passes the key and base URL explicitly, and sends every request through a
transport that keeps only the headers the SDK itself needs and sets the credential last;
`httpx2` applies no environment proxy to a client given its own transport. Redirects and
OpenTelemetry spans are off, and the SDK's loggers stay at `WARNING`, because their debug
records hold request bodies.

Building the adapter makes no request. The request and response mapping arrives with step
40.3; until then every request fails with `ProviderUnavailableError`.
"""

import logging
from typing import Any

import anthropic
import httpx2
from pydantic import SecretStr

from .config import AnthropicProviderConfig
from .provider import (
    CompletionRequest,
    CompletionResponse,
    ProviderCapabilities,
    ProviderCapability,
    ProviderUnavailableError,
)

DEFAULT_BASE_URL = "https://api.anthropic.com"

# What the SDK sends on its own; anything else (such as `ANTHROPIC_CUSTOM_HEADERS`) is dropped.
_SDK_HEADERS = frozenset(
    {
        "accept",
        "accept-encoding",
        "anthropic-version",
        "connection",
        "content-length",
        "content-type",
        "host",
        "user-agent",
    }
)
_SDK_HEADER_PREFIX = "x-stainless-"
_SDK_LOGGERS = ("anthropic", "httpx2")


def _guard(request: httpx2.Request, api_key: SecretStr) -> httpx2.Request:
    """Keep only the SDK's own headers, then send the configured credential and no other."""
    for name in list(request.headers):
        if name not in _SDK_HEADERS and not name.startswith(_SDK_HEADER_PREFIX):
            del request.headers[name]
    request.headers["authorization"] = f"Bearer {api_key.get_secret_value()}"
    return request


class _GuardedTransport(httpx2.BaseTransport):
    def __init__(self, inner: httpx2.BaseTransport, api_key: SecretStr) -> None:
        self._inner = inner
        self._api_key = api_key

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        return self._inner.handle_request(_guard(request, self._api_key))

    def close(self) -> None:
        self._inner.close()


class _AsyncGuardedTransport(httpx2.AsyncBaseTransport):
    def __init__(self, inner: httpx2.AsyncBaseTransport, api_key: SecretStr) -> None:
        self._inner = inner
        self._api_key = api_key

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        return await self._inner.handle_async_request(_guard(request, self._api_key))

    async def aclose(self) -> None:
        await self._inner.aclose()


def _quiet_sdk_loggers() -> None:
    """Hold the SDK's loggers at `WARNING`, whatever `ANTHROPIC_LOG` or the root level says."""
    for name in _SDK_LOGGERS:
        logger = logging.getLogger(name)
        if logger.getEffectiveLevel() < logging.WARNING:
            logger.setLevel(logging.WARNING)


class AnthropicProvider:
    """A `Provider` over the Messages API, built from one configuration and its key.

    `transport` and `async_transport` replace the network transports, so tests can mock
    the API; the guard wraps them as it wraps the real ones.
    """

    def __init__(
        self,
        config: AnthropicProviderConfig,
        api_key: SecretStr,
        *,
        transport: httpx2.BaseTransport | None = None,
        async_transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        _quiet_sdk_loggers()
        self.model = config.model
        self.base_url = DEFAULT_BASE_URL if config.endpoint is None else str(config.endpoint)
        timeout = httpx2.Timeout(
            config.request_timeout_seconds, connect=config.connect_timeout_seconds
        )
        client_options: dict[str, Any] = {"timeout": timeout, "follow_redirects": False}
        sdk_options: dict[str, Any] = {
            "api_key": api_key.get_secret_value(),
            "base_url": self.base_url,
            "timeout": timeout,
            "max_retries": 0,
            "open_telemetry": False,
        }
        self._client = anthropic.Anthropic(
            **sdk_options,
            http_client=httpx2.Client(
                transport=_GuardedTransport(transport or httpx2.HTTPTransport(), api_key),
                **client_options,
            ),
        )
        self._async_client = anthropic.AsyncAnthropic(
            **sdk_options,
            http_client=httpx2.AsyncClient(
                transport=_AsyncGuardedTransport(
                    async_transport or httpx2.AsyncHTTPTransport(), api_key
                ),
                **client_options,
            ),
        )

    def __repr__(self) -> str:
        return f"AnthropicProvider(model={self.model!r}, base_url={self.base_url!r})"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            capabilities={ProviderCapability.TOOL_CALLING, ProviderCapability.PARALLEL_TOOL_CALLS}
        )

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        raise ProviderUnavailableError()

    async def complete_async(self, request: CompletionRequest) -> CompletionResponse:
        raise ProviderUnavailableError()

    def _create(self, **params: Any) -> anthropic.types.Message:
        """Send one Messages API request; the mapping layer (step 40.3) builds `params`."""
        return self._client.messages.create(**params)

    async def _create_async(self, **params: Any) -> anthropic.types.Message:
        return await self._async_client.messages.create(**params)
