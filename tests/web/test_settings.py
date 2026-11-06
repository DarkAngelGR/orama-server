"""Settings and Change setup: edits setup.json directly, never over an unfinished draft."""

from __future__ import annotations

import json
import secrets
import sys
from pathlib import Path

import pytest
from conftest import make_setup, random_secret

from orama import state
from orama.secrets import VPN_KEY_NAME, SecretStore
from orama.setup_model import DesiredSetup
from orama.web import catalogue, forms, wizard
from orama.web import settings as settings_module
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


def passing(kind):
    """A test that passes and records it, as the real ones do."""
    def run(setup, store, http, slug):
        store.set(f"test-ok:{kind}:{slug}", "1")
    return run


def failing(kind, message="Check the details."):
    def run(setup, store, http, slug):
        store.delete(f"test-ok:{kind}:{slug}")
        return message
    return run


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
    monkeypatch.setattr(prowlarr, "test_indexer", passing("indexer"))
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
    monkeypatch.setattr(sabnzbd, "test_usenet_server", passing("usenet"))
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
    assert response.status_code == 409
    assert "Nothing was saved." in page(response)
    assert "You have an unfinished setup change." in page(response)
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
    assert "Yes, turn it off" in html and 'name="confirm" value="1"' in html
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
    assert response.status_code == 400 and "Yes, turn it off" not in page(response)


# ------------------------------------------------------------------ only a passing test is saved


def seeded_indexer(app):
    store = app.extensions["orama"].store
    key = secrets.token_urlsafe(20)
    setup, slug = forms.add_indexer(live(app), DEFINITION, {
        "name": "Mine", "field:apiKey": key, "field:baseUrl": "https://indexer.example",
    }, store)
    setup.save(sd(app) / "setup.json")
    store.set(f"test-ok:indexer:{slug}", "1")
    return store, slug, key


def fake_prowlarr(wrong, calls):
    """Prowlarr holds whatever the last test sent; it refuses the wrong key only."""
    def run(setup, store_, http, slug_):
        item = next(i for i in setup.indexers if i.slug == slug_)
        held = (store_.get(f"indexer:{slug_}:apiKey"), item.fields.get("baseUrl"))
        calls.append(held)
        if held[0] == wrong:
            store_.delete(f"test-ok:indexer:{slug_}")
            return "The indexer refused the key."
        store_.set(f"test-ok:indexer:{slug_}", "1")
        return None
    return run


def edit_with(signed, slug, key):
    edit = signed.get(f"/settings/indexers?edit={slug}")
    return signed.post("/settings/indexers", data={
        "csrf_token": csrf_from(edit), "edit": slug, "definition": "Newznab",
        "field:apiKey": key, "field:baseUrl": "https://indexer.example",
    })


def test_failed_indexer_edit_puts_the_old_one_back(signed, app, runner, monkeypatch):
    monkeypatch.setattr(catalogue, "load", lambda *args: [DEFINITION])
    store, slug, key = seeded_indexer(app)
    wrong = secrets.token_urlsafe(20)
    calls = []
    monkeypatch.setattr(prowlarr, "test_indexer", fake_prowlarr(wrong, calls))
    before = (sd(app) / "setup.json").read_bytes()
    response = edit_with(signed, slug, wrong)
    assert response.status_code == 400
    html = page(response)
    assert "The indexer refused the key." in html and "Nothing was saved." in html
    assert "could not put the old settings back" not in html and wrong not in html
    # The app saw the candidate first, then the old values again.
    assert calls == [(wrong, "https://indexer.example"), (key, "https://indexer.example")]
    assert (sd(app) / "setup.json").read_bytes() == before
    assert store.get(f"indexer:{slug}:apiKey") == key
    assert store.get(f"test-ok:indexer:{slug}") == "1"
    assert starts(runner) == []


