"""The Apps page and Home. Addresses are built at run time (the repository is public)."""

from __future__ import annotations

import re

import pytest

from orama.agent_client import AgentError
from web.conftest import EXTRA, HOST_NAME, LAN, csrf_from, signin, write_setup

VPN_HOST = {"Host": "localhost"}


class FakeAgent:
    def __init__(self, states=None, error: Exception | None = None) -> None:
        self.states = states or {}
        self.error = error

    def status(self):
        if self.error:
            raise self.error
        return dict(self.states)


PORTS = {"plex": 32400, "sonarr": 8989, "radarr": 7878, "prowlarr": 9696, "bazarr": 6767,
         "sabnzbd": 8080, "qbittorrent": 8081, "gluetun": 0, "recyclarr": 0}


@pytest.fixture
def agent(app) -> FakeAgent:
    fake = FakeAgent({
        "plex": "running", "sonarr": "running", "radarr": "restarting", "prowlarr": "exited",
        "bazarr": "running", "sabnzbd": "running",
    })
    app.extensions["orama"].agent = fake
    return fake


@pytest.fixture
def signed(app, client, password, agent):
    write_setup(app.extensions["orama"].state_dir, ports=PORTS_FOR_SETUP)
    assert signin(client, password, headers={"Host": "localhost"}).status_code == 303
    return client


PORTS_FOR_SETUP = {k: v for k, v in PORTS.items() if v}


def get(client, path="/apps", host="localhost"):
    """GET as ``host``: the session cookie is replayed by hand through that Host."""
    cookie = client.get_cookie("orama_session")
    plain = client.application.test_client(use_cookies=False)
    headers = {"Host": host}
    if cookie:
        headers["Cookie"] = f"orama_session={cookie.value}"
    return plain.get(path, headers=headers)


def test_apps_lists_enabled_apps_with_state(signed):
    html = get(signed).get_data(as_text=True)
    for word in ("Running", "Starting", "Stopped"):
        assert word in html
    for name in ("Plex", "Sonarr", "Radarr", "Prowlarr", "Bazarr", "SABnzbd"):
        assert name in html
    assert "qBittorrent" not in html
    # Plex before Sonarr
    assert html.index("Plex") < html.index("Sonarr")


def test_missing_app_is_not_found(signed, agent):
    agent.states.pop("bazarr")
    html = get(signed).get_data(as_text=True)
    assert "Not found" in html


def test_unknown_state_is_not_found_and_never_printed(signed, agent):
    agent.states["plex"] = "weird-raw-<b>state"
    html = get(signed).get_data(as_text=True)
    assert "weird-raw" not in html
    assert "Not found" in html


@pytest.mark.parametrize("raw,word", [
    ("running", "Running"), ("starting", "Starting"), ("restarting", "Starting"),
    ("created", "Starting"), ("exited", "Stopped"), ("stopped", "Stopped"),
    ("dead", "Stopped"), ("paused", "Stopped"), ("", "Not found"),
])
def test_state_words(signed, agent, raw, word):
    agent.states = {"plex": raw}
    html = get(signed, "/apps/list").get_data(as_text=True)
    row = html[html.index("Plex"):html.index("Sonarr")]
    assert word in row


def test_links_use_request_host_and_app_port(signed):
    html = get(signed, host=f"{LAN}:8765").get_data(as_text=True)
    assert f'href="http://{LAN}:32400/"' in html
    assert f'href="http://{LAN}:8989/"' in html
    assert 'target="_blank"' in html and 'rel="noopener noreferrer"' in html


def test_link_host_is_hostname_without_port_and_lowercased(signed):
    html = get(signed, host=f"{HOST_NAME.upper()}.:8765").get_data(as_text=True)
    assert f'href="http://{HOST_NAME}:32400/"' in html
    html = get(signed, host=EXTRA).get_data(as_text=True)
    assert f'href="http://{EXTRA}:8989/"' in html


def test_link_host_ipv6_gets_brackets(signed):
    html = get(signed, host="[::1]:8765").get_data(as_text=True)
    assert 'href="http://[::1]:32400/"' in html


