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


def _ip(*octets):
    return ".".join(str(o) for o in octets)


def test_private_server_url_validation():
    lan_ip = _ip(192, 168, 1, 100)
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


try:
    import httpx
    from httpx import ASGITransport

    from hermes.app import app
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False


@pytest.mark.skipif(not HAS_FASTAPI, reason="fastapi/httpx not installed in environment")
@pytest.mark.anyio
async def test_hermes_fastapi_app_endpoints():
    token = "h" * 64
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Health check
        res = await client.get("/healthz")
        assert res.status_code == 200
        assert res.json() == {"status": "ok"}

        # 2. Public config
        res = await client.get("/v1/config")
        assert res.status_code == 200
        cfg = res.json()
        assert "orama_url" in cfg and "node_name" in cfg

        # 3. Static landing page
        res = await client.get("/")
        assert res.status_code == 200
        assert "Hermes" in res.text

        # 4. Auth required routes
        res = await client.get("/v1/voices")
        assert res.status_code == 401

        # 5. Voices catalog with auth
        auth_header = {"Authorization": f"Bearer {token}"}
        res = await client.get("/v1/voices", headers=auth_header)
        assert res.status_code == 200
        voices = res.json()
        assert "voices" in voices and "languages" in voices

        # 6. Local chat query
        res = await client.post(
            "/v1/chat",
            json={"message": "recommend something to watch"},
            headers=auth_header,
        )
        assert res.status_code == 200
        data = res.json()
        assert "reply" in data
        assert data.get("provider") == "local"


def test_wav_bytes_pcm_validation():
    from hermes.voice import wav_bytes

    pcm = b"\x00\x00" * 100
    wav = wav_bytes(pcm, rate=24000)
    assert wav.startswith(b"RIFF")
    assert len(wav) == len(pcm) + 44

    with pytest.raises(ValueError):
        wav_bytes(b"\x00")  # odd byte length


@pytest.mark.anyio
async def test_voice_stream_resilience_on_upstream_interruption(monkeypatch):
    import httpx

    from hermes.voice import VoiceRequest, stream_voice

    req = VoiceRequest(text="Hello world", provider="gemini")

    async def fake_gemini_audio(_req, _client):
        from hermes.voice import wav_bytes
        yield wav_bytes(b"\x00\x00" * 50)
        # Simulate network drop or upstream abort
        raise httpx.ReadTimeout("upstream dropped")

    monkeypatch.setattr("hermes.voice.gemini_audio", fake_gemini_audio)

    async with httpx.AsyncClient() as client:
        events = [e async for e in stream_voice(req, client)]

    event_types = [e["type"] for e in events]
    assert "audio" in event_types
    assert "warning" in event_types
    assert "done" in event_types
    # Crucially, should not yield a hard error that halts playback
    assert "error" not in event_types