def test_failed_restore_says_so(signed, app, runner, monkeypatch):
    monkeypatch.setattr(catalogue, "load", lambda *args: [DEFINITION])
    store, slug, key = seeded_indexer(app)
    monkeypatch.setattr(prowlarr, "test_indexer", failing("indexer", "Prowlarr does not answer."))
    before = (sd(app) / "setup.json").read_bytes()
    response = edit_with(signed, slug, secrets.token_urlsafe(20))
    html = page(response)
    assert response.status_code == 400
    assert "Orama could not put the old settings back. Apply again to fix it." in html
    assert (sd(app) / "setup.json").read_bytes() == before
    assert store.get(f"indexer:{slug}:apiKey") == key
    assert starts(runner) == []


def test_passing_indexer_edit_saves_applies_and_records_test(signed, app, runner, monkeypatch):
    monkeypatch.setattr(catalogue, "load", lambda *args: [DEFINITION])
    monkeypatch.setattr(prowlarr, "test_indexer", passing("indexer"))
    store, slug, _old = seeded_indexer(app)
    store.delete(f"test-ok:indexer:{slug}")
    new = secrets.token_urlsafe(20)
    edit = signed.get(f"/settings/indexers?edit={slug}")
    response = signed.post("/settings/indexers", data={
        "csrf_token": csrf_from(edit), "edit": slug, "definition": "Newznab",
        "field:apiKey": new, "field:baseUrl": "https://indexer.example",
    })
    assert response.status_code == 200 and "works" in page(response)
    assert store.get(f"indexer:{slug}:apiKey") == new
    assert store.get(f"test-ok:indexer:{slug}") == "1"
    assert starts(runner) == [str(sd(app) / "setup.json")]


def test_failed_new_indexer_leaves_no_entry_or_secret(signed, app, runner, monkeypatch):
    monkeypatch.setattr(catalogue, "load", lambda *args: [DEFINITION])
    monkeypatch.setattr(prowlarr, "test_indexer", failing("indexer"))
    store = app.extensions["orama"].store
    before = (sd(app) / "setup.json").read_bytes()
    key = secrets.token_urlsafe(20)
    response = post(signed, "/settings/indexers", token_from="/settings/indexers",
                    definition="Newznab", name="Fresh", **{"field:apiKey": key})
    assert response.status_code == 400
    assert (sd(app) / "setup.json").read_bytes() == before
    assert store.get("indexer:fresh:apiKey") is None
    assert starts(runner) == []


def test_failed_new_indexer_created_in_the_app_is_marked_for_removal(
        signed, app, runner, monkeypatch):
    monkeypatch.setattr(catalogue, "load", lambda *args: [DEFINITION])
    store = app.extensions["orama"].store

    def created_then_failed(setup, store_, http, slug):
        store_.set(prowlarr.sent_marker_name(f"indexer:{slug}"), "recorded")
        return "It did not answer."

    monkeypatch.setattr(prowlarr, "test_indexer", created_then_failed)
    response = post(signed, "/settings/indexers", token_from="/settings/indexers",
                    definition="Newznab", name="Fresh")
    assert response.status_code == 400
    assert store.get(prowlarr.REMOVE_PREFIX + "fresh") == "Fresh"
    assert starts(runner) == []


def test_failed_usenet_edit_saves_nothing_and_keeps_old_password(signed, app, runner, monkeypatch):
    store = app.extensions["orama"].store
    secret = secrets.token_urlsafe(20)
    wrong = secrets.token_urlsafe(20)
    calls = []

    def fake_sabnzbd(setup, store_, http, slug_):
        held = (store_.get(f"usenet:{slug_}:password"), next(
            s.port for s in setup.usenet_servers if s.slug == slug_))
        calls.append(held)
        if held[0] == wrong:
            store_.delete(f"test-ok:usenet:{slug_}")
            return "Login refused."
        store_.set(f"test-ok:usenet:{slug_}", "1")

    monkeypatch.setattr(sabnzbd, "test_usenet_server", fake_sabnzbd)
    setup, slug = forms.add_usenet_server(live(app), {
        "host": "news.example", "port": "563", "username": "me", "password": secret, "ssl": "on",
    }, store)
    setup.save(sd(app) / "setup.json")
    store.set(f"test-ok:usenet:{slug}", "1")
    before = (sd(app) / "setup.json").read_bytes()
    edit = signed.get(f"/settings/indexers?edit-usenet={slug}")
    response = signed.post("/settings/usenet", data={
        "csrf_token": csrf_from(edit), "edit-usenet": slug, "host": "news.example",
        "port": "119", "connections": "10", "username": "me", "password": wrong,
    })
    assert response.status_code == 400 and "Login refused." in page(response)
    assert calls == [(wrong, 119), (secret, 563)]  # the old server was put back
    assert (sd(app) / "setup.json").read_bytes() == before
    assert store.get(f"usenet:{slug}:password") == secret
    assert store.get(f"test-ok:usenet:{slug}") == "1"
    assert starts(runner) == []


