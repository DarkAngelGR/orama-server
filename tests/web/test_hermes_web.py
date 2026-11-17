"""Test the Hermes assistant integration within Orama's web app."""

from __future__ import annotations

import pytest

from web.conftest import signin, write_setup

HEADERS = {"Host": "localhost"}


@pytest.fixture
def signed(app, client, password):
    write_setup(app.extensions["orama"].state_dir)
    assert signin(client, password, headers=HEADERS).status_code == 303
    return client


def test_hermes_redirect_slash(client):
    res = client.get("/hermes", headers=HEADERS)
    assert res.status_code == 308
    assert res.headers["Location"].endswith("/hermes/")


def test_hermes_requires_auth(client):
    res = client.get("/hermes/", headers=HEADERS)
    # Unauthenticated visitor redirected to sign-in
    assert res.status_code in (302, 303)
    assert "/signin" in res.headers["Location"]


def test_hermes_signed_in(signed):
    res = signed.get("/hermes/", headers=HEADERS)
    assert res.status_code == 200
    assert "text/html" in res.content_type
    assert b"Hermes Conversation" in res.data
    assert b'meta name="orama-session"' in res.data
    assert b'meta name="csrf-token"' in res.data


def test_hermes_static_served(client):
    res = client.get("/hermes/static/style.css", headers=HEADERS)
    assert res.status_code == 200
    assert "text/css" in res.content_type
    assert b"--room: #101426;" in res.data


def test_hermes_config_public(client):
    res = client.get("/hermes/v1/config", headers=HEADERS)
    assert res.status_code == 200
    assert res.json == {"orama_url": "", "node_name": "Orama Embedded Node"}


def test_hermes_api_endpoints_signed_in(signed):
    res = signed.get("/hermes/v1/voices", headers=HEADERS)
    assert res.status_code == 200
    assert "gemini_voices" in res.json

    res = signed.get("/hermes/v1/recommendations", headers=HEADERS)
    assert res.status_code == 200
    assert "recommendations" in res.json


def test_hermes_security_headers(signed):
    res = signed.get("/hermes/", headers=HEADERS)
    assert res.status_code == 200
    csp = res.headers.get("Content-Security-Policy", "")
    assert "https://image.tmdb.org" in csp
    assert "media-src 'self' blob:" in csp
    assert res.headers.get("Permissions-Policy") == "microphone=(self), camera=()"


def test_nav_link_in_base_templates(signed):
    res = signed.get("/", headers=HEADERS)
    assert res.status_code == 200
    assert b'href="/hermes"' in res.data


def test_hermes_external_url_redirect(signed, monkeypatch):
    monkeypatch.setenv("HERMES_URL", "http://192.168.1.50:8787")
    res = signed.get("/hermes", headers=HEADERS)
    assert res.status_code == 302
    assert res.headers["Location"] == "http://192.168.1.50:8787"

