import json
import secrets
from unittest.mock import Mock

import pytest

from orama.secrets import PLEX_TOKEN_NAME
from orama.wiring.http import FakeHttp, Response

from .conftest import csrf_from, signin


@pytest.fixture
def plex_client(client, app, password):
    assert signin(client, password).status_code == 303
    st = app.extensions["orama"]
    http = FakeHttp()
    code = "ABCD"
    token = secrets.token_urlsafe(20)
    http.route("POST", "/api/v2/pins", lambda _: Response(
        201, {}, json.dumps({"id": 51, "code": code, "expiresIn": 900}).encode(),
    ))
    http.route("GET", "/api/v2/pins/51", lambda _: Response(
        200, {}, json.dumps({"authToken": token}).encode(),
    ))
    st.http = http
    st.jobs = Mock()
    st.jobs.current.return_value = None
    csrf = csrf_from(client.get("/setup/finish"))
    return client, st, csrf, code, token


def test_plex_sign_in_shows_code_never_token(plex_client):
    client, st, csrf, code, token = plex_client
    response = client.post("/plex/sign-in", data={"csrf_token": csrf}, follow_redirects=True)
    assert response.status_code == 200
    assert code.encode() in response.data
    assert token.encode() not in response.data
    assert st.store.get(PLEX_TOKEN_NAME) is None
    assert [call.method for call in st.http.calls] == ["POST"]


def test_plex_done_starts_apply(plex_client):
    client, st, csrf, _, token = plex_client
    assert client.post("/plex/sign-in", data={"csrf_token": csrf}).status_code == 303
    assert client.get("/plex/sign-in/status").status_code == 200
    assert client.head("/plex/sign-in/status").status_code == 200
    assert [call.method for call in st.http.calls] == ["POST"]
    assert client.post("/plex/sign-in/status").status_code == 403
    assert st.store.get(PLEX_TOKEN_NAME) is None
    response = client.post("/plex/sign-in/status", data={"csrf_token": csrf}, follow_redirects=True)
    assert response.status_code == 200
    assert token.encode() not in response.data
    assert st.store.get(PLEX_TOKEN_NAME) == token
    st.jobs.start.assert_called_once_with(st.state_dir / "setup.json")
    client.post("/plex/sign-in/status", data={"csrf_token": csrf})
    st.jobs.start.assert_called_once()
