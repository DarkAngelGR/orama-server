"""App logos on the Apps page: fetched from each app itself, through Orama, as PNG only.

The paths are the ones the pinned app images were seen to serve (2026-11-13): Sonarr, Radarr
and Prowlarr share one frontend; Bazarr, SABnzbd and Plex each have their own. Jellyfin's
file names change with every release and qBittorrent asks for a sign-in even for images, so
those two keep the icon Orama ships.
"""

from __future__ import annotations

import pytest

from orama.web import app_icons
from orama.wiring.http import FakeHttp, Response
from web.conftest import signin, write_setup

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PORTS = {"plex": 32400, "sonarr": 8989, "radarr": 7878, "prowlarr": 9696, "bazarr": 6767,
         "sabnzbd": 8080}


class Agent:
    def status(self):
        return {name: "running" for name in PORTS}


@pytest.fixture
def http(app) -> FakeHttp:
    fake = FakeHttp()
    app.extensions["orama"].http = fake
    app.extensions["orama"].agent = Agent()
    return fake


@pytest.fixture
def signed(app, client, password, http):
    write_setup(app.extensions["orama"].state_dir, ports=PORTS)
    assert signin(client, password, headers={"Host": "localhost"}).status_code == 303
    return client


def get(client, path):
    return client.get(path, headers={"Host": "localhost"})


@pytest.mark.parametrize(("app_name", "url"), [
    ("sonarr", "http://sonarr:8989/Content/Images/Icons/apple-touch-icon.png"),
    ("radarr", "http://radarr:7878/Content/Images/Icons/apple-touch-icon.png"),
    ("prowlarr", "http://prowlarr:9696/Content/Images/Icons/apple-touch-icon.png"),
    ("bazarr", "http://bazarr:6767/images/logo128.png"),
    ("sabnzbd", "http://sabnzbd:8080/staticcfg/ico/apple-touch-icon-180x180-precomposed.png"),
    ("plex", "http://plex:32400/web/static/icon-iphone@2x.png"),
])
def test_logo_comes_from_the_app_itself_on_the_internal_network(signed, http, app_name, url):
    from urllib.parse import urlsplit

    http.route("GET", urlsplit(url).path, lambda c: Response(200, {"Content-Type": "image/png"}, PNG))
    r = get(signed, f"/apps/icon/{app_name}")
    assert r.status_code == 200
    assert r.data == PNG
    assert r.headers["Content-Type"] == "image/png"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert [c.url for c in http.calls] == [url]
    assert http.calls[0].headers in (None, {})  # no key, no token: the logo is public


@pytest.mark.parametrize("app_name", ["jellyfin", "qbittorrent", "gluetun", "recyclarr",
                                      "orama", "sonarr.png", "SONARR"])
def test_apps_without_a_fetchable_logo_or_unknown_names_are_not_found(signed, http, app_name):
    assert get(signed, f"/apps/icon/{app_name}").status_code == 404
    assert http.calls == []


def test_an_app_that_is_not_set_up_is_not_asked(app, client, password, http):
    write_setup(app.extensions["orama"].state_dir, ports=PORTS, media_servers={"jellyfin"})
    assert signin(client, password, headers={"Host": "localhost"}).status_code == 303
    assert get(client, "/apps/icon/plex").status_code == 404
    assert http.calls == []


@pytest.mark.parametrize("answer", [
    Response(200, {"Content-Type": "image/png"}, b"<svg onload=alert(1)>"),  # not a PNG
    Response(200, {"Content-Type": "text/html"}, b"<!doctype html><p>sign in</p>"),
    Response(200, {"Content-Type": "image/png"}, PNG + b"\x00" * app_icons.MAX_ICON_BYTES),
    Response(401, {}, b"Unauthorized"),
    Response(302, {"Location": "/login"}, b""),
    Response(200, {"Content-Type": "image/png"}, b""),
])
def test_anything_but_a_small_png_is_refused(signed, http, answer):
    http.route("GET", "/Content/Images/Icons/apple-touch-icon.png", lambda c: answer)
    r = get(signed, "/apps/icon/sonarr")
    assert r.status_code == 404
    assert b"alert" not in r.data and b"sign in" not in r.data


def test_an_unreachable_app_is_not_found_and_not_asked_again_at_once(signed, http, app):
    http.unreachable = True
    assert get(signed, "/apps/icon/sonarr").status_code == 404
    assert get(signed, "/apps/icon/sonarr").status_code == 404
    assert len(http.calls) == 1  # the failure is remembered for a while
    app.extensions["orama"].clock.advance(app_icons.RETRY_AFTER + 1)
    http.unreachable = False
    http.route("GET", "/Content/Images/Icons/apple-touch-icon.png",
               lambda c: Response(200, {"Content-Type": "image/png"}, PNG))
    assert get(signed, "/apps/icon/sonarr").status_code == 200
    assert len(http.calls) == 2


def test_a_logo_is_fetched_once_and_then_served_from_memory(signed, http):
    http.route("GET", "/Content/Images/Icons/apple-touch-icon.png",
               lambda c: Response(200, {"Content-Type": "image/png"}, PNG))
    for _ in range(3):
        assert get(signed, "/apps/icon/radarr").data == PNG
    assert len(http.calls) == 1


def test_logos_need_a_signed_in_owner(app, client, http):
    write_setup(app.extensions["orama"].state_dir, ports=PORTS)
    r = get(client, "/apps/icon/sonarr")
    assert r.status_code in (302, 303, 401)
    assert http.calls == []


def test_apps_page_shows_only_logos_the_apps_gave(signed, http):
    """The page asks for a logo only once Orama has it, so the browser never sees a 404."""
    http.route("GET", "/Content/Images/Icons/apple-touch-icon.png",
               lambda c: Response(200, {"Content-Type": "image/png"}, PNG))
    html = get(signed, "/apps").get_data(as_text=True)
    for name in ("sonarr", "radarr", "prowlarr"):
        assert f'src="/apps/icon/{name}"' in html
    # Plex, Bazarr and SABnzbd gave nothing here: they keep the shipped icon or letter.
    for name in ("plex", "bazarr", "sabnzbd", "jellyfin"):
        assert f'src="/apps/icon/{name}"' not in html
    assert html.count('class="app-icon"') >= 1  # Plex's shipped mark


def test_the_list_refresh_does_not_ask_again_for_a_logo_that_failed(signed, http):
    get(signed, "/apps")
    asked = len(http.calls)
    assert asked == 6  # one try per fetchable app
    get(signed, "/apps/list")
    assert len(http.calls) == asked
