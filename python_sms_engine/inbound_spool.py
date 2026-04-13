"""
Inbound SMS spool — durable SQLite buffer between modem receipt and Laravel ACK.

Write flow:
  1. Message arrives via +CMT unsolicited on serial
  2. insert() — written to spool before SIM delete
  3. AT+CMGD — delete from SIM (safe because spool has it)
  4. POST to Laravel webhook
  5. mark_delivered() on 200 ACK

On engine restart, get_pending() surfaces undelivered records so the retry
loop can re-deliver them before listening for new messages.
"""

import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional


DB_FILE = "inbound_spool.db"

_DDL = """
CREATE TABLE IF NOT EXISTS inbound_spool (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key  TEXT    NOT NULL UNIQUE,
    runtime_sim_id   TEXT    NOT NULL,
    from_number      TEXT    NOT NULL,
    message          TEXT    NOT NULL,
    received_at      TEXT    NOT NULL,
    status           TEXT    NOT NULL DEFAULT 'pending',
    attempts         INTEGER NOT NULL DEFAULT 0,
    last_attempt_at  TEXT,
    created_at       TEXT    NOT NULL
);
"""


class InboundSpool:
    """
    Thread-safe SQLite spool for inbound SMS messages.

    Each instance owns a single SQLite connection with check_same_thread=False
    and a lock to serialise writes across the listener + retry threads.
    """

    def __init__(self, db_file: str = DB_FILE) -> None:
        self._db_file = db_file
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_file, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")  # safe for concurrent reads
        self._conn.execute(_DDL)
        self._conn.commit()

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def insert(
        self,
        runtime_sim_id: str,
        from_number: str,
        message: str,
        received_at: str,
        idempotency_key: Optional[str] = None,
    ) -> str:
        """
        Write a new inbound message to the spool.

        Returns the idempotency_key (generated if not provided).
        Safe to call from multiple threads.
        """
        key = idempotency_key or str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO inbound_spool
                        (idempotency_key, runtime_sim_id, from_number, message,
                         received_at, status, attempts, created_at)
                    VALUES (?, ?, ?, ?, ?, 'pending', 0, ?)
                    """,
                    (key, runtime_sim_id, from_number, message, received_at, now),
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                # duplicate idempotency_key — already spooled, skip
                pass

        return key

    def mark_delivered(self, idempotency_key: str) -> None:
        """Mark a spool record as delivered after confirmed Laravel ACK."""
        with self._lock:
            self._conn.execute(
                "UPDATE inbound_spool SET status='delivered' WHERE idempotency_key=?",
                (idempotency_key,),
            )
            self._conn.commit()

    def record_attempt(self, idempotency_key: str) -> None:
        """Increment attempt counter and update last_attempt_at timestamp."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                """
                UPDATE inbound_spool
                SET attempts = attempts + 1, last_attempt_at = ?
                WHERE idempotency_key = ?
                """,
                (now, idempotency_key),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    def get_pending(self) -> List[Dict]:
        """
        Return all undelivered spool records ordered by created_at ascending.
        Called at startup and by the retry loop.
        """
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT idempotency_key, runtime_sim_id, from_number, message,
                       received_at, attempts, last_attempt_at, created_at
                FROM inbound_spool
                WHERE status = 'pending'
                ORDER BY created_at ASC
                """
            ).fetchall()
        return [dict(r) for r in rows]

    def pending_count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM inbound_spool WHERE status='pending'"
            ).fetchone()
        return row[0] if row else 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()
