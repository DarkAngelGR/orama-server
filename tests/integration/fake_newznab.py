"""A tiny Newznab server for the integration test (stdlib only; runs in python:3.12-slim).

``?t=caps`` answers a minimal valid capabilities document (TV 5000, Movies 2000); every
other request answers an RSS channel with one release: Prowlarr refuses to save an indexer
whose test search returns nothing ("Query successful, but no results were returned").
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
    <category id="5000" name="TV">
      <subcat id="5040" name="HD" />
    </category>
  </categories>
</caps>
"""

EMPTY = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">
  <channel>
    <title>Fake Newznab</title>
    <newznab:response offset="0" total="1" />
    <item>
      <title>Fake.Show.S01E01.1080p.WEB-DL</title>
      <guid isPermaLink="true">http://fake-newznab:8000/details/1</guid>
      <link>http://fake-newznab:8000/getnzb/1.nzb</link>
      <pubDate>Mon, 03 Nov 2025 12:00:00 +0000</pubDate>
      <category>TV &gt; HD</category>
      <enclosure url="http://fake-newznab:8000/getnzb/1.nzb" length="1048576" type="application/x-nzb" />
      <newznab:attr name="category" value="5000" />
      <newznab:attr name="category" value="5040" />
      <newznab:attr name="size" value="1048576" />
    </item>
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
