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


def test_cardigann_unmarked_text_fields_are_kept_private(source):
    store, http = source
    schema = [{
        "definitionName": "tracker", "name": "Tracker", "protocol": "torrent",
        "implementation": "Cardigann", "fields": [
            {"name": "apikey", "type": "textbox"},
            {"name": "alt2fatoken", "type": "textbox"},
            {"name": "custom", "type": "textbox"},
        ],
    }]
    http.route("GET", "/api/v1/indexer/schema", lambda _: Response(200, {}, json.dumps(schema).encode()))
    tracker, = catalogue.load(store, http, frozenset({"torrent"}))
    assert all(field.kind == "secret" for field in tracker.fields)


@pytest.mark.parametrize("name", ["apiKey", "api_key", "baseSettings.apiKey", "password",
                                  "passkey", "rssPasskey", "cookie", "sessionCookie", "token",
                                  "authToken", "secret", "clientSecret"])
def test_secret_looking_names_are_secret_without_a_privacy_flag(source, name):
    # Prowlarr's schema can drop the privacy flag: the name alone makes the field private.
    store, http = source
    schema = [{
        "definitionName": "plain", "name": "Plain", "protocol": "torrent",
        "implementation": "Avistaz", "fields": [
            {"name": name, "type": "textbox"},
            {"name": "baseUrl", "type": "textbox"},
        ],
    }]
    http.route("GET", "/api/v1/indexer/schema", lambda _: Response(200, {}, json.dumps(schema).encode()))
    plain, = catalogue.load(store, http, frozenset({"torrent"}))
    assert [(field.name, field.kind) for field in plain.fields] == [
        (name, "secret"), ("baseUrl", "text"),
    ]


def _kinds(source, fields):
    store, http = source
    schema = [{
        "definitionName": "plain", "name": "Plain", "protocol": "torrent",
        "implementation": "Avistaz", "fields": fields,
    }]
    http.route("GET", "/api/v1/indexer/schema", lambda _: Response(200, {}, json.dumps(schema).encode()))
    plain, = catalogue.load(store, http, frozenset({"torrent"}))
    return [(field.name, field.kind) for field in plain.fields]


@pytest.mark.parametrize("name, field_type, kind", [
    ("useFreeleechToken", "checkbox", "checkbox"),
    ("tokenMode", "select", "select"),
    ("tokenLimit", "number", "number"),
])
def test_non_text_fields_keep_their_kind_despite_a_secret_looking_name(
        source, name, field_type, kind):
    assert _kinds(source, [{"name": name, "type": field_type}]) == [(name, kind)]


@pytest.mark.parametrize("name", ["apiKey", "passkey", "cookie", "token"])
def test_text_fields_with_secret_looking_names_stay_secret(source, name):
    assert _kinds(source, [{"name": name, "type": "text"}]) == [(name, "secret")]


@pytest.mark.parametrize("field_type", ["checkbox", "select", "number", "textbox"])
def test_privacy_flagged_fields_are_secret_of_any_type(source, field_type):
    fields = [{"name": "plainName", "type": field_type, "privacy": "apiKey"}]
    assert _kinds(source, fields) == [("plainName", "secret")]


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
