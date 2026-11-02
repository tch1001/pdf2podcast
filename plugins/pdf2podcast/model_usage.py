"""Attribute every real model attempt without charging cache hits or inventing missing usage."""

from __future__ import annotations

from contextlib import closing, contextmanager
from contextvars import ContextVar
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Any
import uuid


CONTEXT: ContextVar[dict[str, Any]] = ContextVar("pdf2podcast_model_context", default={})


def init_schema(database: sqlite3.Connection) -> None:
    database.executescript("""
        CREATE TABLE IF NOT EXISTS model_requests (
            id TEXT PRIMARY KEY,
            job_id TEXT,
            user_sub TEXT,
            stage TEXT,
            page INTEGER,
            provider TEXT NOT NULL,
            requested_model TEXT NOT NULL,
            model TEXT,
            attempt INTEGER NOT NULL,
            is_fallback INTEGER NOT NULL,
            started_at REAL NOT NULL,
            finished_at REAL,
            status TEXT NOT NULL,
            http_status INTEGER,
            request_id TEXT,
            response_id TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            total_tokens INTEGER,
            cached_input_tokens INTEGER,
            reasoning_tokens INTEGER,
            usage_json TEXT,
            validation_status TEXT,
            validation_error TEXT
        );
        CREATE INDEX IF NOT EXISTS model_requests_job ON model_requests(job_id, started_at);
        CREATE INDEX IF NOT EXISTS model_requests_user ON model_requests(user_sub, started_at);
    """)


def connection() -> sqlite3.Connection:
    path = CONTEXT.get().get("database") or (
        Path(os.environ.get("PDF2PODCAST_DATA_DIR", "~/.pdf2podcast")).expanduser() / "web.sqlite3"
    )
    database = sqlite3.connect(path, timeout=30)
    database.row_factory = sqlite3.Row
    return database


def bind_job(database_path: Path, job_id: str):
    """Charge work once to its initiating uploader, not every duplicate-paper owner."""
    with closing(sqlite3.connect(database_path, timeout=30)) as database:
        row = database.execute(
            "SELECT user_sub FROM upload_events WHERE job_id=? AND outcome='queued' ORDER BY id LIMIT 1",
            (job_id,),
        ).fetchone()
        if row is None:
            row = database.execute(
                "SELECT user_sub FROM user_jobs WHERE job_id=? ORDER BY linked_at, user_sub LIMIT 1",
                (job_id,),
            ).fetchone()
    return CONTEXT.set({"database": database_path, "job_id": job_id, "user_sub": row[0] if row else None})


@contextmanager
def request_context(**values: Any):
    token = CONTEXT.set({**CONTEXT.get(), **values})
    try:
        yield
    finally:
        CONTEXT.reset(token)


def begin(provider: str, model: str, attempt: int, is_fallback: bool) -> str:
    """Persist an unknown-usage attempt before sending; crashes cannot make it look free."""
    context = CONTEXT.get()
    identifier = uuid.uuid4().hex
    with closing(connection()) as database, database:
        database.execute(
            """INSERT INTO model_requests(
                id,job_id,user_sub,stage,page,provider,requested_model,model,
                attempt,is_fallback,started_at,status
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,'started')""",
            (identifier, context.get("job_id"), context.get("user_sub"), context.get("stage"),
             context.get("page"), provider, model, model, attempt, int(is_fallback), time.time()),
        )
    return identifier


def token_count(*values: Any) -> int | None:
    return next((value for value in values if type(value) is int and value >= 0), None)


def finish(
    identifier: str, status: str, payload: Any = None,
    http_status: int | None = None, request_id: str | None = None,
) -> None:
    envelope = payload if isinstance(payload, dict) else {}
    usage = envelope.get("usage")
    usage = usage if isinstance(usage, dict) else None
    values = usage or {}
    inputs = token_count(values.get("prompt_tokens"), values.get("input_tokens"))
    outputs = token_count(values.get("completion_tokens"), values.get("output_tokens"))
    total = token_count(values.get("total_tokens"))
    if total is None and inputs is not None and outputs is not None:
        total = inputs + outputs
    input_details = values.get("prompt_tokens_details") or values.get("input_tokens_details") or {}
    output_details = values.get("completion_tokens_details") or values.get("output_tokens_details") or {}
    cached = token_count(input_details.get("cached_tokens")) if isinstance(input_details, dict) else None
    reasoning = token_count(output_details.get("reasoning_tokens")) if isinstance(output_details, dict) else None
    # Cached and reasoning tokens are subsets, never extra tokens added to totals.
    with closing(connection()) as database, database:
        database.execute(
            """UPDATE model_requests SET finished_at=?,status=?,http_status=?,request_id=?,response_id=?,
                model=COALESCE(?,model),input_tokens=?,output_tokens=?,total_tokens=?,cached_input_tokens=?,
                reasoning_tokens=?,usage_json=? WHERE id=?""",
            (time.time(), status, http_status, request_id,
             str(envelope["id"])[:255] if envelope.get("id") else None,
             str(envelope["model"])[:255] if envelope.get("model") else None,
             inputs, outputs, total, cached, reasoning,
             json.dumps(usage, ensure_ascii=True) if usage is not None else None, identifier),
        )


def validation(payload: Any, status: str, error: str | None = None) -> None:
    identifier = payload.get("_pdf2podcast_usage_id") if isinstance(payload, dict) else None
    if not identifier:
        return
    with closing(connection()) as database, database:
        database.execute(
            "UPDATE model_requests SET validation_status=?,validation_error=? WHERE id=?",
            (status, error[:2000] if error else None, identifier),
        )


def report(database: sqlite3.Connection, user_sub: str | None = None, *, by_job: bool = False) -> list[dict[str, Any]]:
    where = "WHERE r.user_sub = ?" if user_sub is not None else ""
    job_column = ", r.job_id" if by_job else ""
    rows = database.execute(
        f"""SELECT r.user_sub, COALESCE(u.email,'Unattributed') AS user_email,
                   r.provider, r.model {job_column}, COUNT(*) AS requests,
                   SUM(r.is_fallback) AS fallback_requests,
                   SUM(CASE WHEN r.status != 'response' THEN 1 ELSE 0 END) AS incomplete_requests,
                   SUM(CASE WHEN r.validation_status='rejected' THEN 1 ELSE 0 END) AS rejected_responses,
                   SUM(CASE WHEN r.input_tokens IS NULL OR r.output_tokens IS NULL THEN 1 ELSE 0 END) AS unknown_usage_requests,
                   SUM(r.input_tokens) AS input_tokens, SUM(r.output_tokens) AS output_tokens,
                   SUM(r.total_tokens) AS total_tokens, SUM(r.cached_input_tokens) AS cached_input_tokens,
                   SUM(r.reasoning_tokens) AS reasoning_tokens
            FROM model_requests r LEFT JOIN users u ON u.sub=r.user_sub
            {where}
            GROUP BY r.user_sub, r.provider, r.model {job_column}
            ORDER BY r.provider, r.model, r.user_sub""",
        (user_sub,) if user_sub is not None else (),
    ).fetchall()
    return [dict(row) for row in rows]
