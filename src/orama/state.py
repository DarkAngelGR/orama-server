"""Locks for Orama's shared state files.

The CLI ``apply``, the web app (same container) and ``bootstrap`` (root, in a throwaway
container on the same host folder) can all write the setup and the secret store. Each write
is an atomic rename, but read-modify-write sequences need a lock or one writer's change is
lost. These are advisory ``fcntl.flock`` locks on small 0600 files in the state folder:

- ``setup.lock``: every write of ``setup.json`` or ``setup.draft.json``.
- ``secrets.lock`` (next to ``secrets.bin``): every write of the secret store.
- ``apply.lock``: one apply (or app test) at a time, across processes.

Lock order, to avoid deadlocks: ``apply.lock`` before ``setup.lock`` or ``secrets.lock``;
never wait for ``apply.lock`` while holding one of the others.

Lock files are opened without following links and are never removed (removing one races
with the next process that opens it). Closing the file releases the lock, also when the
process dies.
"""

from __future__ import annotations

import errno
import fcntl
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from orama.setup_model import DesiredSetup

SETUP_LOCK = "setup.lock"
APPLY_LOCK = "apply.lock"
LOCK_MODE = 0o600
# O_NONBLOCK: never hang opening a FIFO planted where the lock should be.
_LOCK_FLAGS = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


class Busy(Exception):
    """A non-blocking lock is held by someone else."""


def setup_lock(state_dir: Path | str) -> Path:
    return Path(state_dir) / SETUP_LOCK


def apply_lock(state_dir: Path | str) -> Path:
    return Path(state_dir) / APPLY_LOCK


def open_lock_file(path: Path | str, *, dir_fd: int | None = None) -> int:
    """Open (creating 0600) the lock file at ``path``; never through a link.

    Raises OSError (ELOOP for a link) or, for anything but a regular file, OSError EINVAL.
    """
    fd = os.open(path, _LOCK_FLAGS, LOCK_MODE, dir_fd=dir_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "not a regular file", str(path))
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextmanager
def hold(fd: int, *, blocking: bool = True) -> Iterator[None]:
    """Hold an exclusive flock on ``fd`` for the block, then close ``fd``.

    Takes ownership of ``fd``: it is closed on exit, also when the lock is busy.
    """
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            raise Busy() from None
        yield
    finally:
        os.close(fd)  # releases the lock


@contextmanager
def locked(path: Path, *, blocking: bool = True) -> Iterator[None]:
    """Hold the lock at ``path`` (a 0600 file, created if missing) for the block.

    With ``blocking=False`` raises :class:`Busy` at once when it is held elsewhere.
    """
    with hold(open_lock_file(path), blocking=blocking):
        yield


def save_setup(path: Path, setup: DesiredSetup) -> None:
    """Save ``setup`` to ``path`` while holding ``setup.lock`` in the same folder."""
    path = Path(path)
    with locked(setup_lock(path.parent)):
        setup.save(path)
