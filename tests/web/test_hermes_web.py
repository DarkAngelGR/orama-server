"""Test the Hermes assistant integration within Orama's web app."""

from __future__ import annotations

import os
import stat

import pytest

from web.conftest import csrf_from, signin, write_setup

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


def test_hermes_page_remembers_a_workspace_key(client):
    script = client.get("/hermes/static/app.js", headers=HEADERS)
    assert script.status_code == 200
    text = script.get_data(as_text=True)
    assert "loadWorkspaceKey(storage)" in text
    assert "saveWorkspaceKey(storage,typed)" in text
    assert "saveWorkspaceKey(storage,'')" in text
    page = client.get("/hermes/static/index.html", headers=HEADERS)
    assert b"Enter it once" in page.data


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
    assert res.json["voices"]
    assert "local_voices" in res.json

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


def test_embedded_status_backend_is_available(signed, monkeypatch):
    from orama.web import hermes

    assert hermes.HAS_HERMES_BACKEND
    expected = {"services": {"orama": {"status": "online"}}, "models": {}}

    async def status_without_network(client):
        assert isinstance(client, hermes.httpx.AsyncClient)
        return expected

    monkeypatch.setattr(hermes, "service_status", status_without_network)
    response = signed.get("/hermes/v1/status", headers=HEADERS)
    assert response.status_code == 200
    assert response.json == expected


def test_nav_link_in_base_templates(signed):
    res = signed.get("/", headers=HEADERS)
    assert res.status_code == 200
    assert b'href="/hermes"' in res.data


def _ip(*octets):
    return ".".join(str(o) for o in octets)


def test_hermes_external_url_redirect(signed, monkeypatch):
    lan_ip = _ip(192, 168, 1, 50)
    target = f"http://{lan_ip}:8787"
    monkeypatch.setenv("HERMES_URL", target)
    res = signed.get("/hermes", headers=HEADERS)
    assert res.status_code == 302
    assert res.headers["Location"] == target


def _post_hermes(client, url: str):
    token = csrf_from(client.get("/settings", headers=HEADERS))
    return client.post(
        "/settings/hermes", data={"csrf_token": token, "hermes_url": url}, headers=HEADERS
    )


def test_settings_saves_mini_pc_and_hermes_opens_it(signed, app):
    lan_ip = _ip(192, 168, 1, 13)
    target = f"http://{lan_ip}:8787"
    saved = _post_hermes(signed, target + "/")
    assert saved.status_code == 303
    assert saved.headers["Location"].endswith("saved=hermes")

    path = app.extensions["orama"].state_dir / "hermes.url"
    assert path.read_text(encoding="utf-8") == target + "\n"
    if os.name == "posix":
        assert stat.S_IMODE(os.lstat(path).st_mode) == 0o600

    opened = signed.get("/hermes", headers=HEADERS)
    assert opened.status_code == 302
    assert opened.headers["Location"] == target
    page = signed.get("/settings?saved=hermes", headers=HEADERS)
    assert b"The Hermes link now opens your mini PC." in page.data
    assert target.encode() in page.data


def test_settings_rejects_a_public_hermes_address(signed, app):
    response = _post_hermes(signed, "https://example.com")
    assert response.status_code == 400
    assert b"home network" in response.data
    assert not (app.extensions["orama"].state_dir / "hermes.url").exists()
    opened = signed.get("/hermes", headers=HEADERS)
    assert opened.status_code == 308


def test_clearing_the_hermes_address_opens_the_page_inside_orama(signed, app, monkeypatch):
    monkeypatch.delenv("HERMES_URL", raising=False)
    assert _post_hermes(signed, f"http://{_ip(10, 1, 1, 8)}:8787").status_code == 303
    cleared = _post_hermes(signed, "  ")
    assert cleared.status_code == 303
    assert cleared.headers["Location"].endswith("saved=hermes-off")
    assert not (app.extensions["orama"].state_dir / "hermes.url").exists()
    assert signed.get("/hermes/", headers=HEADERS).status_code == 200


def test_process_environment_wins_over_the_saved_address(signed, app, monkeypatch):
    saved_ip = _ip(10, 2, 2, 2)
    env_ip = _ip(10, 3, 3, 3)
    assert _post_hermes(signed, f"http://{saved_ip}:8787").status_code == 303
    monkeypatch.setenv("HERMES_URL", f"http://{env_ip}:8787")
    opened = signed.get("/hermes/", headers=HEADERS)
    assert opened.status_code == 302
    assert opened.headers["Location"] == f"http://{env_ip}:8787"


def test_a_public_environment_address_is_not_opened(signed, monkeypatch):
    monkeypatch.setenv("HERMES_URL", "https://example.com/hermes")
    opened = signed.get("/hermes", headers=HEADERS)
    assert opened.status_code == 308


def test_hermes_save_waits_while_a_setup_change_is_unfinished(signed, app):
    write_setup(app.extensions["orama"].state_dir, "setup.draft.json")
    response = _post_hermes(signed, f"http://{_ip(10, 4, 4, 4)}:8787")
    assert response.status_code == 409
    assert not (app.extensions["orama"].state_dir / "hermes.url").exists()

