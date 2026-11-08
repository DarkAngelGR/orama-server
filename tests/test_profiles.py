from importlib import resources

import pytest
import yaml

from orama.profiles.compile import compile_profile, device_names

DEVICES = ["apple_tv", "lg", "nvidia_shield", "safest", "samsung_s95f"]
TRASH = yaml.safe_load(resources.files("orama.profiles").joinpath("trash.yaml").read_text())


def _device(name):
    return yaml.safe_load(
        resources.files("orama.profiles").joinpath("devices", f"{name}.yaml").read_text()
    )


def _ids(app, *symbols):
    return {i for s in symbols for i in TRASH[app][s]}


def test_devices_listed():
    assert device_names() == DEVICES


@pytest.mark.parametrize("name", DEVICES)
@pytest.mark.parametrize("app", ["radarr", "sonarr"])
def test_every_symbol_exists_for_both_apps(name, app):
    dev = _device(name)
    assert {"name", "verified", "unplayable", "preferred"} <= set(dev) <= {
        "name",
        "verified",
        "unplayable",
        "preferred",
        "capabilities",
    }
    for symbol in dev["unplayable"] + dev["preferred"]:
        assert TRASH[app].get(symbol), (name, app, symbol)
    assert not set(dev["unplayable"]) & set(dev["preferred"])


def test_verified_flags():
    assert {n for n in DEVICES if _device(n)["verified"]} == {"samsung_s95f", "safest"}


def test_trash_ids_look_like_trash_ids():
    for app in ("radarr", "sonarr"):
        every = [i for ids in TRASH[app].values() for i in ids]
        assert all(len(i) == 32 and set(i) <= set("0123456789abcdef") for i in every)
        assert len(every) == len(set(every))


@pytest.mark.parametrize("app", ["radarr", "sonarr"])
def test_s95f_scores_pinned(app):
    scores = compile_profile("samsung_s95f", "mixed")[app]["scores"]
    bad = _ids(
        app,
        "dv",
        "dv_no_fallback",
        "dts",
        "dts_hd_ma",
        "dts_x",
        "dts_hd_hra",
        "dts_es",
        "pcm_multichannel",
        "flac",
    )
    good = _ids(app, "hdr10", "hdr10_plus", "truehd_atmos", "truehd")
    assert {i for i, s in scores.items() if s == -10000} == bad
    assert {i for i, s in scores.items() if s == 1500} == good
    assert len(scores) == len(bad) + len(good)


def test_s95f_exact_radarr_ids():
    scores = compile_profile("samsung_s95f", "4k")["radarr"]["scores"]
    assert scores["923b6abef9b17f937fab56cfcf89e1f1"] == -10000  # DV (w/o HDR fallback)
    assert scores["f700d29429c023a5734505e77daeaea7"] == -10000  # DV (Disk)
    assert scores["3cafb66171b47f226146a0770576870f"] == 1500  # TrueHD
    assert scores["493b6d1dbec3c3364c59d7607f7e3405"] == 1500  # HDR


def test_safest_blocks_truehd_too():
    for app in ("radarr", "sonarr"):
        scores = compile_profile("safest", "mixed")[app]["scores"]
        assert all(scores[i] == -10000 for i in _ids(app, "truehd", "truehd_atmos"))
        assert {i for i, s in scores.items() if s == 1500} == _ids(app, "hdr10")


def test_templates_per_quality():
    t = {
        q: {a: v["templates"] for a, v in compile_profile("safest", q).items()}
        for q in ("4k", "1080p", "mixed")
    }
    assert t["4k"] == {"radarr": ["uhd-bluray-web"], "sonarr": ["web-2160p"]}
    assert t["1080p"] == {"radarr": ["hd-bluray-web"], "sonarr": ["web-1080p"]}
    assert t["mixed"] == {"radarr": ["uhd-bluray-web"], "sonarr": ["web-1080p"]}


def test_deterministic():
    a = compile_profile("lg", "mixed")
    b = compile_profile("lg", "mixed")
    assert a == b
    for app in a.values():
        assert list(app["scores"]) == sorted(app["scores"])


def test_switching_device_leaves_no_leftovers():
    # Each result is built from scratch: S95F's -10000 entries do not leak into the Shield's map.
    shield = compile_profile("nvidia_shield", "mixed")
    s95f = compile_profile("samsung_s95f", "mixed")
    for app in ("radarr", "sonarr"):
        assert not any(s == -10000 for s in shield[app]["scores"].values())
        assert set(shield[app]["scores"]) != set(s95f[app]["scores"])
    again = compile_profile("samsung_s95f", "mixed")
    assert again == s95f


def test_unknown_device_and_quality_refused():
    with pytest.raises(ValueError, match="Unknown device"):
        compile_profile("../etc", "mixed")
    with pytest.raises(ValueError, match="Unknown quality"):
        compile_profile("safest", "8k")
