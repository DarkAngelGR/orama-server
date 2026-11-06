import secrets

import pytest
from conftest import make_setup

from orama import state
from orama.web import forms
from orama.web.catalogue import IndexerDefinition, IndexerField
from orama.wiring.prowlarr import sent_marker_name
from orama.wiring.sabnzbd import sent_marker_name as usenet_marker

from .conftest import csrf_from, signin


class MemoryStore(dict):
    def set(self, name, value):
        self[name] = value

    def delete(self, name):
        self.pop(name, None)


DEFINITION = IndexerDefinition(
    "Newznab", "Generic Newznab", "usenet", "private",
    (IndexerField("apiKey", "API key", "secret"), IndexerField("baseUrl", "Address", "text")),
)


def test_add_indexer_saves_secret_in_store_not_setup():
    store = MemoryStore()
    secret = secrets.token_urlsafe(20)
    setup, slug = forms.add_indexer(make_setup(), DEFINITION, {
        "name": "My indexer", "field:apiKey": secret, "field:baseUrl": "https://indexer.example",
        "field:unknown": "ignore",
    }, store)
    item, = setup.indexers
    assert item.fields == {"baseUrl": "https://indexer.example"}
    assert item.secret_fields == ("apiKey",)
    assert secret not in setup.model_dump_json()
    assert store.get(f"indexer:{slug}:apiKey") == secret


def test_slug_unique():
    store = MemoryStore()
    setup, first = forms.add_indexer(make_setup(), DEFINITION, {"name": "My Indexer"}, store)
    setup, second = forms.add_indexer(setup, DEFINITION, {"name": "My-Indexer"}, store)
    assert (first, second) == ("my-indexer", "my-indexer-2")


@pytest.mark.parametrize("sent", [False, True])
def test_remove_sets_pending_entry_only_for_orama_items(sent):
    store = MemoryStore()
    setup, slug = forms.add_indexer(make_setup(), DEFINITION, {
        "name": "My indexer", "field:apiKey": secrets.token_urlsafe(20),
    }, store)
    store.set(f"test-ok:indexer:{slug}", "1")
    if sent:
        store.set(sent_marker_name(f"indexer:{slug}"), "recorded")
    setup = forms.remove_indexer(setup, slug, store)
    assert not setup.indexers
    assert f"indexer:{slug}:apiKey" not in store
    assert f"test-ok:indexer:{slug}" not in store
    assert store.get(f"remove:indexer:{slug}") == ("My indexer" if sent else None)


def test_usenet_validation_preserves_store():
    store = MemoryStore()
    with pytest.raises(forms.Invalid):
        forms.add_usenet_server(make_setup(), {
            "host": "news.example", "port": "99999", "password": secrets.token_urlsafe(20),
        }, store)
    assert not store


def test_usenet_password_is_only_in_store_and_removed_with_server():
    store = MemoryStore()
    password = secrets.token_urlsafe(20)
    setup, slug = forms.add_usenet_server(make_setup(), {
        "host": "news.example", "port": "563", "connections": "12", "ssl": "on",
        "username": "reader", "password": password,
    }, store)
    assert password not in setup.model_dump_json()
    assert store[f"usenet:{slug}:password"] == password
    server, = setup.usenet_servers
    assert (server.port, server.connections, server.ssl) == (563, 12, True)
    store.set(usenet_marker(slug), "recorded")
    store.set(f"test-ok:usenet:{slug}", "1")
    setup = forms.remove_usenet_server(setup, slug, store)
    assert not setup.usenet_servers
    assert store.get(f"remove:usenet:{slug}") == slug
    assert f"usenet:{slug}:password" not in store
    assert f"test-ok:usenet:{slug}" not in store


@pytest.fixture
def signed(client, password):
    assert signin(client, password).status_code == 303
    return client


def test_waiting_while_prowlarr_starts(signed):
    page = signed.get("/setup/indexers")
    assert page.status_code == 200
    assert b"Prowlarr is still starting" in page.data
    assert b"every 5s" in page.data


