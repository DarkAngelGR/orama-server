"""A tiny HTTP proxy for the integration test (stdlib only; runs in python:3.12-slim).

Prowlarr checks an HTTP proxy when it is saved (it fetches a page through it), so the
"Gluetun (Orama)" proxy can only be created while something answers on gluetun:8888.
The VPN is not part of CI, so this stands in for it: CONNECT tunnels and plain
requests are passed on to the real destination.
"""

import select
import socket
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_CONNECT(self) -> None:
        host, _, port = self.path.partition(":")
        try:
            upstream = socket.create_connection((host, int(port or 443)), timeout=20)
        except OSError:
            self.send_error(502)
            return
        self.send_response(200, "Connection established")
        self.end_headers()
        sockets = [self.connection, upstream]
        try:
            while True:
                ready, _, _ = select.select(sockets, [], [], 30)
                if not ready:
                    break
                for source in ready:
                    data = source.recv(65536)
                    if not data:
                        return
                    (upstream if source is self.connection else self.connection).sendall(data)
        finally:
            upstream.close()

    def do_GET(self) -> None:
        try:
            with urllib.request.urlopen(self.path, timeout=20) as answer:
                body = answer.read()
                status = answer.status
        except OSError:
            self.send_error(502)
            return
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8888), Handler).serve_forever()
