"""The OpenAI-compatible adapter: the only assistant module that imports the `openai` SDK.

One adapter serves both protocols; the configured `protocol` chooses the endpoint, and is
never guessed from the server. The client is built only from the configuration: the key
and the required `endpoint` are passed explicitly, and requests go through the shared
guarded clients (`adapters.py`), so `OPENAI_BASE_URL`, the organisation and project
variables, `OPENAI_CUSTOM_HEADERS`, and an admin key cannot change the target, headers, or
credential. The SDK's loggers stay at `WARNING`, whatever `OPENAI_LOG` says.

A compatible server cannot be asked what its model supports, so the capabilities are the
ones the configuration declares, and a request needing any other is refused before a call.

Building the adapter makes no request. Until the protocol mappings arrive (steps 41.3 and
41.4), `complete` raises `ProviderUnavailableError` without sending anything.
"""

from collections.abc import Callable, Mapping
from typing import Any

import httpx2
import openai
from pydantic import SecretStr

from .adapters import HTTP_HEADERS, HeaderGuard, guarded_http_clients, quiet_loggers
from .config import OpenAICompatibleProviderConfig, OpenAIProtocol
from .provider import (
    CompletionRequest,
    CompletionResponse,
    ProviderCapabilities,
    ProviderUnavailableError,
)

_SDK_HEADER_PREFIX = "x-stainless-"
_SDK_LOGGERS = ("openai", "httpx2")

type _Client = openai.OpenAI | openai.AsyncOpenAI
# The SDK method each protocol sends with; the sync and async clients share its path.
_ENDPOINTS: Mapping[OpenAIProtocol, Callable[[_Client], Callable[..., Any]]] = {
    OpenAIProtocol.CHAT_COMPLETIONS: lambda client: client.chat.completions.create,
    OpenAIProtocol.RESPONSES: lambda client: client.responses.create,
}


class OpenAIProvider:
    """A `Provider` over an OpenAI-compatible API, built from one configuration and its key.

    `transport` and `async_transport` replace the network transports, so tests can mock
    the API (see `guarded_http_clients`).
    """

    def __init__(
        self,
        config: OpenAICompatibleProviderConfig,
        api_key: SecretStr,
        *,
        transport: httpx2.BaseTransport | None = None,
        async_transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        quiet_loggers(_SDK_LOGGERS)
        self.model = config.model
        self.protocol = config.protocol
        self.base_url = str(config.endpoint)
        self._capabilities = ProviderCapabilities(capabilities=config.capabilities)
        timeout = httpx2.Timeout(
            config.request_timeout_seconds, connect=config.connect_timeout_seconds
        )
        guard = HeaderGuard(
            headers=HTTP_HEADERS,
            prefixes=(_SDK_HEADER_PREFIX,),
            credentials={"authorization": SecretStr(f"Bearer {api_key.get_secret_value()}")},
        )
        http, async_http = guarded_http_clients(
            guard, timeout, transport=transport, async_transport=async_transport
        )
        sdk_options: dict[str, Any] = {
            "api_key": api_key.get_secret_value(),
            "base_url": self.base_url,
            "timeout": timeout,
            "max_retries": 0,
        }
        self._client = openai.OpenAI(**sdk_options, http_client=http)
        self._async_client = openai.AsyncOpenAI(**sdk_options, http_client=async_http)

    def __repr__(self) -> str:
        return (
            f"OpenAIProvider(model={self.model!r}, base_url={self.base_url!r}, "
            f"protocol={self.protocol.value!r})"
        )

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        raise ProviderUnavailableError()

    async def complete_async(self, request: CompletionRequest) -> CompletionResponse:
        raise ProviderUnavailableError()

    def _create(self, **params: Any) -> Any:
        """Send one request to the protocol's endpoint; steps 41.3 and 41.4 build `params`."""
        return _ENDPOINTS[self.protocol](self._client)(**params)

    async def _create_async(self, **params: Any) -> Any:
        return await _ENDPOINTS[self.protocol](self._async_client)(**params)
