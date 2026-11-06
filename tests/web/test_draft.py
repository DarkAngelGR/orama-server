"""The draft layer over the secret store: reads, writes, a test's writes, promotion."""

from __future__ import annotations

import secrets

from orama.secrets import SecretStore
from orama.web import draft


def store(tmp_path) -> SecretStore:
    return SecretStore(tmp_path / "secrets.bin")


def test_scoped_layer_reads_the_draft_over_the_live_store(tmp_path):
    live = store(tmp_path)
    old, new = secrets.token_urlsafe(12), secrets.token_urlsafe(12)
    live.update({"password:jellyfin": old, "remove:indexer:a": "A", "apikey:sonarr": "k" * 8})
    layer = draft.DraftStore(live, scoped=True)
    layer.set("password:jellyfin", new)
    layer.delete("remove:indexer:a")
    layer.set("remove:indexer:b", "B")
    assert layer.get("password:jellyfin") == new and live.get("password:jellyfin") == old
    assert layer.get("remove:indexer:a") is None and live.get("remove:indexer:a") == "A"
    assert layer.names() == ["apikey:sonarr", "password:jellyfin", "remove:indexer:b"]
    layer.set("remove:indexer:a", "A again")  # set after delete wins
    assert layer.get("remove:indexer:a") == "A again"


def test_unscoped_layer_writes_the_live_store(tmp_path):
    live = store(tmp_path)
    layer = draft.DraftStore(live, scoped=False)
    layer.set("password:jellyfin", "pw-value")
    layer.delete("nothing")
    assert live.names() == ["password:jellyfin"]


def test_a_tests_result_stays_in_the_draft_its_markers_go_live(tmp_path):
    live = store(tmp_path)
    live.set("test-ok:indexer:a", "1")
    view = draft.DraftStore(live, scoped=True).for_test()
    view.delete("test-ok:indexer:a")
    view.set("prowlarr:indexer:a:sent", "digest")
    assert live.get("test-ok:indexer:a") == "1" and view.get("test-ok:indexer:a") is None
    assert live.get("prowlarr:indexer:a:sent") == "digest"


def test_promotion_and_discard_cover_every_draft_name(tmp_path):
    live = store(tmp_path)
    live.update({"password:jellyfin": "old-value", "remove:indexer:a": "A"})
    layer = draft.DraftStore(live, scoped=True)
    layer.set("password:jellyfin", "new-value")
    layer.delete("remove:indexer:a")
    layer.touch("indexer", "a", "A", "prowlarr:indexer:a:sent")
    changes = draft.promote_secrets(live)
    assert changes["password:jellyfin"] == "new-value"
    assert changes["remove:indexer:a"] is None
    assert all(changes[n] is None for n in live.names() if n.startswith("draft:"))
    dropped, touched = draft.discard_changes(live)
    assert set(dropped) == {n for n in live.names() if n.startswith("draft:")}
    assert touched == [("indexer", "a", "A", False)]
    assert set(draft.stale_secrets(live)) == {
        n for n in live.names() if n.startswith(("draft:set:", "draft:del:"))}
