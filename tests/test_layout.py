from pathlib import PurePosixPath as P

from conftest import make_setup

from orama.layout import app_env_dir, app_env_file, config_folders, data_folders, env_file_path


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
    assert env_file_path(s, "sonarr") == P("/srv/orama/app-env/sonarr.env")
    assert app_env_file(P("/r"), "x") == P("/r/app-env/x.env")
    assert app_env_dir(P("/r")) == P("/r/app-env")


def test_config_folders_never_contain_app_env_dir():
    s = make_setup(media_servers={"plex", "jellyfin"}, download_methods={"usenet", "torrent"},
                   vpn={"provider": "protonvpn"})
    env_dir = app_env_dir(s.config_root)
    for folder in config_folders(s):
        assert not folder.is_relative_to(env_dir)
        assert not env_dir.is_relative_to(folder)
