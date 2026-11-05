from conftest import make_setup

from orama.secrets import SecretStore
from orama.wiring.http import FakeHttp
from orama.wiring.plan import build_steps


def names(setup, tmp_path):
    store = SecretStore(tmp_path / "secrets.bin")
    return [s.name for s in build_steps(setup, store, FakeHttp(), object())]


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
    assert apps_in_order(steps)[:5] == ["sabnzbd", "qbittorrent", "sonarr", "radarr", "prowlarr"]


def test_full_order_ends_with_media_servers_then_notifications(tmp_path):
    steps = names(make_setup(media_servers={"plex", "jellyfin"},
                             download_methods={"usenet", "torrent"}), tmp_path)
    first = {}
    for i, n in enumerate(steps):
        first.setdefault(n.split(":")[1] if n.startswith("wait:") else n.split(":")[0], i)
    order = ["sabnzbd", "qbittorrent", "sonarr", "radarr", "prowlarr", "bazarr", "recyclarr",
             "plex", "jellyfin"]
    assert [first[a] for a in order] == sorted(first[a] for a in order)
    notify = [i for i, n in enumerate(steps) if ":notify:" in n]
    assert len(notify) == 4 and notify == list(range(len(steps) - 4, len(steps)))
    assert steps.index("jellyfin:api-key") < notify[0]
    assert steps.index("plex:libraries") < notify[0]


def test_deselected_media_server_has_no_steps_but_keeps_its_cleanup(tmp_path):
    steps = names(make_setup(media_servers={"plex"}), tmp_path)
    assert not any(n.startswith(("jellyfin", "wait:jellyfin")) for n in steps)
    assert "sonarr:notify:jellyfin" in steps  # deletes a leftover notification

