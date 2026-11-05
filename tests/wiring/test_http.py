import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from orama.wiring.http import (
    FakeHttp,
    Http,
    Response,
    ResponseTooLarge,
    Unreachable,
    UnsafeRedirect,
)

AWKWARD = "a&b=c%d+e #f Ταινίες"


def test_fake_http_routes_and_records():
    fake = FakeHttp()
    fake.route("GET", "/api", lambda c: Response(200, {}, json.dumps({"n": c.params["k"]}).encode()))
    resp = fake.request("get", "http://sonarr:8989/api", params={"k": 1}, json={"x": 2})
    assert resp.json() == {"n": 1}
    assert fake.calls[0].path == "/api"
    assert fake.calls[0].params == {"k": 1}
    assert fake.calls[0].json == {"x": 2}
    assert fake.request("GET", "http://sonarr:8989/missing").status == 404


def test_fake_http_unreachable():
    fake = FakeHttp(unreachable=True)
    with pytest.raises(Unreachable) as exc:
        fake.request("GET", "http://radarr:7878/x")
    assert exc.value.app_url == "http://radarr:7878"
    assert len(fake.calls) == 1


@pytest.fixture
def server():
    seen = []

    class H(BaseHTTPRequestHandler):
        def _handle(self):
            length = int(self.headers.get("Content-Length") or 0)
            seen.append(
                {
                    "path": self.path,
                    "body": self.rfile.read(length),
                    "ctype": self.headers.get("Content-Type"),
                    "cookie": self.headers.get("Cookie"),
                }
            )
            if self.path.startswith("/redirect"):
                self.send_response(302)
                self.send_header("Location", "/next")
            elif self.path.startswith("/large"):
                self.send_response(200)
            elif self.path.startswith("/login"):
                self.send_response(200)
                self.send_header("Set-Cookie", "SID=abc; Path=/")
            elif self.path.startswith("/missing"):
                self.send_response(404)
            else:
                self.send_response(200)
            body = b"large" if self.path.startswith("/large") else b"{}"
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = _handle

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}", seen
    srv.shutdown()
    srv.server_close()


def test_params_and_form_arrive_intact(server):
    base, seen = server
    http = Http()
    http.request("GET", base + "/q", params={"p": AWKWARD})
    assert parse_qs(urlsplit(seen[0]["path"]).query)["p"] == [AWKWARD]
    http.request("POST", base + "/f", form={"p": AWKWARD})
    assert parse_qs(seen[1]["body"].decode())["p"] == [AWKWARD]
    assert seen[1]["ctype"] == "application/x-www-form-urlencoded"


def test_json_body_and_no_raise_on_status(server):
    base, seen = server
    http = Http()
    resp = http.request("POST", base + "/missing", json={"k": AWKWARD})
    assert resp.status == 404
    assert json.loads(seen[0]["body"]) == {"k": AWKWARD}
    assert seen[0]["ctype"] == "application/json"


def test_cookies_kept_per_instance(server):
    base, seen = server
    http = Http()
    http.request("POST", base + "/login")
    http.request("GET", base + "/next")
    assert seen[1]["cookie"] == "SID=abc"
    Http().request("GET", base + "/other")
    assert seen[2]["cookie"] is None


def test_ignores_proxy_env(server, monkeypatch):
    base, _seen = server
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    assert Http().request("GET", base + "/x").status == 200


def test_unreachable_on_refused_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(Unreachable) as exc:
        Http().request("GET", f"http://127.0.0.1:{port}/x", timeout=2)
    assert exc.value.app_url == f"http://127.0.0.1:{port}"



def test_connection_cut_mid_answer_is_unreachable():
    # An app restarting can cut or garble an answer (http.client.IncompleteRead, BadStatusLine,
    # ...), which are not OSErrors; wait_until must still be able to retry.
    cut = socket.socket()
    cut.bind(("127.0.0.1", 0))
    cut.listen(2)
    port = cut.getsockname()[1]

    def serve():
        for body in (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nab", b"not http\r\n\r\n"):
            conn, _ = cut.accept()
            conn.recv(4096)
            conn.sendall(body)
            conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        for _ in range(2):  # cut chunked body, then a garbled status line
            with pytest.raises(Unreachable) as exc:
                Http().request("GET", f"http://127.0.0.1:{port}/x", timeout=2)
            assert exc.value.app_url == f"http://127.0.0.1:{port}"
    finally:
        thread.join(5)
        cut.close()


def test_api_redirect_is_rejected_without_following_it(server):
    base, seen = server
    with pytest.raises(UnsafeRedirect):
        Http().request("GET", base + "/redirect")
    # The /next handler would create a second entry if urllib followed the redirect.
    assert [item["path"] for item in seen] == ["/redirect"]


def test_response_body_is_bounded(server, monkeypatch):
    import orama.wiring.http as http_module

    base, seen = server
    monkeypatch.setattr(http_module, "MAX_RESPONSE_BYTES", 4)
    with pytest.raises(ResponseTooLarge):
        Http().request("GET", base + "/large")
    assert [item["path"] for item in seen] == ["/large"]


def test_repeated_form_fields_are_sent_one_by_one(server):
    base, seen = server
    Http().request("POST", base + "/f", form={"x": ["el", "en"], "y": "z"})
    assert parse_qs(seen[0]["body"].decode()) == {"x": ["el", "en"], "y": ["z"]}
