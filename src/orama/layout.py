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
    "downloads/usenet/incomplete",
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


def app_env_dir(config_root: PurePosixPath) -> PurePosixPath:
    """Agent-only folder for per-app env files.

    Kept outside every app's config folder (apps mount those read-write, so an app could
    swap its env file for a link) and outside Orama's state folder. It is never in
    ``config_folders`` and never mounted by any app or by Orama.
    """
    return config_root / "app-env"


def app_env_file(config_root: PurePosixPath, app: str) -> PurePosixPath:
    """Per-app env file holding secret values (mode 0600 on disk)."""
    return app_env_dir(config_root) / f"{app}.env"


def orama_state_dir(config_root: PurePosixPath) -> PurePosixPath:
    """Orama's own state (setup, secrets, generated compose). The only folder it mounts."""
    return config_root / "orama"


def config_folders(setup: DesiredSetup) -> list[PurePosixPath]:
    folders = [app_config_dir(setup.config_root, app) for app in enabled_apps(setup)]
    folders.append(orama_state_dir(setup.config_root))
    return folders


def env_file_path(setup: DesiredSetup, app: str) -> PurePosixPath:
    return app_env_file(setup.config_root, app)
