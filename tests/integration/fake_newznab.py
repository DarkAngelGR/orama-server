"""A tiny Newznab server for the integration test (stdlib only; runs in python:3.12-slim).

``?t=caps`` answers a minimal valid capabilities document (TV 5000, Movies 2000); every
other request answers an RSS channel with one TV and one movie release. The apps refuse an
indexer whose test search finds nothing in their categories ("Query successful, but no
results ... were returned"): Prowlarr, Sonarr (TV) and Radarr (movies) each need one.
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
    <category id="2000" name="Movies">
      <subcat id="2040" name="HD" />
    </category>
    <category id="5000" name="TV">
      <subcat id="5040" name="HD" />
    </category>
  </categories>
</caps>
"""

RESULTS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">
  <channel>
    <title>Fake Newznab</title>
    <newznab:response offset="0" total="2" />
    <item>
      <title>Breaking.Bad.S01E01.1080p.WEB-DL.DTS.x264</title>
      <guid isPermaLink="true">http://fake-newznab:8000/details/1</guid>
      <link>http://fake-newznab:8000/getnzb/1.nzb</link>
      <pubDate>Mon, 03 Nov 2025 12:00:00 +0000</pubDate>
      <category>TV &gt; HD</category>
      <enclosure url="http://fake-newznab:8000/getnzb/1.nzb" length="2147483648" type="application/x-nzb" />
      <newznab:attr name="category" value="5000" />
      <newznab:attr name="category" value="5040" />
      <newznab:attr name="size" value="2147483648" />
    </item>
    <item>
      <title>Inception.2010.2160p.WEB-DL.English.DTS.x264</title>
      <guid isPermaLink="true">http://fake-newznab:8000/details/2</guid>
      <link>http://fake-newznab:8000/getnzb/2.nzb</link>
      <pubDate>Mon, 03 Nov 2025 12:00:00 +0000</pubDate>
      <category>Movies &gt; HD</category>
      <enclosure url="http://fake-newznab:8000/getnzb/2.nzb" length="8589934592" type="application/x-nzb" />
      <newznab:attr name="category" value="2000" />
      <newznab:attr name="category" value="2040" />
      <newznab:attr name="size" value="8589934592" />
    </item>
  </channel>
</rss>
"""


LAST_KEY = {"value": ""}  # the API key of the latest request that carried one


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        query = parse_qs(urlparse(self.path).query)
        kind = query.get("t", [""])[0]
        if kind == "lastkey":  # test hook: which key did Prowlarr last send?
            body = LAST_KEY["value"].encode()
        else:
            if query.get("apikey"):
                LAST_KEY["value"] = query["apikey"][0]
            body = CAPS if kind == "caps" else RESULTS
        self.send_response(200)
        self.send_header("Content-Type", "application/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
