"""Tests for authenticated Sonarr and Radarr webhook refresh routes."""

from __future__ import annotations

import json
import secrets

import pytest

from orama.requests.client import AppSnapshot, MediaClient
from orama.requests.models import RequestSpec, RequestUpdate
from orama.requests.worker import RequestWorker
from orama.web import create_app

from .conftest import csrf_from, signin, write_setup


def _make_tokens(app) -> tuple[str, str]:
    st = app.extensions["orama"].store
    sonarr_tok = secrets.token_hex(32)
    radarr_tok = secrets.token_hex(32)
    st.set("webhook:sonarr", sonarr_tok)
    st.set("webhook:radarr", radarr_tok)
    return sonarr_tok, radarr_tok


def test_correct_token_accepts_test_event_without_request_mutation(app, client):
    sonarr_tok, radarr_tok = _make_tokens(app)
    store = app.extensions["orama"].requests

    for path, token in (("/hooks/sonarr", sonarr_tok), ("/hooks/radarr", radarr_tok)):
        resp = client.post(
            path,
            data=json.dumps({"eventType": "Test"}),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 200, path
        assert token not in resp.get_data(as_text=True)

    assert store.list_active() == []
    assert store.drain_refreshes() == []


def test_missing_wrong_and_other_app_tokens_rejected(app, client, password):
    sonarr_tok, radarr_tok = _make_tokens(app)
    store = app.extensions["orama"].requests
    body = json.dumps({"eventType": "Grab", "series": {"id": 10, "tvdbId": 81189}})

    # Missing header
    r1 = client.post("/hooks/sonarr", data=body, content_type="application/json")
    assert r1.status_code == 403

    # Wrong scheme or empty bearer
    for header in ("Bearer ", "Basic " + sonarr_tok, sonarr_tok):
        r = client.post(
            "/hooks/sonarr",
            data=body,
            headers={"Authorization": header, "Content-Type": "application/json"},
        )
        assert r.status_code == 403

    # Wrong token
    bad_tok = secrets.token_hex(32)
    r2 = client.post(
        "/hooks/sonarr",
        data=body,
        headers={"Authorization": f"Bearer {bad_tok}", "Content-Type": "application/json"},
    )
    assert r2.status_code == 403
    assert bad_tok not in r2.get_data(as_text=True)
    assert sonarr_tok not in r2.get_data(as_text=True)

    # Other app's token (Radarr token sent to Sonarr and vice versa)
    r3 = client.post(
        "/hooks/sonarr",
        data=body,
        headers={"Authorization": f"Bearer {radarr_tok}", "Content-Type": "application/json"},
    )
    assert r3.status_code == 403

    r4 = client.post(
        "/hooks/radarr",
        data=json.dumps({"eventType": "Grab", "movie": {"id": 5, "tmdbId": 27205}}),
        headers={"Authorization": f"Bearer {sonarr_tok}", "Content-Type": "application/json"},
    )
    assert r4.status_code == 403

    # Browser session + CSRF token without webhook Bearer token is still rejected on /hooks/*
    signin(client, password)
    csrf = csrf_from(client.get("/"))
    r5 = client.post(
        "/hooks/sonarr",
        data=body,
        headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
    )
    assert r5.status_code == 403
    assert store.drain_refreshes() == []


def test_oversized_and_non_json_payloads_rejected(app, client):
    sonarr_tok, _ = _make_tokens(app)
    headers = {"Authorization": f"Bearer {sonarr_tok}", "Content-Type": "application/json"}

    # Oversized (> 64 KiB)
    huge = json.dumps({"eventType": "Test", "padding": "a" * (65 * 1024)})
    r_big = client.post("/hooks/sonarr", data=huge, headers=headers)
    assert r_big.status_code == 413

    # Non-JSON / malformed JSON / non-object JSON / unknown eventType
    for payload in (
        b"",
        b"{not json",
        b"[]",
        b'"string"',
        json.dumps({"eventType": "UnknownEvent"}).encode(),
        json.dumps({"series": {"id": 1}}).encode(),
        json.dumps({"eventType": "Grab", "series": {"id": "not-an-int"}}).encode(),
        json.dumps({"eventType": "Grab", "series": {"id": -1}}).encode(),
    ):
        resp = client.post("/hooks/sonarr", data=payload, headers=headers)
        assert resp.status_code == 400


def test_unknown_media_ignored_without_creating_request(app, client):
    sonarr_tok, radarr_tok = _make_tokens(app)
    store = app.extensions["orama"].requests

    r1 = client.post(
        "/hooks/sonarr",
        data=json.dumps({"eventType": "Grab", "series": {"id": 999, "tvdbId": 81189}}),
        headers={"Authorization": f"Bearer {sonarr_tok}", "Content-Type": "application/json"},
    )
    assert r1.status_code == 200

    r2 = client.post(
        "/hooks/radarr",
        data=json.dumps({"eventType": "Download", "movie": {"id": 888, "tmdbId": 27205}}),
        headers={"Authorization": f"Bearer {radarr_tok}", "Content-Type": "application/json"},
    )
    assert r2.status_code == 200

    # No requests were created for unknown media
    assert store.list_active() == []


def test_duplicate_delivery_coalesced_and_wakes_worker(state_dir, clock):
    woken = []

    class SpyWorker:
        def wake(self):
            woken.append(1)

    app = create_app(state_dir, clock=clock, request_worker=SpyWorker())
    client = app.test_client()
    sonarr_tok, _ = _make_tokens(app)
    store = app.extensions["orama"].requests

    rec, _ = store.create(RequestSpec(kind="episode", source_id=81189, season=1, episode=1), clock())
    store.update(
        rec.id,
        rec.revision,
        RequestUpdate(stage="searching", remote_id=42, next_check_at=clock() + 300.0),
    )

    body = json.dumps({"eventType": "Grab", "series": {"id": 42, "tvdbId": 81189}})
    headers = {"Authorization": f"Bearer {sonarr_tok}", "Content-Type": "application/json"}

    for _ in range(3):
        resp = client.post("/hooks/sonarr", data=body, headers=headers)
        assert resp.status_code == 200

    assert len(woken) == 3
    refreshes = store.drain_refreshes()
    assert refreshes == [("sonarr", 42)]
    updated = store.get(rec.id)
    assert updated is not None
    assert updated.next_check_at <= clock()


def test_stale_webhook_event_cannot_regress_ready_state(state_dir, clock):
    app = create_app(state_dir, clock=clock)
    client = app.test_client()
    _, radarr_tok = _make_tokens(app)
    store = app.extensions["orama"].requests

    rec, _ = store.create(RequestSpec(kind="movie", source_id=27205), clock())
    store.update(
        rec.id,
        rec.revision,
        RequestUpdate(stage="ready", remote_id=7, next_check_at=clock() + 300.0),
    )

    # A stale "Grab" webhook arrives after the movie is already Ready
    resp = client.post(
        "/hooks/radarr",
        data=json.dumps({"eventType": "Grab", "movie": {"id": 7, "tmdbId": 27205}}),
        headers={"Authorization": f"Bearer {radarr_tok}", "Content-Type": "application/json"},
    )
    assert resp.status_code == 200

    # Even before worker tick, the webhook itself never mutates stage directly
    assert store.get(rec.id).stage == "ready"

    # When the worker checks the authoritative app API (which still has the imported file),
    # the request stays Ready.
    class AuthoritativeClient(MediaClient):
        def __init__(self):
            pass

        def snapshot(self, app_name, remote_ids):
            return AppSnapshot(
                complete=True,
                items={7: {"id": 7, "tmdbId": 27205, "hasFile": True}},
                queue=[],
                history=[],
                files=[{"id": 1, "movieId": 7, "size": 1000}],
            )

    worker = RequestWorker(store=store, client_factory=AuthoritativeClient, clock=clock)
    worker.tick(clock())
    assert store.get(rec.id).stage == "ready"


@pytest.mark.parametrize("method", ["get", "head", "options", "put", "patch", "delete"])
def test_non_post_methods_cannot_mutate(app, client, method):
    sonarr_tok, _ = _make_tokens(app)
    store = app.extensions["orama"].requests
    resp = getattr(client, method)(
        "/hooks/sonarr",
        headers={"Authorization": f"Bearer {sonarr_tok}"},
    )
    assert resp.status_code in (403, 405)
    assert store.drain_refreshes() == []


def test_ordinary_browser_routes_still_require_csrf(app, client, password):
    sonarr_tok, _ = _make_tokens(app)
    signin(client, password)
    # Sending a valid webhook Bearer token to a browser route does not bypass CSRF
    resp = client.post(
        "/signout",
        headers={"Authorization": f"Bearer {sonarr_tok}"},
    )
    assert resp.status_code == 403


def test_host_exception_only_for_exact_webhook_routes(app, client):
    sonarr_tok, radarr_tok = _make_tokens(app)
    write_setup(app.extensions["orama"].state_dir)

    # Exact webhook POST routes accept internal Docker hostname orama-server:8765
    for path, token in (("/hooks/sonarr", sonarr_tok), ("/hooks/radarr", radarr_tok)):
        ok = client.post(
            path,
            data=json.dumps({"eventType": "Test"}),
            headers={
                "Host": "orama-server:8765",
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        assert ok.status_code == 200, path

    # Other routes (including /hooks/other, /healthz, /signin, and GET /hooks/sonarr)
    # reject Host: orama-server:8765 with 400
    for method, path in (
        ("get", "/healthz"),
        ("get", "/signin"),
        ("post", "/signin"),
        ("get", "/hooks/sonarr"),
        ("post", "/hooks/bazarr"),
        ("post", "/hooks/sonarr/extra"),
    ):
        rejected = getattr(client, method)(
            path,
            headers={
                "Host": "orama-server:8765",
                "Authorization": f"Bearer {sonarr_tok}",
            },
        )
        assert rejected.status_code == 400, (method, path)
