"""Tests for Settings: App connections (adopted app API keys)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from conftest import make_setup

from orama.requests.client import MediaClient, RequestError
from orama.setup_model import DesiredSetup
from orama.wiring.http import FakeHttp, Response, UnsafeRedirect

from .conftest import LAN, csrf_from, signin


def sd(app) -> Path:
    return app.extensions["orama"].state_dir


def page(response) -> str:
    return response.get_data(as_text=True)


def post(client, path: str, *, token_from: str = "/settings", **data):
    token = csrf_from(client.get(token_from))
    return client.post(path, data={"csrf_token": token, **data})


@pytest.fixture
def signed(client, password):
    assert signin(client, password).status_code == 303
    return client


@pytest.fixture
def adopted_setup(app):
    state_dir = sd(app)
    setup = make_setup(
        media_servers={"jellyfin"},
        download_methods={"usenet", "torrent"},
        adopted_apps=frozenset({"radarr", "sonarr", "prowlarr", "bazarr", "sabnzbd", "jellyfin"}),
        ports={"radarr": 7878, "sonarr": 8989, "prowlarr": 9696, "bazarr": 6767, "sabnzbd": 8080, "jellyfin": 8096},
        lan_address=LAN,
    )
    setup.save(state_dir / "setup.json")
    return setup


# ------------------------------------------------------------------ access & validation


def test_app_connections_needs_sign_in(client, adopted_setup):
    response = client.post("/settings/app-connections/radarr", data={"key": "any-key"})
    assert response.status_code in (303, 403)


def test_app_connections_csrf_required(signed, adopted_setup):
    response = signed.post("/settings/app-connections/radarr", data={"key": "any-key"})
    assert response.status_code == 403


def test_app_connections_unknown_or_unsupported_app_returns_404(signed, adopted_setup):
    for bad in ("unknown", "plex", "qbittorrent", "nonexistent"):
        response = post(signed, f"/settings/app-connections/{bad}", key="test-key")
        assert response.status_code == 404


def test_app_connections_non_adopted_app_returns_404(signed, app):
    # Setup without adopted apps
    state_dir = sd(app)
    setup = make_setup(
        media_servers={"jellyfin"},
        download_methods={"usenet"},
        adopted_apps=frozenset(),
    )
    setup.save(state_dir / "setup.json")
    response = post(signed, "/settings/app-connections/radarr", key="test-key")
    assert response.status_code == 404


def test_app_connections_draft_exists_returns_409(signed, adopted_setup, app):
    state_dir = sd(app)
    (state_dir / "setup.draft.json").write_text("{}")
    response = post(signed, "/settings/app-connections/radarr", key="test-key")
    assert response.status_code == 409
    assert app.extensions["orama"].store.get("apikey:radarr") is None


def test_app_connections_busy_returns_409(signed, adopted_setup, app, tmp_path):
    st = app.extensions["orama"]
    st.jobs = Mock()
    running_job = Mock()
    running_job.status = "running"
    running_job.lines = []
    running_job.started = 1000.0
    st.jobs.current.return_value = running_job

    response = post(signed, "/settings/app-connections/radarr", key="test-key")
    assert response.status_code == 409
    assert "busy" in page(response).lower()
    assert st.store.get("apikey:radarr") is None


def test_app_connections_blank_input_preserves_old_key(signed, adopted_setup, app):
    st = app.extensions["orama"]
    st.store.set("apikey:radarr", "preserved_old_key")

    for blank in ("", "   ", "\t"):
        response = post(signed, "/settings/app-connections/radarr", key=blank)
        assert response.status_code == 400
        assert "Enter an API key to test." in page(response)
        assert st.store.get("apikey:radarr") == "preserved_old_key"


def test_app_connections_invalid_characters_preserves_old_key(signed, adopted_setup, app):
    st = app.extensions["orama"]
    st.store.set("apikey:radarr", "preserved_old_key")

    response = post(signed, "/settings/app-connections/radarr", key="key_with_\n_newline")
    assert response.status_code == 400
    assert "The API key contains invalid characters." in page(response)
    assert st.store.get("apikey:radarr") == "preserved_old_key"


# ------------------------------------------------------------------ probe behavior


def test_app_connections_unreachable_preserves_old_key(signed, adopted_setup, app):
    st = app.extensions["orama"]
    st.store.set("apikey:radarr", "preserved_old_key")
    fake_http = FakeHttp(unreachable=True)
    st.http = fake_http

    response = post(signed, "/settings/app-connections/radarr", key="new_candidate_key")
    assert response.status_code == 400
    assert "Radarr could not be reached. Check the address and port." in page(response)
    assert st.store.get("apikey:radarr") == "preserved_old_key"
    assert "new_candidate_key" not in page(response)


def test_app_connections_rejected_key_preserves_old_key(signed, adopted_setup, app):
    st = app.extensions["orama"]
    st.store.set("apikey:radarr", "preserved_old_key")
    fake_http = FakeHttp()
    fake_http.route("GET", "/api/v3/system/status", lambda _: Response(401, {}, b"Unauthorized"))
    st.http = fake_http

    response = post(signed, "/settings/app-connections/radarr", key="rejected_key")
    assert response.status_code == 400
    assert "Radarr did not accept this API key." in page(response)
    assert st.store.get("apikey:radarr") == "preserved_old_key"
    assert "rejected_key" not in page(response)


def test_app_connections_redirect_preserves_old_key(signed, adopted_setup, app):
    st = app.extensions["orama"]
    st.store.set("apikey:radarr", "preserved_old_key")
    def redirected(_call):
        raise UnsafeRedirect("http://radarr")

    fake_http = FakeHttp()
    fake_http.route("GET", "/api/v3/system/status", redirected)
    st.http = fake_http

    response = post(signed, "/settings/app-connections/radarr", key="redirected_key")
    assert response.status_code == 400
    assert "Radarr sent a redirect instead of an answer." in page(response)
    assert st.store.get("apikey:radarr") == "preserved_old_key"
    assert "redirected_key" not in page(response)


def test_app_connections_malformed_response_preserves_old_key(signed, adopted_setup, app):
    st = app.extensions["orama"]
    st.store.set("apikey:radarr", "preserved_old_key")
    fake_http = FakeHttp()
    fake_http.route("GET", "/api/v3/system/status", lambda _: Response(200, {}, b"not json at all"))
    st.http = fake_http

    response = post(signed, "/settings/app-connections/radarr", key="candidate_key")
    assert response.status_code == 400
    assert "Radarr gave an answer Orama could not read." in page(response)
    assert st.store.get("apikey:radarr") == "preserved_old_key"
    assert "candidate_key" not in page(response)


def test_app_connections_success_saves_key_and_redirects(signed, adopted_setup, app):
    st = app.extensions["orama"]
    fake_http = FakeHttp()
    fake_http.route("GET", "/api/v3/system/status", lambda call: (
        Response(200, {}, json.dumps({"version": "5.3.6"}).encode())
        if call.headers.get("X-Api-Key") == "super_secret_radarr_key"
        else Response(401, {}, b"")
    ))
    st.http = fake_http

    response = post(signed, "/settings/app-connections/radarr", key="super_secret_radarr_key")
    assert response.status_code == 303
    assert response.headers["Location"] == "/settings?saved=app-radarr"

    assert st.store.get("apikey:radarr") == "super_secret_radarr_key"

    # Follow redirect
    follow = signed.get(response.headers["Location"])
    assert follow.status_code == 200
    html = page(follow)
    assert "Radarr" in html
    assert "API key saved" in html
    assert "super_secret_radarr_key" not in html


def test_app_connections_immediately_visible_to_media_client(signed, adopted_setup, app):
    st = app.extensions["orama"]
    fake_http = FakeHttp()
    fake_http.route("GET", "/api/v3/system/status", lambda call: Response(200, {}, json.dumps({"version": "5.3.6"}).encode()))
    fake_http.route("GET", "/api/v3/system/status", lambda call: Response(200, {}, json.dumps({"version": "5.3.6"}).encode()))
    st.http = fake_http

    setup = DesiredSetup.load(sd(app) / "setup.json")
    media_client = MediaClient(setup, st.store, fake_http)

    # Before saving: key missing
    with pytest.raises(RequestError, match="not configured or key missing"):
        media_client._get_app_info("radarr")

    # Save key through web endpoint
    response = post(signed, "/settings/app-connections/radarr", key="valid_radarr_key_123")
    assert response.status_code == 303

    # After saving: immediately readable without restart or apply
    url, key = media_client._get_app_info("radarr")
    assert key == "valid_radarr_key_123"
    assert url == f"http://{LAN}:7878"


@pytest.mark.parametrize("app_name,endpoint,method,headers,form_data,body", [
    ("radarr", "/api/v3/system/status", "GET", {"X-Api-Key": "k_radarr"}, None, {"version": "5.0"}),
    ("sonarr", "/api/v3/system/status", "GET", {"X-Api-Key": "k_sonarr"}, None, {"version": "4.0"}),
    ("prowlarr", "/api/v1/system/status", "GET", {"X-Api-Key": "k_prowlarr"}, None, {"version": "1.0"}),
    ("bazarr", "/api/system/status", "GET", {"X-API-KEY": "k_bazarr"}, None, {"data": {}}),
    ("sabnzbd", "/api", "POST", None, {"mode": "version", "output": "json"}, {"version": "4.2", "status": True}),
    ("jellyfin", "/System/Info", "GET", {"X-Emby-Token": "k_jellyfin"}, None, {"ServerName": "JF", "Version": "10.9"}),
])
def test_all_supported_apps_probe(signed, adopted_setup, app, app_name, endpoint, method, headers, form_data, body):
    st = app.extensions["orama"]
    key_val = f"secret_for_{app_name}"
    fake_http = FakeHttp()

    def handler(call):
        assert call.method == method
        if app_name == "sabnzbd":
            assert call.form.get("apikey") == key_val
            assert call.form.get("mode") == "version"
        elif app_name == "jellyfin":
            assert call.headers.get("X-Emby-Token") == key_val
            assert f'Token="{key_val}"' in call.headers.get("Authorization", "")
        elif headers:
            for h, expected in headers.items():
                assert call.headers.get(h) == (key_val if expected.startswith("k_") else expected)
        return Response(200, {}, json.dumps(body).encode())

    fake_http.route(method, endpoint, handler)
    st.http = fake_http

    response = post(signed, f"/settings/app-connections/{app_name}", key=key_val)
    assert response.status_code == 303
    assert st.store.get(f"apikey:{app_name}") == key_val


def test_settings_page_renders_app_connections_section_for_adopted_apps(signed, adopted_setup, app):
    st = app.extensions["orama"]
    st.store.set("apikey:radarr", "existing_radarr_key")

    response = signed.get("/settings")
    assert response.status_code == 200
    html = page(response)

    assert 'id="app-connections"' in html
    assert 'href="#app-connections"' in html
    assert "App connections" in html
    assert "Radarr" in html
    assert "Sonarr" in html
    assert "Saved" in html
    assert "existing_radarr_key" not in html
    assert 'id="radarr-api-key"' in html
    assert 'id="sonarr-api-key"' in html
    section = html.split('id="app-connections"', 1)[1].split('id="indexers"', 1)[0]
    assert "leave empty" not in section.lower()
    assert 'action="/settings/app-connections/radarr"' in html
    assert 'action="/settings/app-connections/sonarr"' in html
