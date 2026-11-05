import errno
import os
import stat
import threading
import time

import pytest
from conftest import make_setup

from orama import state
from orama.setup_model import DesiredSetup


def mode(path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def test_lock_paths(tmp_path):
    assert state.setup_lock(tmp_path) == tmp_path / "setup.lock"
    assert state.apply_lock(tmp_path) == tmp_path / "apply.lock"


def test_locked_creates_a_private_lock_file(tmp_path):
    path = state.setup_lock(tmp_path)
    with state.locked(path):
        assert mode(path) == 0o600
    assert path.is_file()  # kept: removing a lock file races with the next locker


def test_non_blocking_lock_is_busy_while_held(tmp_path):
    path = state.apply_lock(tmp_path)
    with (
        state.locked(path, blocking=False),
        pytest.raises(state.Busy),
        state.locked(path, blocking=False),
    ):
        pass
    with state.locked(path, blocking=False):  # released on exit
        pass


def test_blocking_lock_waits_for_the_holder(tmp_path):
    path = state.setup_lock(tmp_path)
    order = []
    held = threading.Event()

    def holder():
        with state.locked(path):
            held.set()
            time.sleep(0.2)
            order.append("holder done")

    t = threading.Thread(target=holder)
    t.start()
    held.wait(5)
    with state.locked(path):
        order.append("waiter in")
    t.join(5)
    assert order == ["holder done", "waiter in"]


def test_lock_file_is_never_followed(tmp_path):
    target = tmp_path / "target"
    target.write_text("keep me")
    os.chmod(target, 0o644)
    path = state.setup_lock(tmp_path)
    path.symlink_to(target)
    with pytest.raises(OSError) as e, state.locked(path):
        pass
    assert e.value.errno == errno.ELOOP
    assert target.read_text() == "keep me"
    assert mode(target) == 0o644


def test_lock_must_be_a_regular_file(tmp_path):
    path = state.setup_lock(tmp_path)
    os.mkfifo(path)
    with pytest.raises(OSError), state.locked(path):
        pass


def test_save_setup_writes_the_setup(tmp_path):
    setup = make_setup(data_root=str(tmp_path / "data"), config_root=str(tmp_path / "cfg"))
    path = tmp_path / "setup.json"
    state.save_setup(path, setup)
    assert DesiredSetup.load(path) == setup
    assert state.setup_lock(tmp_path).is_file()


def test_save_setup_waits_for_the_setup_lock(tmp_path):
    setup = make_setup(data_root=str(tmp_path / "data"), config_root=str(tmp_path / "cfg"))
    path = tmp_path / "setup.json"
    done = threading.Event()
    with state.locked(state.setup_lock(tmp_path)):
        t = threading.Thread(target=lambda: (state.save_setup(path, setup), done.set()))
        t.start()
        assert not done.wait(0.3)
        assert not path.exists()
    t.join(5)
    assert done.is_set() and path.exists()


def test_update_setup_reloads_applies_and_saves(tmp_path):
    path = tmp_path / "setup.json"
    first = make_setup(data_root=str(tmp_path / "data"), config_root=str(tmp_path / "cfg"))
    state.save_setup(path, first)
    newer = first.model_copy(update={"timezone": "Europe/Amsterdam"})
    state.save_setup(path, newer)  # someone else saved after we loaded `first`
    seen = []

    def add_port(current):
        seen.append(current)
        return current.model_copy(update={"ports": {**current.ports, "plex": 32401}})

    result = state.update_setup(path, add_port)
    assert seen == [newer]  # fn got the file as it is now, not a stale copy
    assert result == DesiredSetup.load(path)
    assert result.timezone == "Europe/Amsterdam"
    assert result.ports["plex"] == 32401


def test_update_setup_holds_the_setup_lock(tmp_path):
    path = tmp_path / "setup.json"
    setup = make_setup(data_root=str(tmp_path / "data"), config_root=str(tmp_path / "cfg"))
    state.save_setup(path, setup)
    held = []

    def check(current):
        try:
            with state.locked(state.setup_lock(tmp_path), blocking=False):
                held.append(False)
        except state.Busy:
            held.append(True)
        return current

    state.update_setup(path, check)
    assert held == [True]
