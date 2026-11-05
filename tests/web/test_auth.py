"""Sign-in, sessions, the first-run password, rate limits and the permission model."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from argon2 import PasswordHasher

from orama.secrets import SecretStore
from orama.web import auth, create_app

from .conftest import (
    DAY,
    FIFTEEN_MINUTES,
    csrf_from,
    ip,
    new_password,
    session_cookie,
    signed_in,
    signin,
)

RATE_MESSAGE = "Too many wrong tries. Wait 15 minutes, then try again."


def addr(n: int) -> dict:
    return {"environ_base": {"REMOTE_ADDR": ip(203, 0, 113, n)}}


# ---------------------------------------------------------------- sessions


def _cookie_header(response) -> str:
    cookies = [v for k, v in response.headers.items() if k == "Set-Cookie"]
    assert len(cookies) == 1, cookies
    return cookies[0]


def test_cookie_flags_http(client, password):
    header = _cookie_header(client.get("/signin"))
    assert header.startswith("orama_session=")
    assert "HttpOnly" in header
    assert "SameSite=Strict" in header
    assert "Path=/" in header
    assert "Secure" not in header
    response = signin(client, password)
    header = _cookie_header(response)
    assert "HttpOnly" in header and "SameSite=Strict" in header and "Secure" not in header


def test_cookie_secure_over_https(client, password):
    https = {"base_url": "https://localhost"}
    header = _cookie_header(client.get("/signin", **https))
    assert "Secure" in header and "HttpOnly" in header and "SameSite=Strict" in header
    response = signin(client, password, **https)
    assert response.status_code == 303
    assert "Secure" in _cookie_header(response)


def test_session_cookie_is_long_random(client, password):
    client.get("/signin")
    value = session_cookie(client)
    assert value and len(value) >= 43


def test_session_id_changes_at_sign_in(client, password):
    client.get("/signin")
    before = session_cookie(client)
    response = signin(client, password)
    assert response.status_code == 303
    assert response.headers["Location"] == "/"
    after = session_cookie(client)
    assert after and after != before
    # The old id is gone on the server too.
    stale = client.application.test_client()
    stale.set_cookie("orama_session", before)
    assert not signed_in(stale)
    assert signed_in(client)


def test_signin_page_without_password_points_to_first_run(client):
    response = client.get("/signin")
    assert response.status_code == 303
    assert response.headers["Location"] == "/setup/password"


def test_wrong_password_refused(client, password):
    token = csrf_from(client.get("/signin"))
    response = client.post("/signin", data={"password": password + "x", "csrf_token": token})
    assert response.status_code == 401
    assert "That password is not right." in response.get_data(as_text=True)
    assert password not in response.get_data(as_text=True)
    assert not signed_in(client)


def test_signout_ends_session(client, password):
    signin(client, password)
    old = session_cookie(client)
    token = csrf_from(client.get("/"))
    response = client.post("/signout", data={"csrf_token": token})
    assert response.status_code == 303
    assert response.headers["Location"] == "/signin"
    assert not signed_in(client)
    replay = client.application.test_client()
    replay.set_cookie("orama_session", old)
    assert not signed_in(replay)


def test_idle_and_absolute_timeouts(client, password, clock):
    signin(client, password)
    clock.advance(7 * DAY - 60)
    assert signed_in(client)  # activity resets the idle timer
    clock.advance(7 * DAY + 1)
    assert not signed_in(client)

    signin(client, password)
    for _ in range(4):  # 30 days of activity, never idle for 7
        clock.advance(6 * DAY)
        assert signed_in(client)
    clock.advance(6 * DAY - 1)
    assert signed_in(client)
    clock.advance(1)
    assert not signed_in(client)


def test_password_change_ends_other_sessions(state_dir, clock):
    app = create_app(state_dir, clock=clock)

    @app.post("/test/change")
    def change():
        from flask import request

        ok = auth.change_password(request.form["old"], request.form["new"])
        return ("changed" if ok else "refused"), 200

    pw = new_password()
    app.extensions["orama"].passwords.create(pw)
    mine, other = app.test_client(), app.test_client()
    signin(mine, pw)
    signin(other, pw)
    before = session_cookie(mine)
    token = csrf_from(mine.get("/"))
    new = new_password()
    response = mine.post("/test/change", data={"old": pw + "x", "new": new, "csrf_token": token})
    assert response.data == b"refused"
    assert signed_in(other)
    response = mine.post("/test/change", data={"old": pw, "new": new, "csrf_token": token})
    assert response.data == b"changed"
    assert not signed_in(other)
    assert signed_in(mine)
    assert session_cookie(mine) != before
    assert signin(other, pw).status_code == 401
    assert signin(other, new).status_code == 303


def test_next_is_followed_only_for_local_paths(client, password):
    response = client.get("/?x=1")
    assert response.status_code == 303
    assert response.headers["Location"] == "/signin?next=/?x%3D1"
    for nxt, expected in [
        ("/settings?tab=a", "/settings?tab=a"),
        ("//evil.example/", "/"),
        ("https://evil.example/", "/"),
        ("/\\evil.example", "/"),
        ("javascript:alert(1)", "/"),
        ("", "/"),
        ("/ok\r\nX: y", "/"),
    ]:
        c = client.application.test_client()
        token = csrf_from(c.get("/signin", query_string={"next": nxt}))
        response = c.post(
            "/signin", data={"password": password, "csrf_token": token, "next": "//evil"}
        )
        assert response.status_code == 303, nxt
        assert response.headers["Location"] == expected, nxt


# ---------------------------------------------------------------- first run


def test_first_run_needs_code(client, first_run_code, state_dir):
    for query in ("", "?code="):
        response = client.get("/setup/password" + query)
        assert response.status_code == 200
        text = response.get_data(as_text=True)
        assert "Open the link the installer printed" in text
        assert 'name="password"' not in text
    response = client.get(f"/setup/password?code={first_run_code}")
    assert 'name="password"' in response.get_data(as_text=True)
    # POST without the code (but with a token) creates nothing.
    token = csrf_from(response)
    pw = new_password()
    response = client.post(
        "/setup/password", data={"password": pw, "password_again": pw, "csrf_token": token}
    )
    assert response.status_code == 403
    assert not (state_dir / "auth.json").exists()
    assert (state_dir / "first-run.code").exists()


def _first_run_post(client, code, pw, **kwargs):
    token = csrf_from(client.get("/setup/password", **kwargs))
    return client.post(
        "/setup/password",
        data={"code": code, "password": pw, "password_again": pw, "csrf_token": token},
        **kwargs,
    )


def test_first_run_wrong_code_rate_limited(client, first_run_code, state_dir, clock):
    pw = new_password()
    for _ in range(5):
        response = _first_run_post(client, first_run_code + "x", pw, **addr(1))
        assert response.status_code == 403
        assert "This setup link is not valid" in response.get_data(as_text=True)
    response = _first_run_post(client, first_run_code, pw, **addr(1))
    assert response.status_code == 429
    assert RATE_MESSAGE in response.get_data(as_text=True)
    assert not (state_dir / "auth.json").exists()
    clock.advance(FIFTEEN_MINUTES)
    assert _first_run_post(client, first_run_code, pw, **addr(1)).status_code == 303


def test_first_run_creates_argon2id_hash_and_deletes_code(client, first_run_code, state_dir):
    pw = new_password()
    response = _first_run_post(client, first_run_code, pw)
    assert response.status_code == 303
    assert response.headers["Location"] == "/"
    assert signed_in(client)
    auth_file = state_dir / "auth.json"
    st = auth_file.lstat()
    assert stat.S_ISREG(st.st_mode)
    assert stat.S_IMODE(st.st_mode) == 0o600
    data = json.loads(auth_file.read_text())
    assert set(data) == {"hash"}
    assert data["hash"].startswith("$argon2id$")
    assert pw not in auth_file.read_text()
    PasswordHasher().verify(data["hash"], pw)
    assert not (state_dir / "first-run.code").exists()
    assert not [p for p in state_dir.iterdir() if p.name.endswith(".tmp")]


@pytest.mark.parametrize("length", [0, 11, 257])
def test_short_password_refused(client, first_run_code, state_dir, length):
    pw = ("p" * length)
    response = _first_run_post(client, first_run_code, pw)
    assert response.status_code == 400
    assert "between 12 and 256 characters" in response.get_data(as_text=True)
    assert not (state_dir / "auth.json").exists()
    assert (state_dir / "first-run.code").exists()


def test_password_file_limits(state_dir):
    passwords = auth.PasswordFile(state_dir / "auth.json")
    with pytest.raises(ValueError):
        passwords.create("p" * 11)
    passwords.create("p" * 12)
    with pytest.raises(FileExistsError):
        passwords.create("q" * 12)
    assert passwords.verify("p" * 12)
    assert not passwords.verify("p" * 13)
    assert not passwords.verify("p" * 300)
    assert not passwords.change("wrong-old-password", "r" * 12)
    with pytest.raises(ValueError):
        passwords.change("p" * 12, "short")
    assert passwords.change("p" * 12, "r" * 256)
    assert passwords.verify("r" * 256)


def test_password_rehashed_when_parameters_change(state_dir):
    path = state_dir / "auth.json"
    weak = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1)
    pw = new_password()
    path.write_text(json.dumps({"hash": weak.hash(pw)}))
    path.chmod(0o600)
    passwords = auth.PasswordFile(path)
    assert passwords.verify(pw)
    new_hash = json.loads(path.read_text())["hash"]
    assert not PasswordHasher().check_needs_rehash(new_hash)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_first_run_closed_once_password_exists(client, first_run_code, password, state_dir):
    response = client.get(f"/setup/password?code={first_run_code}")
    assert response.status_code == 303
    assert response.headers["Location"] == "/signin"
    token = csrf_from(client.get("/signin"))
    response = client.post(
        "/setup/password",
        data={"code": first_run_code, "password": "x" * 20, "password_again": "x" * 20,
              "csrf_token": token},
    )
    assert response.status_code == 303
    assert response.headers["Location"] == "/signin"
    assert signin(client, password).status_code == 303


def test_first_run_refused_when_auth_is_a_link(client, first_run_code, state_dir, tmp_path):
    target = tmp_path / "elsewhere.json"
    os.symlink(target, state_dir / "auth.json")
    response = client.get(f"/setup/password?code={first_run_code}")
    assert response.status_code == 303
    assert not target.exists()


def test_first_run_code_link_is_not_read(client, state_dir, tmp_path):
    target = tmp_path / "code"
    target.write_text("known-code-value")
    os.symlink(target, state_dir / "first-run.code")
    response = _first_run_post(client, "known-code-value", new_password())
    assert response.status_code == 403
    assert not (state_dir / "auth.json").exists()


# ---------------------------------------------------------------- rate limits


def _fail(client, password, n):
    return signin(client, password + "-wrong", **addr(n))


def test_five_failures_lock_one_address(client, password):
    for _ in range(5):
        assert _fail(client, password, 1).status_code == 401
    response = signin(client, password, **addr(1))
    assert response.status_code == 429
    assert RATE_MESSAGE in response.get_data(as_text=True)
    assert not signed_in(client, **addr(1))


def test_locked_address_does_not_run_the_hash(client, password, monkeypatch):
    for _ in range(5):
        _fail(client, password, 1)
    called = []
    monkeypatch.setattr(auth.PasswordFile, "verify", lambda self, pw: called.append(1))
    assert signin(client, password, **addr(1)).status_code == 429
    assert called == []


def test_x_forwarded_for_is_ignored(client, password):
    for n in range(5):
        response = signin(
            client, password + "-wrong", headers={"X-Forwarded-For": ip(198, 51, 100, n)},
            **addr(1),
        )
        assert response.status_code == 401
    assert signin(client, password, **addr(1)).status_code == 429


def test_other_address_still_allowed(client, password):
    for _ in range(5):
        _fail(client, password, 1)
    assert signin(client, password, **addr(2)).status_code == 303


def test_global_limit_locks_everyone(client, password, clock):
    for n in range(6):
        for _ in range(5):
            assert _fail(client, password, 10 + n).status_code == 401
    response = signin(client, password, **addr(99))
    assert response.status_code == 429
    assert RATE_MESSAGE in response.get_data(as_text=True)
    clock.advance(FIFTEEN_MINUTES)
    assert signin(client, password, **addr(99)).status_code == 303


def test_lock_clears_after_15_minutes(client, password, clock):
    for _ in range(5):
        _fail(client, password, 1)
    clock.advance(FIFTEEN_MINUTES - 1)
    assert signin(client, password, **addr(1)).status_code == 429
    clock.advance(1)
    assert signin(client, password, **addr(1)).status_code == 303


def test_failures_outside_window_do_not_count(clock):
    limiter = auth.RateLimiter(clock)
    a = ip(203, 0, 113, 5)
    for _ in range(4):
        limiter.fail(a)
    clock.advance(FIFTEEN_MINUTES + 1)
    limiter.fail(a)
    assert not limiter.blocked(a)


# ---------------------------------------------------------------- leaks and model


def test_no_secret_in_any_page(app, client, password, state_dir, first_run_code):
    store = SecretStore(state_dir / "secrets.bin")
    values = [os.urandom(12).hex() for _ in range(5)]
    for i, value in enumerate(values):
        store.set(f"apikey:test{i}", value)
    signin(client, password)
    pages = []
    for rule in app.url_map.iter_rules():
        if "GET" in (rule.methods or ()) and not rule.arguments:
            pages.append(client.get(rule.rule))
    pages.append(client.get("/static/orama.css"))
    assert len(pages) >= 4
    hash_text = (state_dir / "auth.json").read_text()
    for page in pages:
        text = page.get_data(as_text=True)
        for value in values:
            assert value not in text
        assert password not in text
        assert "$argon2" not in text and hash_text not in text
        assert first_run_code not in text


def test_principal_without_permission_gets_403(state_dir, clock):
    app = create_app(state_dir, clock=clock)

    @app.get("/test/apps")
    @auth.requires("apps")
    def apps_page():
        return "apps"

    @app.get("/test/secret")
    @auth.requires("nothing-grants-this")
    def secret_page():
        return "secret"

    client = app.test_client()
    response = client.get("/test/apps")
    assert response.status_code == 303
    assert response.headers["Location"] == "/signin?next=/test/apps"
    pw = new_password()
    app.extensions["orama"].passwords.create(pw)
    signin(client, pw)
    assert client.get("/test/apps").data == b"apps"
    response = client.get("/test/secret")
    assert response.status_code == 403
    assert b"secret" not in response.data
    assert auth.ADMIN == auth.Principal("admin", frozenset({"setup", "settings", "apps"}))


def test_sessions_are_bounded(clock):
    sessions = auth.Sessions(clock)
    anonymous = [sessions.new(None, "a") for _ in range(auth.MAX_ANONYMOUS_SESSIONS + 10)]
    assert sessions.get(anonymous[0].id, "a") is None
    assert sessions.get(anonymous[-1].id, "a") is not None
    admin = sessions.new(auth.ADMIN, "a")
    for _ in range(auth.MAX_ANONYMOUS_SESSIONS + 10):
        sessions.new(None, "a")
    assert sessions.get(admin.id, "a") is not None


def test_main_serves_with_waitress(monkeypatch, state_dir: Path):
    import orama.__main__ as main_module

    calls = {}
    monkeypatch.setenv("ORAMA_STATE_DIR", str(state_dir))
    monkeypatch.setattr(
        main_module.waitress, "serve", lambda app, **kw: calls.update(app=app, **kw)
    )
    main_module.main()
    assert calls["host"] == "0.0.0.0"
    assert calls["port"] == 8765
    assert calls["threads"] == 8
    assert calls["ident"] is None
    assert calls["max_request_body_size"] == 1024 * 1024
    assert calls["app"].extensions["orama"].state_dir == state_dir


def test_first_run_link_with_wrong_code_shows_no_form_and_counts(client, first_run_code):
    for _ in range(5):
        response = client.get(f"/setup/password?code={first_run_code}x", **addr(3))
        assert response.status_code == 403
        text = response.get_data(as_text=True)
        assert 'name="password"' not in text
        assert first_run_code not in text
    response = client.get(f"/setup/password?code={first_run_code}", **addr(3))
    assert response.status_code == 429
    assert 'name="password"' not in response.get_data(as_text=True)


# ---------------------------------------------------------------- fix round 1


def test_cookie_replayed_from_another_address_is_refused(app, password):
    owner = app.test_client()
    signin(owner, password, **addr(1))
    stolen = session_cookie(owner)
    thief = app.test_client()
    thief.set_cookie("orama_session", stolen)
    response = thief.get("/", **addr(2))
    assert response.status_code == 303
    assert response.headers["Location"].startswith("/signin")
    # The session is ended server-side: the owner is signed out too.
    assert not signed_in(owner, **addr(1))


def test_session_keeps_working_from_the_same_address(app, password):
    owner = app.test_client()
    signin(owner, password, **addr(1))
    assert signed_in(owner, **addr(1))
    assert signed_in(owner, **addr(1))
    token = csrf_from(owner.get("/", **addr(1)))
    assert owner.post("/signout", data={"csrf_token": token}, **addr(1)).status_code == 303
    assert not signed_in(owner, **addr(1))


def test_pre_signin_session_bound_to_address(app, password):
    c = app.test_client()
    token = csrf_from(c.get("/signin", **addr(1)))
    response = c.post("/signin", data={"password": password, "csrf_token": token}, **addr(2))
    assert response.status_code == 403
    # Ended: the token no longer works from the first address either.
    response = c.post("/signin", data={"password": password, "csrf_token": token}, **addr(1))
    assert response.status_code == 403


def test_password_change_still_works_with_address_binding(state_dir, clock):
    app = create_app(state_dir, clock=clock)

    @app.post("/test/change")
    def change():
        from flask import request

        return ("changed" if auth.change_password(request.form["old"], request.form["new"])
                else "refused"), 200

    pw, new = new_password(), new_password()
    app.extensions["orama"].passwords.create(pw)
    c = app.test_client()
    signin(c, pw, **addr(4))
    token = csrf_from(c.get("/", **addr(4)))
    response = c.post("/test/change", data={"old": pw, "new": new, "csrf_token": token},
                      **addr(4))
    assert response.data == b"changed"
    assert signed_in(c, **addr(4))
    assert not signed_in(c, **addr(5))


def _concurrent(clients_and_calls):
    import threading

    barrier = threading.Barrier(len(clients_and_calls))
    results = []

    def run(call):
        barrier.wait()
        results.append(call())

    threads = [threading.Thread(target=run, args=(call,)) for call in clients_and_calls]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    return results


def test_concurrent_wrong_passwords_verify_at_most_five(app, password, monkeypatch):
    import threading
    import time as _time

    lock, count = threading.Lock(), []

    def slow_wrong(self, pw):
        with lock:
            count.append(1)
        _time.sleep(0.05)
        return False

    calls = []
    for _ in range(20):
        c = app.test_client()
        token = csrf_from(c.get("/signin", **addr(7)))
        calls.append(lambda c=c, token=token: c.post(
            "/signin", data={"password": "wrong-" + token, "csrf_token": token}, **addr(7)
        ).status_code)
    monkeypatch.setattr(auth.PasswordFile, "verify", slow_wrong)
    results = _concurrent(calls)
    assert len(count) <= 5
    assert sorted(set(results)) == [401, 429]
    assert results.count(401) == len(count)


def test_concurrent_wrong_codes_checked_at_most_five(app, first_run_code, monkeypatch):
    import threading
    import time as _time

    lock, count = threading.Lock(), []

    def slow_no(path, code):
        with lock:
            count.append(1)
        _time.sleep(0.05)
        return False

    calls = []
    for _ in range(20):
        c = app.test_client()
        token = csrf_from(c.get("/setup/password", **addr(8)))
        calls.append(lambda c=c, token=token: c.post(
            "/setup/password",
            data={"code": "bad", "password": "p" * 20, "csrf_token": token}, **addr(8),
        ).status_code)
    monkeypatch.setattr(auth, "code_matches", slow_no)
    results = _concurrent(calls)
    assert len(count) <= 5
    assert results.count(403) == len(count)


def test_success_does_not_count_as_failure(clock):
    limiter = auth.RateLimiter(clock)
    a = ip(203, 0, 113, 9)
    for _ in range(10):
        assert limiter.try_begin(a)
        limiter.finish(a, failed=False)
    assert not limiter.blocked(a)
    for _ in range(5):
        assert limiter.try_begin(a)
        limiter.finish(a, failed=True)
    assert not limiter.try_begin(a)


def test_setup_reader_retries_after_a_bad_file(state_dir, client):
    from .conftest import LAN, write_setup

    good = (state_dir / "setup.json").read_text()
    with open(state_dir / "setup.json", "w") as f:  # same inode and size, not JSON
        f.write("{" + "x" * (len(good.encode()) - 1))
    assert client.get("/healthz", headers={"Host": LAN}).status_code == 400
    # Same inode, mtime and size are possible on fast filesystems; content is re-read
    # because a failed parse is never cached.
    st = os.stat(state_dir / "setup.json")
    with open(state_dir / "setup.json", "w") as f:
        f.write(good)
    os.utime(state_dir / "setup.json", ns=(st.st_atime_ns, st.st_mtime_ns))
    assert client.get("/healthz", headers={"Host": LAN}).status_code == 200
    write_setup(state_dir)


class _HookLock:
    """A lock that runs ``hook`` after every release (to look into the gaps between
    locked steps, deterministically)."""

    def __init__(self) -> None:
        import threading

        self._lock = threading.Lock()
        self.hook = None

    def __enter__(self):
        self._lock.acquire()
        return self

    def __exit__(self, *exc):
        self._lock.release()
        hook, self.hook = self.hook, None  # once, and never from inside the hook
        if hook is not None:
            hook()
            self.hook = hook
        return False


@pytest.mark.parametrize("scope", ["address", "global"])
def test_finish_records_the_failure_with_no_gap(clock, scope):
    limiter = auth.RateLimiter(clock)
    a = ip(203, 0, 113, 30)
    if scope == "address":
        for _ in range(4):
            limiter.fail(a)
    else:
        for n in range(29):
            limiter.fail(ip(198, 51, 100, n))  # 29 failures overall, none locked here
    assert limiter.try_begin(a)  # the 5th (or 30th) check is now running
    lock = _HookLock()
    limiter._lock = lock
    seen = []
    lock.hook = lambda: seen.append(limiter.try_begin(ip(203, 0, 113, 30)))
    limiter.finish(a, failed=True)
    lock.hook = None
    # After every release inside finish, a parallel check must already be refused.
    assert seen and not any(seen)
