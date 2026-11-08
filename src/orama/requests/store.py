"""Durable SQLite storage for media requests and background work."""
from __future__ import annotations

import os
import sqlite3
import uuid
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


class StoreError(Exception):
    """Base error for request store operations."""


class CorruptStoreError(StoreError):
    """Raised when the database schema or file cannot be safely read."""


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

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT version FROM schema_version").fetchone()
            except sqlite3.OperationalError:
                row = None

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
                        year INTEGER
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
                ):
                    if col_name not in existing_cols:
                        conn.execute(f"ALTER TABLE requests ADD COLUMN {col_name} {col_type}")
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
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _row_to_record(self, row: sqlite3.Row) -> RequestRecord:
        spec = RequestSpec(
            kind=row["kind"],  # type: ignore[arg-type]
            source_id=row["source_id"],
            season=row["season"],
            episode=row["episode"],
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
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
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
                conn.execute("COMMIT")
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
                        "UPDATE requests SET title = ?, year = COALESCE(year, ?) WHERE id = ?",
                        (title, year, row["id"]),
                    )
                    row = conn.execute(
                        "SELECT * FROM requests WHERE target_key = ?", (target_key,)
                    ).fetchone()
                conn.execute("COMMIT")
                if row is None:
                    raise StoreError("Integrity error on target key but existing row not found")
                return self._row_to_record(row), False
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def get(self, request_id: str) -> RequestRecord | None:
        """Fetch request record by UUID."""
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
            return self._row_to_record(row) if row else None
        finally:
            conn.close()

    def list_active(self, limit: int = 100) -> list[RequestRecord]:
        """List active requests (unresolved requests plus ongoing series subscriptions)."""
        conn = self._connect()
        try:
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
        finally:
            conn.close()

    def list_recent(self, limit: int = 20) -> list[RequestRecord]:
        """List recent requests ordered newest first for Home."""
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT * FROM requests
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [self._row_to_record(r) for r in rows]
        finally:
            conn.close()

    def update(self, request_id: str, expected_revision: int, changes: RequestUpdate) -> bool:
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
        set_clause = ", ".join(fields)
        values.extend([request_id, expected_revision])

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                f"UPDATE requests SET {set_clause} WHERE id = ? AND revision = ?",
                values,
            )
            success = cur.rowcount > 0
            conn.execute("COMMIT")
            return success
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def enqueue_refresh(self, app: str, remote_id: int, now: float) -> None:
        """Enqueue an authenticated webhook event notification."""
        if app not in ("sonarr", "radarr"):
            raise ValueError(f"Unsupported webhook app: {app!r}")
        if type(remote_id) is not int or remote_id <= 0:
            raise ValueError("remote_id must be a positive integer")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
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
                    SET next_check_at = MIN(next_check_at, ?)
                    WHERE kind = 'movie' AND (remote_id = ? OR source_id = ?)
                    """,
                    (now, remote_id, remote_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE requests
                    SET next_check_at = MIN(next_check_at, ?)
                    WHERE kind != 'movie' AND (remote_id = ? OR source_id = ?)
                    """,
                    (now, remote_id, remote_id),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def drain_refreshes(self) -> list[tuple[str, int]]:
        """Retrieve and clear all pending webhook refresh items."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT app, remote_id FROM refreshes").fetchall()
            items = [(r["app"], r["remote_id"]) for r in rows]
            conn.execute("DELETE FROM refreshes")
            conn.execute("COMMIT")
            return items
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def claim_due(self, owner: str, now: float, lease_seconds: float = 60.0) -> RequestRecord | None:
        """Atomically claim an eligible request for background processing."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM requests
                WHERE next_check_at <= ?
                  AND (claim_expires_at IS NULL OR claim_expires_at <= ?)
                  AND (
                      stage != 'ready'
                      OR kind = 'series'
                      OR playback_note IS NULL
                      OR playback_note LIKE '%not checked%'
                      OR subtitle_note IS NULL
                      OR subtitle_note LIKE '%not checked%'
                  )
                ORDER BY
                  CASE WHEN stage != 'ready' THEN 0 ELSE 1 END ASC,
                  next_check_at ASC
                LIMIT 1
                """,
                (now, now),
            ).fetchone()

            if row is None:
                conn.execute("COMMIT")
                return None

            rec_id = row["id"]
            expires_at = now + lease_seconds
            conn.execute(
                """
                UPDATE requests
                SET claim_owner = ?, claim_expires_at = ?
                WHERE id = ?
                """,
                (owner, expires_at, rec_id),
            )
            conn.execute("COMMIT")
            updated_row = conn.execute("SELECT * FROM requests WHERE id = ?", (rec_id,)).fetchone()
            return self._row_to_record(updated_row) if updated_row else None
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def release(self, request_id: str, owner: str, next_check_at: float) -> None:
        """Release claim on a request and set its next check schedule."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE requests
                SET claim_owner = NULL, claim_expires_at = NULL, next_check_at = ?
                WHERE id = ? AND (claim_owner = ? OR claim_owner IS NULL)
                """,
                (next_check_at, request_id, owner),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

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

        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO followups (request_id, kind, reason_code, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(request_id, kind, reason_code) DO NOTHING
                """,
                (request_id, kind, reason_code, now),
            )
        finally:
            conn.close()

    def list_followups(self, kind: str | None = None) -> list[dict[str, Any]]:
        """Return recorded typed follow-up entries."""
        conn = self._connect()
        try:
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
        finally:
            conn.close()

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
        conn = self._connect()
        try:
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
        finally:
            conn.close()

    def list_episode_targets(
        self, series_id: int, season: int | None = None
    ) -> list[EpisodeTarget]:
        """List recorded episode targets for a TV series or season."""
        conn = self._connect()
        try:
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
        finally:
            conn.close()