# ------------------------------------------------------------------ smaller fixes


def test_cannot_remove_the_name_in_use_right_now(signed, app, password):
    # Another name is in use here, so removing EXTRA is fine (and EXTRA comes back after).
    assert post(signed, "/settings/addresses", remove=EXTRA).status_code == 303
    write_setup(sd(app))
    host = {"Host": EXTRA.upper() + ".:8765"}
    via_name = app.test_client()
    assert signin(via_name, password, headers=host).status_code == 303
    token = csrf_from(via_name.get("/settings", headers=host))
    refused = via_name.post("/settings/addresses", headers=host,
                            data={"csrf_token": token, "remove": EXTRA})
    assert refused.status_code == 400
    assert "You are using Orama through that name right now." in page(refused)
    assert EXTRA in live(app).extra_host_names


def test_turning_the_vpn_off_asks_first(signed, app, runner):
    vpn_setup(app)
    response = post(signed, "/settings/downloads", torrent="on")
    assert response.status_code == 200
    assert "This turns the VPN off. Torrent downloads will no longer go through the VPN." in page(response)
    assert live(app).vpn is not None and starts(runner) == []
    done = post(signed, "/settings/downloads", torrent="on", confirm="1")
    assert done.status_code == 303 and live(app).vpn is None


def test_usenet_off_with_servers_is_refused_not_confirmed(signed, app):
    setup, _slug = forms.add_usenet_server(live(app), {
        "host": "news.example", "port": "563", "username": "me", "password": "x" * 12,
    }, app.extensions["orama"].store)
    setup.save(sd(app) / "setup.json")
    write_setup_methods = setup.to_json_dict()
    assert write_setup_methods["usenet_servers"]
    response = post(signed, "/settings/downloads", torrent="on")
    assert response.status_code == 400
    assert "Yes, turn it off" not in page(response) and "Usenet servers" in page(response)
    assert "usenet" in live(app).download_methods


def test_graphics_the_model_refuses_gives_plain_message(signed, app, monkeypatch):
    from pydantic import ValidationError

    write_hardware(app, render_gid=105)

    def refuse(*args, **kwargs):
        DesiredSetup.model_validate({})  # raises ValidationError
    monkeypatch.setattr(forms, "_rebuild", refuse)
    response = post(signed, "/settings/graphics")
    assert response.status_code == 400
    assert "Orama cannot use what the installer found." in page(response)
    assert ValidationError  # imported for the comment above


def test_current_password_guesses_are_rate_limited(signed, app, password):
    new = new_password()
    for _ in range(5):
        wrong = post(signed, "/settings/password", current_password="wrong-" + "x" * 8,
                     new_password=new, repeat_password=new)
        assert wrong.status_code == 400
    limited = post(signed, "/settings/password", current_password=password,
                   new_password=new, repeat_password=new)
    assert limited.status_code == 429
    assert "Too many wrong tries." in page(limited)
    assert app.extensions["orama"].passwords.verify(password)


def test_post_without_csrf_token_is_refused(signed, app):
    before = (sd(app) / "setup.json").read_bytes()
    response = signed.post("/settings/playback", data={"device": "lg", "quality": "4k"})
    assert response.status_code == 403
    assert (sd(app) / "setup.json").read_bytes() == before


# ------------------------------------------------------------------ no network under setup.lock


