from orama.discovery import (
    DiscoveredApp,
    extract_api_key_from_xml,
    extract_host_port,
    identify_app,
    is_orama_container,
)


def test_is_orama_container():
    assert is_orama_container("orama")
    assert is_orama_container("/orama-server")
    assert is_orama_container("orama-apps-sonarr-1")
    assert not is_orama_container("sonarr")
    assert not is_orama_container("/plex")
    assert not is_orama_container("custom-qbittorrent")


def test_identify_app_by_image():
    assert identify_app("my-app", image="linuxserver/sonarr:latest") == "sonarr"
    assert identify_app("pms", image="plexinc/pms-docker:latest") == "plex"
    assert identify_app("downloader", image="markusmcnubs/qbittorrent") == "qbittorrent"


def test_identify_app_by_name():
    assert identify_app("sonarr") == "sonarr"
    assert identify_app("/plex_server") == "plex"
    assert identify_app("my-jellyfin-instance") == "jellyfin"
    assert identify_app("qbittorrent_vpn") == "qbittorrent"
    assert identify_app("unknown_service") is None


def test_identify_app_by_port():
    assert identify_app("unknown_service", published_ports=[8989]) == "sonarr"
    assert identify_app("unknown_service", published_ports=[32400]) == "plex"
    assert identify_app("unknown_service", published_ports=[12345]) is None


def test_identify_app_ignores_orama_containers():
    assert identify_app("orama-apps-sonarr-1", image="linuxserver/sonarr:latest") is None
    assert identify_app("orama-server") is None


def test_extract_host_port():
    assert extract_host_port("0.0.0.0:8989->8989/tcp") == 8989
    assert extract_host_port("[::]:8989->8989/tcp, 0.0.0.0:8989->8989/tcp") == 8989
    assert extract_host_port("0.0.0.0:18989->8989/tcp", target_container_port=8989) == 18989
    assert extract_host_port("invalid_string") is None
    assert extract_host_port("") is None


def test_extract_api_key_from_xml():
    xml = """
    <Config>
        <Port>8989</Port>
        <ApiKey>abc12345xyz67890</ApiKey>
    </Config>
    """
    assert extract_api_key_from_xml(xml) == "abc12345xyz67890"

    xml_empty = "<Config><Port>8989</Port></Config>"
    assert extract_api_key_from_xml(xml_empty) is None


def test_discovered_app_roundtrip():
    app = DiscoveredApp(
        name="sonarr",
        container_name="my-sonarr",
        image="linuxserver/sonarr",
        host_port=8989,
        source="docker",
        api_key="secretkey",
    )
    d = app.to_dict()
    reconstructed = DiscoveredApp.from_dict(d)
    assert reconstructed == app
