"""Small HTTP client for talking to the apps on the internal network."""

from __future__ import annotations

import http.client
import json as jsonlib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from http.cookiejar import CookieJar
from typing import Any


class Unreachable(Exception):
    """The app could not be reached or returned an unsafe response."""

    def __init__(self, app_url: str):
        super().__init__(app_url)
        self.app_url = app_url


MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class UnsafeRedirect(Unreachable):
    """The app attempted to redirect an authenticated API request."""


class ResponseTooLarge(Unreachable):
    """The app returned a response larger than Orama is willing to buffer."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """API requests must never follow redirects, especially across origins."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _read_response(resp: Any, url: str) -> Response:
    length = resp.headers.get("Content-Length")
    if length is not None:
        try:
            if int(length) > MAX_RESPONSE_BYTES:
                raise ResponseTooLarge(_origin(url))
        except ValueError:
            pass
    body = resp.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ResponseTooLarge(_origin(url))
    return Response(resp.status, dict(resp.headers.items()), body)


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return jsonlib.loads(self.body)


def _origin(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


class Http:
    """Stdlib HTTP client with a cookie jar per instance and no proxy use.

    Never raises on HTTP status codes; raises Unreachable on connection failure.
    """

    def __init__(self) -> None:
        self._cookies = CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirect(),
            urllib.request.HTTPCookieProcessor(self._cookies),
        )

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json: Any = None,
        form: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> Response:
        if params:
            sep = "&" if "?" in url else "?"
            url = url + sep + urllib.parse.urlencode(params)
        send_headers = dict(headers or {})
        data: bytes | None = None
        if json is not None:
            data = jsonlib.dumps(json).encode()
            send_headers.setdefault("Content-Type", "application/json")
        elif form is not None:
            data = urllib.parse.urlencode(form, doseq=True).encode()
            send_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        req = urllib.request.Request(url, data=data, headers=send_headers, method=method.upper())
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                return _read_response(resp, url)
        except urllib.error.HTTPError as err:
            with err:
                if 300 <= err.code < 400:
                    raise UnsafeRedirect(_origin(url)) from None
                return _read_response(err, url)
        except (urllib.error.URLError, OSError, http.client.HTTPException) as err:
            raise Unreachable(_origin(url)) from err


@dataclass
class Call:
    method: str
    url: str
    path: str
    headers: dict[str, str] | None
    params: dict[str, Any] | None
    json: Any
    form: dict[str, Any] | None


@dataclass
class FakeHttp(Http):
    """Test double: routes (method, path) to handlers and records every call."""

    routes: dict[tuple[str, str], Callable[[Call], Response]] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)
    unreachable: bool = False

    def __post_init__(self) -> None:
        pass  # no real opener needed

    def route(self, method: str, path: str, handler: Callable[[Call], Response]) -> None:
        self.routes[(method.upper(), path)] = handler

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json: Any = None,
        form: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> Response:
        call = Call(
            method.upper(), url, urllib.parse.urlsplit(url).path, headers, params, json, form
        )
        self.calls.append(call)
        if self.unreachable:
            raise Unreachable(_origin(url))
        handler = self.routes.get((call.method, call.path))
        if handler is None:
            return Response(404, {}, b"")
        return handler(call)
