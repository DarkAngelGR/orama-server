"""Contract tests for the offline torrent fixture."""

from hashlib import sha1
from http.client import HTTPConnection
from threading import Thread
from xml.etree import ElementTree

import pytest
from e2e.fake_torznab import RELEASES, make_server


@pytest.fixture
def indexer(tmp_path):
    for name, _ in RELEASES:
        (tmp_path / name).write_bytes(b"0123456789")
    server = make_server(tmp_path, "fixture-key", "http://indexer:8000", ("127.0.0.1", 0))
    worker = Thread(target=server.serve_forever)
    worker.start()
    connection = HTTPConnection(*server.server_address, timeout=5)

    def get(path, **headers):
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        return response.status, response.headers, response.read()

    try:
        yield get
    finally:
        connection.close()
        server.shutdown()
        worker.join()
        server.server_close()


def test_caps_and_search_require_key_and_report_categories(indexer):
    for kind in ("caps", "search", "tvsearch", "movie"):
        assert indexer(f"/api?t={kind}")[0] == 401
        assert indexer(f"/api?t={kind}&apikey=wrong")[0] == 401
    status, _, body = indexer("/api?t=caps&apikey=fixture-key")
    assert status == 200
    assert ElementTree.fromstring(body).find(".//tv-search").get("available") == "yes"
    for kind, expected in (("search", 2), ("tvsearch", 1), ("movie", 1)):
        status, _, body = indexer(f"/api?t={kind}&apikey=fixture-key")
        assert status == 200
        assert len(ElementTree.fromstring(body).findall("./channel/item")) == expected


def test_torrent_hash_covers_the_file_and_range_serves_exact_bytes(indexer):
    name, _ = RELEASES[0]
    status, _, torrent = indexer("/torrent/1.torrent")
    assert status == 200
    piece = sha1(b"0123456789").digest()
    encoded_name = name.encode()
    info = (
        b"d6:lengthi10e4:name" + str(len(encoded_name)).encode() + b":" + encoded_name
        + b"12:piece lengthi262144e6:pieces20:" + piece + b"e"
    )
    assert torrent.startswith(b"d4:info" + info)
    status, _, body = indexer("/api?t=tvsearch&q=Breaking+Bad&cat=5040&apikey=fixture-key")
    assert status == 200
    attrs = ElementTree.fromstring(body).findall(
        "./channel/item/{http://torznab.com/schemas/2015/feed}attr"
    )
    advertised = {attr.get("name"): attr.get("value") for attr in attrs}
    assert advertised["infohash"] == sha1(info).hexdigest()
    assert advertised["size"] == "10"
    assert b"http://indexer:8000/files/" + encoded_name in torrent
    status, headers, data = indexer(f"/files/{name}", Range="bytes=2-5")
    assert (status, headers["Content-Range"], data) == (206, "bytes 2-5/10", b"2345")
    assert indexer(f"/files/{name}", Range="bytes=20-21")[0] == 416
    assert indexer("/files/../fake_torznab.py")[0] == 404


def test_target_scrubs_setup_codes_and_registered_secrets():
    from e2e.target import LocalTarget

    target = LocalTarget(
        puid=1000,
        pgid=1000,
        data_root="/srv/orama-e2e/data",
        config_root="/srv/orama-e2e/cfg",
        repo_dir="/tmp/repo",
    )
    target.register_secret("short", "long-secret-token", "longer-secret-token-value")
    raw = (
        "Orama is ready. Open http://127.0.0.1:8765/setup?code=AbCdEfGh12345678 in your browser.\n"
        "short longer-secret-token-value long-secret-token"
    )
    scrubbed = target.scrub(raw)
    assert "AbCdEfGh12345678" not in scrubbed
    assert "longer-secret-token-value" not in scrubbed
    assert "long-secret-token" not in scrubbed
    assert "/setup?code=<hidden>" in scrubbed
    assert "short" in scrubbed
    assert target.url(8765) == "http://127.0.0.1:8765"



def test_debian_image_pin_matches_a_dated_generic_qcow2():
    import re
    from pathlib import Path

    pin = Path(__file__).with_name("e2e") / "debian-12.image"
    url, checksum = pin.read_text(encoding="ascii").splitlines()
    assert re.fullmatch(
        r"https://cloud\.debian\.org/images/cloud/bookworm/(\d{8}-\d+)/"
        r"debian-12-generic-amd64-\1\.qcow2",
        url,
    )
    assert re.fullmatch(r"[0-9a-f]{128}", checksum)
