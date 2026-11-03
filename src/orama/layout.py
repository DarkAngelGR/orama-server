"""Folder layout: where data, per-app config and env files live on the host."""

from __future__ import annotations

from pathlib import PurePosixPath

from orama.setup_model import DesiredSetup, enabled_apps

_DATA_SUBFOLDERS = (
    "media",
    "media/movies",
    "media/tv",
    "downloads",
    "downloads/usenet",
    "downloads/usenet/movies",
    "downloads/usenet/tv",
    "downloads/torrents",
    "downloads/torrents/movies",
    "downloads/torrents/tv",
)


def data_folders(data_root: PurePosixPath) -> list[PurePosixPath]:
    """The shared data tree, parents before children."""
    return [data_root / sub for sub in _DATA_SUBFOLDERS]


def app_config_dir(config_root: PurePosixPath, app: str) -> PurePosixPath:
    return config_root / "config" / app


def app_env_file(config_root: PurePosixPath, app: str) -> PurePosixPath:
    """Per-app env file holding secret values (mode 0600 on disk)."""
    return app_config_dir(config_root, app) / "orama.env"


def config_folders(setup: DesiredSetup) -> list[PurePosixPath]:
    folders = [app_config_dir(setup.config_root, app) for app in enabled_apps(setup)]
    folders.append(setup.config_root / "orama")
    return folders


def env_file_path(setup: DesiredSetup, app: str) -> PurePosixPath:
    return app_env_file(setup.config_root, app)