def test_slow_test_does_not_hold_setup_lock(signed, app, runner, monkeypatch):
    import threading

    monkeypatch.setattr(catalogue, "load", lambda *args: [DEFINITION])
    _store, slug, _key = seeded_indexer(app)
    started, release = threading.Event(), threading.Event()
    got = {}

    def slow(setup, store_, http, slug_):
        started.set()
        release.wait(10)
        store_.set(f"test-ok:indexer:{slug_}", "1")

    monkeypatch.setattr(prowlarr, "test_indexer", slow)
    edit = signed.get(f"/settings/indexers?edit={slug}")
    form = {"csrf_token": csrf_from(edit), "edit": slug, "definition": "Newznab",
            "field:apiKey": secrets.token_urlsafe(20), "field:baseUrl": "https://indexer.example"}
    worker = threading.Thread(
        target=lambda: got.setdefault("r", signed.post("/settings/indexers", data=form)))
    worker.start()
    try:
        assert started.wait(10)
        # While the test is "on the network", setup.lock is free for everyone else.
        with state.locked(state.setup_lock(sd(app)), blocking=False):
            got["free"] = True
    finally:
        release.set()
        worker.join(10)
    assert got["free"] and got["r"].status_code == 200


def test_setup_changed_during_test_saves_nothing(signed, app, runner, monkeypatch):
    monkeypatch.setattr(catalogue, "load", lambda *args: [DEFINITION])
    store, slug, key = seeded_indexer(app)

    def changes_meanwhile(setup, store_, http, slug_):
        forms._rebuild(live(app), device="lg").save(sd(app) / "setup.json")
        store_.set(f"test-ok:indexer:{slug_}", "1")

    monkeypatch.setattr(prowlarr, "test_indexer", changes_meanwhile)
    response = edit_with(signed, slug, secrets.token_urlsafe(20))
    assert response.status_code == 409
    assert "Settings changed while Orama was testing. Nothing was saved. Try again." in page(response)
    assert live(app).device == "lg"  # the other change stands
    assert live(app).indexers[0].fields["baseUrl"] == "https://indexer.example"
    assert store.get(f"indexer:{slug}:apiKey") == key
    assert starts(runner) == []


def test_replayed_usenet_server_that_fails_is_not_queued_for_removal(
        signed, app, runner, monkeypatch):
    store = app.extensions["orama"].store
    form = {"host": "news.example", "port": "563", "connections": "20", "username": "me",
            "ssl": "on", "password": secrets.token_urlsafe(20)}
    setup, slug = forms.add_usenet_server(live(app), form, store)
    setup.save(sd(app) / "setup.json")

    def created_then_failed(setup_, store_, http, slug_):
        store_.set(sabnzbd.sent_marker_name(slug_), "recorded")
        return "Login refused."

    monkeypatch.setattr(sabnzbd, "test_usenet_server", created_then_failed)
    replay = {k: v for k, v in form.items() if k != "password"}
    response = post(signed, "/settings/usenet", token_from="/settings/indexers", **replay)
    assert response.status_code == 400 and "Login refused." in page(response)
    assert store.get(sabnzbd.REMOVE_PREFIX + slug) is None
    assert live(app).usenet_servers[0].slug == slug


# ------------------------------------------------------------------ a passing test that is not saved is undone

RESTORE_FAILED = "Orama could not put the old settings back. Apply again to fix it."
CHANGED = settings_module.CHANGED_MEANWHILE


def changed_by_someone_else(app):
    forms._rebuild(live(app), device="lg").save(sd(app) / "setup.json")


def draft_appears(app):
    (sd(app) / "setup.draft.json").write_bytes((sd(app) / "setup.json").read_bytes())


def created_and_passed(app, meanwhile, marker, kind):
    """The real test's effect: the app now holds it (sent marker) and test-ok is set."""
    def run(setup, store_, http, slug_):
        store_.set(marker(slug_), "recorded")
        meanwhile(app)
        store_.set(f"test-ok:{kind}:{slug_}", "1")
    return run