def test_forwarded_headers_do_not_change_links(signed):
    response = signed.get("/apps", headers={
        "Host": f"{LAN}:8765", "X-Forwarded-Host": "evil.example",
        "Forwarded": "host=evil.example"})
    assert "evil.example" not in response.get_data(as_text=True)


def test_no_link_for_gluetun_and_recyclarr(app, client, password, agent, state_dir):
    write_setup(state_dir, download_methods={"torrent"}, ports=PORTS_FOR_SETUP)
    signin(client, password, headers=VPN_HOST)
    agent.states = {"qbittorrent": "running"}
    html = get(client).get_data(as_text=True)
    assert "qBittorrent" in html
    assert 'href="http://localhost:8081/"' in html
    assert "Recyclarr" not in html


def test_gluetun_row_has_state_but_no_link(app, client, password, agent, state_dir, monkeypatch):
    import orama.web.apps as apps_mod

    real = apps_mod.enabled_apps
    monkeypatch.setattr(apps_mod, "enabled_apps", lambda s: [*real(s), "gluetun", "recyclarr"])
    write_setup(state_dir, ports=PORTS_FOR_SETUP)
    signin(client, password, headers=VPN_HOST)
    agent.states = {"gluetun": "running"}
    html = get(client).get_data(as_text=True)
    assert "Gluetun" in html
    assert ":0/" not in html
    assert html.count('href="http://localhost:') == len(real(app.extensions["orama"].setup.current()))
    assert "Recyclarr" in html and "Open Recyclarr" not in html


def test_agent_down_message(signed, agent):
    for error in (AgentError("x"), OSError("sock")):
        agent.error = error
        html = get(signed).get_data(as_text=True)
        assert ("Orama cannot reach its helper right now — the apps keep running. "
                "Try again in a minute.") in html
        assert "Running" not in html and "Not found" not in html


def test_partial_is_only_the_list(signed):
    html = get(signed, "/apps/list").get_data(as_text=True)
    assert "<html" not in html and "Plex" in html


def test_page_refreshes_with_htmx_without_inline_code(signed):
    html = get(signed).get_data(as_text=True)
    assert 'hx-get="/apps/list"' in html and 'hx-trigger="every 10s"' in html
    assert "<script>" not in html and " style=" not in html
    assert not re.search(r"\son[a-z]+=", html)


def test_apps_signed_out_redirects(app, client, password):
    response = client.get("/apps", headers=VPN_HOST)
    assert response.status_code == 303 and response.headers["Location"].startswith("/signin")
    assert client.get("/apps/list", headers=VPN_HOST).status_code == 303


def test_apps_post_not_allowed(signed):
    token = csrf_from(get(signed))
    assert signed.post("/apps", data={"csrf_token": token}).status_code == 405


def test_apps_uses_draft_when_no_setup_json(app, client, password, agent, state_dir):
    (state_dir / "setup.json").unlink()
    write_setup(state_dir, "setup.draft.json", ports=PORTS_FOR_SETUP)
    signin(client, password, headers=VPN_HOST)
    assert 'href="http://localhost:32400/"' in get(client).get_data(as_text=True)


# ---- Home

def test_home_redirects_to_wizard_before_setup(app, client, password, agent, state_dir):
    (state_dir / "setup.json").unlink()
    write_setup(state_dir, "setup.draft.json")
    signin(client, password, headers=VPN_HOST)
    response = get(client, "/")
    assert response.status_code == 303 and response.headers["Location"] == "/setup/media"


def test_home_without_password_goes_to_first_run(client):
    response = get(client, "/")
    assert response.status_code == 303 and response.headers["Location"] == "/setup/password"


def test_home_signed_out_goes_to_signin(client, password):
    response = get(client, "/")
    assert response.status_code == 303 and response.headers["Location"].startswith("/signin")


def test_home_shows_welcome_links_and_status(signed):
    html = get(signed, "/").get_data(as_text=True)
    assert "Requests arrive in the next update." in html
    assert 'href="/apps"' in html and 'href="/settings"' in html
    assert "4 of 6 apps running" in html


def test_home_without_helper_still_renders(signed, agent):
    agent.error = AgentError("x")
    response = get(signed, "/")
    assert response.status_code == 200
    assert "apps running" not in response.get_data(as_text=True)
