import base64
import os

import pytest

from orama_agent.seed import config_file, render
from orama_agent.validate import Refused


def hex_key(seed: str = "a1") -> str:
    # Built at runtime so no key-shaped string lands in tracked files.
    return (seed * 16)[:32]


def qt_hash(seed: int = 1) -> str:
    salt = base64.b64encode(bytes([seed]) * 16).decode()
    key = base64.b64encode(bytes([seed + 1]) * 64).decode()
    return f"@ByteArray({salt}:{key})"


SAB = {"api_key": hex_key("a1"), "nzb_key": hex_key("b2")}
QBT = {"password_pbkdf2": qt_hash()}

SAB_INI = (
    "__version__ = 19\n"
    "__encoding__ = utf-8\n"
    "[misc]\n"
    "  # a comment the user wrote\n"
    "port = 8080\n"
    "api_key = 00000000000000000000000000000000\n"
    "complete_dir = /data/downloads/usenet\n"
    "odd line with \xe9 and trailing space   \n"
    "\n"
    "[servers]\n"
    "[[news.example.org]]\n"
    "host = news.example.org\n"
    "api_key = not-ours\n"
)


def test_config_files():
    assert config_file("sabnzbd") == ("sabnzbd.ini",)
    assert config_file("qbittorrent") == ("qBittorrent", "qBittorrent.conf")


def test_sabnzbd_patches_section_and_keeps_every_other_line():
    out = render("sabnzbd", SAB_INI, SAB)
    old, new = SAB_INI.split("\n"), out.split("\n")
    assert new[:5] == old[:5]
    assert new[5] == f"api_key = {SAB['api_key']}"
    assert new[6:8] == old[6:8]
    assert new[8:11] == [f"nzb_key = {SAB['nzb_key']}",
                         "host_whitelist = sabnzbd, gluetun, localhost", ""]
    assert new[11:] == old[9:]  # [servers] untouched, its api_key too


def test_qbittorrent_patches_existing_keys_and_appends_missing():
    text = (
        "[Application]\r\n"
        "FileLogger\\Enabled=true\r\n"
        "\r\n"
        "[Preferences]\r\n"
        "Connection\\PortRangeMin=6881\r\n"
        "WebUI\\LocalHostAuth=true\r\n"
        "WebUI\\Port=8090\r\n"
    )
    out = render("qbittorrent", text, QBT)
    assert out == (
        "[Application]\r\n"
        "FileLogger\\Enabled=true\r\n"
        "\r\n"
        "[Preferences]\r\n"
        "Connection\\PortRangeMin=6881\r\n"
        "WebUI\\LocalHostAuth=false\r\n"
        "WebUI\\Port=8090\r\n"
        f'WebUI\\Password_PBKDF2="{QBT["password_pbkdf2"]}"\n'
        "WebUI\\Username=orama\n"
        "WebUI\\HostHeaderValidation=false\n"
    )


@pytest.mark.parametrize("current", [None, "", "[Application]\nx=1"])
def test_appends_missing_section(current):
    out = render("qbittorrent", current, QBT)
    body = (
        "[Preferences]\n"
        f'WebUI\\Password_PBKDF2="{QBT["password_pbkdf2"]}"\n'
        "WebUI\\Username=orama\n"
        "WebUI\\LocalHostAuth=false\n"
        "WebUI\\HostHeaderValidation=false\n"
    )
    assert out == ("[Application]\nx=1\n\n" + body if current else body)


@pytest.mark.parametrize("app,values,current", [
    ("sabnzbd", SAB, SAB_INI),
    ("qbittorrent", QBT, "[Preferences]\nWebUI\\Port=8090\n"),
    ("qbittorrent", QBT, None),
])
def test_second_render_is_unchanged(app, values, current):  # [RF 3]
    out = render(app, current, values)
    assert out is not None
    assert render(app, out, values) is None


def test_values_written_the_way_the_apps_rewrite_them_count_as_unchanged():
    sab = (f"[misc]\napi_key = {SAB['api_key']}\nnzb_key = \"{SAB['nzb_key']}\"\n"
           "host_whitelist = sabnzbd,gluetun,localhost,\n")
    assert render("sabnzbd", sab, SAB) is None
    qbt = ("[Preferences]\nWebUI\\HostHeaderValidation=false\nWebUI\\LocalHostAuth=false\n"
           f"WebUI\\Password_PBKDF2=\"{QBT['password_pbkdf2']}\"\nWebUI\\Username=orama\n")
    assert render("qbittorrent", qbt, QBT) is None


def test_a_changed_value_is_rewritten():
    first = render("sabnzbd", None, SAB)
    other = dict(SAB, api_key=hex_key("c3"))
    out = render("sabnzbd", first, other)
    assert f"api_key = {other['api_key']}\n" in out and SAB["api_key"] not in out


@pytest.mark.parametrize("app", ["sonarr", "SABnzbd", "", None, 3, "../sabnzbd"])
def test_unknown_app_refused(app):
    with pytest.raises(Refused):
        render(app, None, SAB)


@pytest.mark.parametrize("values", [
    {},
    {"api_key": hex_key()},
    dict(SAB, host_whitelist="*"),
    dict(SAB, extra=hex_key()),
    {"api_key": hex_key(), "password_pbkdf2": qt_hash()},
    [SAB["api_key"]],
    None,
])
def test_unknown_or_missing_keys_refused(values):
    with pytest.raises(Refused):
        render("sabnzbd", None, values)


@pytest.mark.parametrize("bad", [
    hex_key().upper(),
    hex_key()[:31],
    hex_key() + "0",
    hex_key()[:31] + "\n",
    hex_key() + "\n",
    hex_key()[:31] + "]",
    hex_key()[:16] + "\nport=1" + hex_key()[:9],
    32,
    None,
])
def test_bad_sabnzbd_values_refused(bad):
    with pytest.raises(Refused) as err:
        render("sabnzbd", None, dict(SAB, nzb_key=bad))
    if isinstance(bad, str):
        assert bad.strip() not in str(err.value)


def _swap(text, old, new):
    return text.replace(old, new, 1)


@pytest.mark.parametrize("bad", [
    _swap(qt_hash(), "@ByteArray(", ""),
    qt_hash()[:-1],
    qt_hash() + "\n",
    _swap(qt_hash(), ")", "]"),
    _swap(qt_hash(), "==:", "==\n"),
    _swap(qt_hash(), "==)", "==)\n[Preferences]\nWebUI\\Username=x"),
    _swap(qt_hash(), "AQ", "A-"),
    "password",
    "",
])
def test_bad_qbittorrent_values_refused(bad):
    with pytest.raises(Refused):
        render("qbittorrent", None, {"password_pbkdf2": bad})


def test_refusal_message_never_shows_the_value():
    value = os.urandom(8).hex() + "zz"
    with pytest.raises(Refused) as err:
        render("sabnzbd", None, dict(SAB, api_key=value))
    assert value not in str(err.value)
