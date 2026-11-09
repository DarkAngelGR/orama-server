"""The Agent's Unix-socket server: a fixed allowlist of commands with validated arguments.

Protocol: one JSON object per line. Request ``{"id": str, "cmd": str, "args": {...}}``,
response ``{"id", "ok": true, "result": ...}`` or ``{"id", "ok": false, "error": str}``.

Orama (the client) and every app container are untrusted. Folders and files are created
by walking from the Agent's own roots one component at a time through directory file
descriptors, never following a symlink, so a link planted by an app cannot redirect a
root-owned write or chown. Logs carry the command name, request id and outcome only:
never arguments. When docker compose fails, the tail of its stderr is logged too, with
every env-file value (from the request and from the stored files) replaced by ``***``;
the client only ever gets a plain message.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import secrets
import socketserver
import stat
import threading
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from orama.catalog import CATALOG
from orama.compose import dump_yaml
from orama.layout import app_config_dir, app_env_dir, data_folders
from orama_agent import recyclarr, seed
from orama_agent.runner import (
    AGENT_ENV_FILE,
    COMPOSE_FILE,
    CORE_COMPOSE_FILE,
    RunnerError,
    project_file,
)
from orama_agent.validate import (
    Refused,
    Roots,
    check_host_paths,
    validate_compose,
    validate_env_files,
    validate_secrets_present,
)

log = logging.getLogger("orama_agent")

DEFAULT_SOCKET = "/run/orama/agent.sock"
MAX_REQUEST = 1024 * 1024  # bytes per request line
MAX_CONNECTIONS = 16  # bound concurrent idle clients and command waiters
MAX_PATHS = 64
READ_TIMEOUT = 30.0
SOCKET_MODE = 0o660
SEED_FILE_MODE = 0o600  # app config files written by seed-config, PUID:PGID
SHARED_DIR_MODE = 0o2775  # data folders and app config folders, PUID:PGID
AGENT_DIR_MODE = 0o755  # <config>/config, root-owned
ENV_DIR_MODE = 0o700  # <config>/app-env, root-owned
APPLY_MARKER = ".apply-incomplete"  # in app-env while an up is running or after it failed
_ID_MAX = 128
COMMANDS = frozenset({"ensure-folders", "apply", "status", "restart", "seed-config",
                      "recyclarr-sync", "data-space", "self-update"})
_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_NEW_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC

Chown = Callable[..., None]


def _skip_chown(*_args: Any, **_kw: Any) -> None:
    """Not root: owners cannot be changed, and need not be (tests, development)."""


def apps_can_write(st: os.stat_result, puid: int, pgid: int) -> bool:
    """True if the apps (PUID:PGID) may write in a folder with this owner, group and mode.

    The kernel's order: the owner's bits decide for the owner, else the group's bits for
    the group, else the bits for everyone. ACLs and supplementary groups are not seen.
    """
    mode = st.st_mode
    if st.st_uid == puid:
        return bool(mode & stat.S_IWUSR)
    if st.st_gid == pgid:
        return bool(mode & stat.S_IWGRP)
    return bool(mode & stat.S_IWOTH)


def _show(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= _ID_MAX else text[: _ID_MAX - 3] + "..."


# ---------------------------------------------------------------- safe file system


def _open_child_dir(parent_fd: int, name: str, shown: Path) -> int:
    try:
        if stat.S_ISLNK(os.stat(name, dir_fd=parent_fd, follow_symlinks=False).st_mode):
            raise Refused(
                f"The folder {_show(str(shown))} is a link; Orama only uses real folders."
            )
    except FileNotFoundError:
        pass
    except Refused:
        raise
    except OSError:
        pass
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except OSError as e:
        if e.errno not in (errno.ELOOP, errno.ENOTDIR):
            raise
        # O_NOFOLLOW|O_DIRECTORY reports a link as ENOTDIR on Linux; say which it is.
        try:
            is_link = stat.S_ISLNK(os.stat(name, dir_fd=parent_fd, follow_symlinks=False).st_mode)
        except OSError:
            is_link = False
        if is_link or e.errno == errno.ELOOP:
            raise Refused(
                f"The folder {_show(str(shown))} is a link; Orama only uses real folders."
            ) from None
        raise Refused(f"{_show(str(shown))} is not a folder.") from None


def _write_atomic(
    dir_fd: int, name: str, data: bytes, mode: int, chown: Callable[[int], None] | None = None
) -> None:
    """Replace ``name`` in ``dir_fd`` atomically. A link at ``name`` is replaced, not followed.

    ``chown`` is called with the new file's descriptor before it takes ``name``'s place.
    """
    tmp = f".{name}.{secrets.token_hex(8)}.tmp"
    fd = os.open(tmp, _NEW_FILE_FLAGS, mode, dir_fd=dir_fd)
    try:
        try:
            os.fchmod(fd, mode)  # mode from creation is reduced by umask
            if chown is not None:
                chown(fd)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
        raise
    os.fsync(dir_fd)


def _env_file_text(values: dict[str, str]) -> bytes:
    """One ``KEY='value'`` line per secret, checked to read back exactly as sent.

    ``validate_env_files`` only lets through values Compose takes literally inside single
    quotes. As a second guard against a future edit, the text is parsed back the way
    Compose reads it; if that does not give exactly ``values`` (same keys, same values),
    nothing is written.
    """
    text = "".join(f"{key}='{value}'\n" for key, value in values.items())
    if _parse_env_file(text) != values:
        raise Refused("A secrets file would not be read back exactly as sent; nothing was written.")
    return text.encode()


def _parse_env_file(text: str) -> dict[str, str] | None:
    """``{KEY: value}`` as Compose's env-file parser reads ``KEY='value'`` lines.

    Inside single quotes Compose treats a backslash before a quote as an escaped quote (the
    value then runs on, across lines, to the next unescaped quote) and keeps any other
    backslash. Only the strict form ``_env_file_text`` writes is accepted: each statement is
    ``KEY='...'`` followed directly by a line break. Anything else, or a repeated key,
    gives None.
    """
    result: dict[str, str] = {}
    i, end = 0, len(text)
    while i < end:
        eq = text.find("=", i)
        if eq < 0 or not _ENV_KEY.fullmatch(text[i:eq]) or text[eq + 1 : eq + 2] != "'":
            return None
        key = text[i:eq]
        chars: list[str] = []
        escaped = False
        j = eq + 2
        while True:
            if j >= end:
                return None  # no closing quote
            char = text[j]
            if char == "'":
                if not escaped:
                    break  # the closing quote
                chars.append(char)  # an escaped quote: part of the value
                escaped = False
            elif escaped:
                chars += ["\\", char]  # a backslash Compose keeps, then this character
                escaped = False
            elif char == "\\":
                escaped = True
            else:
                chars.append(char)
            j += 1
        if text[j + 1 : j + 2] != "\n" or key in result:
            return None
        result[key] = "".join(chars)
        i = j + 2
    return result


def _env_file_values(text: str) -> list[str]:
    """The values of a file written by ``_env_file_text``."""
    values = []
    for line in text.splitlines():
        _key, sep, rest = line.partition("=")
        if sep:
            values.append(rest[1:-1] if len(rest) >= 2 and rest[0] == rest[-1] == "'" else rest)
    return values


def redact(text: str, values: Any) -> str:
    """``text`` with every non-empty value replaced by ``***`` (longest first)."""
    for value in sorted({v for v in values if isinstance(v, str) and v}, key=len, reverse=True):
        text = text.replace(value, "***")
    return text


class Agent:
    """Command handlers. Each returns a result or raises Refused / RunnerError."""

    def __init__(self, roots: Roots, runner: Any, chown: Chown) -> None:
        self.roots = roots
        self.runner = runner
        self._chown = chown
        self._lock = threading.Lock()  # one docker / file-system operation at a time
        self._shared = {str(p) for p in data_folders(PurePosixPath(roots.data))} | {
            str(app_config_dir(PurePosixPath(roots.config), app)) for app in CATALOG
        }

    def handle(self, cmd: str, args: dict[str, Any]) -> Any:
        handler = {
            "ensure-folders": self.ensure_folders,
            "apply": self.apply,
            "status": self.status,
            "restart": self.restart,
            "seed-config": self.seed_config,
            "recyclarr-sync": self.recyclarr_sync,
            "data-space": self.data_space,
            "self-update": self.self_update,
        }[cmd]
        with self._lock:
            return handler(args)

    # ------------------------------------------------------------ ensure-folders

    def ensure_folders(self, args: dict[str, Any]) -> dict[str, list[str]]:
        """Create the missing folders; list the existing ones the apps cannot write to.

        Existing folders are only looked at (``fstat`` on the no-follow descriptor), never
        changed: they are the owner's, and Orama manages only what it created.
        """
        _expect_keys(args, {"paths"})
        paths = args["paths"]
        if not isinstance(paths, list) or len(paths) > MAX_PATHS:
            raise Refused(f"The folder list must be a list of at most {MAX_PATHS} paths.")
        for p in paths:
            if not isinstance(p, str) or p not in self._shared:
                raise Refused(f"The folder {_show(p)} is not one Orama may create.")
        created: list[str] = []
        not_writable: list[str] = []
        for p in paths:
            fd, made = self._open_dirs(Path(p), self._shared)
            try:
                existing = p not in made and p not in created and p not in not_writable
                if existing and not apps_can_write(
                    os.fstat(fd), self.roots.puid, self.roots.pgid
                ):
                    not_writable.append(p)
            finally:
                os.close(fd)
            created += made
        return {"created": created, "not_writable": not_writable}

    def _open_dirs(self, target: Path, shared: set[str]) -> tuple[int, list[str]]:
        """Open ``target`` (creating missing folders, never following a link) and return
        its descriptor and the folders created. New folders in ``shared`` belong to
        PUID:PGID; other new folders to root."""
        root = self.roots.data if target.is_relative_to(self.roots.data) else self.roots.config
        created = []
        fd = os.open(root, _DIR_FLAGS)
        try:
            current = root
            for part in target.relative_to(root).parts:
                current = current / part
                is_shared = str(current) in shared
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                    made = True
                except FileExistsError:
                    made = False
                child = _open_child_dir(fd, part, current)
                os.close(fd)
                fd = child
                if made:
                    if is_shared:
                        self._chown(fd, self.roots.puid, self.roots.pgid)
                    os.fchmod(fd, SHARED_DIR_MODE if is_shared else AGENT_DIR_MODE)
                    created.append(str(current))
        except BaseException:
            os.close(fd)
            raise
        return fd, created

    # ------------------------------------------------------------ apply

    def apply(self, args: dict[str, Any]) -> dict[str, list[str]]:
        _expect_keys(args, {"compose", "env_files"})
        doc, env_files = args["compose"], args["env_files"]
        validate_compose(doc, self.roots)
        validate_env_files(env_files, self.roots)
        validate_secrets_present(doc, env_files)
        env_texts = {app: _env_file_text(values) for app, values in env_files.items()}

        config_fd = os.open(self.roots.config, _DIR_FLAGS)
        try:
            env_fd = self._open_env_dir(config_fd)
            try:
                _write_atomic(env_fd, AGENT_ENV_FILE, b"", 0o600)
                for app, text in env_texts.items():
                    _write_atomic(env_fd, f"{app}.env", text, 0o600)
            finally:
                os.close(env_fd)
            _write_atomic(config_fd, COMPOSE_FILE, dump_yaml(doc).encode(), 0o644)
        finally:
            os.close(config_fd)

        check_host_paths(doc, self.roots)
        return {"changed": self._up(doc)}

    def _up(self, doc: dict[str, Any]) -> list[str]:
        """Run up behind a marker file, so an interrupted apply is reported in full.

        A failed ``up`` may already have recreated some containers; the retry would then
        see their IDs unchanged and under-report. While the marker from a failed run is
        present, every service in the document counts as changed.
        """
        config_fd = os.open(self.roots.config, _DIR_FLAGS)
        try:
            env_fd = self._open_env_dir(config_fd)
        finally:
            os.close(config_fd)
        try:
            incomplete = _exists(env_fd, APPLY_MARKER)
            _write_atomic(env_fd, APPLY_MARKER, b"", 0o600)
            changed = self.runner.up(str(project_file(self.roots)))
            os.unlink(APPLY_MARKER, dir_fd=env_fd)
        finally:
            os.close(env_fd)
        return sorted(doc["services"]) if incomplete else changed

    def _open_env_dir(self, config_fd: int) -> int:
        """Open (creating if missing, root 0700) the Agent-only env folder; never a link."""
        name = app_env_dir(PurePosixPath(".")).name
        try:
            os.mkdir(name, ENV_DIR_MODE, dir_fd=config_fd)
            made = True
        except FileExistsError:
            made = False
        fd = _open_child_dir(config_fd, name, Path(app_env_dir(PurePosixPath(self.roots.config))))
        if made:
            try:
                os.fchmod(fd, ENV_DIR_MODE)
            except BaseException:
                os.close(fd)
                raise
        return fd

    def secret_values(self, cmd: str, args: dict[str, Any]) -> list[str]:
        """Every env-file or seed-config value this request carried, and every env-file
        value the Agent has stored, for redaction."""
        values: list[str] = []
        seeded = args.get("values") if cmd == "seed-config" else None
        if isinstance(seeded, dict):
            values += [v for v in seeded.values() if isinstance(v, str)]
        env_files = args.get("env_files") if cmd == "apply" else None
        if isinstance(env_files, dict):
            for app_values in env_files.values():
                if isinstance(app_values, dict):
                    values += [v for v in app_values.values() if isinstance(v, str)]
        values += self._stored_env_values()
        return values

    def _stored_env_values(self) -> list[str]:
        values: list[str] = []
        try:
            config_fd = os.open(self.roots.config, _DIR_FLAGS)
            try:
                env_fd = os.open(app_env_dir(PurePosixPath(".")).name, _DIR_FLAGS,
                                 dir_fd=config_fd)
            finally:
                os.close(config_fd)
        except OSError:
            return values
        try:
            for name in os.listdir(env_fd):
                if name.endswith(".env"):
                    values += _env_file_values(_read_small(env_fd, name))
        except OSError:
            pass
        finally:
            os.close(env_fd)
        return values

    # ------------------------------------------------------------ status / restart

    def status(self, args: dict[str, Any]) -> dict[str, str]:
        _expect_keys(args, set())
        if not self._ensure_agent_env_file():
            return {}  # nothing applied yet
        return self.runner.ps()

    def data_space(self, args: dict[str, Any]) -> dict[str, int]:
        """Return total and available bytes on the configured data root without following links."""
        _expect_keys(args, set())
        try:
            fd = os.open(self.roots.data, _DIR_FLAGS)
        except OSError:
            raise Refused("Orama cannot check the available storage space right now.") from None
        try:
            vfs = os.fstatvfs(fd)
        except OSError:
            raise Refused("Orama cannot check the available storage space right now.") from None
        finally:
            os.close(fd)
        return {
            "total_bytes": int(vfs.f_frsize * vfs.f_blocks),
            "free_bytes": int(vfs.f_frsize * vfs.f_bavail),
        }

    def restart(self, args: dict[str, Any]) -> None:
        _expect_keys(args, {"app"})
        app = args["app"]
        if not isinstance(app, str) or app not in CATALOG:
            raise Refused(f"{_show(app)} is not an app Orama knows about.")
        self._ensure_agent_env_file()
        self.runner.restart(app)

    def self_update(self, args: dict[str, Any]) -> dict[str, bool]:
        """Pull Orama's core images and update the core project from core.compose.yaml."""
        _expect_keys(args, set())
        config_fd = os.open(self.roots.config, _DIR_FLAGS)
        try:
            shown = self.roots.config / CORE_COMPOSE_FILE
            if _read_regular(config_fd, CORE_COMPOSE_FILE, shown) is None:
                raise Refused("Orama's core compose file is missing; run the installer first.")
            env_fd = self._open_env_dir(config_fd)
            try:
                if not _exists(env_fd, AGENT_ENV_FILE):
                    _write_atomic(env_fd, AGENT_ENV_FILE, b"", 0o600)
            finally:
                os.close(env_fd)
        finally:
            os.close(config_fd)
        self.runner.self_update()
        return {"updated": True}

    # ------------------------------------------------------------ seed-config

    def seed_config(self, args: dict[str, Any]) -> dict[str, bool]:
        """Write SABnzbd's or qBittorrent's credentials into its own config file.

        Only the keys and value shapes in ``orama_agent.seed`` are accepted. The file is
        read and replaced without following links. When it would change, a running app
        is stopped first (both apps save their config on exit), the file is read again,
        written PUID:PGID 0600, and the app is started again.
        """
        _expect_keys(args, {"app", "values"})
        app, values = args["app"], args["values"]
        seed.check(app, values)
        *folders, name = seed.config_file(app)
        app_dir = Path(app_config_dir(PurePosixPath(self.roots.config), app))
        folder = app_dir.joinpath(*folders)
        shared = self._shared | {str(app_dir.joinpath(*folders[: i + 1]))
                                 for i in range(len(folders))}
        dir_fd, _ = self._open_dirs(folder, shared)
        try:
            shown = folder / name
            if seed.render(app, _read_regular(dir_fd, name, shown), values) is None:
                return {"changed": False}
            running = self._ensure_agent_env_file() and self.runner.ps().get(app) == "running"
            if running:
                self.runner.stop(app)
            try:
                text = seed.render(app, _read_regular(dir_fd, name, shown), values)
                if text is not None:
                    _write_atomic(
                        dir_fd, name, text.encode("utf-8", "surrogateescape"), SEED_FILE_MODE,
                        chown=lambda fd: self._chown(fd, self.roots.puid, self.roots.pgid),
                    )
            finally:
                if running:
                    self.runner.start(app)
        finally:
            os.close(dir_fd)
        return {"changed": True}

    # ------------------------------------------------------------ recyclarr-sync

    def recyclarr_sync(self, args: dict[str, Any]) -> dict[str, bool]:
        """Sync Sonarr's and Radarr's quality profiles with Recyclarr, once.

        The YAML is rendered here (``orama_agent.recyclarr``) with the API keys from the
        Agent's own env files, written PUID:PGID 0600 into Recyclarr's config folder next to
        the fixed settings that pin the guide data, and both are deleted after the run
        whether it worked or not. When the config (keys left out) is
        the one that last synced and ``force`` is not set, nothing runs.
        """
        apps, force = recyclarr.check(args)
        wanted = recyclarr.digest(apps)
        config_fd = os.open(self.roots.config, _DIR_FLAGS)
        try:
            env_fd = self._open_env_dir(config_fd)
        finally:
            os.close(config_fd)
        try:
            if not force and _read_small(env_fd, recyclarr.STATE_FILE).strip() == wanted:
                return {"changed": False}
            text = yaml.safe_dump(recyclarr.render(apps, self._arr_keys(env_fd, apps)),
                                  sort_keys=False).encode()
            _unlink(env_fd, recyclarr.STATE_FILE)  # a failed run must not count as done
            folder = Path(app_config_dir(PurePosixPath(self.roots.config), "recyclarr"))
            dir_fd, _ = self._open_dirs(folder, self._shared)
            def owned(fd: int) -> None:
                self._chown(fd, self.roots.puid, self.roots.pgid)

            try:
                try:
                    # Only Orama's pinned settings are read: never another spelling of them.
                    _unlink(dir_fd, recyclarr.SETTINGS_ALTERNATE)
                    _write_atomic(dir_fd, recyclarr.SETTINGS_FILE,
                                  recyclarr.settings_text().encode(), SEED_FILE_MODE,
                                  chown=owned)
                    _write_atomic(dir_fd, recyclarr.CONFIG_FILE, text, SEED_FILE_MODE,
                                  chown=owned)
                    self.runner.recyclarr_sync()
                finally:
                    _unlink(dir_fd, recyclarr.CONFIG_FILE)
                    _unlink(dir_fd, recyclarr.SETTINGS_FILE)
            finally:
                os.close(dir_fd)
            _write_atomic(env_fd, recyclarr.STATE_FILE, f"{wanted}\n".encode(), 0o600)
        finally:
            os.close(env_fd)
        return {"changed": True}

    def _arr_keys(self, env_fd: int, apps: dict[str, Any]) -> dict[str, str]:
        keys = {}
        for app in apps:
            name = recyclarr.APPS[app].key_env
            values = _parse_env_file(_read_small(env_fd, f"{app}.env")) or {}
            if not values.get(name):
                raise Refused(
                    f"{app.title()}'s API key is not set up yet; run apply first."
                )
            keys[app] = values[name]
        return keys

    def _ensure_agent_env_file(self) -> bool:
        """Create the empty --env-file if missing; True if a compose file exists yet."""
        config_fd = os.open(self.roots.config, _DIR_FLAGS)
        try:
            env_fd = self._open_env_dir(config_fd)
            try:
                if not _exists(env_fd, AGENT_ENV_FILE):
                    _write_atomic(env_fd, AGENT_ENV_FILE, b"", 0o600)
            finally:
                os.close(env_fd)
            return _exists(config_fd, COMPOSE_FILE)
        finally:
            os.close(config_fd)


