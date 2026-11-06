"""Settings and Change setup: edits setup.json directly, never over an unfinished draft."""

from __future__ import annotations

import json
import secrets
import sys
from pathlib import Path

import pytest
from conftest import make_setup, random_secret

from orama import state
from orama.secrets import VPN_KEY_NAME
from orama.setup_model import DesiredSetup
from orama.web import catalogue, forms, wizard
from orama.web.jobs import JobRunner
from orama.wiring import prowlarr, sabnzbd

from .conftest import EXTRA, HOST_NAME, LAN, NEW_LAN, csrf_from, new_password, signin, write_setup
from .test_wizard_indexers import DEFINITION


@pytest.fixture
def signed(client, password):
    assert signin(client, password).status_code == 303
    return client


def sd(app) -> Path:
    return app.extensions["orama"].state_dir


def live(app) -> DesiredSetup:
    return DesiredSetup.load(sd(app) / "setup.json")


def page(response) -> str:
    return response.get_data(as_text=True)


def post(client, path: str, *, token_from: str = "/settings", **data):
    token = csrf_from(client.get(token_from))
    return client.post(path, data={"csrf_token": token, **data})


@pytest.fixture
def runner(app, tmp_path):
    """A job runner that records every start and ends at once."""
    log = tmp_path / "starts.log"
    code = f"import sys\nopen({str(log)!r},'a').write(sys.argv[1]+'\\n')\n"
    job = JobRunner(sd(app), lambda p: [sys.executable, "-c", code, str(p)])
    app.extensions["orama"].jobs = job
    job.log = log
    return job


def starts(runner) -> list[str]:
    runner.wait(10)
    return runner.log.read_text().split() if runner.log.exists() else []


def write_hardware(app, **values):
    (sd(app) / "hardware.json").write_text(json.dumps({"render_gid": None, "nvidia": False, **values}))


# ------------------------------------------------------------------ access


def test_settings_needs_sign_in(client, password):
    response = client.get("/settings")
    assert response.status_code == 303 and "/signin" in response.headers["Location"]
    assert client.post("/settings/media", data={}).status_code in (303, 403)


def test_page_lists_every_section(signed, app):
    html = page(signed.get("/settings"))
    for text in ("Media servers", "Folders", "Downloads and VPN", "Playback", "Subtitles",
                 "Indexers", "Addresses", "Orama password", "Change setup"):
        assert text in html
    assert 'action="/settings/media"' in html and 'action="/settings/change-setup"' in html


def test_embedded_sections_leak_no_page_title(signed):
    assert "Setup · Orama" not in page(signed.get("/settings"))


def test_sections_are_phone_friendly_and_have_no_inline_code(signed):
    html = page(signed.get("/settings"))
    for bad in (" style=", " onclick=", "<script>", "confirm("):
        assert bad not in html


# ------------------------------------------------------------------ sections


SAVES = {
    "media": ({"server_plex": "on"}, lambda s: s.media_servers == frozenset({"plex"})),
    "folders": ({"data_root": "/srv/other"}, lambda s: str(s.data_root) == "/srv/other"),
    "downloads": ({"usenet": "on", "torrent": "on"},
                  lambda s: s.download_methods == frozenset({"usenet", "torrent"})),
    "playback": ({"device": "lg", "quality": "1080p"},
                 lambda s: (s.device, s.quality) == ("lg", "1080p")),
    "subtitles": ({"language_1": "el", "language_2": "en"},
                  lambda s: s.subtitle_languages == ("el", "en")),
}


@pytest.mark.parametrize("section", sorted(SAVES))
def test_each_section_saves_and_starts_apply(signed, app, runner, section):
    form, check = SAVES[section]
    response = post(signed, f"/settings/{section}", **form)
    assert response.status_code == 303
    assert response.headers["Location"] == "/settings?applied=1"
    assert check(live(app))
    assert starts(runner) == [str(sd(app) / "setup.json")]
    assert not (sd(app) / "setup.draft.json").exists()
    body = page(signed.get("/settings?applied=1"))
    assert 'id="checklist-items"' in body


def test_graphics_save_starts_apply(signed, app, runner):
    write_hardware(app, render_gid=44, nvidia=True)
    response = post(signed, "/settings/graphics")
    assert response.status_code == 303
    assert starts(runner) == [str(sd(app) / "setup.json")]
    saved = live(app)
    assert (saved.render_gid, saved.nvidia) == (44, True)


def test_invalid_section_shows_errors_and_saves_nothing(signed, app, runner):
    before = (sd(app) / "setup.json").read_bytes()
    response = post(signed, "/settings/media")  # no media server ticked
    assert response.status_code == 400
    assert "Choose at least one: Plex or Jellyfin." in page(response)
    assert (sd(app) / "setup.json").read_bytes() == before
    assert starts(runner) == []


