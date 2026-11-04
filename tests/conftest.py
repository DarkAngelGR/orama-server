import os

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


def random_secret(prefix: str = "", nbytes: int = 8) -> str:
    """A random secret-shaped value built at run time, so no key-like literal sits in the repo."""
    return prefix + os.urandom(nbytes).hex()
