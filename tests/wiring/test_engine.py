import pytest

from orama.wiring.engine import (
    Step,
    StepError,
    StepResult,
    format_checklist,
    run_steps,
    wait_until,
)
from orama.wiring.http import Unreachable


def test_runs_in_order_and_statuses():
    order = []

    def mk(n, ret):
        def run():
            order.append(n)
            return ret

        return run

    res = run_steps([Step("a", "sonarr", mk("a", "did a")), Step("b", "radarr", mk("b", None))])
    assert order == ["a", "b"]
    assert [r.status for r in res] == ["changed", "unchanged"]
    assert res[0].message == "did a"


def test_failed_step_skips_dependents_transitively():
    def boom():
        raise StepError("Sonarr rejected the key")

    ran = []
    res = run_steps(
        [
            Step("s1", "sonarr", boom),
            Step("s2", "prowlarr", lambda: ran.append("s2"), needs=("s1",)),
            Step("s3", "prowlarr", lambda: ran.append("s3"), needs=("s2",)),
            Step("s4", "radarr", lambda: "ok"),
        ]
    )
    assert ran == []
    assert [r.status for r in res] == ["failed", "skipped", "skipped", "changed"]
    assert res[0].message == "Sonarr rejected the key"
    assert res[1].message == "waiting for Sonarr"
    assert res[2].message == "waiting for Sonarr"


def test_exception_text_never_in_message():
    def boom():
        raise ValueError("secret-key-123")

    [r] = run_steps([Step("a", "sabnzbd", boom)])
    assert r.status == "failed"
    assert "secret-key-123" not in r.message
    assert r.message == "unexpected error in SABnzbd (ValueError)"


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += s


def test_wait_until_times_out_with_exact_message():
    clock = FakeClock()
    with pytest.raises(StepError) as exc:
        wait_until(lambda: False, app="sonarr", timeout=10, interval=3, clock=clock, sleep=clock.sleep)
    assert str(exc.value) == "Sonarr is still starting — run apply again in a few minutes."
    assert clock.now >= 10


def test_wait_until_treats_unreachable_as_not_ready_then_succeeds():
    clock = FakeClock()
    n = {"i": 0}

    def check():
        n["i"] += 1
        if n["i"] < 3:
            raise Unreachable("http://x")
        return True

    wait_until(check, app="radarr", timeout=60, clock=clock, sleep=clock.sleep)
    assert n["i"] == 3
    assert clock.sleeps == [3.0, 3.0]


def test_checklist_formatting():
    results = [
        StepResult("a", "sabnzbd", "changed", "added categories"),
        StepResult("b", "sonarr", "unchanged", ""),
        StepResult("c", "qbittorrent", "failed", "wrong password"),
        StepResult("d", "prowlarr", "skipped", "waiting for Sonarr"),
    ]
    assert format_checklist(results) == [
        "✓ SABnzbd: added categories",
        "✗ qBittorrent: wrong password",
        "… Prowlarr: skipped, waiting for Sonarr",
    ]


def test_checklist_nothing_to_change():
    assert format_checklist([StepResult("b", "sonarr", "unchanged", "")]) == ["✓ Nothing to change"]
    assert format_checklist([]) == ["✓ Nothing to change"]


def test_skipped_step_names_the_root_cause_app_through_the_chain():
    res = run_steps(
        [
            Step("wait:sonarr", "sonarr", lambda: (_ for _ in ()).throw(StepError("still starting"))),
            Step("prowlarr:app:sonarr", "prowlarr", lambda: "x", needs=("wait:sonarr",)),
            Step("prowlarr:indexer", "prowlarr", lambda: "x", needs=("prowlarr:app:sonarr",)),
            Step("prowlarr:sync", "prowlarr", lambda: "x", needs=("prowlarr:indexer",)),
        ]
    )
    assert [r.message for r in res[1:]] == ["waiting for Sonarr"] * 3
    assert format_checklist(res)[-1] == "… Prowlarr: skipped, waiting for Sonarr"


def test_a_step_can_skip_itself_and_its_dependents_say_why_once():
    from orama.wiring.engine import StepSkipped

    def wait():
        raise StepSkipped("Plex is waiting for sign-in")

    ran = []
    res = run_steps(
        [
            Step("p1", "plex", wait),
            Step("p2", "plex", lambda: ran.append("p2"), needs=("p1",)),
            Step("s1", "sonarr", lambda: None),
        ]
    )
    assert ran == []
    assert [r.status for r in res] == ["skipped", "skipped", "unchanged"]
    assert res[0].message == res[1].message == "Plex is waiting for sign-in"
    # Shown even when nothing else changed, and only once.
    assert format_checklist(res) == ["… Plex: skipped, Plex is waiting for sign-in"]