def test_busy_shows_plain_message_and_saves_nothing(signed, app, tmp_path):
    gate = tmp_path / "gate"
    code = f"import time,pathlib\nwhile not pathlib.Path({str(gate)!r}).exists(): time.sleep(0.02)\n"
    job = JobRunner(sd(app), lambda p: [sys.executable, "-c", code])
    app.extensions["orama"].jobs = job
    job.start(sd(app) / "setup.json")
    before = (sd(app) / "setup.json").read_bytes()
    try:
        response = post(signed, "/settings/playback", device="lg", quality="4k")
    finally:
        gate.write_text("x")
        job.wait(10)
    assert response.status_code == 409
    assert "Orama is busy applying the setup. Try again in a moment." in page(response)
    assert (sd(app) / "setup.json").read_bytes() == before


def test_page_shows_job_checklist_polling_while_running(signed, app, tmp_path):
    gate = tmp_path / "gate"
    code = f"import time,pathlib\nwhile not pathlib.Path({str(gate)!r}).exists(): time.sleep(0.02)\n"
    job = JobRunner(sd(app), lambda p: [sys.executable, "-c", code])
    app.extensions["orama"].jobs = job
    job.start(sd(app) / "setup.json")
    try:
        html = page(signed.get("/settings"))
    finally:
        gate.write_text("x")
        job.wait(10)
    assert 'id="checklist-items"' in html and 'hx-get="/jobs/current"' in html


# ------------------------------------------------------------------ secrets


def vpn_setup(app):
    write_setup(sd(app), vpn={"provider": "protonvpn"}, download_methods={"torrent"})
    key = random_secret("vpn", 12)
    app.extensions["orama"].store.set(VPN_KEY_NAME, key)
    return key


def test_empty_secret_keeps_stored_vpn_key(signed, app, runner):
    key = vpn_setup(app)
    html = page(signed.get("/settings"))
    assert key not in html and "Saved" in html
    response = post(signed, "/settings/downloads", torrent="on", vpn="on",
                    vpn_provider="protonvpn", vpn_key="")
    assert response.status_code == 303
    assert app.extensions["orama"].store.get(VPN_KEY_NAME) == key
    assert key not in page(signed.get("/settings?applied=1"))


def test_indexer_empty_secret_keeps_stored_value(signed, app, runner, monkeypatch):
    monkeypatch.setattr(catalogue, "load", lambda *args: [DEFINITION])
    monkeypatch.setattr(prowlarr, "test_indexer", lambda *args: None)
    store = app.extensions["orama"].store
    key = secrets.token_urlsafe(20)
    setup, slug = forms.add_indexer(live(app), DEFINITION, {
        "name": "Mine", "field:apiKey": key, "field:baseUrl": "https://indexer.example",
    }, store)
    setup.save(sd(app) / "setup.json")
    edit = signed.get(f"/settings/indexers?edit={slug}")
    assert edit.status_code == 200 and key not in page(edit)
    response = signed.post("/settings/indexers", data={
        "csrf_token": csrf_from(edit), "edit": slug, "definition": "Newznab",
        "field:apiKey": "", "field:baseUrl": "https://other.example",
    })
    assert response.status_code == 200
    assert store.get(f"indexer:{slug}:apiKey") == key
    assert key not in page(response)
    assert live(app).indexers[0].fields["baseUrl"] == "https://other.example"
    assert starts(runner) == [str(sd(app) / "setup.json")]


def test_usenet_empty_password_keeps_stored_value(signed, app, runner, monkeypatch):
    monkeypatch.setattr(sabnzbd, "test_usenet_server", lambda *args: None)
    store = app.extensions["orama"].store
    secret = secrets.token_urlsafe(20)
    setup, slug = forms.add_usenet_server(live(app), {
        "host": "news.example", "port": "563", "username": "me", "password": secret, "ssl": "on",
    }, store)
    setup.save(sd(app) / "setup.json")
    edit = signed.get(f"/settings/indexers?edit-usenet={slug}")
    assert secret not in page(edit)
    response = signed.post("/settings/usenet", data={
        "csrf_token": csrf_from(edit), "edit-usenet": slug, "host": "news.example",
        "port": "119", "connections": "10", "username": "me", "password": "",
    })
    assert response.status_code == 200
    assert store.get(f"usenet:{slug}:password") == secret
    assert secret not in page(response)
    assert live(app).usenet_servers[0].port == 119


def test_remove_indexer_from_settings_starts_apply(signed, app, runner):
    store = app.extensions["orama"].store
    setup, slug = forms.add_indexer(live(app), DEFINITION, {"name": "Gone"}, store)
    setup.save(sd(app) / "setup.json")
    response = post(signed, f"/settings/indexers/{slug}/remove", token_from="/settings/indexers")
    assert response.status_code == 200 and "Removed." in page(response)
    assert not live(app).indexers
    assert starts(runner) == [str(sd(app) / "setup.json")]


