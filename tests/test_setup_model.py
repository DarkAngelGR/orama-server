import json

import pytest
from conftest import make_setup
from pydantic import ValidationError

from orama.setup_model import DesiredSetup, IndexerChoice, UsenetServer, VpnConfig, enabled_apps


def test_vpn_requires_torrent():
    with pytest.raises(ValidationError):
        make_setup(download_methods={"usenet"}, vpn=VpnConfig())


def test_qbittorrent_cannot_be_removed_while_port_forwarding_is_on():
    with pytest.raises(ValidationError, match="port forwarding"):
        make_setup(download_methods={"torrent"}, vpn=VpnConfig(),
                   removed_apps={"qbittorrent"})
    # Without port forwarding, or without the VPN, removing it is fine.
    make_setup(download_methods={"torrent"}, vpn=VpnConfig(port_forwarding=False),
               removed_apps={"qbittorrent"})
    make_setup(download_methods={"torrent"}, removed_apps={"qbittorrent"})


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


@pytest.mark.parametrize("field", ["data_root", "config_root"])
def test_roots_must_not_contain_colon(field):
    with pytest.raises(ValidationError, match="Docker cannot mount"):
        make_setup(**{field: "/srv/a:b"})


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
    root = '/mnt/Μέσα θέματα/it\'s "data" #1 & a'
    s = make_setup(data_root=root)
    assert str(s.data_root) == root
    path = tmp_path / "setup.json"
    s.save(path)
    assert str(DesiredSetup.load(path).data_root) == root


# ---------------------------------------------------------------- indexers and usenet servers

def _indexer(**kw):
    values = {
        "slug": "geek",
        "definition": "nzbgeek",
        "name": "NZBgeek",
        "fields": {"baseUrl": "https://api.example.test", "minimumSeeders": 1, "rss": True},
        "secret_fields": ("apiKey",),
    }
    values.update(kw)
    return IndexerChoice(**values)


def test_indexers_and_servers_roundtrip(tmp_path):
    s = make_setup(
        indexers=(_indexer(), _indexer(slug="own", definition="newznab", name="Own Μέσα #1",
                                       fields={}, secret_fields=())),
        usenet_servers=(
            UsenetServer(slug="main", host="news.example.test", username="me"),
            UsenetServer(slug="fill", host="eu.news.example.test", port=119, ssl=False,
                         connections=4),
        ),
    )
    path = tmp_path / "setup.json"
    s.save(path)
    loaded = DesiredSetup.load(path)
    assert loaded == s
    assert loaded.indexers[0].fields == {
        "baseUrl": "https://api.example.test", "minimumSeeders": 1, "rss": True
    }
    assert type(loaded.indexers[0].fields["rss"]) is bool
    assert type(loaded.indexers[0].fields["minimumSeeders"]) is int
    main = loaded.usenet_servers[0]
    assert (main.port, main.ssl, main.connections) == (563, True, 20)
    first = path.read_bytes()
    s.save(path)
    assert path.read_bytes() == first


def test_plan1_setup_without_new_fields_loads(tmp_path):
    data = make_setup().to_json_dict()
    del data["indexers"], data["usenet_servers"]
    path = tmp_path / "setup.json"
    path.write_text(json.dumps(data))
    loaded = DesiredSetup.load(path)
    assert loaded.indexers == () and loaded.usenet_servers == ()


def test_duplicate_indexer_slug_is_refused():
    with pytest.raises(ValidationError, match="unique"):
        make_setup(indexers=(_indexer(), _indexer(name="Other")))


def test_duplicate_indexer_name_with_distinct_slugs_is_refused():
    with pytest.raises(ValidationError, match="unique"):
        make_setup(indexers=(_indexer(), _indexer(slug="other")))


def test_duplicate_server_slug_is_refused():
    servers = (UsenetServer(slug="a", host="x.test"), UsenetServer(slug="a", host="y.test"))
    with pytest.raises(ValidationError, match="unique"):
        make_setup(usenet_servers=servers)