@pytest.mark.parametrize("meanwhile, status, text", [
    (changed_by_someone_else, 409, CHANGED),
    (draft_appears, 409, "Nothing was saved."),
])
def test_new_indexer_passed_but_not_saved_is_queued_for_removal(
        signed, app, runner, monkeypatch, meanwhile, status, text):
    monkeypatch.setattr(catalogue, "load", lambda *args: [DEFINITION])
    store = app.extensions["orama"].store
    monkeypatch.setattr(prowlarr, "test_indexer", created_and_passed(
        app, meanwhile, lambda s: prowlarr.sent_marker_name(f"indexer:{s}"), "indexer"))
    key = secrets.token_urlsafe(20)
    response = post(signed, "/settings/indexers", token_from="/settings/indexers",
                    definition="Newznab", name="Fresh", **{"field:apiKey": key})
    after_test = (sd(app) / "setup.json").read_bytes()
    assert response.status_code == status and text in page(response)
    assert RESTORE_FAILED not in page(response)
    assert store.get(prowlarr.REMOVE_PREFIX + "fresh") == "Fresh"
    assert store.get("test-ok:indexer:fresh") is None
    assert store.get("indexer:fresh:apiKey") is None
    assert (sd(app) / "setup.json").read_bytes() == after_test
    assert not any(i.slug == "fresh" for i in live(app).indexers)
    assert starts(runner) == []


def test_new_usenet_server_passed_but_not_saved_is_queued_for_removal(
        signed, app, runner, monkeypatch):
    store = app.extensions["orama"].store
    monkeypatch.setattr(sabnzbd, "test_usenet_server", created_and_passed(
        app, changed_by_someone_else, sabnzbd.sent_marker_name, "usenet"))
    response = post(signed, "/settings/usenet", token_from="/settings/indexers",
                    host="news.example", port="563", connections="10", username="me",
                    password=secrets.token_urlsafe(20), ssl="on")
    assert response.status_code == 409 and CHANGED in page(response)
    queued = [n for n in store.names() if n.startswith(sabnzbd.REMOVE_PREFIX)]
    assert len(queued) == 1
    slug = queued[0][len(sabnzbd.REMOVE_PREFIX):]
    assert store.get(queued[0]) == slug
    assert store.get(f"test-ok:usenet:{slug}") is None
    assert store.get(f"usenet:{slug}:password") is None
    assert not live(app).usenet_servers
    assert starts(runner) == []


def recording_prowlarr(app, calls, meanwhile, *, restore_fails=False):
    """Records what each test sent; runs ``meanwhile`` during the first (candidate) test."""
    def run(setup, store_, http, slug_):
        item = next(i for i in setup.indexers if i.slug == slug_)
        calls.append((store_.get(f"indexer:{slug_}:apiKey"), setup.device))
        if len(calls) == 1:
            meanwhile(app)
        elif restore_fails:
            store_.delete(f"test-ok:indexer:{slug_}")
            return "Prowlarr does not answer."
        assert item.fields.get("baseUrl") == "https://indexer.example"
        store_.set(f"test-ok:indexer:{slug_}", "1")
        return None
    return run


@pytest.mark.parametrize("meanwhile, text", [
    (changed_by_someone_else, settings_module.CHANGED_MEANWHILE),
    (draft_appears, "Nothing was saved."),
])
def test_indexer_edit_passed_but_not_saved_puts_the_old_one_back(
        signed, app, runner, monkeypatch, meanwhile, text):
    monkeypatch.setattr(catalogue, "load", lambda *args: [DEFINITION])
    store, slug, key = seeded_indexer(app)
    calls = []
    monkeypatch.setattr(prowlarr, "test_indexer", recording_prowlarr(app, calls, meanwhile))
    new = secrets.token_urlsafe(20)
    before_device = live(app).device
    response = edit_with(signed, slug, new)
    after_test = (sd(app) / "setup.json").read_bytes()
    html = page(response)
    assert response.status_code == 409 and text in html and RESTORE_FAILED not in html
    # The candidate first, then the old key on the setup as it is now.
    now_device = live(app).device
    assert calls == [(new, before_device), (key, now_device)]
    assert store.get(f"indexer:{slug}:apiKey") == key
    assert store.get(f"test-ok:indexer:{slug}") == "1"  # recorded by the restore, for the old key
    assert (sd(app) / "setup.json").read_bytes() == after_test
    assert new not in html
    assert starts(runner) == []


