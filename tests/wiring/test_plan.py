from conftest import make_setup

from orama.secrets import SecretStore
from orama.wiring.http import FakeHttp
from orama.wiring.plan import build_steps


def names(setup, tmp_path):
    store = SecretStore(tmp_path / "secrets.bin")
    return [s.name for s in build_steps(setup, store, FakeHttp())]


def apps_in_order(steps):
    seen = []
    for name in steps:
        app = name.split(":")[1] if name.startswith("wait:") else name.split(":")[0]
        if app not in seen:
            seen.append(app)
    return seen


def test_usenet_only_leaves_out_qbittorrent(tmp_path):
    steps = names(make_setup(download_methods={"usenet"}), tmp_path)
    assert "wait:sabnzbd" in steps
    assert not any("qbittorrent" in n and not n.endswith("client:qbittorrent") for n in steps)
    assert "wait:qbittorrent" not in steps


def test_torrent_only_leaves_out_sabnzbd(tmp_path):
    steps = names(make_setup(download_methods={"torrent"}), tmp_path)
    assert "wait:qbittorrent" in steps
    assert "wait:sabnzbd" not in steps


def test_order_is_sabnzbd_qbittorrent_sonarr_radarr_prowlarr(tmp_path):
    steps = names(make_setup(download_methods={"usenet", "torrent"}), tmp_path)
    assert apps_in_order(steps) == ["sabnzbd", "qbittorrent", "sonarr", "radarr", "prowlarr"]
