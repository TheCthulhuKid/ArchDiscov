"""Plumbing every vendor adapter shares; it imports no vendor SDK.

Vendor SDKs read the environment for their credential, base URL, extra headers, proxies,
tracing, and logging. An adapter therefore builds its HTTP clients with
`guarded_http_clients`, whose transport keeps only the headers the SDK itself sends and then
sets the configured credential, so nothing in the environment can add headers or replace
the key; `httpx2` applies no environment proxy to a client given its own transport, and
redirects are not followed. `quiet_loggers` holds the SDK's loggers at `WARNING`, because
their debug records hold request bodies.

`Retries` maps an SDK failure to a typed provider error through the failure's class
hierarchy, never by message text, and retries only transport and rate-limit errors, up to
`max_retries`, after a bounded exponential backoff or the server's `retry-after`, never
longer than the request timeout. The response rules apply to any protocol: `require_known`
refuses unknown block or item types, and `require_text_only` refuses a length or refusal
stop that carries tool calls or no text.
"""

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from itertools import count
from typing import Any

import httpx2
from pydantic import SecretStr

from .provider import (
    ProviderError,
    ProviderMalformedOutputError,
    ProviderRefusalError,
    ProviderTruncatedOutputError,
    StopReason,
)

# The HTTP headers every SDK's client sends itself; each adapter adds its SDK's own names.
HTTP_HEADERS = frozenset(
    {
        "accept",
        "accept-encoding",
        "connection",
        "content-length",
        "content-type",
        "host",
        "user-agent",
    }
)
RETRY_BACKOFF_SECONDS = 0.5
MAX_RETRY_WAIT_SECONDS = 30.0

# These stops are usable only as text without tool calls; otherwise they are this error.
_TEXT_ONLY_STOPS: Mapping[StopReason, type[ProviderError]] = {
    StopReason.LENGTH: ProviderTruncatedOutputError,
    StopReason.REFUSAL: ProviderRefusalError,
}


# ---- clients ------------------------------------------------------------------------


@dataclass(frozen=True)
class HeaderGuard:
    """The SDK's own headers, kept; every other header is dropped, then `credentials` set."""

    headers: frozenset[str]
    prefixes: tuple[str, ...]
    credentials: Mapping[str, SecretStr]

    def __call__(self, request: httpx2.Request) -> httpx2.Request:
        for name in list(request.headers):
            if name not in self.headers and not name.startswith(self.prefixes):
                del request.headers[name]
        for name, value in self.credentials.items():
            request.headers[name] = value.get_secret_value()
        return request


class _GuardedTransport(httpx2.BaseTransport):
    def __init__(self, inner: httpx2.BaseTransport, guard: HeaderGuard) -> None:
        self._inner = inner
        self._guard = guard

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        return self._inner.handle_request(self._guard(request))

    def close(self) -> None:
        self._inner.close()


class _AsyncGuardedTransport(httpx2.AsyncBaseTransport):
    def __init__(self, inner: httpx2.AsyncBaseTransport, guard: HeaderGuard) -> None:
        self._inner = inner
        self._guard = guard

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        return await self._inner.handle_async_request(self._guard(request))

    async def aclose(self) -> None:
        await self._inner.aclose()


def guarded_http_clients(
    guard: HeaderGuard,
    timeout: httpx2.Timeout,
    *,
    transport: httpx2.BaseTransport | None = None,
    async_transport: httpx2.AsyncBaseTransport | None = None,
) -> tuple[httpx2.Client, httpx2.AsyncClient]:
    """Sync and async clients that send through `guard` and follow no redirect.

    `transport` and `async_transport` replace the network transports, so tests can mock
    the API; the guard wraps them as it wraps the real ones.
    """
    options: dict[str, Any] = {"timeout": timeout, "follow_redirects": False}
    sync = httpx2.Client(
        transport=_GuardedTransport(transport or httpx2.HTTPTransport(), guard), **options
    )
    asynchronous = httpx2.AsyncClient(
        transport=_AsyncGuardedTransport(async_transport or httpx2.AsyncHTTPTransport(), guard),
        **options,
    )
    return sync, asynchronous


def quiet_loggers(names: Iterable[str]) -> None:
    """Hold these loggers at `WARNING`, whatever the SDK's log variable or the root says."""
    for name in names:
        logger = logging.getLogger(name)
        if logger.getEffectiveLevel() < logging.WARNING:
            logger.setLevel(logging.WARNING)


# ---- failures -----------------------------------------------------------------------


def provider_error(
    error: Exception, errors: Mapping[type[Exception], type[ProviderError]]
) -> ProviderError:
    """The typed, fixed-message error for an SDK failure, by the first mapped class."""
    return next(errors[cls] for cls in type(error).__mro__ if cls in errors)()


def retry_wait(error: Exception, attempt: int, timeout_seconds: float) -> float:
    """The server's `retry-after`, else exponential backoff, capped by the request timeout."""
    wait = _retry_after(error)
    if wait is None:
        wait = RETRY_BACKOFF_SECONDS * 2**attempt
    return min(wait, MAX_RETRY_WAIT_SECONDS, timeout_seconds)


def _retry_after(error: Exception) -> float | None:
    """`retry-after` in seconds, when the response sent a usable one."""
    response = getattr(error, "response", None)
    value = None if response is None else response.headers.get("retry-after")
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


@dataclass(frozen=True)
class Retries:
    """Calls the SDK, mapping its failures through `errors` and retrying the retryable ones.

    Only the classes in `errors` (and their subclasses) are caught. `sleep` and
    `async_sleep` wait between attempts.
    """

    errors: Mapping[type[Exception], type[ProviderError]]
    max_retries: int
    sleep: Callable[[float], None] = time.sleep
    async_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

    def call[T](self, send: Callable[[], T], timeout_seconds: float) -> T:
        failures = tuple(self.errors)
        # The last attempt's failure is raised by `_wait`, which ends the loop.
        for attempt in count():
            try:
                return send()
            except failures as error:
                self.sleep(self._wait(error, attempt, timeout_seconds))

    async def call_async[T](self, send: Callable[[], Awaitable[T]], timeout_seconds: float) -> T:
        failures = tuple(self.errors)
        for attempt in count():
            try:
                return await send()
            except failures as error:
                await self.async_sleep(self._wait(error, attempt, timeout_seconds))

    def _wait(self, error: Exception, attempt: int, timeout_seconds: float) -> float:
        """Seconds to wait before the next attempt; raises the provider error when final."""
        failure = provider_error(error, self.errors)
        if not failure.retryable or attempt >= self.max_retries:
            raise failure from error
        return retry_wait(error, attempt, timeout_seconds)


# ---- responses ----------------------------------------------------------------------


def require_known(kinds: Iterable[Any], known: Collection[str]) -> None:
    """Refuse a response holding a block or item type the adapter does not map."""
    if any(kind not in known for kind in kinds):
        raise ProviderMalformedOutputError()


def require_text_only(stop: StopReason, text: str | None, tool_calls: Collection[Any]) -> None:
    """A length or refusal stop is usable only as text alone; otherwise it is its error."""
    if stop in _TEXT_ONLY_STOPS and (tool_calls or text is None):
        raise _TEXT_ONLY_STOPS[stop]()
