"""Persist approximate engagement without exposing the people behind share links.

Callers enforce publication and authentication rules. Each mutation acquires its
SQLite write lock before reading counters, protecting concurrent server workers.
"""

from __future__ import annotations

import hashlib
import math
import secrets
import sqlite3
import time
from typing import Any


MAX_LISTEN_SECONDS = 300
_SHARE_FIELDS = """
    links.code, links.job_id, links.share_count, links.click_count,
    (SELECT COUNT(*) FROM engagement_visitors AS visitors
     WHERE visitors.code = links.code) AS unique_clicks
"""


def init_schema(database: sqlite3.Connection) -> None:
    """Create an additive, restart-safe schema in the existing web database."""
    database.executescript("""
        CREATE TABLE IF NOT EXISTS engagement_listening (
            job_id TEXT PRIMARY KEY,
            listening_seconds REAL NOT NULL DEFAULT 0 CHECK (listening_seconds >= 0)
        );
        CREATE TABLE IF NOT EXISTS engagement_links (
            code TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            sharer_key TEXT NOT NULL,
            user_sub TEXT,
            created_at INTEGER NOT NULL,
            share_count INTEGER NOT NULL DEFAULT 0 CHECK (share_count >= 0),
            click_count INTEGER NOT NULL DEFAULT 0 CHECK (click_count >= 0),
            UNIQUE (job_id, sharer_key)
        );
        CREATE INDEX IF NOT EXISTS engagement_links_sharer
            ON engagement_links(sharer_key, created_at DESC);
        CREATE TABLE IF NOT EXISTS engagement_visitors (
            code TEXT NOT NULL REFERENCES engagement_links(code) ON DELETE CASCADE,
            visitor_key TEXT NOT NULL,
            PRIMARY KEY (code, visitor_key)
        );
    """)


def account_key(sub: str) -> str:
    return "account:" + sub


def guest_key(token: str) -> str:
    return "guest:" + hashlib.sha256(token.encode()).hexdigest()


def add_listening(database: sqlite3.Connection, job_id: str, seconds: Any) -> float:
    """Accumulate a bounded heartbeat, rejecting booleans and non-finite numbers."""
    if (type(seconds) not in (int, float) or not 0 < seconds <= MAX_LISTEN_SECONDS
            or not math.isfinite(seconds)):
        raise ValueError("seconds must be a finite number greater than 0 and at most 300.")
    with database:
        database.execute(
            """INSERT INTO engagement_listening(job_id, listening_seconds) VALUES (?, ?)
               ON CONFLICT(job_id) DO UPDATE SET
                   listening_seconds = engagement_listening.listening_seconds + excluded.listening_seconds""",
            (job_id, seconds),
        )
        return float(database.execute(
            "SELECT listening_seconds FROM engagement_listening WHERE job_id = ?", (job_id,),
        ).fetchone()[0])


def _stats(row: Any) -> dict[str, Any]:
    result = dict(zip(("code", "job_id", "share_count", "click_count", "unique_clicks"), row))
    result["url"] = f"/?share={result['code']}#read/{result['job_id']}"
    return result


def get_share(database: sqlite3.Connection, code: str) -> dict[str, Any]:
    """Return only public fields; possession of a code never reveals its owner."""
    row = database.execute(
        f"SELECT {_SHARE_FIELDS} FROM engagement_links AS links WHERE links.code = ?", (code,),
    ).fetchone()
    if row is None:
        raise LookupError("Share link not found.")
    return _stats(row)


def prepare_share(
    database: sqlite3.Connection, job_id: str, sharer_key: str, user_sub: str | None = None,
) -> dict[str, Any]:
    """Reuse a person's opaque link without counting preparation as a share."""
    with database:
        database.execute(
            """INSERT INTO engagement_links(code, job_id, sharer_key, user_sub, created_at)
               VALUES (?, ?, ?, ?, ?) ON CONFLICT(job_id, sharer_key) DO NOTHING""",
            (secrets.token_urlsafe(24), job_id, sharer_key, user_sub, int(time.time())),
        )
        row = database.execute(
            f"""SELECT {_SHARE_FIELDS} FROM engagement_links AS links
                WHERE links.job_id = ? AND links.sharer_key = ?""", (job_id, sharer_key),
        ).fetchone()
        return _stats(row)


def record_share(database: sqlite3.Connection, code: str, sharer_key: str) -> dict[str, Any]:
    """Only the creator may report successful native sharing or copying."""
    with database:
        changed = database.execute(
            """UPDATE engagement_links SET share_count = share_count + 1
               WHERE code = ? AND sharer_key = ?""", (code, sharer_key),
        ).rowcount
        result = get_share(database, code)
        if not changed:
            raise PermissionError("Only the creator of this link can record a share.")
        return result


def record_click(database: sqlite3.Connection, code: str, visitor_key: str) -> dict[str, Any]:
    """Count landings and distinct visitors together, ignoring the link creator."""
    with database:
        changed = database.execute(
            """UPDATE engagement_links SET click_count = click_count + 1
               WHERE code = ? AND sharer_key != ?""", (code, visitor_key),
        ).rowcount
        if changed:
            database.execute(
                "INSERT OR IGNORE INTO engagement_visitors(code, visitor_key) VALUES (?, ?)",
                (code, visitor_key),
            )
        return get_share(database, code)


def sharing_rows(database: sqlite3.Connection, sharer_key: str | None) -> list[dict[str, Any]]:
    """Return private dashboard rows; a None filter is reserved for administrators."""
    query = f"SELECT {_SHARE_FIELDS}, links.user_sub FROM engagement_links AS links"
    parameters: tuple[str, ...] = ()
    if sharer_key is not None:
        query += " WHERE links.sharer_key = ?"
        parameters = (sharer_key,)
    query += " ORDER BY links.created_at DESC, links.code ASC"
    return [{**_stats(row[:5]), "user_sub": row[5]} for row in database.execute(query, parameters)]
