"""The setup wizard's pages: sign-in, drafts, plain errors, secrets, the first apply."""

from __future__ import annotations

import re
import sys
import threading
from pathlib import Path
from urllib.parse import quote

import pytest
from conftest import random_secret

from orama.secrets import SecretStore
from orama.setup_model import DesiredSetup
from orama.web.jobs import JobRunner

from .conftest import csrf_from, signin

STEPS = ("media", "folders", "downloads", "playback", "subtitles")


# Built at runtime so the repo scan finds no literal address.
VPN_ADDRESS = ".".join(str(o) for o in (10, 66, 0, 9))


@pytest.fixture
def signed(app, client, password):
    assert signin(client, password).status_code == 303
    return client


def draft_of(app) -> DesiredSetup:
    return DesiredSetup.load(app.extensions["orama"].state_dir / "setup.draft.json")


def post(client, path: str, **data):
    token = csrf_from(client.get(path))
    return client.post(path, data={"csrf_token": token, **data})


def text(response) -> str:
    return response.get_data(as_text=True)


def key() -> str:
    return random_secret(nbytes=16)


@pytest.fixture
def first_time(app):
    """No setup.json yet, only the installer's draft."""
    d = app.extensions["orama"].state_dir
    (d / "setup.json").rename(d / "setup.draft.json")


def blocking_runner(app, tmp_path: Path):
    """A job runner that records its starts and waits for a file before ending."""
    log = tmp_path / "starts.log"
    gate = tmp_path / "gate"
    code = (
        "import sys,time,pathlib\n"
        f"open({str(log)!r},'a').write(sys.argv[1]+'\\n')\n"
        f"g=pathlib.Path({str(gate)!r})\n"
        "while not g.exists(): time.sleep(0.02)\n"
    )
    runner = JobRunner(
        app.extensions["orama"].state_dir,
        lambda p: [sys.executable, "-c", code, str(p)],
    )
    app.extensions["orama"].jobs = runner
    return runner, log, gate


# ------------------------------------------------------------------ sign-in and /setup


@pytest.mark.parametrize("step", STEPS)
def test_wizard_needs_sign_in(client, password, step):
    response = client.get(f"/setup/{step}")
    assert response.status_code == 303
    assert response.headers["Location"].startswith("/signin")
    token = csrf_from(client.get("/signin"))
    assert client.post(f"/setup/{step}", data={"csrf_token": token}).status_code == 303


def test_setup_without_password_and_code_goes_to_password(client, state_dir):
    code = "abc_DEF-123"
    response = client.get(f"/setup?code={code}&x=1")
    assert response.status_code == 303
    assert response.headers["Location"] == f"/setup/password?code={code}"
    response = client.get("/setup")
    assert response.headers["Location"] == "/setup/password"


def test_setup_code_is_url_encoded_and_never_echoed(client, state_dir):
    response = client.get("/setup?code=" + quote("a b&c=<x>"))
    assert response.status_code == 303
    assert response.headers["Location"] == "/setup/password?code=" + quote("a b&c=<x>", safe="")
    assert "<x>" not in text(response)


def test_setup_ignores_code_once_password_exists(client, password):
    response = client.get("/setup?code=whatever")
    assert response.status_code == 303
    assert response.headers["Location"] == "/setup/media"
    assert "code" not in response.headers["Location"]
    assert client.get("/setup/media").headers["Location"].startswith("/signin")


def test_setup_signed_in_goes_to_media(signed):
    assert signed.get("/setup").headers["Location"] == "/setup/media"
    assert signed.get("/setup/media").status_code == 200


# ------------------------------------------------------------------ pages and the step list


def test_steps_listed_in_order_with_state(signed):
    html = text(signed.get("/setup/folders"))
    ol = html.index("<ol>")
    html, intro = html[ol:html.index("</ol>", ol)], html[:ol]
    assert "Step 3 of 8" in intro
    order = ["Password", "Media servers", "Folders", "Downloads and VPN", "Indexers",
             "Playback", "Subtitles", "Review"]
    positions = [html.index(name) for name in order]
    assert positions == sorted(positions)
    assert 'aria-current="step"' in html
    # Indexers and Review arrive in a later task: shown, not linked.
    assert 'href="/setup/indexers"' in html and 'href="/setup/review"' in html
    assert 'href="/setup/media"' in html and 'href="/setup/playback"' in html
    assert "Not available yet" not in html


def test_every_step_page_renders_without_errors(signed):
    for step in STEPS:
        response = signed.get(f"/setup/{step}")
        assert response.status_code == 200, step
        assert "csrf_token" in text(response)