def test_failed_test_keeps_entry_and_shows_reason(signed, app, monkeypatch):
    from orama.web import catalogue
    from orama.wiring import prowlarr

    monkeypatch.setattr(catalogue, "load", lambda *args: [DEFINITION])
    monkeypatch.setattr(prowlarr, "test_indexer", lambda *args: "Check the indexer address.")
    page = signed.get("/setup/indexers?definition=Newznab")
    key = secrets.token_urlsafe(20)
    response = signed.post("/setup/indexers", data={
        "csrf_token": csrf_from(page), "definition": "Newznab", "name": "Try me",
        "field:apiKey": key, "field:baseUrl": "https://indexer.example",
    })
    assert response.status_code == 200
    assert b"Check the indexer address." in response.data
    assert b"Try me" in response.data
    assert key.encode() not in response.data
    assert app.extensions["orama"].store.get("indexer:try-me:apiKey") == key

    edit = signed.get("/setup/indexers?edit=try-me")
    assert b"Edit Try me" in edit.data and b"leave empty to keep" in edit.data
    assert key.encode() not in edit.data
    response = signed.post("/setup/indexers", data={
        "csrf_token": csrf_from(edit), "edit": "try-me",
        "field:apiKey": "", "field:baseUrl": "https://corrected.example",
    })
    assert response.status_code == 200
    assert app.extensions["orama"].store.get("indexer:try-me:apiKey") == key
    from orama.setup_model import DesiredSetup

    draft = DesiredSetup.load(app.extensions["orama"].state_dir / "setup.draft.json")
    item, = draft.indexers
    assert item.slug == "try-me" and item.fields["baseUrl"] == "https://corrected.example"


def test_tests_wait_for_apply_lock(signed, app, state_dir, monkeypatch):
    from orama.wiring import prowlarr

    _, slug = _draft(app, state_dir)
    tested = []
    monkeypatch.setattr(prowlarr, "test_indexer", lambda *args: tested.append(args) or None)
    page = signed.get("/setup/indexers")
    assert slug.encode() in page.data  # a real entry, so only the lock can refuse it
    with state.locked(state.apply_lock(state_dir)):
        response = signed.post(f"/setup/indexers/{slug}/test",
                               data={"csrf_token": csrf_from(page)})
    assert response.status_code == 409
    assert b"Orama is busy applying the setup." in response.data
    assert not tested
    assert signed.post(f"/setup/indexers/{slug}/test",
                       data={"csrf_token": csrf_from(page)}).status_code == 200
    assert len(tested) == 1


def test_catalogue_is_read_before_setup_lock(signed, app, state_dir, monkeypatch):
    from orama.web import catalogue
    from orama.wiring import prowlarr

    held = []

    def load(*args):
        try:
            with state.locked(state.setup_lock(state_dir), blocking=False):
                held.append(False)
        except state.Busy:
            held.append(True)
        return [DEFINITION]

    monkeypatch.setattr(catalogue, "load", load)
    monkeypatch.setattr(prowlarr, "test_indexer", lambda *args: None)
    page = signed.get("/setup/indexers?definition=Newznab")
    held.clear()
    response = signed.post("/setup/indexers", data={
        "csrf_token": csrf_from(page), "definition": "Newznab", "name": "Locked out",
        "field:apiKey": secrets.token_urlsafe(20),
    })
    assert response.status_code == 200
    assert held and not any(held)


def test_resubmitting_usenet_form_reuses_server(signed, app, state_dir, monkeypatch):
    from orama.setup_model import DesiredSetup
    from orama.wiring import sabnzbd

    checked = []
    monkeypatch.setattr(sabnzbd, "test_usenet_server", lambda *args: checked.append(args[-1]))
    password = secrets.token_urlsafe(20)
    token = csrf_from(signed.get("/setup/indexers"))
    data = {"csrf_token": token, "host": "news.example", "ssl": "on", "password": password}
    for _ in range(2):
        response = signed.post("/setup/usenet", data=data)
        assert response.status_code == 200
        assert password.encode() not in response.data
    server, = DesiredSetup.load(state_dir / "setup.draft.json").usenet_servers
    assert checked == [server.slug, server.slug]
    assert app.extensions["orama"].store.get(f"usenet:{server.slug}:password") == password


def _install(signed):
    review = signed.get("/setup/review")
    return signed.post("/setup/install", data={"csrf_token": csrf_from(review)})


def _draft(app, state_dir, *, usenet=False):
    store = app.extensions["orama"].store
    setup, slug = forms.add_indexer(make_setup(), DEFINITION, {
        "name": "My indexer", "field:apiKey": secrets.token_urlsafe(20),
    }, store)
    if usenet:
        setup, _ = forms.add_usenet_server(setup, {"host": "news.example"}, store)
    setup.save(state_dir / "setup.draft.json")
    return store, slug


def test_install_refused_without_working_indexer(signed, app, state_dir):
    _draft(app, state_dir)
    assert _install(signed).status_code == 400
    assert (state_dir / "setup.draft.json").exists()


def test_install_refused_without_working_usenet_server_when_usenet(signed, app, state_dir):
    store, slug = _draft(app, state_dir, usenet=True)
    store.set(f"test-ok:indexer:{slug}", "1")
    response = _install(signed)
    assert response.status_code == 400
    assert b"Test at least one Usenet server" in response.data


def test_review_shows_no_secret(signed, app, state_dir):
    store, slug = _draft(app, state_dir)
    secret = store.get(f"indexer:{slug}:apiKey")
    response = signed.get("/setup/review")
    assert response.status_code == 200
    assert b"My indexer" in response.data
    assert b"Credentials saved" in response.data
    assert secret.encode() not in response.data


