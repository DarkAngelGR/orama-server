"""Orama refuses to be used through its own Docker networks (Claude Security, 2026-11-14).

The sign-in cookie reaches every app port on the host. A session is bound to the client
address, but a request from the server itself (or over IPv6) arrives through Docker's proxy
from the orama-net gateway, the same address an app container gets when it calls Orama's
published port. Such a session could be replayed by a compromised app, so Orama is not used
from those addresses at all, unless the owner creates ``server-signin.allowed`` in the state
folder (root or the Orama user only; the end-to-end proof uses it).
"""

from __future__ import annotations

import ipaddress

import pytest

from orama.web import create_app, security
from web.conftest import signin

ORAMA_NET = [ipaddress.ip_network("172.18.0.0/16")]
GATEWAY = "172.18.0.1"
APP_CONTAINER = "172.18.0.7"
LAN_PHONE = "192.168.1.50"


@pytest.fixture
def guarded(state_dir, clock):
    app = create_app(state_dir, clock=clock, internal_networks=ORAMA_NET)
    app.config["TESTING"] = True
    return app


def get(app, path, address):
    return app.test_client().get(path, environ_base={"REMOTE_ADDR": address},
                                 headers={"Host": "localhost"})


@pytest.mark.parametrize("address", [GATEWAY, APP_CONTAINER])
@pytest.mark.parametrize("path", ["/", "/signin", "/setup/password", "/apps", "/settings"])
def test_pages_are_refused_through_dockers_own_network(guarded, address, path):
    response = get(guarded, path, address)
    assert response.status_code == 403
    assert "another device" in response.get_data(as_text=True)
    assert "Set-Cookie" not in response.headers  # no session is ever started there


def test_a_sign_in_post_from_the_gateway_is_refused_before_the_password_is_checked(
        guarded, password, monkeypatch):
    from orama.web import auth

    called = []
    monkeypatch.setattr(auth.PasswordFile, "verify", lambda self, pw: called.append(1))
    client = guarded.test_client()
    response = client.post("/signin", data={"password": password},
                           environ_base={"REMOTE_ADDR": GATEWAY}, headers={"Host": "localhost"})
    assert response.status_code == 403 and called == []


def test_health_check_and_static_files_still_answer(guarded):
    assert get(guarded, "/healthz", GATEWAY).status_code == 200
    assert get(guarded, "/static/orama.css", GATEWAY).status_code == 200


def test_a_device_on_the_home_network_is_unaffected(guarded, password):
    client = guarded.test_client()
    response = signin(client, password, headers={"Host": "localhost"},
                      environ_base={"REMOTE_ADDR": LAN_PHONE})
    assert response.status_code == 303


def test_the_owner_can_allow_it_with_a_file_in_the_state_folder(guarded, state_dir):
    assert get(guarded, "/signin", GATEWAY).status_code == 403
    (state_dir / security.SERVER_SIGNIN_FILE).write_text("")
    assert get(guarded, "/signin", GATEWAY).status_code != 403


ROUTES = (
    "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
    "eth0\t00000000\t010012AC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
    "eth0\t000012AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
    "eth1\t0000A8C0\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n"
)


def test_networks_are_read_from_the_route_table_inside_a_container(tmp_path):
    routes = tmp_path / "route"
    routes.write_text(ROUTES)
    marker = tmp_path / ".dockerenv"
    marker.write_text("")
    nets = security.container_networks(routes, marker)
    assert [str(n) for n in nets] == ["172.18.0.0/16", "192.168.0.0/24"]
    assert security.is_internal(GATEWAY, nets)
    assert security.is_internal("::ffff:172.18.0.1", nets)
    assert not security.is_internal(LAN_PHONE, nets)
    assert not security.is_internal("not an address", nets)


def test_outside_a_container_nothing_is_treated_as_internal(tmp_path):
    routes = tmp_path / "route"
    routes.write_text(ROUTES)
    assert security.container_networks(routes, tmp_path / "missing-dockerenv") == ()
    assert security.container_networks(tmp_path / "no-route", tmp_path / "x") == ()
