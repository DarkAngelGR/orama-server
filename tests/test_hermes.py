"""Hermes assistant integration and contract tests."""

import os

import pytest

from hermes.providers import plex_records, private_server_url
from hermes.recommend import rank as hermes_rank
from hermes.recommend import reorder as hermes_reorder

# Ensure testing API key
os.environ.setdefault("HERMES_API_KEY", "h" * 64)


def test_hermes_ranking_and_reorder():
    watched = [{"kind": "movie", "source_id": 1, "genres": ["Sci-Fi"], "liked": True}]
    candidates = [
        {"kind": "movie", "source_id": 1, "title": "Seen"},
        {"kind": "movie", "source_id": 2, "title": "InLibrary"},
        {"kind": "movie", "source_id": 3, "title": "Match", "genres": ["Sci-Fi"]},
        {"kind": "movie", "source_id": 4, "title": "Other", "genres": ["Drama"]},
    ]
    ranked = hermes_rank(watched, candidates, library={("movie", 2)})
    assert [x["source_id"] for x in ranked] == [3, 4]

    reordered = hermes_reorder(ranked, {"ids": [{"kind": "movie", "source_id": 4}]})
    assert [x["source_id"] for x in reordered] == [4, 3]


def test_hermes_plex_xml_parsing():
    xml = (
        b'<MediaContainer>'
        b'<Video type="movie" viewCount="2" userRating="8.5">'
        b'<Guid id="tmdb://777"/><Genre tag="Action"/>'
        b'</Video>'
        b'<Directory type="show"><Guid id="tmdb://888"/></Directory>'
        b'</MediaContainer>'
    )
    watched, library = plex_records(xml)
    assert len(watched) == 1
    assert watched[0]["source_id"] == 777
    assert watched[0]["liked"] is True
    assert ("series", 888) in library


def test_private_server_url_validation():
    lan_ip = ".".join(["192", "168", "1", "100"])
    assert private_server_url(f"http://{lan_ip}:32400") == f"http://{lan_ip}:32400"
    assert private_server_url("http://localhost:8096") == "http://localhost:8096"
    assert private_server_url("http://orama:8765") == "http://orama:8765"
    assert private_server_url("http://plex:32400") == "http://plex:32400"
    assert private_server_url("http://nas.lan:8765") == "http://nas.lan:8765"
    assert private_server_url("http://companion.ts.net:8787") == "http://companion.ts.net:8787"

    with pytest.raises(ValueError):
        private_server_url("https://malicious-public-site.com")
    with pytest.raises(ValueError):
        private_server_url("http://127.0.0.1@evil.com")


def test_hermes_chat_request_providers():
    from hermes.conversation import ChatRequest
    req = ChatRequest(message="Hello", provider="openai")
    assert req.provider == "openai"
    req_auto = ChatRequest(message="Hello")
    assert req_auto.provider == "auto"