# ------------------------------------------------------------------ addresses


def other_name() -> str:
    return "extra-" + secrets.token_hex(3) + ".home.example"


def test_extra_host_name_allows_that_host_next_request(signed, app):
    name = other_name()
    assert signed.get("/healthz", headers={"Host": name}).status_code == 400
    response = post(signed, "/settings/addresses", add_name=name.upper())
    assert response.status_code == 303
    assert name in live(app).extra_host_names
    assert signed.get("/healthz", headers={"Host": name}).status_code == 200
    removal = post(signed, "/settings/addresses", remove=name)
    assert removal.status_code == 303
    assert signed.get("/healthz", headers={"Host": name}).status_code == 400


def test_addresses_show_current_values_read_only(signed):
    html = page(signed.get("/settings"))
    assert LAN in html and HOST_NAME in html and EXTRA in html
    assert 'name="lan_address"' not in html and 'name="host_name"' not in html


@pytest.mark.parametrize("bad", ["not a name!", "a b", "-x", "x" * 300])
def test_bad_host_name_is_refused(signed, app, bad):
    before = live(app).extra_host_names
    response = post(signed, "/settings/addresses", add_name=bad)
    assert response.status_code == 400 and "not a name Orama can use" in page(response)
    assert live(app).extra_host_names == before


def test_duplicate_and_too_many_host_names(signed, app):
    assert post(signed, "/settings/addresses", add_name=EXTRA).status_code == 400
    names = tuple(f"n{i}.home.example" for i in range(9))
    write_setup(sd(app), extra_host_names=(EXTRA, *names))
    response = post(signed, "/settings/addresses", add_name="more.home.example")
    assert response.status_code == 400 and "up to 10" in page(response)


def test_addresses_do_not_start_apply(signed, app, runner):
    post(signed, "/settings/addresses", add_name=other_name())
    assert starts(runner) == []


# ------------------------------------------------------------------ graphics


def test_graphics_refresh_only_when_different(signed, app):
    write_hardware(app, render_gid=None, nvidia=False)
    assert "The installer found" not in page(signed.get("/settings"))
    write_hardware(app, render_gid=105, nvidia=True)
    html = page(signed.get("/settings"))
    assert "The installer found" in html and "Use this" in html
    assert 'action="/settings/graphics"' in html


def test_graphics_with_nothing_different_changes_nothing(signed, app, runner):
    write_hardware(app)
    response = post(signed, "/settings/graphics")
    assert response.status_code == 303 and response.headers["Location"] == "/settings"
    assert starts(runner) == []


def test_graphics_ignores_a_damaged_hardware_file(signed, app):
    (sd(app) / "hardware.json").write_text("{not json")
    assert signed.get("/settings").status_code == 200
    assert post(signed, "/settings/graphics").status_code == 303


# ------------------------------------------------------------------ password


def test_password_change_needs_current_password(signed, app, password):
    new = new_password()
    wrong = post(signed, "/settings/password", current_password="nope" * 4,
                 new_password=new, repeat_password=new)
    assert wrong.status_code == 400 and "not your current password" in page(wrong)
    assert app.extensions["orama"].passwords.verify(password)
    short = post(signed, "/settings/password", current_password=password,
                 new_password="short", repeat_password="short")
    assert short.status_code == 400 and "12" in page(short)
    mismatch = post(signed, "/settings/password", current_password=password,
                    new_password=new, repeat_password=new + "x")
    assert mismatch.status_code == 400 and "do not match" in page(mismatch)
    assert app.extensions["orama"].passwords.verify(password)


def test_password_change_ends_other_sessions(signed, app, password):
    other = app.test_client()
    assert signin(other, password).status_code == 303
    new = new_password()
    response = post(signed, "/settings/password", current_password=password,
                    new_password=new, repeat_password=new)
    assert response.status_code == 303
    assert app.extensions["orama"].passwords.verify(new)
    assert other.get("/settings").status_code == 303  # signed out
    assert signed.get("/settings").status_code == 200  # this one stays signed in
    assert new not in page(signed.get("/settings"))


# ------------------------------------------------------------------ Change setup and drafts


def test_change_setup_opens_prefilled_wizard(signed, app):
    write_setup(sd(app), device="lg")
    response = post(signed, "/settings/change-setup")
    assert response.status_code == 303 and response.headers["Location"] == "/setup/media"
    draft = DesiredSetup.load(sd(app) / "setup.draft.json")
    assert draft == live(app)
    playback = page(signed.get("/setup/playback"))
    assert 'value="lg" checked' in playback


