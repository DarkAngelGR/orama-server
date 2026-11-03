import pytest

from orama.catalog import CATALOG, CORE, UGOS_RESERVED_PORTS
from orama.ports import assign_ports


def test_assign_ports_uses_defaults():
    assert assign_ports(["sonarr", "sabnzbd"], {}, set()) == {"sonarr": 8989, "sabnzbd": 8080}


def test_taken_port_moves_only_that_app():
    got = assign_ports(["sonarr", "sabnzbd"], {}, taken={8080})
    assert got["sonarr"] == 8989 and got["sabnzbd"] not in {8080, 8989}


def test_previous_assignments_are_kept():
    assert assign_ports(["sabnzbd"], {"sabnzbd": 8081}, taken={8081})["sabnzbd"] == 8081


def test_never_assigns_reserved_or_other_apps_defaults():
    got = assign_ports(["sabnzbd"], {}, taken={8080, 8081})
    forbidden = UGOS_RESERVED_PORTS | {
        s.default_host_port for s in [*CATALOG.values(), *CORE.values()]}
    assert got["sabnzbd"] not in forbidden
    assert got["sabnzbd"] == 8082


def test_scan_skips_other_apps_defaults():
    # 8090 is qbittorrent's default; sabnzbd scanning up from 8080 must skip it.
    got = assign_ports(["sabnzbd"], {}, taken=set(range(8080, 8090)))
    assert got["sabnzbd"] == 8091


def test_orama_resolves_from_core_and_8765_never_given_to_apps():
    assert assign_ports(["orama"], {}, set()) == {"orama": 8765}
    got = assign_ports(["sabnzbd"], {}, taken=set(range(8080, 8765)) - {8090})
    assert got["sabnzbd"] not in {8090, 8765}


def test_portless_apps_skipped():
    assert assign_ports(["gluetun", "recyclarr", "orama-agent"], {}, set()) == {}


def test_unknown_app_raises():
    with pytest.raises(ValueError, match="nope"):
        assign_ports(["nope"], {}, set())


def test_assigned_ports_unique_when_defaults_collide():
    got = assign_ports(["sonarr", "radarr"], {"radarr": 8990}, taken=set())
    assert len(set(got.values())) == 2


def test_overflow_raises():
    with pytest.raises(ValueError):
        assign_ports(["sabnzbd"], {}, taken=set(range(8080, 65536)))
