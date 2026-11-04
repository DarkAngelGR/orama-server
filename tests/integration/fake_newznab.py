"""A tiny Newznab server for the integration test (stdlib only; runs in python:3.12-slim).

``?t=caps`` answers a minimal valid capabilities document (TV 5000, Movies 2000); every
other request, searches included, answers an empty RSS channel.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

CAPS = b"""<?xml version="1.0" encoding="UTF-8"?>
<caps>
  <server version="1.0" title="Fake Newznab" />
  <limits max="100" default="50" />
  <registration available="no" open="no" />
  <searching>
    <search available="yes" supportedParams="q" />
    <tv-search available="yes" supportedParams="q,season,ep" />
    <movie-search available="yes" supportedParams="q,imdbid" />
  </searching>
  <categories>
    <category id="2000" name="Movies" />
    <category id="5000" name="TV" />
  </categories>
</caps>
"""

EMPTY = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">
  <channel>
    <title>Fake Newznab</title>
    <newznab:response offset="0" total="0" />
  </channel>
</rss>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        kind = parse_qs(urlparse(self.path).query).get("t", [""])[0]
        body = CAPS if kind == "caps" else EMPTY
        self.send_response(200)
        self.send_header("Content-Type", "application/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
