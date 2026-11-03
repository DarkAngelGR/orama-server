"""Run Orama: ``python -m orama``.

Plan 1 placeholder: a tiny HTTP server on port 8765 that answers a health check and a
short notice. The web app replaces it in a later update.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8765
NOTICE = "Orama is running — setup page arrives in a later update."


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = {"/healthz": "ok", "/": NOTICE}.get(self.path)
        if body is None:
            self.send_error(404)
            return
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        """Quiet: the health check would otherwise fill the log."""


def make_server(host: str = "0.0.0.0", port: int = PORT) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), _Handler)


def main() -> None:
    with make_server() as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
