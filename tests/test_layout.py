from pathlib import PurePosixPath as P

from conftest import make_setup

from orama.layout import app_env_file, config_folders, data_folders, env_file_path


def test_data_folders_exact_list():
    root = P("/srv/data")
    assert data_folders(root) == [root / s for s in (
        "media", "media/movies", "media/tv", "downloads", "downloads/usenet",
        "downloads/usenet/movies", "downloads/usenet/tv", "downloads/torrents",
        "downloads/torrents/movies", "downloads/torrents/tv")]


def test_config_folders_per_enabled_app_plus_orama():
    s = make_setup()
    folders = config_folders(s)
    assert P("/srv/orama/config/plex") in folders
    assert P("/srv/orama/config/sabnzbd") in folders
    assert P("/srv/orama/config/qbittorrent") not in folders
    assert folders[-1] == P("/srv/orama/orama")


def test_env_file_path_delegates_to_app_env_file():
    s = make_setup()
    assert env_file_path(s, "sonarr") == P("/srv/orama/config/sonarr/orama.env")
    assert app_env_file(P("/r"), "x") == P("/r/config/x/orama.env")
