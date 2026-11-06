"""Disposable webhook receiver: records shapes and authentication success, never tokens."""
import hmac
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path("/capture")
TOKEN = (ROOT / "token").read_text()


class Receiver(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        authenticated = hmac.compare_digest(
            self.headers.get("Authorization", ""), "Bearer " + TOKEN)
        if not authenticated or not 0 < size <= 65536:
            self.send_response(401)
            self.end_headers()
            return
        body = json.loads(self.rfile.read(size))
        record = {"authenticated": True, "path": self.path,
                  "eventType": body.get("eventType"), "fields": sorted(body)}
        with (ROOT / "events").open("a") as output:
            output.write(json.dumps(record) + "\n")
        self.send_response(200)
        self.end_headers()


server = HTTPServer(("0.0.0.0", 8080), Receiver)
(ROOT / "ready").touch()
server.serve_forever()