def test_usenet_servers_require_usenet():
    with pytest.raises(ValidationError, match="usenet"):
        make_setup(
            download_methods={"torrent"},
            usenet_servers=(UsenetServer(slug="a", host="x.test"),),
        )


@pytest.mark.parametrize(
    "host", ["a:b", "a/b", "a b", "", "-a.test", "a..test", "a\nb", "x" * 254, "[::1]"]
)
def test_bad_server_host_is_refused(host):
    with pytest.raises(ValidationError):
        UsenetServer(slug="a", host=host)


@pytest.mark.parametrize(
    "kw",
    [
        {"slug": "Bad"},
        {"slug": ""},
        {"slug": "a" * 41},
        {"slug": "a_b"},
        {"port": 0},
        {"port": 65536},
        {"connections": 0},
        {"connections": 101},
        {"username": "me\nyou"},
    ],
)
def test_bad_server_values_are_refused(kw):
    with pytest.raises(ValidationError):
        UsenetServer(**{"slug": "a", "host": "x.test", **kw})


@pytest.mark.parametrize(
    "kw",
    [
        {"slug": "a b"},
        {"slug": "a" * 41},
        {"name": ""},
        {"name": "x" * 61},
        {"name": "bad\x1b[31m"},
        {"definition": ""},
        {"definition": "a/b"},
        {"fields": {"bad key": "v"}},
        {"fields": {"": "v"}},
        {"fields": {"k": "line\nbreak"}},
        {"fields": {"k": "nul\x00"}},
        {"fields": {"k": "x" * 513}},
        {"fields": {f"k{i}": i for i in range(41)}},
        {"fields": {"k": ["a"]}},
        {"secret_fields": ("bad key",)},
        {"secret_fields": ("apiKey", "apiKey")},
        {"secret_fields": ("baseUrl",)},  # also a plain field
    ],
)
def test_bad_indexer_values_are_refused(kw):
    with pytest.raises(ValidationError):
        _indexer(**kw)


def test_indexer_limits_are_inclusive():
    choice = _indexer(name="x" * 60, fields={f"k{i}": "v" * 512 for i in range(40)})
    assert len(choice.fields) == 40


# Plan 2b playback preferences

def test_playback_defaults_and_legacy_setup(tmp_path):
    data = make_setup().to_json_dict()
    for key in ("subtitle_languages", "device", "quality", "lan_address"):
        data.pop(key, None)
    path = tmp_path / "setup.json"
    path.write_text(json.dumps(data))
    setup = DesiredSetup.load(path)
    assert setup.subtitle_languages == ("el", "en")
    assert setup.device == "safest"
    assert setup.quality == "mixed"
    assert setup.lan_address is None


def test_playback_roundtrip_keeps_language_order(tmp_path):
    setup = make_setup(subtitle_languages=("en", "el"), device="samsung_s95f",
                       quality="4k", lan_address=".".join(map(str, (192, 168, 1, 10))))
    path = tmp_path / "setup.json"
    setup.save(path)
    assert DesiredSetup.load(path) == setup
    assert json.loads(path.read_text())["subtitle_languages"] == ["en", "el"]
    assert "password" not in path.read_text()


@pytest.mark.parametrize("languages", [(), ("el", "el"), ("en",) * 6,
                                         ("EN",), ("english",), ("1a",)])
def test_invalid_subtitle_languages(languages):
    with pytest.raises(ValidationError):
        make_setup(subtitle_languages=languages)


@pytest.mark.parametrize("field,value", [("device", "unknown"), ("quality", "8k"),
                                          ("lan_address", "8.8.8.8"),
                                          ("lan_address", "2001:db8::1"),
                                          ("lan_address", 3232235784),
                                          ("lan_address", b"\xc0\xa8\x01\x08")])
def test_invalid_playback_values(field, value):
    with pytest.raises(ValidationError):
        make_setup(**{field: value})


def test_lan_address_json_integer_refused(tmp_path):
    good = make_setup(lan_address=".".join(map(str, (192, 168, 1, 10))))
    data = json.loads(good.model_dump_json())
    data["lan_address"] = 3232235786  # the same address as a JSON number
    with pytest.raises(ValidationError):
        type(good).model_validate_json(json.dumps(data))
    data["lan_address"] = ".".join(map(str, (192, 168, 1, 10)))
    assert str(type(good).model_validate_json(json.dumps(data)).lan_address) == data["lan_address"]


