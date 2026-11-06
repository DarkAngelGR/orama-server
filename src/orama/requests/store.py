"""Durable SQLite storage for media requests and background work."""
from __future__ import annotations

import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from .models import RequestRecord, RequestSpec, RequestUpdate

SCHEMA_VERSION = 1


class StoreError(Exception):
    """Base error for request store operations."""


class CorruptStoreError(StoreError):
    """Raised when the database schema or file cannot be safely read."""


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
            # Verify or initialize schema
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
                        claim_expires_at REAL
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
            else:
                db_version = row["version"]
                if db_version > SCHEMA_VERSION:
                    raise CorruptStoreError(
                        f"Database schema version {db_version} is newer than supported {SCHEMA_VERSION}"
                    )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

        # Enforce secure file permissions (0o600)
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
        )

    def create(self, spec: RequestSpec, now: float) -> tuple[RequestRecord, bool]:
        """Create a new media request or return existing matching record idempotently."""
        target_key = spec.target_key
        record_id = str(uuid.uuid4())
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    INSERT INTO requests (
                        id, target_key, kind, source_id, season, episode,
                        created_at, updated_at, revision, stage, next_check_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'requested', ?)
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
                )
                return rec, True
            except sqlite3.IntegrityError:
                # Target already requested; retrieve existing
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
        """List active requests (not completed or terminal)."""
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT * FROM requests
                WHERE stage != 'ready'
                ORDER BY created_at ASC
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

        if changes.stage is not None:
            fields.append("stage = ?")
            values.append(changes.stage)
        if changes.waiting_reason is not None:
            fields.append("waiting_reason = ?")
            values.append(changes.waiting_reason)
        if changes.progress is not None:
            fields.append("progress = ?")
            values.append(changes.progress)
        if changes.playback_note is not None:
            fields.append("playback_note = ?")
            values.append(changes.playback_note)
        if changes.subtitle_note is not None:
            fields.append("subtitle_note = ?")
            values.append(changes.subtitle_note)
        if changes.next_check_at is not None:
            fields.append("next_check_at = ?")
            values.append(changes.next_check_at)

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
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO refreshes (app, remote_id, enqueued_at)
                VALUES (?, ?, ?)
                ON CONFLICT(app, remote_id) DO UPDATE SET enqueued_at = excluded.enqueued_at
                """,
                (app, remote_id, now),
            )
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
                  AND stage != 'ready'
                ORDER BY next_check_at ASC
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
