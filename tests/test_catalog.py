import re

from orama.catalog import CATALOG, CORE, UGOS_RESERVED_PORTS, orama_version

ALL = [*CATALOG.values(), *CORE.values()]


def test_expected_services_present():
    assert set(CATALOG) == {"plex", "jellyfin", "sonarr", "radarr", "prowlarr", "bazarr",
                            "sabnzbd", "gluetun", "qbittorrent", "recyclarr"}
    assert set(CORE) == {"orama", "orama-agent"}


def test_no_latest_tags():
    for s in ALL:
        assert ":" in s.image and not s.image.endswith(":latest")


def test_core_images_use_package_version():
    assert CORE["orama"].image.endswith(f":{orama_version()}")
    assert CORE["orama-agent"].image.endswith(f":{orama_version()}")
    assert re.search(r":\d+\.\d+", CORE["orama"].image)


def test_default_ports_unique_and_not_reserved():
    ports = [s.default_host_port for s in ALL if s.default_host_port]
    assert len(ports) == len(set(ports))
    assert not set(ports) & UGOS_RESERVED_PORTS


def test_qbittorrent_and_sabnzbd_do_not_clash():
    assert CATALOG["qbittorrent"].default_host_port == 8090
    assert CATALOG["sabnzbd"].default_host_port == 8080


def test_portless_services():
    for name in ("gluetun", "recyclarr"):
        assert CATALOG[name].default_host_port == 0 and CATALOG[name].container_port == 0
    assert CORE["orama-agent"].default_host_port == 0


def test_secret_env_and_gluetun_privileges():
    assert CATALOG["sonarr"].secret_env == ("SONARR__AUTH__APIKEY",)
    assert CATALOG["radarr"].secret_env == ("RADARR__AUTH__APIKEY",)
    assert CATALOG["prowlarr"].secret_env == ("PROWLARR__AUTH__APIKEY",)
    assert CATALOG["gluetun"].secret_env == ("WIREGUARD_PRIVATE_KEY",)
    assert CATALOG["gluetun"].cap_add == ("NET_ADMIN",)
    assert CATALOG["gluetun"].devices == ("/dev/net/tun",)
    assert CATALOG["plex"].secret_env == ()
