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
import secrets
import socketserver
import stat
import threading
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from orama.catalog import CATALOG
from orama.compose import dump_yaml
from orama.layout import app_config_dir, app_env_dir, data_folders
from orama_agent.runner import AGENT_ENV_FILE, COMPOSE_FILE, RunnerError, project_file
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
MAX_PATHS = 64
READ_TIMEOUT = 30.0
SOCKET_MODE = 0o660
SHARED_DIR_MODE = 0o2775  # data folders and app config folders, PUID:PGID
AGENT_DIR_MODE = 0o755  # <config>/config, root-owned
ENV_DIR_MODE = 0o700  # <config>/app-env, root-owned
APPLY_MARKER = ".apply-incomplete"  # in app-env while an up is running or after it failed
_ID_MAX = 128

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_NEW_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC

Chown = Callable[..., None]


def _skip_chown(*_args: Any, **_kw: Any) -> None:
    """Not root: owners cannot be changed, and need not be (tests, development)."""


def _show(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= _ID_MAX else text[: _ID_MAX - 3] + "..."


# ---------------------------------------------------------------- safe file system


def _open_child_dir(parent_fd: int, name: str, shown: Path) -> int:
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


def _write_atomic(dir_fd: int, name: str, data: bytes, mode: int) -> None:
    """Replace ``name`` in ``dir_fd`` atomically. A link at ``name`` is replaced, not followed."""
    tmp = f".{name}.{secrets.token_hex(8)}.tmp"
    fd = os.open(tmp, _NEW_FILE_FLAGS, mode, dir_fd=dir_fd)
    try:
        try:
            os.fchmod(fd, mode)  # mode from creation is reduced by umask
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
    # Single-quoted: compose takes the value literally (validate_env_files refuses quotes).
    return "".join(f"{key}='{value}'\n" for key, value in values.items()).encode()


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
        }[cmd]
        with self._lock:
            return handler(args)

    # ------------------------------------------------------------ ensure-folders

    def ensure_folders(self, args: dict[str, Any]) -> list[str]:
        _expect_keys(args, {"paths"})
        paths = args["paths"]
        if not isinstance(paths, list) or len(paths) > MAX_PATHS:
            raise Refused(f"The folder list must be a list of at most {MAX_PATHS} paths.")
        for p in paths:
            if not isinstance(p, str) or p not in self._shared:
                raise Refused(f"The folder {_show(p)} is not one Orama may create.")
        created: list[str] = []
        for p in paths:
            created += self._make_dirs(Path(p))
        return created

    def _make_dirs(self, target: Path) -> list[str]:
        root = self.roots.data if target.is_relative_to(self.roots.data) else self.roots.config
        created = []
        fd = os.open(root, _DIR_FLAGS)
        try:
            current = root
            for part in target.relative_to(root).parts:
                current = current / part
                shared = str(current) in self._shared
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                    made = True
                except FileExistsError:
                    made = False
                child = _open_child_dir(fd, part, current)
                os.close(fd)
                fd = child
                if made:
                    if shared:
                        self._chown(fd, self.roots.puid, self.roots.pgid)
                    os.fchmod(fd, SHARED_DIR_MODE if shared else AGENT_DIR_MODE)
                    created.append(str(current))
        finally:
            os.close(fd)
        return created

    # ------------------------------------------------------------ apply

    def apply(self, args: dict[str, Any]) -> dict[str, list[str]]:
        _expect_keys(args, {"compose", "env_files"})
        doc, env_files = args["compose"], args["env_files"]
        validate_compose(doc, self.roots)
        validate_env_files(env_files, self.roots)
        validate_secrets_present(doc, env_files)

        config_fd = os.open(self.roots.config, _DIR_FLAGS)
        try:
            env_fd = self._open_env_dir(config_fd)
            try:
                _write_atomic(env_fd, AGENT_ENV_FILE, b"", 0o600)
                for app, values in env_files.items():
                    _write_atomic(env_fd, f"{app}.env", _env_file_text(values), 0o600)
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
        """Every env-file value this request carried or the Agent has stored, for redaction."""
        values: list[str] = []
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

    def restart(self, args: dict[str, Any]) -> None:
        _expect_keys(args, {"app"})
        app = args["app"]
        if not isinstance(app, str) or app not in CATALOG:
            raise Refused(f"{_show(app)} is not an app Orama knows about.")
        self._ensure_agent_env_file()
        self.runner.restart(app)

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
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     dir_fd=dir_fd)
    except OSError:
        return ""
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return ""
        return os.read(fd, MAX_REQUEST).decode("utf-8", "replace")
    finally:
        os.close(fd)


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
        self.agent = Agent(roots, runner, chown)
        self.socket_path = socket_path
        _remove_stale_socket(socket_path)
        super().__init__(socket_path, _Handler)

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
        if cmd == "self-update":
            log.info("%s id=%s: not implemented", cmd, shown_id)
            return {"id": rid, "ok": False, "error": "not implemented in Plan 1"}
        if cmd not in {"ensure-folders", "apply", "status", "restart"}:
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
