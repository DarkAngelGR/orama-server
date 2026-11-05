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


def test_tests_wait_for_apply_lock(signed, state_dir):
    page = signed.get("/setup/indexers")
    with state.locked(state.apply_lock(state_dir)):
        response = signed.post("/setup/indexers/one/test", data={"csrf_token": csrf_from(page)})
    assert response.status_code == 409
    assert b"Orama is busy applying the setup." in response.data


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
    response = _install(signed)
    assert response.status_code == 303
    assert response.location == "/setup/finish"
    assert not (state_dir / "setup.draft.json").exists()
    jobs.start.assert_called_once_with(state_dir / "setup.json")


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
