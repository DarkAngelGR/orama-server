"""Host allowlist, CSRF and security headers."""

from __future__ import annotations

import socket
import threading

import pytest
from werkzeug.test import EnvironBuilder

from orama.setup_model import DesiredSetup
from orama.web import create_app
from orama.web.security import allowed_hosts

from .conftest import (
    EXTRA,
    HOST_NAME,
    LAN,
    NEW_LAN,
    csrf_from,
    signin,
    write_setup,
)

HOST_MESSAGE = (
    "Orama does not answer at this address. Open it with the address the installer showed, "
    "or add this name in Settings → Addresses."
)
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "connect-src 'self'; font-src 'self'; form-action 'self'; frame-ancestors 'none'; "
    "base-uri 'none'"
)
EXPIRED = "This page expired. Go back, reload it and try again."


def get_host(client, host: str, path: str = "/healthz"):
    return client.get(path, headers={"Host": host})


# ---------------------------------------------------------------- Host


def test_allowed_hosts_lists_every_kind(state_dir):
    setup = DesiredSetup.load(state_dir / "setup.json")
    assert allowed_hosts(setup) == frozenset(
        {"localhost", "127.0.0.1", "::1", LAN, HOST_NAME, f"{HOST_NAME}.local", EXTRA}
    )
    assert allowed_hosts(None) == frozenset({"localhost", "127.0.0.1", "::1"})


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "LOCALHOST:8765",
        "localhost.",
        "127.0.0.1",
        "127.0.0.1:8765",
        "[::1]",
        "[::1]:8765",
        LAN,
        f"{LAN}:8765",
        f"{LAN}.",
        HOST_NAME,
        HOST_NAME.upper() + ":8765",
        f"{HOST_NAME}.",
        f"{HOST_NAME}.local",
        f"{HOST_NAME}.LOCAL.:8765",
        EXTRA,
        EXTRA.upper() + ".:443",
    ],
)
def test_allowed_hosts_accepted(client, host):
    response = get_host(client, host)
    assert response.status_code == 200
    assert response.data == b"ok"


@pytest.mark.parametrize(
    "host",
    [
        "evil.example",
        "evil.example:8765",
        f"{HOST_NAME}.evil.example",
        "localhost..",
        "localhost:abc",
        "localhost:",
        "::1",
        "[::1",
        "[::2]:8765",
        f"x{LAN}",
        "local host",
        "localhost:8765:1",
    ],
)
def test_unknown_host_rejected_without_listing_names(client, host):
    response = get_host(client, host, "/signin")
    assert response.status_code == 400
    text = response.get_data(as_text=True)
    assert HOST_MESSAGE in text
    for name in (LAN, HOST_NAME, EXTRA, "localhost", "127.0.0.1"):
        assert name not in text
    assert host not in text
    assert "Set-Cookie" not in response.headers


def test_missing_host_rejected(app):
    environ = EnvironBuilder(path="/healthz").get_environ()
    del environ["HTTP_HOST"]
    captured = {}

    def start_response(status, headers, exc_info=None):
        captured["status"] = status

    body = b"".join(app(environ, start_response))
    assert captured["status"].startswith("400")
    assert HOST_MESSAGE.encode() in body

    environ = EnvironBuilder(path="/healthz").get_environ()
    environ["HTTP_HOST"] = ""
    body = b"".join(app(environ, start_response))
    assert captured["status"].startswith("400")


def test_two_host_headers_rejected(client):
    # Waitress joins repeated headers with ", ".
    for joined in ("localhost, localhost", "localhost,evil.example", f"{LAN}, evil.example"):
        response = client.get("/healthz", environ_overrides={"HTTP_HOST": joined})
        assert response.status_code == 400
        assert HOST_MESSAGE in response.get_data(as_text=True)


def test_new_lan_address_allowed_without_restart(client, state_dir):
    assert get_host(client, NEW_LAN).status_code == 400
    write_setup(state_dir, lan_address=NEW_LAN)
    assert get_host(client, NEW_LAN).status_code == 200
    assert get_host(client, LAN).status_code == 400


