import base64
import hashlib
import logging
import multiprocessing
import re
import stat

import pytest
from conftest import make_setup, random_secret

from orama.secrets import (
    MissingSecret,
    RedactingFilter,
    SecretStore,
    ensure_app_keys,
    env_files_for,
    generate_api_key,
    install_redaction,
    qbittorrent_pbkdf2,
)


def fake_secret(seed: str = "f00dfeed") -> str:
    # Built at runtime so no key-shaped string lands in tracked files.
    return seed * 4


def mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_store_file_is_encrypted_and_private(tmp_path):
    st = SecretStore(tmp_path / "secrets.bin")
    value = random_secret()
    st.set("apikey:sonarr", value)
    raw = (tmp_path / "secrets.bin").read_bytes()
    assert value.encode() not in raw
    assert mode(tmp_path / "secrets.bin") == 0o600
    assert mode(tmp_path / "secrets.key") == 0o600


def test_roundtrip_across_instances_and_names_values(tmp_path):
    p = tmp_path / "secrets.bin"
    SecretStore(p).set("a", "one-value")
    st = SecretStore(p)
    st.set("b", "two-value")
    assert st.get("a") == "one-value"
    assert st.get("missing") is None
    assert sorted(SecretStore(p).names()) == ["a", "b"]
    assert sorted(SecretStore(p).values()) == ["one-value", "two-value"]


def test_missing_parent_dir_created_private(tmp_path):
    st = SecretStore(tmp_path / "sub" / "dir" / "s.bin")
    st.set("a", "valuevalue")
    assert mode(tmp_path / "sub" / "dir") == 0o700
    assert mode(tmp_path / "sub" / "dir" / "s.bin") == 0o600


def test_no_temp_files_left_behind(tmp_path):
    st = SecretStore(tmp_path / "s.bin")
    st.set("a", "valuevalue")
    st.set("a", "valuevalue2")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["s.bin", "s.key", "s.lock"]


def test_key_file_not_overwritten(tmp_path):
    p = tmp_path / "s.bin"
    SecretStore(p).set("a", "valuevalue")
    key = (tmp_path / "s.key").read_bytes()
    SecretStore(p).set("b", "valuevalue")
    assert (tmp_path / "s.key").read_bytes() == key


def _set_many(path, prefix, count, start):
    store = SecretStore(path)
    start.wait(60)  # both processes write at the same time
    for i in range(count):
        store.set(f"{prefix}:{i}", f"{prefix}-value-{i}")


def test_parallel_secret_writes_keep_every_value(tmp_path):
    path = tmp_path / "secrets.bin"
    ctx = multiprocessing.get_context("spawn")
    start = ctx.Barrier(2)
    workers = [
        ctx.Process(target=_set_many, args=(path, p, 50, start)) for p in ("one", "two")
    ]
    for w in workers:
        w.start()
    for w in workers:
        w.join(120)
        assert w.exitcode == 0
    names = SecretStore(path).names()
    assert len(names) == 100
    assert set(names) == {f"{p}:{i}" for p in ("one", "two") for i in range(50)}


def test_store_writes_take_the_store_lock(tmp_path, monkeypatch):
    from orama import secrets as secrets_module

    seen = []
    real = secrets_module.state.locked

    def spy(path, **kw):
        seen.append(path)
        return real(path, **kw)

    monkeypatch.setattr(secrets_module.state, "locked", spy)
    st = SecretStore(tmp_path / "secrets.bin")
    st.set("a", "valuevalue")
    st.delete("a")
    assert seen == [tmp_path / "secrets.lock"] * 2
    assert mode(tmp_path / "secrets.lock") == 0o600


def test_store_path_must_not_end_in_lock(tmp_path):
    with pytest.raises(ValueError):
        SecretStore(tmp_path / "secrets.lock")


def test_delete_removes_one_name_only(tmp_path):
    p = tmp_path / "secrets.bin"
    st = SecretStore(p)
    st.set("a", "one-value")
    st.set("b", "two-value")
    st.delete("a")
    assert SecretStore(p).names() == ["b"]
    assert SecretStore(p).get("b") == "two-value"
    assert mode(p) == 0o600