def test_install_promotes_draft_and_starts_job(signed, app, state_dir):
    from unittest.mock import Mock

    store, slug = _draft(app, state_dir, usenet=True)
    store.set(f"test-ok:indexer:{slug}", "1")
    store.set("test-ok:usenet:news-example", "1")
    jobs = Mock()
    jobs.current.return_value = None
    app.extensions["orama"].jobs = jobs
    from orama.setup_model import DesiredSetup

    draft = DesiredSetup.load(state_dir / "setup.draft.json")
    live = DesiredSetup.load(state_dir / "setup.json")
    response = _install(signed)
    assert response.status_code == 303
    assert response.location == "/setup/finish"
    assert not (state_dir / "setup.draft.json").exists()
    # The owner's answers come from the draft; the machine's own facts stay from the live file.
    machine = {f: getattr(live, f) for f in ("lan_address", "host_name", "extra_host_names", "ports")}
    assert DesiredSetup.load(state_dir / "setup.json") == draft.model_copy(update=machine)
    jobs.start.assert_called_once_with(state_dir / "setup.json")


def test_install_without_draft_applies_saved_setup(signed, app, state_dir):
    from unittest.mock import Mock

    from orama.setup_model import DesiredSetup

    store, slug = _draft(app, state_dir, usenet=True)
    (state_dir / "setup.draft.json").rename(state_dir / "setup.json")
    saved = DesiredSetup.load(state_dir / "setup.json")
    store.set(f"test-ok:indexer:{slug}", "1")
    store.set("test-ok:usenet:news-example", "1")
    jobs = Mock()
    jobs.current.return_value = None
    app.extensions["orama"].jobs = jobs
    assert _install(signed).status_code == 303
    assert not (state_dir / "setup.draft.json").exists()
    assert DesiredSetup.load(state_dir / "setup.json") == saved
    jobs.start.assert_called_once_with(state_dir / "setup.json")


def _finished(app, state_dir, status):
    from unittest.mock import Mock

    from orama.web.jobs import Job

    from .conftest import write_setup

    write_setup(state_dir, media_servers={"plex", "jellyfin"})
    jobs = Mock()
    jobs.current.return_value = Job(status, ["✓ x"], 1.0, 2.0)
    app.extensions["orama"].jobs = jobs


@pytest.mark.parametrize("status", ["failed", "interrupted"])
def test_finish_hides_plex_and_jellyfin_after_failed_apply(signed, app, state_dir, status):
    _finished(app, state_dir, status)
    for headers in ({}, {"HX-Request": "true"}):
        page = signed.get("/setup/finish", headers=headers).get_data(as_text=True)
        assert "Sign in with Plex" not in page
        assert "Jellyfin is ready" not in page


def test_finish_names_the_jellyfin_admin_after_success(signed, app, state_dir):
    from orama.secrets import JELLYFIN_USER_NAME

    _finished(app, state_dir, "done")
    page = signed.get("/setup/finish").get_data(as_text=True)
    assert "Sign in with Plex" in page
    assert "Jellyfin is ready — sign in as orama-admin." in page
    app.extensions["orama"].store.set(JELLYFIN_USER_NAME, "Movie <night>")
    page = signed.get("/setup/finish").get_data(as_text=True)
    assert "Jellyfin is ready — sign in as Movie &lt;night&gt;." in page
    assert "you chosen" not in page and "you chose" not in page


def test_draft_removal_keeps_active_credentials_and_does_not_delete_live_indexer(
    signed, app, state_dir,
):
    from orama.setup_model import DesiredSetup
    from orama.wiring import prowlarr

    store, slug = _draft(app, state_dir)
    path = state_dir / "setup.json"
    active = DesiredSetup.load(state_dir / "setup.draft.json")
    active.save(path)
    store.set(sent_marker_name(f"indexer:{slug}"), "recorded")
    key = store.get(f"indexer:{slug}:apiKey")
    page = signed.get("/setup/indexers")
    response = signed.post(f"/setup/indexers/{slug}/remove", data={"csrf_token": csrf_from(page)})
    assert response.status_code == 200
    assert store.get(f"indexer:{slug}:apiKey") == key
    assert DesiredSetup.load(path).indexers == active.indexers
    assert not DesiredSetup.load(state_dir / "setup.draft.json").indexers
    steps = prowlarr.prowlarr_steps(active, store, app.extensions["orama"].http)
    assert all(step.name != f"prowlarr:remove:{slug}" for step in steps)


# ------------------------------------------------------------------ remove, then add again


def _wired_store(tmp_path, *keys):
    from orama.secrets import SecretStore

    store = SecretStore(tmp_path / "secrets.bin")
    for name, value in keys:
        store.set(name, value)
    return store