def test_draft_setup_used_until_saved(tmp_path, clock):
    state_dir = tmp_path / "orama"
    state_dir.mkdir()
    client = create_app(state_dir, clock=clock).test_client()
    assert get_host(client, LAN).status_code == 400
    write_setup(state_dir, "setup.draft.json")
    assert get_host(client, LAN).status_code == 200
    write_setup(state_dir, lan_address=NEW_LAN)  # setup.json wins over the draft
    assert get_host(client, LAN).status_code == 400
    assert get_host(client, NEW_LAN).status_code == 200


def test_unreadable_setup_means_local_names_only(state_dir, client):
    (state_dir / "setup.json").write_text("{not json")
    assert get_host(client, LAN).status_code == 400
    assert get_host(client, "127.0.0.1").status_code == 200


def test_healthz_on_127_0_0_1(client):
    response = get_host(client, "127.0.0.1")
    assert response.status_code == 200
    assert response.data == b"ok"
    assert response.mimetype == "text/plain"


# ---------------------------------------------------------------- CSRF


def _post_rules(app):
    """A URL for every POST rule, built by Flask with sample values for its arguments."""
    from flask import url_for
    from werkzeug.routing import BuildError

    urls = []
    with app.test_request_context():
        for rule in app.url_map.iter_rules():
            if "POST" not in (rule.methods or ()):
                continue
            try:
                url = url_for(rule.endpoint, **{arg: "x" for arg in rule.arguments})
            except BuildError:  # a number converter
                url = url_for(rule.endpoint, **{arg: 1 for arg in rule.arguments})
            urls.append(url)
    return urls


def test_post_without_token_refused(app, client, password):
    rules = list(_post_rules(app))
    assert {"/signin", "/signout", "/setup/password"} <= set(rules)
    for path in rules:
        # Without any session.
        fresh = app.test_client()
        response = fresh.post(path, data={"password": password})
        assert response.status_code == 403, path
        assert EXPIRED in response.get_data(as_text=True)
    # With a session (pre-sign-in, then signed in) but no token.
    client.get("/signin")
    for path in rules:
        assert client.post(path, data={"password": password}).status_code == 403, path
    assert signin(client, password).status_code == 303
    for path in rules:
        assert client.post(path, data={}).status_code == 403, path
    for method in ("put", "patch", "delete"):
        assert getattr(client, method)("/signout").status_code == 403


def test_post_with_wrong_token_refused(client, password):
    token = csrf_from(client.get("/signin"))
    other = csrf_from(client.application.test_client().get("/signin"))
    assert other != token
    for bad in (other, token[:-1], "", token + "x"):
        response = client.post("/signin", data={"password": password, "csrf_token": bad})
        assert response.status_code == 403
        response = client.post(
            "/signin", data={"password": password}, headers={"X-CSRF-Token": bad}
        )
        assert response.status_code == 403


def test_htmx_header_token_accepted(client, password):
    signin(client, password)
    page = client.get("/")
    token = csrf_from(page)
    assert f'"X-CSRF-Token": "{token}"' in page.get_data(as_text=True).replace(
        "&#34;", '"'
    )
    response = client.post("/signout", headers={"X-CSRF-Token": token})
    assert response.status_code == 303


def test_cross_origin_post_refused(client, password):
    token = csrf_from(client.get("/signin"))
    for origin in ("http://evil.example", "null", "https://localhost", "http://localhost:1"):
        response = client.post(
            "/signin",
            data={"password": password, "csrf_token": token},
            headers={"Origin": origin},
        )
        assert response.status_code == 403, origin
    response = client.post(
        "/signin",
        data={"password": password, "csrf_token": token},
        headers={"Origin": "http://localhost"},
    )
    assert response.status_code == 303


def test_same_origin_post_with_port_and_case(client, password):
    token = csrf_from(client.get("/signin", headers={"Host": "LocalHost:8765"}))
    response = client.post(
        "/signin",
        data={"password": password, "csrf_token": token},
        headers={"Origin": "http://localhost:8765", "Host": "LocalHost:8765"},
    )
    assert response.status_code == 303


# ---------------------------------------------------------------- headers


