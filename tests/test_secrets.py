import logging
import re
import stat

import pytest
from conftest import make_setup

from orama.secrets import (
    RedactingFilter,
    SecretStore,
    ensure_app_keys,
    env_files_for,
    generate_api_key,
    install_redaction,
)


def fake_secret(seed: str = "f00dfeed") -> str:
    # Built at runtime so no key-shaped string lands in tracked files.
    return seed * 4


def mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_store_file_is_encrypted_and_private(tmp_path):
    st = SecretStore(tmp_path / "secrets.bin")
    st.set("apikey:sonarr", "abc123secret")
    raw = (tmp_path / "secrets.bin").read_bytes()
    assert b"abc123secret" not in raw
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
    assert sorted(p.name for p in tmp_path.iterdir()) == ["s.bin", "s.key"]


def test_key_file_not_overwritten(tmp_path):
    p = tmp_path / "s.bin"
    SecretStore(p).set("a", "valuevalue")
    key = (tmp_path / "s.key").read_bytes()
    SecretStore(p).set("b", "valuevalue")
    assert (tmp_path / "s.key").read_bytes() == key


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
    assert sorted(st.names()) == ["apikey:prowlarr", "apikey:radarr", "apikey:sonarr"]
    before = {n: st.get(n) for n in st.names()}
    ensure_app_keys(st, ["sonarr", "radarr", "prowlarr"])
    assert {n: st.get(n) for n in st.names()} == before
    for v in before.values():
        assert re.fullmatch(r"[0-9a-f]{32}", v)


def test_ensure_app_keys_never_overwrites_existing(tmp_path):
    st = SecretStore(tmp_path / "s.bin")
    st.set("apikey:sonarr", "existing-key")
    ensure_app_keys(st, ["sonarr", "radarr"])
    assert st.get("apikey:sonarr") == "existing-key"
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
    ensure_app_keys(st, ["sonarr", "radarr", "prowlarr"])
    st.set("vpn:wireguard_private_key", fake_secret("ab"))
    setup = make_setup(download_methods={"torrent"}, vpn=VpnConfig())
    envs = env_files_for(setup, st)
    assert envs["gluetun"] == {"WIREGUARD_PRIVATE_KEY": fake_secret("ab")}
    assert "qbittorrent" not in envs and "bazarr" not in envs


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
    logging.getLogger("x").warning('cfg {"password": "hunter2hunter2"} token: abcdefgh')
    assert "hunter2hunter2" not in caplog.text and "abcdefgh" not in caplog.text


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
