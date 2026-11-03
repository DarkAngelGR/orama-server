import json

import pytest
from conftest import make_setup
from pydantic import ValidationError

from orama.setup_model import DesiredSetup, VpnConfig, enabled_apps


def test_vpn_requires_torrent():
    with pytest.raises(ValidationError):
        make_setup(download_methods={"usenet"}, vpn=VpnConfig())


def test_media_server_and_download_method_required():
    with pytest.raises(ValidationError):
        make_setup(media_servers=set())
    with pytest.raises(ValidationError):
        make_setup(download_methods=set())


@pytest.mark.parametrize("bad", ["relative/path", "/data/../etc", "/", ""])
def test_roots_must_be_absolute_normalised_and_not_root(bad):
    with pytest.raises(ValidationError):
        make_setup(data_root=bad)
    with pytest.raises(ValidationError):
        make_setup(config_root=bad)


def test_roots_are_normalised():
    s = make_setup(data_root="/srv//data/./")
    assert str(s.data_root) == "/srv/data"


@pytest.mark.parametrize(
    "data,config",
    [
        ("/volume1/docker/orama/data", "/volume1/docker/orama"),
        ("/srv", "/srv/orama"),
        ("/srv/x", "/srv/x"),
    ],
)
def test_roots_must_not_nest(data, config):
    with pytest.raises(ValidationError):
        make_setup(data_root=data, config_root=config)


def test_enabled_apps_usenet_plex():
    assert enabled_apps(make_setup(media_servers={"plex"}, download_methods={"usenet"})) == [
        "plex",
        "sonarr",
        "radarr",
        "prowlarr",
        "bazarr",
        "sabnzbd",
    ]


def test_enabled_apps_both_with_vpn():
    s = make_setup(
        media_servers={"plex", "jellyfin"},
        download_methods={"usenet", "torrent"},
        vpn=VpnConfig(),
    )
    assert enabled_apps(s) == [
        "plex",
        "jellyfin",
        "sonarr",
        "radarr",
        "prowlarr",
        "bazarr",
        "sabnzbd",
        "gluetun",
        "qbittorrent",
    ]


def test_enabled_apps_torrent_without_vpn_has_no_gluetun():
    s = make_setup(download_methods={"torrent"})
    assert enabled_apps(s) == ["plex", "sonarr", "radarr", "prowlarr", "bazarr", "qbittorrent"]


def test_removed_apps_are_not_enabled():
    assert "bazarr" not in enabled_apps(make_setup(removed_apps={"bazarr"}))


def test_save_load_roundtrip(tmp_path):
    s = make_setup(
        media_servers={"plex", "jellyfin"},
        download_methods={"usenet", "torrent"},
        vpn=VpnConfig(route_prowlarr=True),
        render_gid=105,
        nvidia=True,
        ports={"plex": 32400},
        removed_apps={"bazarr"},
    )
    path = tmp_path / "setup.json"
    s.save(path)
    assert DesiredSetup.load(path) == s
    first = path.read_bytes()
    s.save(path)
    assert path.read_bytes() == first  # deterministic
    data = json.loads(first)
    assert data["media_servers"] == ["jellyfin", "plex"]
    assert [p.name for p in tmp_path.iterdir()] == ["setup.json"]  # no temp left over


def test_unusual_path_characters_roundtrip(tmp_path):
    root = '/mnt/Μέσα θέματα/it\'s "data" #1:a'
    s = make_setup(data_root=root)
    assert str(s.data_root) == root
    path = tmp_path / "setup.json"
    s.save(path)
    assert str(DesiredSetup.load(path).data_root) == root