def _assert_security_headers(response, *, html: bool = True):
    h = response.headers
    assert h["Content-Security-Policy"] == CSP
    assert h["X-Content-Type-Options"] == "nosniff"
    assert h["Referrer-Policy"] == "no-referrer"
    assert h["X-Frame-Options"] == "DENY"
    assert h["Cross-Origin-Opener-Policy"] == "same-origin"
    if html:
        assert h["Cache-Control"] == "no-store"
    assert not [k for k, _ in h.items() if k.lower().startswith("access-control-")]


def test_security_headers_on_every_response(client, app, password):
    _assert_security_headers(client.get("/signin"))
    _assert_security_headers(client.get("/healthz"))
    _assert_security_headers(get_host(client, "evil.example"))
    _assert_security_headers(client.get("/no-such-page"))
    _assert_security_headers(client.post("/signin"))  # 403
    static = client.get("/static/orama.css")
    assert static.status_code == 200
    _assert_security_headers(static, html=False)
    assert "no-store" not in static.headers.get("Cache-Control", "")


def test_not_found_page_is_plain(client):
    response = client.get("/no-such-page")
    assert response.status_code == 404
    text = response.get_data(as_text=True)
    assert "404" not in text
    assert "no-such-page" not in text


def test_no_cors_headers_even_with_origin(client, password):
    origin = {"Origin": "http://evil.example"}
    for response in (
        client.get("/signin", headers=origin),
        client.get("/healthz", headers=origin),
        client.post("/signin", headers=origin),
        client.options("/signin", headers={**origin, "Access-Control-Request-Method": "POST"}),
    ):
        assert not [
            k for k, _ in response.headers.items() if k.lower().startswith("access-control-")
        ]


def test_options_not_allowed(client):
    for path in ("/signin", "/healthz", "/", "/static/orama.css", "/no-such-page", "*"):
        response = client.options(path)
        assert response.status_code == 405, path
        _assert_security_headers(response)


def test_templates_have_no_inline_script_or_style(client, first_run_code):
    pages = [
        client.get("/signin"),
        client.get("/setup/password"),
        client.get(f"/setup/password?code={first_run_code}"),
        client.get("/no-such-page"),
    ]
    for page in pages:
        text = page.get_data(as_text=True).lower()
        assert "<script>" not in text
        assert "<style" not in text
        assert " style=" not in text
        assert " onclick=" not in text and " onload=" not in text


# ---------------------------------------------------------------- the real server


@pytest.fixture
def served(app):
    from waitress import create_server

    from orama.__main__ import SERVE_OPTIONS

    options = {**SERVE_OPTIONS, "host": "127.0.0.1", "port": 0, "threads": 2}
    server = create_server(app, **options)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        yield server.effective_port
    finally:
        server.close()
        thread.join(5)


def _raw(port: int, request: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(request)
        data = b""
        while chunk := s.recv(65536):
            data += chunk
    return data


def test_waitress_healthz_and_no_server_header(served):
    reply = _raw(served, b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
    head, _, body = reply.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 200")
    assert body == b"ok"
    assert b"\r\nserver:" not in head.lower()


def test_waitress_two_host_headers_rejected(served):
    reply = _raw(
        served,
        b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\nHost: 127.0.0.1\r\n"
        b"Connection: close\r\n\r\n",
    )
    assert reply.startswith(b"HTTP/1.1 400")


def test_waitress_missing_host_rejected(served):
    reply = _raw(served, b"GET /healthz HTTP/1.0\r\n\r\n")
    assert reply.startswith((b"HTTP/1.0 400", b"HTTP/1.1 400"))


def test_waitress_refuses_oversize_body_before_flask(served, app, monkeypatch):
    reached = []
    monkeypatch.setattr(app, "wsgi_app", lambda environ, start: reached.append(1) or [])
    size = 1024 * 1024 + 1
    reply = _raw(
        served,
        b"POST /signin HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n"
        b"Content-Type: application/x-www-form-urlencoded\r\n"
        + f"Content-Length: {size}\r\n\r\n".encode(),  # refused from the header alone
    )
    assert reply.startswith(b"HTTP/1.1 413")
    assert reached == []