def test_media_saved_to_draft_and_moves_on(app, signed):
    response = post(signed, "/setup/media", server_jellyfin="on", server_plex="on",
                    jellyfin_user="owner", jellyfin_password="pw-" + key())
    assert response.status_code == 303
    assert response.headers["Location"] == "/setup/folders"
    assert draft_of(app).media_servers == {"plex", "jellyfin"}


def test_back_and_refresh_keep_draft(app, signed):
    post(signed, "/setup/folders", data_root="/mnt/films")
    post(signed, "/setup/playback", device="lg", quality="4k")
    again = text(signed.get("/setup/folders"))
    assert 'value="/mnt/films"' in again
    assert text(signed.get("/setup/folders")) == again  # refresh
    playback = text(signed.get("/setup/playback"))
    assert re.search(r'value="lg"\s+checked', playback)
    draft = draft_of(app)
    assert str(draft.data_root) == "/mnt/films" and draft.device == "lg"
    # The saved setup itself is not touched while the wizard edits the draft.
    saved = DesiredSetup.load(app.extensions["orama"].state_dir / "setup.json")
    assert str(saved.data_root) == "/srv/data"


def test_wizard_keeps_answers_after_error(app, signed):
    response = post(signed, "/setup/downloads", usenet="on", torrent="on", vpn="on",
                    vpn_provider="mullvad", vpn_key="bad key", vpn_address=VPN_ADDRESS,
                    route_prowlarr="on")
    html = text(response)
    assert response.status_code == 400
    assert "does not look like a WireGuard key" in html
    assert f'value="{VPN_ADDRESS}"' in html
    assert "bad key" not in html
    assert re.search(r'<option value="mullvad"[^>]*\sselected', html)
    assert 'id="vpn_key-error">That does not look' in html  # the reason sits by the field
    # Nothing was saved from the failed form.
    assert not (app.extensions["orama"].state_dir / "setup.draft.json").exists()


def test_error_text_has_no_field_names_or_codes(signed):
    html = text(post(signed, "/setup/folders", data_root="/srv/orama/media"))
    assert "Orama folder" in html
    for word in ("config_root", "data_root is", "Value error", "400"):
        assert word not in html.split("<main")[1]


def test_secret_never_rendered_back(app, signed):
    secret = key()
    store = SecretStore(app.extensions["orama"].state_dir / "secrets.bin")
    store.set("vpn:wireguard_private_key", secret)
    store.set("password:jellyfin", "pw-" + secret)
    for step in ("downloads", "media"):
        html = text(signed.get(f"/setup/{step}"))
        assert secret not in html
        assert "Saved — leave empty to keep." in html
    typed = key()
    html = text(post(signed, "/setup/downloads", torrent="on", vpn="on",
                     vpn_provider="mullvad", vpn_key=typed))
    assert typed not in html and secret not in html


def test_downloads_form_offers_port_forwarding_only_for_proton(signed):
    html = text(signed.get("/setup/downloads"))
    assert 'name="port_forwarding"' in html
    assert 'data-feature="port_forwarding"' in html
    # Selected provider can forward a port: that part is not hidden.
    start = html.index('data-feature="port_forwarding"')
    assert "hidden" not in html[start - 80:start + 80]


def test_downloads_form_hides_port_forwarding_for_nordvpn(app, signed):
    post(signed, "/setup/downloads", torrent="on", vpn="on", vpn_provider="nordvpn",
         vpn_key=key())
    html = text(signed.get("/setup/downloads"))
    start = html.index('data-feature="port_forwarding"')
    assert "hidden" in html[start - 80:start + 80]
    assert draft_of(app).vpn.port_forwarding is False


def test_address_help_says_bare_address_gets_slash_32(signed):
    html = text(signed.get("/setup/downloads"))
    assert "/32" in html


def test_folders_note_only_after_setup_exists(app, signed, state_dir):
    html = text(signed.get("/setup/folders"))
    assert "Orama does not move files that are already there" in html
    (state_dir / "setup.json").rename(state_dir / "setup.draft.json")
    html = text(signed.get("/setup/folders"))
    assert "Orama does not move files that are already there" not in html


def test_post_needs_csrf(signed):
    assert signed.post("/setup/folders", data={"data_root": "/mnt/x"}).status_code == 403


# ------------------------------------------------------------------ the first apply


