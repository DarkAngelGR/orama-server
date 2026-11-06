"""Secrets typed into a setup change stay out of the live store until it is installed.

While a draft changes a setup that is already live (``setup.json`` exists), what the wizard
writes to the secret store goes under draft names instead: ``draft:set:<name>`` holds a new
value, ``draft:del:<name>`` says the draft deletes ``<name>``. :class:`DraftStore` reads the
draft over the live store, so the forms and tests see the draft's values. :func:`promote_secrets`
moves them in one atomic store write when the draft becomes the setup (the first apply and
Install); :func:`discard` drops them and puts the apps back the way the live setup has them.

Before the first install nothing is live, so the layer passes everything through: one code
path, with the store written directly.

An indexer or Usenet test sends the draft's entry to Prowlarr or SABnzbd. What the app now
holds is real, so the test's own markers (sent markers) go to the live store; its result
(``test-ok``) belongs to the draft. The first test of an entry records it under
``draft:touched:<kind>:<slug>``, so throwing the draft away can test the live entry again
(the shared :func:`undo_candidate`), restoring the app and the live ``test-ok``.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from orama.secrets import DRAFT_SET_PREFIX
from orama.setup_model import DesiredSetup
from orama.wiring import prowlarr, sabnzbd

SETUP_FILE = "setup.json"
SET_PREFIX = DRAFT_SET_PREFIX
DEL_PREFIX = "draft:del:"
TOUCHED_PREFIX = "draft:touched:"
_DRAFT_PREFIX = "draft:"
_TEST_RESULT = "test-ok:"


class DraftStore:
    """The secret store as the wizard sees it while it edits a draft."""

    def __init__(self, store: Any, scoped: bool) -> None:
        self.store = store
        self.scoped = scoped

    def get(self, name: str) -> str | None:
        if not self.scoped:
            return self.store.get(name)
        if self.store.get(DEL_PREFIX + name) is not None:
            return None
        value = self.store.get(SET_PREFIX + name)
        return value if value is not None else self.store.get(name)

    def set(self, name: str, value: str) -> None:
        if not self.scoped:
            self.store.set(name, value)
        else:
            self.store.update({SET_PREFIX + name: value, DEL_PREFIX + name: None})

    def delete(self, name: str) -> None:
        if not self.scoped:
            self.store.delete(name)
        else:
            self.store.update({SET_PREFIX + name: None, DEL_PREFIX + name: "1"})

    def names(self) -> list[str]:
        """The names as the draft sees them (the draft's own bookkeeping left out)."""
        items = dict(self.store.items())
        if not self.scoped:
            return sorted(items)
        names = {n for n in items if not n.startswith(_DRAFT_PREFIX)
                 and DEL_PREFIX + n not in items}
        names.update(n[len(SET_PREFIX):] for n in items if n.startswith(SET_PREFIX))
        return sorted(names)

    def for_test(self) -> _TestView:
        """The store an indexer or Usenet test of the draft uses."""
        return _TestView(self)

    def touch(self, kind: str, slug: str, label: str, marker_name: str) -> None:
        """Remember, once, that the draft is about to send ``slug`` to its app."""
        if not self.scoped:
            return
        name = f"{TOUCHED_PREFIX}{kind}:{slug}"
        if self.store.get(name) is None:
            sent_before = bool(self.store.get(marker_name))
            self.store.set(name, json.dumps({"name": label, "sent_before": sent_before}))


class _TestView:
    """A test's reads see the draft; its result goes to the draft, its markers to the store."""

    def __init__(self, layer: DraftStore) -> None:
        self._layer = layer

    def get(self, name: str) -> str | None:
        return self._layer.get(name)

    def set(self, name: str, value: str) -> None:
        if name.startswith(_TEST_RESULT):
            self._layer.set(name, value)
        else:
            self._live({name: value})

    def delete(self, name: str) -> None:
        if name.startswith(_TEST_RESULT):
            self._layer.delete(name)
        else:
            self._live({name: None})

    def names(self) -> list[str]:
        return self._layer.names()

    def _live(self, changes: dict[str, str | None]) -> None:
        store = self._layer.store
        if self._layer.scoped:  # nothing in the draft may hide what really happened
            for name in list(changes):
                changes[SET_PREFIX + name] = None
                changes[DEL_PREFIX + name] = None
        store.update(changes)


def draft_store(st) -> DraftStore:
    """The wizard's store: draft-scoped while a live setup exists."""
    return DraftStore(st.store, (st.state_dir / SETUP_FILE).exists())


def promote_secrets(store: Any) -> dict[str, str | None]:
    """The store changes that make the draft's secrets live and drop every draft name."""
    items = store.items()
    changes: dict[str, str | None] = {}
    for name, value in items:
        if name.startswith(SET_PREFIX):
            changes[name[len(SET_PREFIX):]] = value
        elif name.startswith(DEL_PREFIX):
            changes[name[len(DEL_PREFIX):]] = None
    for name, _value in items:
        if name.startswith(_DRAFT_PREFIX):
            changes[name] = None  # every draft name goes, the bookkeeping too
    return changes


def stale_secrets(store: Any) -> dict[str, None]:
    """Draft secrets left behind with no draft (an interrupted Install): to drop before a new
    draft starts, so it never sees them."""
    return {n: None for n, _ in store.items() if n.startswith((SET_PREFIX, DEL_PREFIX))}


def discard_changes(store: Any) -> tuple[dict[str, None], list[tuple[str, str, str, bool]]]:
    """The store changes that drop every draft name, and the entries the draft sent to an app
    (kind, slug, label, sent before)."""
    changes: dict[str, None] = {}
    touched: list[tuple[str, str, str, bool]] = []
    for name, value in store.items():
        if not name.startswith(_DRAFT_PREFIX):
            continue
        changes[name] = None
        if name.startswith(TOUCHED_PREFIX):
            kind, _, slug = name[len(TOUCHED_PREFIX):].partition(":")
            try:
                data = json.loads(value)
                label, sent_before = str(data["name"]), bool(data["sent_before"])
            except (ValueError, KeyError, TypeError):
                label, sent_before = slug, True  # unknown: never mark it for removal
            if kind in ("indexer", "usenet") and slug:
                touched.append((kind, slug, label, sent_before))
    return changes, touched


def undo_touched(st, path, touched: list[tuple[str, str, str, bool]]) -> bool:
    """Test the live entries a thrown-away draft sent to the apps again (apply.lock held,
    setup.lock not). Returns False when one could not be put back."""
    ok = True
    for kind, slug, label, sent_before in touched:
        if kind == "indexer":
            test, marker = prowlarr.test_indexer, prowlarr.sent_marker_name(f"indexer:{slug}")
        else:
            test, marker = sabnzbd.test_usenet_server, sabnzbd.sent_marker_name(slug)
        ok = undo_candidate(st, path, None, kind, slug, label, test, marker, sent_before) and ok
    return ok


def undo_candidate(st, path, reference: DesiredSetup | None, kind: str, slug: str,
                   name: str, test, marker_name: str, sent_before: bool,
                   candidate: DesiredSetup | None = None, layer=None) -> bool:
    """Undo a candidate entry's test in the app when the candidate is not saved.

    The test sent the candidate to Prowlarr or SABnzbd. ``reference`` is the setup that stands
    (None: ``setup.json`` as it is now). An entry it has is tested again with the stored
    secrets, which puts its values back in the app; one it lacks that the test created gets a
    removal marker for the next apply. The candidate's ``test-ok`` is dropped either way.
    A candidate that is the setup itself, with no secret of its own (a replayed form), changed
    nothing in the app: there is nothing to undo, so no second test runs on the old values.
    Returns False when this could not be done.
    """
    try:
        if reference is None:
            reference = DesiredSetup.load(path)
        st.store.delete(f"test-ok:{kind}:{slug}")
        if (candidate is not None and layer is not None and candidate == reference
                and not any(not name.startswith("test-ok:") for name in layer.pending)):
            return True
        entries = reference.indexers if kind == "indexer" else reference.usenet_servers
        if any(i.slug == slug for i in entries):
            return test(reference, st.store, st.http, slug) is None
        if not sent_before and st.store.get(marker_name):
            if kind == "indexer":
                st.store.set(prowlarr.REMOVE_PREFIX + slug, name)
            else:
                st.store.set(sabnzbd.REMOVE_PREFIX + slug, slug)
        return True
    except (OSError, ValueError):
        return False


def write_back(path: Path, data: bytes) -> None:
    """Put ``data`` back in ``path`` atomically (temp file in the same folder + replace)."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