def test_delete_missing_is_noop(tmp_path):
    p = tmp_path / "secrets.bin"
    st = SecretStore(p)
    st.delete("never-there")  # no store yet: nothing to do, nothing created
    assert not p.exists()
    st.set("a", "one-value")
    before = p.read_bytes()
    st.delete("never-there")
    assert p.read_bytes() == before
    assert st.names() == ["a"]


def test_wrong_key_raises_clear_error(tmp_path):
    p = tmp_path / "s.bin"
    SecretStore(p).set("a", "valuevalue")
    (tmp_path / "s.key").unlink()
    with pytest.raises(ValueError, match="decrypt"):
        SecretStore(p).get("a")


def test_generated_key_format():
    assert re.fullmatch(r"[0-9a-f]{32}", generate_api_key())
    assert generate_api_key() != generate_api_key()


def test_ensure_app_keys_is_idempotent(tmp_path):
    st = SecretStore(tmp_path / "s.bin")
    ensure_app_keys(st, ["sonarr", "radarr", "prowlarr", "bazarr", "plex"])
    assert sorted(st.names()) == ["apikey:bazarr", "apikey:prowlarr", "apikey:radarr", "apikey:sonarr", "plex:client_id"]
    before = {n: st.get(n) for n in st.names()}
    ensure_app_keys(st, ["sonarr", "radarr", "prowlarr"])
    assert {n: st.get(n) for n in st.names()} == before
    for name, value in before.items():
        if name != "plex:client_id":
            assert re.fullmatch(r"[0-9a-f]{32}", value)


def test_ensure_app_keys_never_overwrites_existing(tmp_path):
    st = SecretStore(tmp_path / "s.bin")
    existing = random_secret()
    st.set("apikey:sonarr", existing)
    ensure_app_keys(st, ["sonarr", "radarr"])
    assert st.get("apikey:sonarr") == existing
    assert st.get("apikey:radarr") is not None


def test_env_files_for_enabled_apps_only(tmp_path):
    st = SecretStore(tmp_path / "s.bin")
    setup = make_setup(removed_apps={"radarr"})
    ensure_app_keys(st, ["sonarr", "radarr", "prowlarr"])
    envs = env_files_for(setup, st)
    assert envs == {
        "sonarr": {"SONARR__AUTH__APIKEY": st.get("apikey:sonarr")},
        "prowlarr": {"PROWLARR__AUTH__APIKEY": st.get("apikey:prowlarr")},
    }


def test_env_files_for_gluetun(tmp_path):
    from orama.setup_model import VpnConfig

    st = SecretStore(tmp_path / "s.bin")
    ensure_app_keys(st, ["sonarr", "radarr", "prowlarr", "qbittorrent"])
    st.set("vpn:wireguard_private_key", fake_secret("ab"))
    setup = make_setup(download_methods={"torrent"}, vpn=VpnConfig())
    envs = env_files_for(setup, st)
    assert envs["gluetun"] == {
        "WIREGUARD_PRIVATE_KEY": fake_secret("ab"),
        "QBT_WEBUI_PASSWORD": st.get("password:qbittorrent"),
    }
    assert "qbittorrent" not in envs and "bazarr" not in envs


VPN_APPS = ["sonarr", "radarr", "prowlarr", "gluetun", "qbittorrent"]


def test_ensure_app_keys_creates_proxy_password_only_when_asked(tmp_path):
    st = SecretStore(tmp_path / "s.bin")
    ensure_app_keys(st, VPN_APPS)
    assert st.get("password:gluetun-proxy") is None
    ensure_app_keys(st, ["sonarr"], gluetun_proxy=True)  # no gluetun: no proxy
    assert st.get("password:gluetun-proxy") is None
    ensure_app_keys(st, VPN_APPS, gluetun_proxy=True)
    first = st.get("password:gluetun-proxy")
    assert first is not None and re.fullmatch(r"[A-Za-z0-9_-]{32}", first)
    ensure_app_keys(st, VPN_APPS, gluetun_proxy=True)
    assert st.get("password:gluetun-proxy") == first