def test_failed_undo_after_settings_changed_says_so(signed, app, runner, monkeypatch):
    monkeypatch.setattr(catalogue, "load", lambda *args: [DEFINITION])
    store, slug, key = seeded_indexer(app)
    calls = []
    monkeypatch.setattr(prowlarr, "test_indexer", recording_prowlarr(
        app, calls, changed_by_someone_else, restore_fails=True))
    response = edit_with(signed, slug, secrets.token_urlsafe(20))
    html = page(response)
    assert response.status_code == 409
    assert f"{settings_module.CHANGED_MEANWHILE} {RESTORE_FAILED}" in html
    assert len(calls) == 2
    assert store.get(f"indexer:{slug}:apiKey") == key
    assert store.get(f"test-ok:indexer:{slug}") is None
    assert starts(runner) == []


def test_setup_is_saved_before_the_new_secret(signed, app, runner, monkeypatch):
    """A failed setup.json write leaves the old secret and puts the old entry back."""
    monkeypatch.setattr(catalogue, "load", lambda *args: [DEFINITION])
    store, slug, key = seeded_indexer(app)
    calls = []
    monkeypatch.setattr(prowlarr, "test_indexer", recording_prowlarr(app, calls, lambda _: None))
    before = (sd(app) / "setup.json").read_bytes()
    real_save = DesiredSetup.save

    def broken_save(self, path):
        if Path(path).name == "setup.json":
            raise OSError("disk full")
        real_save(self, path)

    monkeypatch.setattr(DesiredSetup, "save", broken_save)
    new = secrets.token_urlsafe(20)
    response = edit_with(signed, slug, new)
    html = page(response)
    assert response.status_code == 500
    assert settings_module.SAVE_FAILED in html and RESTORE_FAILED not in html
    assert "disk full" not in html and new not in html
    assert (sd(app) / "setup.json").read_bytes() == before
    assert store.get(f"indexer:{slug}:apiKey") == key
    assert [c[0] for c in calls] == [new, key]
    assert starts(runner) == []


def test_failed_secret_write_puts_setup_json_back(signed, app, runner, monkeypatch):
    monkeypatch.setattr(catalogue, "load", lambda *args: [DEFINITION])
    store, slug, key = seeded_indexer(app)
    calls = []
    monkeypatch.setattr(prowlarr, "test_indexer", recording_prowlarr(app, calls, lambda _: None))
    before = (sd(app) / "setup.json").read_bytes()
    order = []
    real_save = DesiredSetup.save

    def logged_save(self, path):
        order.append("setup")
        real_save(self, path)

    def broken_update(self, changes):
        order.append("secrets")
        raise OSError("disk full")

    monkeypatch.setattr(DesiredSetup, "save", logged_save)
    monkeypatch.setattr(SecretStore, "update", broken_update)
    new = secrets.token_urlsafe(20)
    response = edit_with(signed, slug, new)
    html = page(response)
    assert order == ["setup", "secrets"]
    assert response.status_code == 500 and settings_module.SAVE_FAILED in html
    assert (sd(app) / "setup.json").read_bytes() == before
    assert store.get(f"indexer:{slug}:apiKey") == key
    assert [c[0] for c in calls] == [new, key]
    assert starts(runner) == []


def test_secret_store_update_is_one_write(tmp_path):
    store = SecretStore(tmp_path / "secrets.bin")
    store.set("a", "one-value")
    store.set("b", "two-value")
    store.update({"a": None, "b": "three-value", "c": "four-value"})
    assert sorted(store.names()) == ["b", "c"]
    assert store.get("b") == "three-value"


# ------------------------------------------------------------------ undo after a test (Task 12)


def seeded_usenet(app):
    store = app.extensions["orama"].store
    password = secrets.token_urlsafe(20)
    setup, slug = forms.add_usenet_server(live(app), {
        "host": "news.example", "port": "563", "username": "me", "password": password,
        "ssl": "on",
    }, store)
    setup.save(sd(app) / "setup.json")
    store.set(f"test-ok:usenet:{slug}", "1")
    return store, slug, password


def replay_usenet(signed, password=""):
    token = csrf_from(signed.get("/settings/indexers"))
    return signed.post("/settings/usenet", data={
        "csrf_token": token, "host": "news.example", "port": "563", "connections": "20",
        "username": "me", "password": password, "ssl": "on",
    })