# ---------------------------------------------------------------- VPN providers


def _address(*octets, prefix=32):
    return ".".join(map(str, octets)) + f"/{prefix}"


def test_vpn_providers_are_the_five_in_this_version():
    from orama.catalog import VPN_PROVIDERS

    assert {name: p.label for name, p in VPN_PROVIDERS.items()} == {
        "protonvpn": "Proton VPN", "nordvpn": "NordVPN", "mullvad": "Mullvad",
        "ivpn": "IVPN", "surfshark": "Surfshark",
    }
    assert [n for n, p in VPN_PROVIDERS.items() if p.port_forwarding] == ["protonvpn"]
    assert {n for n, p in VPN_PROVIDERS.items() if p.needs_address} == {
        "mullvad", "ivpn", "surfshark",
    }
    assert all(name == p.name for name, p in VPN_PROVIDERS.items())


@pytest.mark.parametrize("provider", ["expressvpn", "ProtonVPN", "", "custom", 7])
def test_unknown_vpn_provider_refused(provider):
    with pytest.raises(ValidationError):
        VpnConfig(provider=provider, port_forwarding=False)


def test_port_forwarding_refused_for_nordvpn():
    with pytest.raises(ValidationError, match="Port forwarding works only with Proton VPN"):
        VpnConfig(provider="nordvpn", port_forwarding=True)
    VpnConfig(provider="nordvpn", port_forwarding=False)
    VpnConfig(provider="protonvpn", port_forwarding=True)


@pytest.mark.parametrize("provider", ["mullvad", "ivpn", "surfshark"])
def test_mullvad_needs_wireguard_address(provider):
    with pytest.raises(ValidationError, match="WireGuard address"):
        VpnConfig(provider=provider, port_forwarding=False)
    vpn = VpnConfig(provider=provider, port_forwarding=False,
                    wireguard_address=_address(10, 64, 1, 2))
    assert str(vpn.wireguard_address) == _address(10, 64, 1, 2)


@pytest.mark.parametrize("provider", ["protonvpn", "nordvpn"])
def test_address_refused_for_protonvpn(provider):
    with pytest.raises(ValidationError, match="WireGuard address"):
        VpnConfig(provider=provider, port_forwarding=False,
                  wireguard_address=_address(10, 2, 0, 2))


@pytest.mark.parametrize("bad", [
    _address(10, 64, 1, 2, prefix=7), _address(10, 64, 1, 2, prefix=33), "2001:db8::2/128",
    "10.64.1", 167772161, " " + _address(10, 64, 1, 2), _address(10, 64, 1, 2) + ",",
    _address(300, 1, 1, 1),
])
def test_bad_wireguard_address_refused(bad):
    with pytest.raises(ValidationError):
        VpnConfig(provider="mullvad", port_forwarding=False, wireguard_address=bad)


def test_wireguard_address_round_trips_as_text(tmp_path):
    setup = make_setup(download_methods={"torrent"}, vpn=VpnConfig(
        provider="ivpn", port_forwarding=False, wireguard_address=_address(172, 18, 3, 4, prefix=24)))
    setup.save(tmp_path / "s.json")
    saved = json.loads((tmp_path / "s.json").read_text())
    assert saved["vpn"]["wireguard_address"] == _address(172, 18, 3, 4, prefix=24)
    assert DesiredSetup.load(tmp_path / "s.json") == setup


def test_saved_protonvpn_setup_without_address_still_loads(tmp_path):
    path = tmp_path / "s.json"
    data = make_setup(download_methods={"torrent"}, vpn=VpnConfig()).to_json_dict()
    del data["vpn"]["wireguard_address"]
    path.write_text(json.dumps(data))
    loaded = DesiredSetup.load(path)
    assert loaded.vpn.provider == "protonvpn" and loaded.vpn.wireguard_address is None