def _read_small(dir_fd: int, name: str) -> str:
    """A regular file's text (never a link, FIFO or more than MAX_REQUEST bytes), else ""."""
    try:
        return _read_regular(dir_fd, name, Path(name)) or ""
    except (OSError, Refused):
        return ""


def _read_regular(dir_fd: int, name: str, shown: Path) -> str | None:
    """A regular file's text, or None if there is none. Refuses a link, a FIFO or other
    non-regular file, and a file over MAX_REQUEST bytes (never read in part). Bytes that
    are not UTF-8 survive a round trip through ``encode("utf-8", "surrogateescape")``."""
    try:
        mode = os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode
    except FileNotFoundError:
        return None
    link = Refused(f"{_show(str(shown))} is a link; Orama only changes real files.")
    if stat.S_ISLNK(mode):
        raise link
    if not stat.S_ISREG(mode):  # checked before opening: opening a device or FIFO can act
        raise Refused(f"{_show(str(shown))} is not a regular file.")
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise link from None
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):  # swapped since the check above
            raise Refused(f"{_show(str(shown))} is not a regular file.")
        chunks, size = [], 0
        while chunk := os.read(fd, MAX_REQUEST + 1 - size):
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_REQUEST:
                raise Refused(f"{_show(str(shown))} is too large for Orama to change.")
    finally:
        os.close(fd)
    return b"".join(chunks).decode("utf-8", "surrogateescape")