GEEK = IndexerDefinition("geek", "Geek", "usenet", "private",
                         (IndexerField("apikey", "API key", "secret"),))


@pytest.mark.parametrize("live", [False, True])
def test_indexer_removed_then_added_again_reuses_its_entry(tmp_path, live):
    from wiring.test_prowlarr import PROWLARR_KEY, FakeProwlarr

    from orama.wiring.prowlarr import prowlarr_steps, test_indexer

    pw = FakeProwlarr()
    store = _wired_store(tmp_path, ("apikey:prowlarr", PROWLARR_KEY))
    key = secrets.token_urlsafe(20)
    setup, slug = forms.add_indexer(make_setup(), GEEK, {"name": "My indexer",
                                                         "field:apikey": key}, store)
    assert test_indexer(setup, store, pw.http, slug) is None
    setup = forms.remove_indexer(setup, slug, store, keep_credentials=live)
    assert store.get(f"remove:indexer:{slug}") == "My indexer"
    form = {"name": "My indexer", "field:apikey": "" if live else key}
    setup, again = forms.add_indexer(setup, GEEK, form, store)
    assert again == slug
    assert store.get(f"remove:indexer:{slug}") is None
    assert store.get(sent_marker_name(f"indexer:{slug}"))  # still recognised as Orama's
    assert store.get(f"indexer:{slug}:apikey") == key
    assert setup.indexers[0].secret_fields == ("apikey",)
    assert test_indexer(setup, store, pw.http, slug) is None
    assert len(pw.indexers) == 1
    assert not any(s.name.startswith("prowlarr:remove:")
                   for s in prowlarr_steps(setup, store, pw.http))
    assert not [n for n in store.names() if n.startswith("remove:")]


def test_indexer_added_again_under_another_spelling_gets_its_own_entry(tmp_path):
    store = _wired_store(tmp_path)
    setup, slug = forms.add_indexer(make_setup(), GEEK, {"name": "My indexer"}, store)
    store.set(sent_marker_name(f"indexer:{slug}"), "recorded")
    setup = forms.remove_indexer(setup, slug, store)
    # Prowlarr knows the old one by its exact name: that one is still removed.
    setup, again = forms.add_indexer(setup, GEEK, {"name": "My Indexer"}, store)
    assert again == f"{slug}-2"
    assert store.get(f"remove:indexer:{slug}") == "My indexer"


def test_usenet_server_removed_then_added_again_reuses_its_entry(tmp_path):
    from wiring.test_sabnzbd import KEY, FakeSab

    from orama.wiring.sabnzbd import sabnzbd_steps, test_usenet_server

    sab = FakeSab()
    store = _wired_store(tmp_path, ("apikey:sabnzbd", KEY))
    password = secrets.token_urlsafe(20)
    form = {"host": "news.example", "ssl": "on", "password": password}
    setup, slug = forms.add_usenet_server(make_setup(), form, store)
    assert test_usenet_server(setup, store, sab.http, slug) is None
    setup = forms.remove_usenet_server(setup, slug, store)
    assert store.get(f"remove:usenet:{slug}") == slug
    setup, again = forms.add_usenet_server(setup, form, store)
    assert again == slug
    assert store.get(f"remove:usenet:{slug}") is None
    assert store.get(usenet_marker(slug))
    assert test_usenet_server(setup, store, sab.http, slug) is None
    assert list(sab.servers) == [slug]
    assert not any(s.name.startswith("sabnzbd:remove:")
                   for s in sabnzbd_steps(setup, store, sab.http))


def test_usenet_resubmitted_with_empty_password_reuses_saved_server():
    store = MemoryStore()
    password = secrets.token_urlsafe(20)
    form = {"host": "news.example", "ssl": "on", "password": password}
    setup, slug = forms.add_usenet_server(make_setup(), form, store)
    again, same = forms.add_usenet_server(setup, {**form, "password": ""}, store)
    assert same == slug and again == setup
    assert store.get(f"usenet:{slug}:password") == password


@pytest.mark.parametrize("kind", ["indexer", "usenet"])
def test_live_entry_never_sent_loses_its_credentials_on_removal(kind):
    store = MemoryStore()
    if kind == "indexer":
        setup, slug = forms.add_indexer(make_setup(), DEFINITION, {
            "name": "My indexer", "field:apiKey": secrets.token_urlsafe(20)}, store)
        forms.remove_indexer(setup, slug, store, keep_credentials=True)
        assert f"indexer:{slug}:apiKey" not in store
    else:
        setup, slug = forms.add_usenet_server(make_setup(), {
            "host": "news.example", "password": secrets.token_urlsafe(20)}, store)
        forms.remove_usenet_server(setup, slug, store, keep_credentials=True)
        assert f"usenet:{slug}:password" not in store
    assert not [n for n in store if n.startswith("remove:")]
