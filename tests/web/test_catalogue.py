import json
import secrets
from unittest.mock import Mock

import pytest

from orama.web import catalogue
from orama.wiring.http import FakeHttp, Response


@pytest.fixture
def source():
    store = Mock()
    key = secrets.token_urlsafe(16)
    store.get.return_value = key
    http = FakeHttp()
    fields = [
        {"name": "token", "type": "textbox", "privacy": "apiKey", "value": key},
        {"name": "password", "type": "password", "value": key},
        {"name": "advanced", "type": "textbox", "advanced": True},
        {"name": "hidden", "type": "hidden"},
        {"name": "advancedCookie", "type": "password", "hidden": True},
        {"name": "explanation", "type": "future-kind"},
    ]
    schema = [
        {"definitionName": "first", "name": "First", "protocol": "usenet", "fields": fields},
        {"definitionName": "second", "name": "Second", "protocol": "torrent", "fields": []},
        {"definitionName": "other", "name": "Other", "protocol": "other", "fields": []},
    ]
    http.route("GET", "/api/v1/indexer/schema", lambda _: Response(200, {}, json.dumps(schema).encode()))
    return store, http


def test_catalogue_filtered_by_download_method(source):
    store, http = source
    assert [item.name for item in catalogue.load(store, http, frozenset({"usenet"}))] == ["First"]
    assert [item.name for item in catalogue.load(store, http, frozenset({"torrent"}))] == ["Second"]
    assert len(http.calls) == 1
    assert http.calls[0].headers == {"X-Api-Key": store.get.return_value}
    assert http.calls[0].params is None


def test_secret_fields_detected(source):
    store, http = source
    first, = catalogue.load(store, http, frozenset({"usenet"}))
    assert [field.name for field in first.fields if field.kind == "secret"] == ["token", "password"]
    assert not any(store.get.return_value in str(field) for field in first.fields)


def test_advanced_fields_hidden(source):
    store, http = source
    first, = catalogue.load(store, http, frozenset({"usenet"}))
    assert [(field.name, field.kind) for field in first.fields] == [
        ("token", "secret"), ("password", "secret"), ("explanation", "info"),
    ]


def test_unreachable_schema_can_be_retried(source):
    store, http = source
    http.unreachable = True
    with pytest.raises(catalogue.Unavailable):
        catalogue.load(store, http, frozenset({"usenet"}))
    http.unreachable = False
    assert catalogue.load(store, http, frozenset({"usenet"}))


def test_catalogue_expires_and_refreshes_when_key_changes(source, monkeypatch):
    store, http = source
    now = 0
    monkeypatch.setattr(catalogue.time, "monotonic", lambda: now)
    methods = frozenset({"usenet"})
    catalogue.load(store, http, methods)
    now = 3599
    catalogue.load(store, http, methods)
    assert len(http.calls) == 1
    now = 3600
    catalogue.load(store, http, methods)
    assert len(http.calls) == 2
    store.get.return_value = secrets.token_urlsafe(16)
    catalogue.load(store, http, methods)
    assert len(http.calls) == 3