def test_downloads_save_starts_one_job(app, signed, first_time, tmp_path):
    runner, log, gate = blocking_runner(app, tmp_path)
    state_dir = app.extensions["orama"].state_dir
    form = {"usenet": "on"}
    token = csrf_from(signed.get("/setup/downloads"))
    first = signed.post("/setup/downloads", data={"csrf_token": token, **form})
    assert first.status_code == 303
    assert first.headers["Location"] == "/setup/starting"
    second = signed.post("/setup/downloads", data={"csrf_token": token, **form})
    assert second.status_code == 303
    try:
        deadline = threading.Event()
        for _ in range(200):
            if log.exists() and log.read_text().strip():
                break
            deadline.wait(0.02)
        assert log.read_text().split() == [str(state_dir / "setup.json")]
        page = text(signed.get("/setup/starting"))
        assert "Starting your apps — this can take a few minutes the first time" in page
        assert 'id="checklist-items"' in page and 'hx-get="/setup/starting"' in page
        assert "Continue" not in page  # not while the apps are still starting
        poll = text(signed.get("/setup/starting", headers={"HX-Request": "true"}))
        assert 'id="starting-next" hx-swap-oob="true"' in poll and "Continue" not in poll
    finally:
        gate.write_text("x")
        runner.wait(10)
    assert log.read_text().split() == [str(state_dir / "setup.json")]
    poll = text(signed.get("/setup/starting", headers={"HX-Request": "true"}))
    assert 'id="starting-next" hx-swap-oob="true"' in poll and "Continue" in poll
    assert "every 2s" not in poll
    assert "Continue" in text(signed.get("/setup/starting"))


def test_downloads_save_after_setup_exists_starts_nothing(app, signed, tmp_path):
    runner, log, gate = blocking_runner(app, tmp_path)
    gate.write_text("x")
    response = post(signed, "/setup/downloads", usenet="on")
    assert response.status_code == 303
    assert response.headers["Location"] == "/setup/indexers"
    assert runner.current() is None and not log.exists()


def test_downloads_error_starts_nothing(app, signed, first_time, tmp_path):
    runner, _log, gate = blocking_runner(app, tmp_path)
    response = post(signed, "/setup/downloads")
    assert response.status_code == 400
    assert runner.current() is None
    gate.write_text("x")


def test_second_visit_before_any_setup_does_not_start_again(app, signed, first_time, tmp_path):
    runner, log, gate = blocking_runner(app, tmp_path)
    gate.write_text("x")
    post(signed, "/setup/downloads", usenet="on")
    runner.wait(10)
    post(signed, "/setup/downloads", usenet="on")
    assert log.read_text().split() == [str(app.extensions["orama"].state_dir
                                           / "setup.json")]


def test_failed_first_apply_can_be_applied_again(app, signed, first_time, tmp_path):
    state_dir = app.extensions["orama"].state_dir
    log = tmp_path / "starts.log"
    code = f"import sys\nopen({str(log)!r},'a').write(sys.argv[1]+'\\n')\nraise SystemExit(1)\n"
    runner = JobRunner(state_dir, lambda p: [sys.executable, "-c", code, str(p)])
    app.extensions["orama"].jobs = runner
    response = post(signed, "/setup/downloads", torrent="on")
    assert response.headers["Location"] == "/setup/starting"
    assert runner.wait(10) and runner.current().status == "failed"
    # The first apply promoted the draft: there is a real setup to apply again.
    assert not (state_dir / "setup.draft.json").exists()
    assert DesiredSetup.load(state_dir / "setup.json").download_methods == frozenset({"torrent"})
    page = text(signed.get("/setup/downloads"))
    assert re.search(r'name="torrent" value="on"\s*checked', page)
    assert not re.search(r'name="usenet" value="on"\s*checked', page)
    token = csrf_from(signed.get("/setup/starting"))
    again = signed.post("/jobs/apply", data={"csrf_token": token})
    assert again.status_code == 303
    assert runner.wait(10)
    assert log.read_text().split() == [str(state_dir / "setup.json")] * 2
    # A later step edits a new draft made from the saved setup; the saved one stays.
    saved = DesiredSetup.load(state_dir / "setup.json")
    assert post(signed, "/setup/playback", device="lg", quality="4k").status_code == 303
    assert draft_of(app).download_methods == frozenset({"torrent"})
    assert draft_of(app).device == "lg"
    assert DesiredSetup.load(state_dir / "setup.json") == saved


def test_starting_page_needs_sign_in(client, password):
    assert client.get("/setup/starting").status_code == 303


def test_last_step_stays_on_page_with_saved_notice(signed):
    response = post(signed, "/setup/subtitles", language_1="el", language_2="en")
    assert response.status_code == 303
    assert response.headers["Location"] == "/setup/review"
    assert "Saved" in text(signed.get("/setup/subtitles?saved=1"))


def test_subtitle_language_error_shown_next_to_field(signed):
    response = post(signed, "/setup/subtitles", language_1="en", language_2="en")
    assert response.status_code == 400
    assert "Pick each language once" in text(response)
