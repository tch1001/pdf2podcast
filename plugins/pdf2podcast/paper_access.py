"""Keep publication ownership separate from shared conversion work, so deduplication never grants publication control."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import uuid


def _create_alias(database: sqlite3.Connection, jobs: Path, source_job_id: str,
                  filename: str, timestamp: int) -> str:
    job_id = f"pdf-{uuid.uuid4().hex[:16]}"
    folder = jobs / job_id
    folder.mkdir(mode=0o700, parents=True)
    record = {
        "job_id": job_id, "source_job_id": source_job_id, "filename": filename,
        "created_at": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
    }
    temporary = folder / "job.json.tmp"
    temporary.write_text(json.dumps(record), encoding="utf-8")
    temporary.replace(folder / "job.json")
    database.execute("INSERT INTO job_aliases(job_id, source_job_id) VALUES(?, ?)",
                     (job_id, source_job_id))
    return job_id


def init_schema(database: sqlite3.Connection, jobs: Path, pro_emails: set[str]) -> None:
    columns = {row["name"] for row in database.execute("PRAGMA table_info(users)")}
    if "plan" not in columns:
        database.execute("ALTER TABLE users ADD COLUMN plan TEXT NOT NULL DEFAULT 'free' CHECK(plan IN ('free','pro'))")
    database.execute("CREATE TABLE IF NOT EXISTS job_aliases (job_id TEXT PRIMARY KEY, source_job_id TEXT NOT NULL)")
    database.execute("CREATE INDEX IF NOT EXISTS job_aliases_source ON job_aliases(source_job_id)")
    native_registry_exists = database.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='native_job_aliases'"
    ).fetchone() is not None
    database.execute("CREATE TABLE IF NOT EXISTS native_job_aliases (job_id TEXT PRIMARY KEY, source_job_id TEXT NOT NULL UNIQUE)")
    # Backfill only on introduction, never reclassify deleted web accounts on restart.
    if not native_registry_exists:
        database.execute(
            """INSERT OR IGNORE INTO native_job_aliases(job_id, source_job_id)
               SELECT documents.job_id, documents.job_id FROM documents
               WHERE NOT EXISTS(SELECT 1 FROM user_jobs WHERE user_jobs.job_id=documents.job_id)
                 AND NOT EXISTS(SELECT 1 FROM job_aliases WHERE job_aliases.source_job_id=documents.job_id
                                OR job_aliases.job_id=documents.job_id)"""
        )
    for email in pro_emails:
        database.execute("UPDATE users SET plan='pro' WHERE lower(email)=?", (email,))
    # Older versions linked several people to one publication. Retain the earliest
    # owner's URL and give other owners independent entries without regenerating media.
    shared = database.execute("SELECT job_id FROM user_jobs GROUP BY job_id HAVING COUNT(*) > 1").fetchall()
    for row in shared:
        owners = database.execute("SELECT * FROM user_jobs WHERE job_id=? ORDER BY linked_at,user_sub",
                                  (row["job_id"],)).fetchall()
        for owner in owners[1:]:
            alias = _create_alias(database, jobs, row["job_id"], owner["filename"], owner["linked_at"])
            database.execute("UPDATE user_jobs SET job_id=? WHERE user_sub=? AND job_id=?",
                             (alias, owner["user_sub"], row["job_id"]))


def create_native_alias(database: sqlite3.Connection, jobs: Path, source_job_id: str,
                        filename: str, timestamp: int) -> str:
    job_id = _create_alias(database, jobs, source_job_id, filename, timestamp)
    database.execute("INSERT INTO native_job_aliases(job_id,source_job_id) VALUES(?,?)", (job_id, source_job_id))
    return job_id


def link_upload(database: sqlite3.Connection, jobs: Path, source_job_id: str,
                user_sub: str, filename: str, source_bytes: int, timestamp: int,
                public_release: bool, terms_version: str | None, *, new_source: bool) -> str:
    owned = database.execute(
        """SELECT links.job_id FROM user_jobs AS links
           LEFT JOIN job_aliases AS aliases ON aliases.job_id=links.job_id
           WHERE links.user_sub=? AND (links.job_id=? OR aliases.source_job_id=?) LIMIT 1""",
        (user_sub, source_job_id, source_job_id),
    ).fetchone()
    if owned is not None:
        job_id = owned["job_id"]
    elif new_source:
        job_id = source_job_id
    else:
        job_id = _create_alias(database, jobs, source_job_id, filename, timestamp)
    database.execute(
        """INSERT INTO user_jobs(user_sub,job_id,filename,source_bytes,linked_at,
                                 public_release,terms_version,terms_accepted_at)
           VALUES(?,?,?,?,?,?,?,?)
           ON CONFLICT(user_sub,job_id) DO UPDATE SET
               filename=excluded.filename, public_release=excluded.public_release,
               terms_version=excluded.terms_version, terms_accepted_at=excluded.terms_accepted_at""",
        (user_sub, job_id, filename, source_bytes, timestamp, int(public_release), terms_version, timestamp),
    )
    return str(job_id)


def access_flags(database: sqlite3.Connection, job_id: str, user_sub: str | None = None) -> dict[str, bool]:
    row = database.execute(
        """SELECT
           EXISTS(SELECT 1 FROM user_jobs WHERE job_id=? AND public_release=1) AS is_public,
           EXISTS(SELECT 1 FROM user_jobs WHERE job_id=? AND user_sub=?) AS is_owner,
           (EXISTS(SELECT 1 FROM user_jobs WHERE job_id=?) OR
            EXISTS(SELECT 1 FROM job_aliases WHERE job_id=? OR source_job_id=?)) AS has_web_owner""",
        (job_id, job_id, user_sub, job_id, job_id, job_id),
    ).fetchone()
    return {key: bool(row[key]) for key in ("is_public", "is_owner", "has_web_owner")}


def set_visibility(database: sqlite3.Connection, job_id: str, user: dict,
                   visibility: str, terms_version: str, timestamp: int) -> None:
    if visibility not in {"public", "private"}:
        raise ValueError("Visibility must be public or private.")
    if not access_flags(database, job_id, user["sub"])["is_owner"]:
        raise FileNotFoundError(job_id)
    if user.get("plan") != "pro":
        raise PermissionError("Changing paper visibility is a Pro feature.")
    database.execute(
        """UPDATE user_jobs SET public_release=?, terms_version=?, terms_accepted_at=?
           WHERE user_sub=? AND job_id=?""",
        (int(visibility == "public"), terms_version, timestamp, user["sub"], job_id),
    )


def upload_is_public(identity: dict, requested: str | None) -> bool:
    if requested is not None and requested not in {"public", "private"}:
        raise ValueError("Visibility must be public or private.")
    if identity["kind"] != "web":
        if requested is not None:
            raise PermissionError("Sign in with a Google account to select upload visibility.")
        return False  # Preserve the existing native-token library, not Pro privacy.
    pro = identity["user"].get("plan") == "pro"
    if requested == "private" and not pro:
        raise PermissionError("Private uploads are a Pro feature.")
    return requested == "public" if requested is not None else not pro
