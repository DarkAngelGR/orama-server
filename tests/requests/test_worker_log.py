"""The worker log stays quiet: one line per kind of failure, back-off, no request data."""
import logging

from orama.requests.client import RequestError
from orama.requests.store import StoreError
from orama.requests.worker import RequestWorker


def _worker(clock):
    w = RequestWorker(store=None, client_factory=lambda: None, clock=lambda: 0.0)
    w._mono = clock
    return w


def _lines(caplog, level=logging.INFO):
    return [r for r in caplog.records if r.levelno >= level and r.name.endswith("worker")]


def test_a_lasting_failure_is_logged_once_a_minute(caplog):
    now = [0.0]
    w = _worker(lambda: now[0])
    caplog.set_level(logging.DEBUG)
    for _ in range(20):
        w._note_tick(StoreError("The request database could not be used."))
        now[0] += 2.0
    assert len(_lines(caplog, logging.WARNING)) == 1
    now[0] += 60.0
    w._note_tick(StoreError("The request database could not be used."))
    assert len(_lines(caplog, logging.WARNING)) == 2


def test_a_new_kind_of_failure_and_the_recovery_are_logged(caplog):
    now = [0.0]
    w = _worker(lambda: now[0])
    caplog.set_level(logging.INFO)
    w._note_tick(StoreError("The request database could not be used."))
    w._note_tick(KeyError("secret-title"))
    assert len(_lines(caplog, logging.WARNING)) == 2
    w._note_tick(None)
    w._note_tick(None)
    recovered = [r for r in _lines(caplog) if r.levelno == logging.INFO]
    assert len(recovered) == 1
    assert all("secret-title" not in r.getMessage() for r in caplog.records)


def test_store_error_text_is_logged_but_other_messages_are_not(caplog):
    w = _worker(lambda: 0.0)
    caplog.set_level(logging.INFO)
    w._note_tick(StoreError("The request database could not be written because the disk is full."))
    assert "disk is full" in caplog.text
    w._note_tick(ValueError("secret-title"))
    assert "secret-title" not in caplog.text and "ValueError" in caplog.text


def test_an_unreachable_app_is_not_a_warning(caplog):
    w = _worker(lambda: 0.0)
    caplog.set_level(logging.DEBUG)
    for _ in range(5):
        w._note_tick(RequestError("could not reach http://radarr secret-title"))
    assert not _lines(caplog, logging.WARNING)
    assert "secret-title" not in " ".join(r.getMessage() for r in caplog.records)


def test_the_wait_grows_after_failures_and_resets(caplog):
    w = _worker(lambda: 0.0)
    waits = [w._note_tick(KeyError("x")) for _ in range(8)]
    assert waits[0] == w.poll_interval
    assert waits[1] > waits[0] and waits[-1] > waits[2]
    assert max(waits) <= 60.0
    assert w._note_tick(None) == w.poll_interval
