"""Minimal stdlib HTTP client for the provider checks (design §4 `doctor`, §3 expiry).

Every provider module takes a `Transport` so tests replay recorded responses (tests/fixtures/http/) and never
reach a real API. Request headers may carry credentials: nothing here logs a request, and errors name the
host and path only (never query strings, headers or bodies).
"""

from __future__ import annotations

import http.client
import io
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from mcctl import __version__

USER_AGENT = f"mcctl/{__version__} (+https://github.com/edvardk/smallworld)"
TIMEOUT = 20.0
BLOCKSIZE = 65536  # a request body is written in blocks of this size, each under its own timeout


@dataclass(frozen=True)
class Request:
    method: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes | None = None
    # seconds per socket operation (connect, each BLOCKSIZE block of the body, each read); None: TIMEOUT.
    # An idle timeout, not a cap on the whole request.
    timeout: float | None = None


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str]  # lower-case names
    body: bytes

    def json(self) -> Any:  # noqa: ANN401 - arbitrary JSON
        try:
            return json.loads(self.body or b"null")
        except ValueError:
            return None

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())


Transport = Callable[[Request], Response]


class HttpError(RuntimeError):
    """The request didn't produce an HTTP response (DNS, TLS, timeout, connection refused)."""


def where(url: str) -> str:
    """host/path of a URL, safe to show (no query string, which could carry a token)."""
    parts = urllib.parse.urlsplit(url)
    return f"{parts.hostname}{parts.path}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirects are returned, not followed: an Authorization header must not follow a redirect elsewhere."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        return None


# A server may answer (401, 413, ...) and stop reading before the whole body is sent: the next write then
# fails or times out. Its answer is what the caller needs, so after such an error the response is still read.
_WRITE_ERRORS = (TimeoutError, BrokenPipeError, ConnectionResetError, ssl.SSLError)


class _AnswerFirst:
    """Mixin for http.client connections: a failed body write doesn't hide the server's answer."""

    sock: Any
    write_error: OSError | None = None

    def send(self, data: Any) -> None:  # noqa: ANN401 - bytes or a file-like, as http.client
        if self.write_error is not None:
            return  # the server stopped reading: skip the rest of the body, read its answer
        if self.sock is None:
            super().send(data)  # type: ignore[misc] # connects (and tunnels): those errors are errors
            return
        try:
            super().send(data)  # type: ignore[misc]
        except _WRITE_ERRORS as e:
            self.write_error = e

    def getresponse(self) -> http.client.HTTPResponse:
        try:
            return super().getresponse()  # type: ignore[misc,no-any-return]
        except (OSError, http.client.HTTPException):
            if self.write_error is not None:
                raise self.write_error from None  # no answer after all: the write's error is the story
            raise


class _HTTPConnection(_AnswerFirst, http.client.HTTPConnection):
    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        super().__init__(*args, **({"blocksize": BLOCKSIZE} | kwargs))


class _HTTPSConnection(_AnswerFirst, http.client.HTTPSConnection):
    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        super().__init__(*args, **({"blocksize": BLOCKSIZE} | kwargs))


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        return self.do_open(_HTTPConnection, req)  # type: ignore[no-any-return]


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self) -> None:
        super().__init__()
        self.tls = ssl.create_default_context()
        self.tls.minimum_version = ssl.TLSVersion.TLSv1_2  # the default since Python 3.10, made explicit

    def https_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        return self.do_open(_HTTPSConnection, req, context=self.tls)  # type: ignore[no-any-return]


def build_opener(proxies: Mapping[str, str] | None = None) -> urllib.request.OpenerDirector:
    """The opener requests go through; `proxies` None: from the environment, as urllib does."""
    proxy = urllib.request.ProxyHandler(None if proxies is None else dict(proxies))
    return urllib.request.build_opener(proxy, _NoRedirect, _HTTPHandler, _HTTPSHandler)


_OPENER = build_opener()


def urllib_transport(req: Request) -> Response:
    if not req.url.startswith("https://"):
        raise HttpError(f"refusing a non-HTTPS request to {where(req.url)}")
    return send(req, _OPENER)


def send(req: Request, opener: urllib.request.OpenerDirector) -> Response:
    """One request through `opener`, whatever the scheme (urllib_transport is the https-only entry point)."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json", **req.headers}
    data: io.BytesIO | None = None
    if req.body is not None:
        # a file-like body is written block by block, so the timeout applies to each block, not the total
        data = io.BytesIO(req.body)
        if not any(k.lower() == "content-length" for k in headers):
            headers["Content-Length"] = str(len(req.body))
    r = urllib.request.Request(  # noqa: S310 - the scheme is the caller's (urllib_transport: https only)
        req.url, data=data, method=req.method, headers=headers
    )
    try:
        with opener.open(r, timeout=req.timeout or TIMEOUT) as resp:
            return Response(resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read())
    except urllib.error.HTTPError as e:
        return Response(e.code, {k.lower(): v for k, v in e.headers.items()}, e.read())
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        reason = getattr(e, "reason", e)
        raise HttpError(f"{where(req.url)}: {reason}") from None


def get(transport: Transport, url: str, headers: Mapping[str, str] | None = None) -> Response:
    return transport(Request("GET", url, dict(headers or {})))


def post_form(
    transport: Transport, url: str, form: Mapping[str, str], headers: Mapping[str, str] | None = None
) -> Response:
    body = urllib.parse.urlencode(dict(form)).encode()
    hdrs = {"Content-Type": "application/x-www-form-urlencoded", **(headers or {})}
    return transport(Request("POST", url, hdrs, body))


def send_json(
    transport: Transport,
    method: str,
    url: str,
    payload: Any,  # noqa: ANN401 - any JSON
    headers: Mapping[str, str] | None = None,
) -> Response:
    body = json.dumps(payload).encode()
    return transport(Request(method, url, {"Content-Type": "application/json", **(headers or {})}, body))