def test_change_setup_never_overwrites_a_draft(signed, app):
    write_setup(sd(app), "setup.draft.json", device="apple_tv")
    response = post(signed, "/settings/change-setup")
    assert response.status_code == 303 and response.headers["Location"] == "/setup/media"
    assert DesiredSetup.load(sd(app) / "setup.draft.json").device == "apple_tv"


def test_draft_blocks_settings_saves(signed, app, runner):
    write_setup(sd(app), "setup.draft.json", device="apple_tv")
    html = page(signed.get("/settings"))
    assert "You have an unfinished setup change." in html
    assert "Continue it" in html and "Throw it away" in html
    assert 'action="/settings/media"' not in html
    before = (sd(app) / "setup.json").read_bytes()
    draft = (sd(app) / "setup.draft.json").read_bytes()
    response = post(signed, "/settings/playback", device="lg", quality="4k")
    assert response.status_code == 303 and response.headers["Location"] == "/settings"
    assert (sd(app) / "setup.json").read_bytes() == before
    assert (sd(app) / "setup.draft.json").read_bytes() == draft
    assert starts(runner) == []


def test_throw_away_deletes_draft(signed, app):
    write_setup(sd(app), "setup.draft.json", device="apple_tv")
    response = post(signed, "/settings/discard-draft")
    assert response.status_code == 303 and response.headers["Location"] == "/settings"
    assert not (sd(app) / "setup.draft.json").exists()
    assert "You have an unfinished setup change." not in page(signed.get("/settings"))


def test_settings_before_any_install_points_to_wizard(signed, app):
    (sd(app) / "setup.json").rename(sd(app) / "setup.draft.json")
    html = page(signed.get("/settings"))
    assert 'action="/settings/media"' not in html and "/setup/media" in html
    assert post(signed, "/settings/playback", device="lg", quality="4k").status_code == 409


def test_promote_keeps_machine_facts_from_live_setup(app):
    st = app.extensions["orama"]
    draft = make_setup(lan_address=LAN, host_name=HOST_NAME, extra_host_names=(),
                       ports={"orama": 8000}, device="lg")
    newer = make_setup(lan_address=NEW_LAN, host_name="new-" + HOST_NAME, device="safest",
                       extra_host_names=(EXTRA,), ports={"orama": 8765})
    newer.save(st.state_dir / "setup.json")
    draft.save(st.state_dir / "setup.draft.json")
    with state.locked(state.setup_lock(st.state_dir)):
        wizard._promote(st, draft)
    promoted = DesiredSetup.load(st.state_dir / "setup.json")
    assert str(promoted.lan_address) == NEW_LAN
    assert promoted.host_name == "new-" + HOST_NAME
    assert promoted.extra_host_names == (EXTRA,) and promoted.ports == {"orama": 8765}
    assert promoted.device == "lg"  # the owner's answers still win
    assert not (st.state_dir / "setup.draft.json").exists()


# ------------------------------------------------------------------ turning an app off


def test_turning_off_app_warns_and_keeps_folder(signed, app, runner):
    write_setup(sd(app), media_servers={"plex", "jellyfin"})
    before = (sd(app) / "setup.json").read_bytes()
    response = post(signed, "/settings/media", server_plex="on")
    assert response.status_code == 200
    html = page(response)
    assert "This stops Jellyfin. Its settings folder is kept and no media is deleted." in html
    assert "Yes, stop it" in html and 'name="confirm" value="1"' in html
    assert (sd(app) / "setup.json").read_bytes() == before
    assert starts(runner) == []
    done = post(signed, "/settings/media", server_plex="on", confirm="1")
    assert done.status_code == 303
    assert live(app).media_servers == frozenset({"plex"})
    assert starts(runner) == [str(sd(app) / "setup.json")]


def test_download_apps_warn_too(signed, app):
    write_setup(sd(app), download_methods={"usenet", "torrent"})
    html = page(post(signed, "/settings/downloads", usenet="on"))
    assert "This stops qBittorrent." in html
    html = page(post(signed, "/settings/downloads", torrent="on"))
    assert "This stops SABnzbd." in html


def test_confirm_page_never_echoes_typed_secrets(signed, app):
    write_setup(sd(app), media_servers={"plex", "jellyfin"})
    secret = random_secret("pw", 12) + "Aa1"
    html = page(post(signed, "/settings/media", server_jellyfin="on", jellyfin_user="admin",
                     jellyfin_password=secret))
    assert "This stops Plex." in html and secret not in html
    assert 'name="jellyfin_password"' in html  # asked again, empty


def test_invalid_form_is_not_asked_to_confirm(signed, app):
    write_setup(sd(app), media_servers={"plex", "jellyfin"})
    response = post(signed, "/settings/media")  # nothing ticked
    assert response.status_code == 400 and "Yes, stop it" not in page(response)
