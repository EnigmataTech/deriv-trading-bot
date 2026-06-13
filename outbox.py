"""Local SQLite write-buffer (outbox) for the VPS trader.

When the cluster Postgres is unreachable (e.g. the reverse-SSH tunnel drops),
trade/balance writes would otherwise be lost. Instead they are appended here as
JSON-encoded operations and replayed in order once Postgres comes back.

Stdlib only (sqlite3 + json) so it has no dependency on the Postgres engine and
keeps working precisely when that engine is down. A fresh connection per call
sidesteps cross-thread sharing between the asyncio loop and the monitor.
"""
import json
import logging
import os
import sqlite3
from datetime import datetime
from typing import Callable, List, Tuple

logger = logging.getLogger(__name__)

# Co-located with the bot by default; override on the VPS via env if desired.
OUTBOX_DB_PATH = os.getenv(
    "OUTBOX_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "outbox.db"),
)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(OUTBOX_DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS outbox (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            op         TEXT NOT NULL,
            payload    TEXT NOT NULL,
            created_at TEXT NOT NULL,
            attempts   INTEGER NOT NULL DEFAULT 0
        )"""
    )
    return conn


def enqueue(op: str, payload: dict) -> None:
    """Append a write operation to the outbox. Never raises into the caller —
    a failed buffer write only logs (we are already on a degraded path)."""
    try:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO outbox (op, payload, created_at) VALUES (?, ?, ?)",
                (op, json.dumps(payload), datetime.utcnow().isoformat()),
            )
            conn.commit()
        finally:
            conn.close()
        logger.warning("Buffered '%s' to local outbox (Postgres unreachable)", op)
    except Exception as e:  # pragma: no cover - last-resort
        logger.error("Outbox enqueue failed for '%s': %s", op, e)


def pending_count() -> int:
    try:
        conn = _connect()
        try:
            return conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        finally:
            conn.close()
    except Exception:
        return 0


def _fetch_batch(conn: sqlite3.Connection, limit: int) -> List[Tuple[int, str, str]]:
    return conn.execute(
        "SELECT id, op, payload FROM outbox ORDER BY id ASC LIMIT ?", (limit,)
    ).fetchall()


def replay(apply_fn: Callable[[str, dict], None], batch: int = 200) -> int:
    """Replay buffered ops in id order via apply_fn(op, payload).

    Stops at the first op that raises (keeping it and everything after it for a
    later attempt) so ordering is preserved and a sustained outage doesn't drain
    the queue into the void. Returns the number successfully replayed.
    """
    replayed = 0
    try:
        conn = _connect()
    except Exception as e:
        logger.error("Outbox open failed during replay: %s", e)
        return 0
    try:
        while True:
            rows = _fetch_batch(conn, batch)
            if not rows:
                break
            for row_id, op, payload_json in rows:
                payload = json.loads(payload_json)
                try:
                    apply_fn(op, payload)
                except Exception as e:
                    conn.execute(
                        "UPDATE outbox SET attempts = attempts + 1 WHERE id = ?",
                        (row_id,),
                    )
                    conn.commit()
                    logger.warning(
                        "Outbox replay paused at id=%s op=%s: %s", row_id, op, e
                    )
                    return replayed
                conn.execute("DELETE FROM outbox WHERE id = ?", (row_id,))
                conn.commit()
                replayed += 1
    finally:
        conn.close()
    if replayed:
        logger.info("Outbox replayed %d buffered write(s) to Postgres", replayed)
    return replayed
