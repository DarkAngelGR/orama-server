import pytest  # noqa: F401

from orama.setup_model import DesiredSetup


def make_setup(**overrides) -> DesiredSetup:
    values = {
        "media_servers": {"plex"},
        "download_methods": {"usenet"},
        "vpn": None,
        "data_root": "/srv/data",
        "config_root": "/srv/orama",
        "puid": 1000,
        "pgid": 1000,
        "render_gid": None,
    }
    values.update(overrides)
    return DesiredSetup(**values)