@pytest.mark.parametrize("route", [True, False])
def test_env_files_for_gluetun_proxy_password(tmp_path, route):
    from orama.setup_model import VpnConfig

    st = SecretStore(tmp_path / "s.bin")
    st.set("vpn:wireguard_private_key", fake_secret("ab"))
    setup = make_setup(download_methods={"torrent"}, vpn=VpnConfig(route_prowlarr=route))
    ensure_app_keys(st, VPN_APPS, gluetun_proxy=route)
    gluetun = env_files_for(setup, st)["gluetun"]
    qbt = {"QBT_WEBUI_PASSWORD": st.get("password:qbittorrent")}  # port forwarding is on
    if route:
        assert gluetun == {
            "WIREGUARD_PRIVATE_KEY": fake_secret("ab"),
            "HTTPPROXY_PASSWORD": st.get("password:gluetun-proxy"),
            **qbt,
        }
    else:
        assert gluetun == {"WIREGUARD_PRIVATE_KEY": fake_secret("ab"), **qbt}


def test_env_files_for_missing_proxy_password(tmp_path):
    from orama.setup_model import VpnConfig

    st = SecretStore(tmp_path / "s.bin")
    ensure_app_keys(st, VPN_APPS)
    st.set("vpn:wireguard_private_key", fake_secret("ab"))
    setup = make_setup(download_methods={"torrent"}, vpn=VpnConfig(route_prowlarr=True))
    with pytest.raises(MissingSecret) as ei:
        env_files_for(setup, st)
    assert (ei.value.secret_name, ei.value.app) == ("password:gluetun-proxy", "gluetun")


def test_env_files_for_missing_secret_names_app_not_value(tmp_path):
    st = SecretStore(tmp_path / "s.bin")
    with pytest.raises(KeyError) as ei:
        env_files_for(make_setup(), st)
    msg = str(ei.value)
    assert "sonarr" in msg and "apikey:sonarr" in msg


def test_logs_never_contain_secrets(tmp_path, caplog):
    st = SecretStore(tmp_path / "s.bin")
    st.set("apikey:radarr", fake_secret())
    install_redaction(st)
    log = logging.getLogger("x")
    log.warning("calling radarr with %s", fake_secret())
    log.warning("url=http://radarr:7878/api?apikey=zzz999")
    assert "f00dfeed" not in caplog.text and "zzz999" not in caplog.text
    assert "***" in caplog.text
    assert all("f00dfeed" not in r.getMessage() for r in caplog.records)


def test_secrets_added_after_install_are_redacted(tmp_path, caplog):
    st = SecretStore(tmp_path / "s.bin")
    install_redaction(st)
    st.set("late", "latesecretvalue")
    logging.getLogger("x").warning("got latesecretvalue here")
    assert "latesecretvalue" not in caplog.text


def test_args_dict_and_non_str_args_redacted(tmp_path, caplog):
    st = SecretStore(tmp_path / "s.bin")
    st.set("k", "dictsecret99")
    install_redaction(st)
    logging.getLogger("x").warning("v=%(v)s", {"v": "dictsecret99"})
    logging.getLogger("x").warning("n=%d %s", 5, ["dictsecret99"])
    assert "dictsecret99" not in caplog.text
    assert "n=5" in caplog.text


def test_short_values_not_used_for_literal_replacement(tmp_path, caplog):
    st = SecretStore(tmp_path / "s.bin")
    st.set("short", "ab")
    install_redaction(st)
    logging.getLogger("x").warning("about a cab and a lab")
    assert "about a cab and a lab" in caplog.text


def test_pattern_rule_applies_without_store_values(tmp_path, caplog):
    install_redaction(SecretStore(tmp_path / "s.bin"))
    pw, tok = random_secret(), random_secret()
    logging.getLogger("x").warning('cfg {"password": "' + pw + '"} token: ' + tok)
    assert pw not in caplog.text and tok not in caplog.text