def test_failed_replay_of_a_saved_usenet_server_does_not_claim_a_failed_restore(
        signed, app, runner, monkeypatch):
    """A refreshed add form changes nothing in SABnzbd: no second test, no false sentence."""
    store, slug, password = seeded_usenet(app)
    calls = []

    def down(setup, store_, http, slug_):
        calls.append(slug_)
        store_.delete(f"test-ok:usenet:{slug_}")
        return "SABnzbd does not answer."

    monkeypatch.setattr(sabnzbd, "test_usenet_server", down)
    before = (sd(app) / "setup.json").read_bytes()
    response = replay_usenet(signed)
    html = page(response)
    assert response.status_code == 400
    assert "SABnzbd does not answer. Nothing was saved." in html
    assert "could not put the old settings back" not in html
    assert calls == [slug]  # tested once, not again on the old values
    assert (sd(app) / "setup.json").read_bytes() == before
    assert store.get(f"usenet:{slug}:password") == password
    assert starts(runner) == []


def test_lock_failure_after_a_pass_still_undoes_the_test(signed, app, runner, monkeypatch):
    monkeypatch.setattr(catalogue, "load", lambda *args: [DEFINITION])
    store, slug, key = seeded_indexer(app)
    wrong = secrets.token_urlsafe(20)
    calls = []

    def passing_then_recorded(setup, store_, http, slug_):
        item = next(i for i in setup.indexers if i.slug == slug_)
        calls.append((store_.get(f"indexer:{slug_}:apiKey"), item.fields.get("baseUrl")))
        store_.set(f"test-ok:indexer:{slug_}", "1")

    monkeypatch.setattr(prowlarr, "test_indexer", passing_then_recorded)

    def broken(_dir):
        raise OSError("disk")

    monkeypatch.setattr(settings_module.state, "setup_lock", broken)
    before = (sd(app) / "setup.json").read_bytes()
    response = edit_with(signed, slug, wrong)
    html = page(response)
    assert response.status_code == 500 and "Orama could not save the settings." in html
    assert wrong not in html
    assert calls == [(wrong, "https://indexer.example"), (key, "https://indexer.example")]
    assert (sd(app) / "setup.json").read_bytes() == before
    assert store.get(f"indexer:{slug}:apiKey") == key
    assert starts(runner) == []


def test_double_fault_says_what_is_true(signed, app, runner, monkeypatch):
    monkeypatch.setattr(catalogue, "load", lambda *args: [DEFINITION])
    monkeypatch.setattr(prowlarr, "test_indexer", passing("indexer"))
    _store, slug, _key = seeded_indexer(app)

    def no_commit(self):
        raise OSError("disk")

    monkeypatch.setattr(settings_module._Candidate, "commit", no_commit)
    monkeypatch.setattr(settings_module, "_write_back", lambda *a: (_ for _ in ()).throw(OSError("disk")))
    response = edit_with(signed, slug, secrets.token_urlsafe(20))
    html = page(response)
    assert response.status_code == 500
    assert settings_module.STUCK in html
    assert "Nothing was saved" not in html
    assert starts(runner) == []


def test_candidate_secrets_are_not_logged(signed, app, caplog):
    """The real test steps and the page must never log what the owner typed."""
    import logging

    from orama.wiring.http import FakeHttp, Response
    http = FakeHttp()  # Prowlarr is up, and every other path answers "not found"
    http.route("GET", "/api/v1/system/status", lambda _: Response(200, {}, b"{}"))
    app.extensions["orama"].http = http
    caplog.set_level(logging.DEBUG)
    store = app.extensions["orama"].store
    store.set("apikey:prowlarr", "k" * 32)
    secret = secrets.token_urlsafe(24)
    setup, slug = forms.add_indexer(live(app), DEFINITION, {
        "name": "Mine", "field:apiKey": secret, "field:baseUrl": "https://indexer.example",
    }, store)
    prowlarr.test_indexer(setup, store, app.extensions["orama"].http, slug)
    assert secret not in caplog.text
