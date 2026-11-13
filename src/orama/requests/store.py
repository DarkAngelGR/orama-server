"""Durable SQLite storage for media requests and background work."""
from __future__ import annotations

import os
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .models import EpisodeTarget, RequestRecord, RequestSpec, RequestUpdate

SCHEMA_VERSION = 1

_UPDATABLE_COLUMNS = frozenset(
    {
        "stage",
        "waiting_reason",
        "progress",
        "playback_note",
        "subtitle_note",
        "next_check_at",
        "remote_id",
        "wait_mode",
        "fallback_deadline",
        "fallback_applied",
    }
)


# A row in ``refreshes`` under this name is the "media changed" note, not a webhook item.
_MEDIA_CHANGED_APP = "media"


class StoreError(Exception):
    """Base error for request store operations."""


class CorruptStoreError(StoreError):
    """Raised when the database schema or file cannot be safely read."""


class StoreBusyError(StoreError):
    """The database is locked by someone else right now; trying again later is fine."""


def _translate(error: sqlite3.Error) -> StoreError:
    """Turn a raw sqlite error into a StoreError with a plain message (no request data)."""
    text = str(error).lower()
    if "locked" in text or "busy" in text:
        return StoreBusyError("The request database is busy. Try again in a moment.")
    if (
        "malformed" in text
        or "not a database" in text
        or "encrypted" in text
        or (isinstance(error, sqlite3.OperationalError) and "no such table" in text)
        or (isinstance(error, sqlite3.OperationalError) and "no such column" in text)
    ):
        return CorruptStoreError("The request database is damaged.")
    if "full" in text:
        return StoreError("The request database could not be written because the disk is full.")
    return StoreError("The request database could not be used.")


def _snapshot_policy(created_at: float, setup: Any) -> tuple[str | None, float | None]:
    if setup is None:
        return None, None
    mode = getattr(setup, "request_wait_mode", "timed")
    hours = getattr(setup, "request_wait_hours", 24)
    if mode == "always":
        return "always", None
    if mode == "never":
        return "never", float(created_at)
    return "timed", float(created_at) + float(hours) * 3600.0


