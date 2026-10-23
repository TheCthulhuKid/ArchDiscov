"""Mocked provider APIs for adapter tests: an HTTP transport and a loopback server.

`MockApi` replaces an adapter's transport in process; `mock_api_server` serves response
bodies on a loopback port for an installed command in a child process. Both keep what they
received, so a test can check the requests, and neither reaches the network.
"""

import json
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx2

from archdiscov.assistant.fake import FakeClock

type Served = httpx2.Response | Exception | Callable[[httpx2.Request], httpx2.Response]


class DelayedResponse(httpx2.Response):
    """A response the API sends after `delay` seconds, without waiting in the test."""

    def __init__(self, delay: float, **kwargs: Any) -> None:
        super().__init__(200, **kwargs)
        self.delay = delay


class MockApi(httpx2.MockTransport):
    """A mocked API: answers each request with the next response, and keeps the requests.

    A response may be a function of the request, or an exception, raised as the transport
    would. A `DelayedResponse` advances `clock` by its delay, up to the request's read
    timeout, and a delay beyond that timeout is a read timeout.
    """

    def __init__(self, responses: Sequence[Served], clock: FakeClock | None = None) -> None:
        self.requests: list[httpx2.Request] = []
        self._responses = list(responses)
        self._clock = clock
        super().__init__(self._handle)

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        response = self._responses.pop(0)
        if callable(response):
            response = response(request)
        if isinstance(response, Exception):
            raise response
        delay, timeout = getattr(response, "delay", 0.0), request.extensions["timeout"]["read"]
        if self._clock is not None:
            self._clock.sleep(min(delay, timeout))
        if delay > timeout:
            raise httpx2.ReadTimeout("scripted delay", request=request)
        return response


def status_failure(
    status: int, body: Callable[[str], Any]
) -> Callable[[httpx2.Request], httpx2.Response]:
    """An API error whose body (in the API's own shape) echoes the request, so a leak shows."""
    return lambda request: httpx2.Response(
        status, json=body(f"Rejected: {request.content.decode()}")
    )


def transport_failure(error: type[httpx2.TransportError]) -> Callable[[httpx2.Request], Exception]:
    return lambda request: error("scripted transport failure", request=request)


def unparseable_body(request: httpx2.Request) -> httpx2.Response:
    """A success that is not JSON, as a broken server or gateway might send."""
    return httpx2.Response(200, content=b"{not json", headers={"content-type": "application/json"})


class Waits(list):
    """The waits between retries, recorded instead of slept."""

    def sleep(self, seconds: float) -> None:
        self.append(seconds)

    async def sleep_async(self, seconds: float) -> None:
        self.append(seconds)


def no_wait(seconds: float) -> None:
    """Retries in the suites do not wait; the adapter tests check the waits."""


async def no_wait_async(seconds: float) -> None:
    """Retries in the suites do not wait; the adapter tests check the waits."""


class _ApiHandler(BaseHTTPRequestHandler):
    """Each POST gets the server's next body; the requests are kept on the server."""

    server: "MockApiServer"

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["content-length"]))
        self.server.requests.append((self.path, dict(self.headers), json.loads(body)))
        payload = json.dumps(self.server.bodies.pop(0)).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: Any) -> None:
        """Requests are kept on the server, not logged."""


class MockApiServer(ThreadingHTTPServer):
    """A loopback HTTP server answering each POST with the next of `bodies`."""

    def __init__(self, bodies: list[dict]) -> None:
        self.bodies = bodies
        self.requests: list[tuple[str, dict, dict]] = []
        super().__init__(("127.0.0.1", 0), _ApiHandler)

    @property
    def port(self) -> int:
        return self.server_address[1]


@contextmanager
def mock_api_server(bodies: list[dict]) -> Iterator[MockApiServer]:
    server = MockApiServer(bodies)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
