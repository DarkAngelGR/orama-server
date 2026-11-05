import secrets

import pytest
from conftest import make_setup

from orama.web import forms
from orama.web.catalogue import IndexerDefinition, IndexerField
from orama.wiring.prowlarr import sent_marker_name
from orama.wiring.sabnzbd import sent_marker_name as usenet_marker


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