class RequestStore:
    """Manages transactional persistence for media requests."""

    def __init__(self, path: Path | str, *, busy_timeout_ms: int = 5000) -> None:
        self.path = Path(path)
        self._busy_timeout_ms = int(busy_timeout_ms)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path, timeout=self._busy_timeout_ms / 1000.0, isolation_level=None
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        return conn

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        """A connection whose sqlite errors surface as StoreError; always closed."""
        conn: sqlite3.Connection | None = None
        try:
            conn = self._connect()
            yield conn
        except sqlite3.Error as error:
            raise _translate(error) from None
        finally:
            if conn is not None:
                conn.close()

    @contextmanager
    def _txn(self) -> Iterator[sqlite3.Connection]:
        """One write transaction: commit on success, roll back only if one is open."""
        with self._conn() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                raise

    def _init_db(self) -> None:
        with self._txn() as conn:
            tables = {
                r["name"]
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            row = (
                conn.execute("SELECT version FROM schema_version").fetchone()
                if "schema_version" in tables
                else None
            )
            if row is None and "requests" in tables:
                raise CorruptStoreError("The request database is not one Orama created.")
            if row is not None and "requests" not in tables:
                raise CorruptStoreError("The request database is damaged.")

            if row is None:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)"
                )
                conn.execute(
                    "INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,)
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS requests (
                        id TEXT PRIMARY KEY,
                        target_key TEXT UNIQUE NOT NULL,
                        kind TEXT NOT NULL,
                        source_id INTEGER NOT NULL,
                        season INTEGER,
                        episode INTEGER,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        revision INTEGER NOT NULL DEFAULT 1,
                        stage TEXT NOT NULL DEFAULT 'requested',
                        waiting_reason TEXT,
                        progress INTEGER,
                        playback_note TEXT,
                        subtitle_note TEXT,
                        next_check_at REAL NOT NULL,
                        claim_owner TEXT,
                        claim_expires_at REAL,
                        remote_id INTEGER,
                        wait_mode TEXT,
                        fallback_deadline REAL,
                        fallback_applied INTEGER NOT NULL DEFAULT 0,
                        title TEXT,
                        year INTEGER,
                        refresh_hint_at REAL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS refreshes (
                        app TEXT NOT NULL,
                        remote_id INTEGER NOT NULL,
                        enqueued_at REAL NOT NULL,
                        PRIMARY KEY (app, remote_id)
                    )
                    """
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_due ON requests (next_check_at)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_stage ON requests (stage)")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS followups (
                        request_id TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        reason_code TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        PRIMARY KEY (request_id, kind, reason_code),
                        FOREIGN KEY (request_id) REFERENCES requests(id) ON DELETE CASCADE
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS episode_targets (
                        series_id INTEGER NOT NULL,
                        season INTEGER NOT NULL,
                        episode INTEGER NOT NULL,
                        title TEXT NOT NULL DEFAULT '',
                        stage TEXT NOT NULL DEFAULT 'searching',
                        has_file INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (series_id, season, episode)
                    )
                    """
                )
            else:
                db_version = row["version"]
                if db_version > SCHEMA_VERSION:
                    raise CorruptStoreError(
                        f"Database schema version {db_version} is newer than supported {SCHEMA_VERSION}"
                    )
                existing_cols = {
                    r["name"] for r in conn.execute("PRAGMA table_info(requests)").fetchall()
                }
                for col_name, col_type in (
                    ("remote_id", "INTEGER"),
                    ("wait_mode", "TEXT"),
                    ("fallback_deadline", "REAL"),
                    ("fallback_applied", "INTEGER NOT NULL DEFAULT 0"),
                    ("title", "TEXT"),
                    ("year", "INTEGER"),
                    ("refresh_hint_at", "REAL"),
                ):
                    if col_name not in existing_cols:
                        conn.execute(f"ALTER TABLE requests ADD COLUMN {col_name} {col_type}")
                if db_version < SCHEMA_VERSION:
                    conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS followups (
                        request_id TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        reason_code TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        PRIMARY KEY (request_id, kind, reason_code),
                        FOREIGN KEY (request_id) REFERENCES requests(id) ON DELETE CASCADE
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS episode_targets (
                        series_id INTEGER NOT NULL,
                        season INTEGER NOT NULL,
                        episode INTEGER NOT NULL,
                        title TEXT NOT NULL DEFAULT '',
                        stage TEXT NOT NULL DEFAULT 'searching',
                        has_file INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (series_id, season, episode)
                    )
                    """
                )

        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _row_to_record(self, row: sqlite3.Row) -> RequestRecord:
        spec = RequestSpec.from_stored(
            row["kind"], row["source_id"], row["season"], row["episode"]
        )
        keys = row.keys()
        return RequestRecord(
            id=row["id"],
            spec=spec,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            revision=row["revision"],
            stage=row["stage"],
            waiting_reason=row["waiting_reason"],
            progress=row["progress"],
            playback_note=row["playback_note"],
            subtitle_note=row["subtitle_note"],
            next_check_at=row["next_check_at"],
            claim_owner=row["claim_owner"],
            claim_expires_at=row["claim_expires_at"],
            remote_id=row["remote_id"] if "remote_id" in keys else None,
            wait_mode=row["wait_mode"] if "wait_mode" in keys else None,
            fallback_deadline=row["fallback_deadline"] if "fallback_deadline" in keys else None,
            fallback_applied=bool(row["fallback_applied"]) if "fallback_applied" in keys else False,
            title=row["title"] if "title" in keys else None,
            year=row["year"] if "year" in keys else None,
        )

    def create(
        self,
        spec: RequestSpec,
        now: float,
        *,
        setup: Any = None,
        title: str | None = None,
        year: int | None = None,
    ) -> tuple[RequestRecord, bool]:
        """Create a new media request or return existing matching record idempotently."""
        target_key = spec.target_key
        record_id = str(uuid.uuid4())
        wait_mode, fallback_deadline = _snapshot_policy(now, setup)
        with self._txn() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO requests (
                        id, target_key, kind, source_id, season, episode,
                        created_at, updated_at, revision, stage, next_check_at,
                        wait_mode, fallback_deadline, fallback_applied, title, year
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'requested', ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        record_id,
                        target_key,
                        spec.kind,
                        spec.source_id,
                        spec.season,
                        spec.episode,
                        now,
                        now,
                        now,
                        wait_mode,
                        fallback_deadline,
                        title,
                        year,
                    ),
                )
                rec = RequestRecord(
                    id=record_id,
                    spec=spec,
                    created_at=now,
                    updated_at=now,
                    revision=1,
                    stage="requested",
                    next_check_at=now,
                    wait_mode=wait_mode,
                    fallback_deadline=fallback_deadline,
                    fallback_applied=False,
                    title=title,
                    year=year,
                )
                return rec, True
            except sqlite3.IntegrityError:
                row = conn.execute(
                    "SELECT * FROM requests WHERE target_key = ?", (target_key,)
                ).fetchone()
                if row is not None and title and not row["title"]:
                    conn.execute(
                        "UPDATE requests SET title = ?, year = COALESCE(year, ?), updated_at = ? WHERE id = ?",
                        (title, year, now, row["id"]),
                    )
                    row = conn.execute(
                        "SELECT * FROM requests WHERE target_key = ?", (target_key,)
                    ).fetchone()
                if row is None:
                    raise StoreError("Integrity error on target key but existing row not found")
                if row["stage"] in ("ready", "unavailable") and row["kind"] != "series":
                    # Asking again for a finished item starts the same record again. The
                    # remote id is kept, so nothing is added twice to Radarr or Sonarr.
                    conn.execute(
                        """
                        UPDATE requests
                        SET stage = 'requested', waiting_reason = NULL, progress = NULL,
                            playback_note = NULL, subtitle_note = NULL,
                            next_check_at = ?, refresh_hint_at = ?,
                            wait_mode = ?, fallback_deadline = ?,
                            fallback_applied = 0, revision = revision + 1, updated_at = ?
                        WHERE id = ?
                        """,
                        (now, now, wait_mode, fallback_deadline, now, row["id"]),
                    )
                    row = conn.execute(
                        "SELECT * FROM requests WHERE id = ?", (row["id"],)
                    ).fetchone()
                return self._row_to_record(row), False

    def get(self, request_id: str) -> RequestRecord | None:
        """Fetch request record by UUID."""
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
            return self._row_to_record(row) if row else None

    def list_active(self, limit: int = 100) -> list[RequestRecord]:
        """List active requests (unresolved requests plus ongoing series subscriptions)."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM requests
                WHERE stage != 'ready' OR kind = 'series'
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [self._row_to_record(r) for r in rows]

    def list_recent(self, limit: int = 20) -> list[RequestRecord]:
        """List recent requests ordered newest first for Home."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM requests
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [self._row_to_record(r) for r in rows]

    def update(
        self,
        request_id: str,
        expected_revision: int,
        changes: RequestUpdate,
        *,
        now: float | None = None,
    ) -> bool:
        """Optimistically update request record; returns False if revision is stale."""
        fields: list[str] = []
        values: list[Any] = []

        for col in (
            "stage",
            "waiting_reason",
            "progress",
            "playback_note",
            "subtitle_note",
            "next_check_at",
            "remote_id",
            "wait_mode",
            "fallback_deadline",
            "fallback_applied",
        ):
            if col in changes.provided_fields and col in _UPDATABLE_COLUMNS:
                val = getattr(changes, col)
                if col == "fallback_applied" and val is not None:
                    val = 1 if val else 0
                fields.append(f"{col} = ?")
                values.append(val)

        if not fields:
            return True

        fields.append("revision = revision + 1")
        fields.append("updated_at = ?")
        values.append(time.time() if now is None else float(now))
        set_clause = ", ".join(fields)
        values.extend([request_id, expected_revision])

        with self._txn() as conn:
            cur = conn.execute(
                f"UPDATE requests SET {set_clause} WHERE id = ? AND revision = ?",
                values,
            )
            success = cur.rowcount > 0
            return success

    def enqueue_refresh(self, app: str, remote_id: int, now: float) -> None:
        """Enqueue an authenticated webhook event notification."""
        if app not in ("sonarr", "radarr"):
            raise ValueError(f"Unsupported webhook app: {app!r}")
        if type(remote_id) is not int or remote_id <= 0:
            raise ValueError("remote_id must be a positive integer")
        with self._txn() as conn:
            conn.execute(
                """
                INSERT INTO refreshes (app, remote_id, enqueued_at)
                VALUES (?, ?, ?)
                ON CONFLICT(app, remote_id) DO UPDATE SET enqueued_at = excluded.enqueued_at
                """,
                (app, remote_id, now),
            )
            if app == "radarr":
                conn.execute(
                    """
                    UPDATE requests
                    SET next_check_at = MIN(next_check_at, ?), refresh_hint_at = ?
                    WHERE kind = 'movie' AND (remote_id = ? OR source_id = ?)
                    """,
                    (now, now, remote_id, remote_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE requests
                    SET next_check_at = MIN(next_check_at, ?), refresh_hint_at = ?
                    WHERE kind != 'movie' AND (remote_id = ? OR source_id = ?)
                    """,
                    (now, now, remote_id, remote_id),
                )

    def request_recheck(self, request_id: str, now: float) -> None:
        """Ask for an early check (Retry button); kept even if a worker holds the record."""
        with self._txn() as conn:
            conn.execute(
                """
                UPDATE requests
                SET next_check_at = MIN(next_check_at, ?), refresh_hint_at = ?
                WHERE id = ?
                """,
                (now, now, request_id),
            )

    def drain_refreshes(self) -> list[tuple[str, int]]:
        """Retrieve and clear all pending webhook refresh items."""
        with self._txn() as conn:
            rows = conn.execute(
                "SELECT app, remote_id FROM refreshes WHERE app != ?", (_MEDIA_CHANGED_APP,)
            ).fetchall()
            items = [(r["app"], r["remote_id"]) for r in rows]
            conn.execute("DELETE FROM refreshes WHERE app != ?", (_MEDIA_CHANGED_APP,))
            return items

    def mark_media_changed(self, now: float) -> None:
        """Note that Sonarr or Radarr changed a media file (one row, however many events)."""
        with self._txn() as conn:
            conn.execute(
                """
                INSERT INTO refreshes (app, remote_id, enqueued_at) VALUES (?, 0, ?)
                ON CONFLICT(app, remote_id) DO UPDATE SET enqueued_at = excluded.enqueued_at
                """,
                (_MEDIA_CHANGED_APP, now),
            )

    def take_media_changed(self) -> bool:
        """Return whether media changed since the last call, and clear the note."""
        with self._txn() as conn:
            cur = conn.execute("DELETE FROM refreshes WHERE app = ?", (_MEDIA_CHANGED_APP,))
            return cur.rowcount > 0

    def claim_due(self, owner: str, now: float, lease_seconds: float = 60.0) -> RequestRecord | None:
        """Atomically claim an eligible request for background processing."""
        with self._txn() as conn:
            row = conn.execute(
                """
                SELECT * FROM requests
                WHERE next_check_at <= ?
                  AND (claim_expires_at IS NULL OR claim_expires_at <= ?)
                ORDER BY
                  CASE WHEN stage != 'ready' THEN 0 ELSE 1 END ASC,
                  next_check_at ASC
                LIMIT 1
                """,
                (now, now),
            ).fetchone()

            if row is None:
                return None

            rec_id = row["id"]
            expires_at = now + lease_seconds
            conn.execute(
                """
                UPDATE requests
                SET claim_owner = ?, claim_expires_at = ?, refresh_hint_at = NULL
                WHERE id = ?
                """,
                (owner, expires_at, rec_id),
            )
            updated_row = conn.execute("SELECT * FROM requests WHERE id = ?", (rec_id,)).fetchone()
            return self._row_to_record(updated_row) if updated_row else None

    def release(self, request_id: str, owner: str, next_check_at: float) -> None:
        """Release claim on a request and set its next check schedule."""
        with self._txn() as conn:
            conn.execute(
                """
                UPDATE requests
                SET claim_owner = NULL, claim_expires_at = NULL,
                    next_check_at = CASE
                        WHEN refresh_hint_at IS NOT NULL THEN MIN(?, refresh_hint_at)
                        ELSE ? END,
                    refresh_hint_at = NULL
                WHERE id = ? AND (claim_owner = ? OR claim_owner IS NULL)
                """,
                (next_check_at, next_check_at, request_id, owner),
            )

    def record_followup(
        self, request_id: str, kind: str, reason_code: str, now: float
    ) -> None:
        """Record a typed follow-up reason for Release 2 conversion/translation."""
        if kind not in ("conversion", "translation"):
            raise ValueError(f"Invalid follow-up kind: {kind!r}")
        if (
            not isinstance(reason_code, str)
            or not (1 <= len(reason_code) <= 64)
            or not all(c.isalnum() or c == "_" for c in reason_code)
        ):
            raise ValueError(f"Invalid follow-up reason_code: {reason_code!r}")

        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO followups (request_id, kind, reason_code, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(request_id, kind, reason_code) DO NOTHING
                """,
                (request_id, kind, reason_code, now),
            )

    def list_followups(self, kind: str | None = None) -> list[dict[str, Any]]:
        """Return recorded typed follow-up entries."""
        with self._conn() as conn:
            if kind is not None:
                rows = conn.execute(
                    "SELECT request_id, kind, reason_code, created_at FROM followups WHERE kind = ? ORDER BY created_at ASC",
                    (kind,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT request_id, kind, reason_code, created_at FROM followups ORDER BY created_at ASC"
                ).fetchall()
            return [dict(r) for r in rows]

    def upsert_episode_target(
        self,
        series_id: int,
        season: int,
        episode: int,
        *,
        title: str = "",
        stage: str = "searching",
        has_file: bool = False,
    ) -> None:
        """Record or update a shared episode target for TV requests."""
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO episode_targets (series_id, season, episode, title, stage, has_file)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(series_id, season, episode) DO UPDATE SET
                    title = CASE WHEN excluded.title != '' THEN excluded.title ELSE episode_targets.title END,
                    stage = excluded.stage,
                    has_file = excluded.has_file
                """,
                (series_id, season, episode, title, stage, 1 if has_file else 0),
            )

    def list_episode_targets(
        self, series_id: int, season: int | None = None
    ) -> list[EpisodeTarget]:
        """List recorded episode targets for a TV series or season."""
        with self._conn() as conn:
            if season is not None:
                rows = conn.execute(
                    """
                    SELECT series_id, season, episode, title, stage, has_file
                    FROM episode_targets
                    WHERE series_id = ? AND season = ?
                    ORDER BY season ASC, episode ASC
                    """,
                    (series_id, season),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT series_id, season, episode, title, stage, has_file
                    FROM episode_targets
                    WHERE series_id = ?
                    ORDER BY season ASC, episode ASC
                    """,
                    (series_id,),
                ).fetchall()
            return [
                EpisodeTarget(
                    series_id=r["series_id"],
                    season=r["season"],
                    episode=r["episode"],
                    title=r["title"],
                    stage=r["stage"],
                    has_file=bool(r["has_file"]),
                )
                for r in rows
            ]