def test_exception_tracebacks_redacted(tmp_path, caplog):
    st = SecretStore(tmp_path / "s.bin")
    st.set("k", "tracebacksecret")
    install_redaction(st)
    try:
        raise RuntimeError("boom tracebacksecret")
    except RuntimeError:
        logging.getLogger("x").exception("failed")
    assert "tracebacksecret" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_install_redaction_idempotent(tmp_path):
    st = SecretStore(tmp_path / "s.bin")
    handler = logging.StreamHandler()
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        install_redaction(st)
        install_redaction(st)
        assert sum(isinstance(f, RedactingFilter) for f in handler.filters) == 1
    finally:
        root.removeHandler(handler)


def test_bad_format_args_do_not_raise_and_are_redacted(tmp_path, caplog):
    st = SecretStore(tmp_path / "s.bin")
    st.set("k", "badargsecret")
    install_redaction(st)
    logging.getLogger("x").warning("bad %d badargsecret apikey=zzz999", "notanint")
    assert "badargsecret" not in caplog.text and "zzz999" not in caplog.text


def test_unreadable_store_does_not_raise_and_pattern_redacts(tmp_path, caplog):
    st = SecretStore(tmp_path / "s.bin")
    st.set("k", "gonesecret")
    install_redaction(st)
    (tmp_path / "s.key").unlink()
    logging.getLogger("x").warning("hello apikey=zzz999")
    assert "hello" in caplog.text and "zzz999" not in caplog.text


def test_read_with_missing_key_does_not_create_key(tmp_path):
    p = tmp_path / "s.bin"
    SecretStore(p).set("a", "valuevalue")
    (tmp_path / "s.key").unlink()
    blob = p.read_bytes()
    with pytest.raises(ValueError, match="decrypt"):
        SecretStore(p).get("a")
    assert not (tmp_path / "s.key").exists()
    assert p.read_bytes() == blob


def test_key_suffix_path_refused(tmp_path):
    with pytest.raises(ValueError):
        SecretStore(tmp_path / "secrets.key")


def test_exc_info_cleared_and_exc_text_redacted(tmp_path):
    st = SecretStore(tmp_path / "s.bin")
    st.set("k", "tracebacksecret")
    try:
        raise RuntimeError("boom tracebacksecret")
    except RuntimeError:
        import sys
        rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed", (), sys.exc_info())
    RedactingFilter(st).filter(rec)
    assert rec.exc_info is None
    assert "tracebacksecret" not in rec.exc_text and "RuntimeError" in rec.exc_text


DOWNLOAD_APPS = ["sonarr", "sabnzbd", "qbittorrent", "gluetun"]


def test_ensure_app_keys_creates_download_client_secrets(tmp_path):
    st = SecretStore(tmp_path / "s.bin")
    ensure_app_keys(st, ["sonarr"])
    assert st.names() == ["apikey:sonarr"]
    ensure_app_keys(st, DOWNLOAD_APPS)
    assert sorted(st.names()) == [
        "apikey:sabnzbd", "apikey:sonarr", "nzbkey:sabnzbd", "password:qbittorrent",
    ]
    assert re.fullmatch(r"[0-9a-f]{32}", st.get("apikey:sabnzbd"))
    assert re.fullmatch(r"[0-9a-f]{32}", st.get("nzbkey:sabnzbd"))
    assert st.get("apikey:sabnzbd") != st.get("nzbkey:sabnzbd")
    assert re.fullmatch(r"[A-Za-z0-9_-]{32}", st.get("password:qbittorrent"))
    before = {n: st.get(n) for n in st.names()}
    ensure_app_keys(st, DOWNLOAD_APPS)
    assert {n: st.get(n) for n in st.names()} == before


def test_ensure_app_keys_keeps_existing_download_client_secrets(tmp_path):
    st = SecretStore(tmp_path / "s.bin")
    password, nzb = random_secret(), random_secret()
    st.set("password:qbittorrent", password)
    st.set("nzbkey:sabnzbd", nzb)
    ensure_app_keys(st, DOWNLOAD_APPS)
    assert st.get("password:qbittorrent") == password
    assert st.get("nzbkey:sabnzbd") == nzb
    assert st.get("apikey:sabnzbd") is not None


