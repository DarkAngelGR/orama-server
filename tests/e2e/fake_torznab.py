"""Local Torznab feed and BEP 19 web seed for generated media only."""

import argparse
import hmac
import os
import re
from email.utils import formatdate
from hashlib import sha1
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from xml.etree.ElementTree import Element, SubElement, tostring

RELEASES = (
    ("Breaking.Bad.S01E01.1080p.WEB-DL.English.DDP5.1.H.264-ORAMA.mkv", 5040),
    ("Inception.2010.1080p.WEB-DL.English.DDP5.1.H.264-ORAMA.mkv", 2040),
)
PIECE_LENGTH = 256 * 1024
NS = "http://torznab.com/schemas/2015/feed"
CAPS = b"""<caps>
  <server version="1.0" title="Orama fixture"/>
  <limits max="100" default="50"/>
  <searching>
    <search available="yes" supportedParams="q"/>
    <tv-search available="yes" supportedParams="q,season,ep"/>
    <movie-search available="yes" supportedParams="q"/>
  </searching>
  <categories>
    <category id="2000" name="Movies"><subcat id="2040" name="HD"/></category>
    <category id="5000" name="TV"><subcat id="5040" name="HD"/></category>
  </categories>
</caps>"""


def _string(value: bytes) -> bytes:
    return str(len(value)).encode() + b":" + value


def torrent_for(path: Path, url: str) -> tuple[bytes, str]:
    with path.open("rb") as clip:
        pieces = b"".join(
            sha1(chunk).digest() for chunk in iter(lambda: clip.read(PIECE_LENGTH), b"")
        )
    info = (
        f"d6:lengthi{path.stat().st_size}e4:name".encode()
        + _string(path.name.encode())
        + f"12:piece lengthi{PIECE_LENGTH}e6:pieces".encode()
        + _string(pieces)
        + b"e"
    )
    return b"d4:info" + info + b"8:url-list" + _string(url.encode()) + b"e", sha1(info).hexdigest()


def make_server(folder: Path, key: str, base_url: str, address: tuple[str, int]):
    files = {name: folder / name for name, _ in RELEASES}
    torrents = {
        f"/torrent/{i}.torrent": torrent_for(files[name], f"{base_url}/files/{name}")
        for i, (name, _) in enumerate(RELEASES, 1)
    }

    def search(query):
        root = Element("rss", version="2.0", attrib={"xmlns:torznab": NS})
        channel = SubElement(root, "channel")
        SubElement(channel, "title").text = "Orama fixture"
        kind = query.get("t", ["search"])[0]
        categories = set(query.get("cat", [""])[0].split(",")) - {""}
        title = re.sub(r"[^a-z0-9]", "", query.get("q", [""])[0].lower())
        total = 0
        for i, (name, category) in enumerate(RELEASES, 1):
            parent = category // 1000 * 1000
            if (kind == "tvsearch" and parent != 5000) or (kind == "movie" and parent != 2000):
                continue
            if categories and not categories.intersection({str(category), str(parent)}):
                continue
            if title and title not in re.sub(r"[^a-z0-9]", "", name.lower()):
                continue
            total += 1
            route = f"/torrent/{i}.torrent"
            url = base_url + route
            size = str(files[name].stat().st_size)
            item = SubElement(channel, "item")
            for tag, value in (
                ("title", files[name].stem), ("guid", url), ("link", url),
                ("pubDate", formatdate(usegmt=True)), ("size", size),
            ):
                SubElement(item, tag).text = value
            SubElement(item, "enclosure", url=url, length=size, type="application/x-bittorrent")
            for attr, value in (
                ("category", str(parent)), ("category", str(category)),
                ("size", size), ("seeders", "1"), ("peers", "1"),
                ("infohash", torrents[route][1]),
            ):
                SubElement(item, "torznab:attr", name=attr, value=value)
        SubElement(channel, "torznab:response", offset="0", total=str(total))
        return tostring(root, encoding="utf-8", xml_declaration=True)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # Request targets can contain the indexer key.

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            request = urlsplit(self.path)
            query = parse_qs(request.query)
            if request.path == "/api":
                supplied = query.get("apikey", [""])[0]
                if not hmac.compare_digest(supplied.encode(), key.encode()):
                    self.send_error(401)
                    return
                kind = query.get("t", [""])[0]
                if kind not in {"caps", "search", "tvsearch", "movie"}:
                    self.send_error(400)
                    return
                self.reply(CAPS if kind == "caps" else search(query), "application/xml")
            elif request.path in torrents:
                self.reply(torrents[request.path][0], "application/x-bittorrent")
            elif request.path.startswith("/files/") and request.path[7:] in files:
                self.serve_file(files[request.path[7:]])
            else:
                self.send_error(404)

        def reply(self, body, content_type):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def serve_file(self, path):
            length = path.stat().st_size
            start, stop = 0, length - 1
            requested = self.headers.get("Range")
            if requested is not None:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested)
                if match and any(match.groups()):
                    first, last = match.groups()
                    if first:
                        start = int(first)
                        stop = min(stop, int(last)) if last else stop
                    else:
                        start = max(0, length - int(last))
                else:
                    start = length
                if start > stop:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{length}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
            self.send_response(206 if requested is not None else 200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Accept-Ranges", "bytes")
            if requested is not None:
                self.send_header("Content-Range", f"bytes {start}-{stop}/{length}")
            remaining = stop - start + 1
            self.send_header("Content-Length", str(remaining))
            self.end_headers()
            if self.command != "HEAD":
                with path.open("rb") as clip:
                    clip.seek(start)
                    while remaining and (chunk := clip.read(min(PIECE_LENGTH, remaining))):
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                if remaining == 0:
                    print(f"web seed served {path.name} bytes {start}-{stop}", flush=True)

    return ThreadingHTTPServer(address, Handler)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--base-url", default="http://orama-e2e-indexer:8000")
    args = parser.parse_args()
    make_server(args.folder, os.environ["TORZNAB_KEY"], args.base_url, ("0.0.0.0", 8000)).serve_forever()