def _unlink(dir_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=dir_fd)
    except FileNotFoundError:
        pass


def _exists(dir_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _expect_keys(args: dict[str, Any], keys: set[str]) -> None:
    if set(args) != keys:
        wanted = ", ".join(sorted(keys)) or "no arguments"
        raise Refused(f"This command takes {wanted}.")


# ---------------------------------------------------------------- protocol


class _Bad(Exception):
    """A malformed request; the message goes back to the client."""


def _parse(line: bytes) -> tuple[str, str, dict[str, Any]]:
    try:
        req = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise _Bad("request is not valid JSON") from None
    except RecursionError:  # deeply nested input
        raise _Bad("malformed request") from None
    if not isinstance(req, dict):
        raise _Bad("request is not a JSON object")
    rid, cmd, args = req.get("id"), req.get("cmd"), req.get("args", {})
    if not isinstance(rid, str) or not rid or len(rid) > _ID_MAX:
        raise _Bad("request id must be a short string")
    if not isinstance(cmd, str):
        raise _Bad("request cmd must be a string")
    if not isinstance(args, dict):
        raise _Bad("request args must be an object")
    return rid, cmd, args


class _Handler(socketserver.StreamRequestHandler):
    server: AgentServer

    def setup(self) -> None:
        self.timeout = self.server.read_timeout
        super().setup()

    def handle(self) -> None:
        while True:
            try:
                line = self.rfile.readline(MAX_REQUEST + 1)
            except (TimeoutError, OSError):
                return
            if not line:
                return
            if len(line) > MAX_REQUEST or not line.endswith(b"\n"):
                if len(line) > MAX_REQUEST:
                    log.warning("refused a request: too large")
                    self._send({"id": None, "ok": False, "error": "request too large"})
                return  # oversized or truncated: drop the connection
            self._send(self.server.respond(line))

    def _send(self, response: dict[str, Any]) -> None:
        try:
            self.wfile.write(json.dumps(response).encode() + b"\n")
            self.wfile.flush()
        except OSError:
            pass


class AgentServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(
        self,
        roots: Roots,
        runner: Any,
        socket_path: str = DEFAULT_SOCKET,
        *,
        chown: Chown | None = None,
        read_timeout: float = READ_TIMEOUT,
    ) -> None:
        if chown is None:
            chown = os.chown if os.geteuid() == 0 else _skip_chown
        self._chown = chown
        self.roots = roots
        self.read_timeout = read_timeout
        self._connection_slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self.agent = Agent(roots, runner, chown)
        self.socket_path = socket_path
        _remove_stale_socket(socket_path)
        super().__init__(socket_path, _Handler)

    def process_request(self, request, client_address) -> None:
        # Refuse excess clients rather than queueing unbounded handler threads.
        if not self._connection_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._connection_slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connection_slots.release()

    def server_bind(self) -> None:
        old = os.umask(0o777 & ~SOCKET_MODE)  # bind creates the socket 0660 directly
        try:
            super().server_bind()
        finally:
            os.umask(old)
        self._chown(self.socket_path, -1, self.roots.pgid, follow_symlinks=False)

    def respond(self, line: bytes) -> dict[str, Any]:
        """Always an answer: nothing raised while handling a line reaches the handler."""
        try:
            return self._respond(line)
        except Exception as e:  # noqa: BLE001
            log.error("request failed: internal error (%s)", type(e).__name__)
            return {"id": None, "ok": False, "error": "internal error"}

    def _respond(self, line: bytes) -> dict[str, Any]:
        try:
            rid, cmd, args = _parse(line)
        except _Bad as e:
            log.warning("refused a malformed request: %s", e)
            return {"id": None, "ok": False, "error": str(e)}
        shown_id = _show(rid)
        if cmd not in COMMANDS:
            log.warning("unknown command %s id=%s", _show(cmd), shown_id)
            return {"id": rid, "ok": False, "error": "unknown command"}
        try:
            result = self.agent.handle(cmd, args)
        except (Refused, RunnerError) as e:
            # Both messages are written to be safe to show and log.
            log.warning("%s id=%s: refused: %s", cmd, shown_id, e)
            if isinstance(e, RunnerError) and e.detail:
                # Only here, and only redacted: compose's own words, for whoever reads the log.
                detail = redact(e.detail, self.agent.secret_values(cmd, args))
                log.warning("%s id=%s: docker compose said (secrets hidden as ***):\n%s",
                            cmd, shown_id, detail)
            return {"id": rid, "ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001 - never leak details or args to the client
            log.error("%s id=%s: internal error (%s)", cmd, shown_id, type(e).__name__)
            return {"id": rid, "ok": False, "error": "internal error"}
        log.info("%s id=%s: ok", cmd, shown_id)
        return {"id": rid, "ok": True, "result": result}


def _remove_stale_socket(path: str) -> None:
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(mode):
        raise Refused(f"{_show(path)} exists and is not a socket; the Agent will not replace it.")
    os.unlink(path)