def test_qbittorrent_pbkdf2_known_answer():
    salt = bytes(range(16))
    password = "pa ss&w=rd+%#\u03b1"
    key = hashlib.pbkdf2_hmac("sha512", password.encode(), salt, 100000, dklen=64)
    expected = f"@ByteArray({base64.b64encode(salt).decode()}:{base64.b64encode(key).decode()})"
    assert qbittorrent_pbkdf2(password, salt) == expected


def test_qbittorrent_pbkdf2_random_salt_and_shape():
    first, second = qbittorrent_pbkdf2("pw"), qbittorrent_pbkdf2("pw")
    assert first != second
    shape = r"@ByteArray\([A-Za-z0-9+/]{22}==:[A-Za-z0-9+/]{86}==\)"
    assert re.fullmatch(shape, first) and re.fullmatch(shape, second)


def test_qbittorrent_pbkdf2_refuses_wrong_salt_size():
    with pytest.raises(ValueError):
        qbittorrent_pbkdf2("pw", b"short")


@pytest.mark.parametrize("port_forwarding", [True, False])
def test_env_files_for_gluetun_qbittorrent_password_only_with_port_forwarding(
    tmp_path, port_forwarding
):
    from orama.setup_model import VpnConfig

    st = SecretStore(tmp_path / "s.bin")
    st.set("vpn:wireguard_private_key", fake_secret("ab"))
    ensure_app_keys(st, VPN_APPS)
    setup = make_setup(download_methods={"torrent"},
                       vpn=VpnConfig(port_forwarding=port_forwarding))
    gluetun = env_files_for(setup, st)["gluetun"]
    if port_forwarding:
        assert gluetun["QBT_WEBUI_PASSWORD"] == st.get("password:qbittorrent")
    else:
        assert "QBT_WEBUI_PASSWORD" not in gluetun
    assert "qbittorrent" not in env_files_for(setup, st)


def test_env_files_for_missing_qbittorrent_password(tmp_path):
    from orama.setup_model import VpnConfig

    st = SecretStore(tmp_path / "s.bin")
    st.set("vpn:wireguard_private_key", fake_secret("ab"))
    ensure_app_keys(st, ["sonarr", "radarr", "prowlarr"])
    setup = make_setup(download_methods={"torrent"}, vpn=VpnConfig(port_forwarding=True))
    with pytest.raises(MissingSecret) as ei:
        env_files_for(setup, st)
    assert (ei.value.secret_name, ei.value.app) == ("password:qbittorrent", "gluetun")


def test_generated_secrets_fit_the_env_file_allowlist():
    # Every generated secret may end up in an Agent env file, which accepts only these.
    from orama.catalog import ENV_FILE_VALUE
    from orama.secrets import _new_password as new_password

    for _ in range(500):
        assert ENV_FILE_VALUE.fullmatch(generate_api_key())
        assert ENV_FILE_VALUE.fullmatch(new_password())


def test_media_secrets_generated_once_and_never_overwritten(tmp_path):
    from uuid import UUID

    st = SecretStore(tmp_path / "s.bin")
    ensure_app_keys(st, ["bazarr", "jellyfin", "plex"])
    first = {name: st.get(name) for name in (
        "apikey:bazarr", "password:jellyfin", "plex:client_id"
    )}
    assert re.fullmatch(r"[0-9a-f]{32}", first["apikey:bazarr"])
    assert len(first["password:jellyfin"]) >= 24
    assert str(UUID(first["plex:client_id"])) == first["plex:client_id"]
    ensure_app_keys(st, ["bazarr", "jellyfin", "plex"])
    assert {name: st.get(name) for name in first} == first
    assert st.get("token:plex") is None
    assert st.get("apikey:jellyfin") is None


def test_redacting_filter_public_redact(tmp_path):
    st = SecretStore(tmp_path / "s.bin")
    value = random_secret("v")
    st.set("x", value)
    flt = RedactingFilter(st)
    assert flt.redact(f"hello {value} there") == "hello *** there"
    assert "abc123" not in flt.redact("password: abc123")
