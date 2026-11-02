"""Create and serve chaptered, two-voice, audio-first PDF companions."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import html
import inspect
import json
import math
import os
import queue
import random
import re
import secrets
import shutil
import sqlite3
import subprocess
import threading
import traceback
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import closing
from contextvars import copy_context
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from . import engagement, model_usage, paper_access
from .titles import first_page_title, select_document_title
from .url_import import URLImportError, download_pdf, normalize_pdf_url
from .playback import authorized_playback, playback_path, send_audio
from .read_along import ReadAlongUnavailable, read_along_map
from .vision_paragraphs import VISION_PIPELINE, build_visual_chapters, extract_visual_document
from .speech_text import (
    SPEECH_PROMPT, SPEECH_VERSION, apply_notation_readings,
    fallback_notation, notation_spans, pronunciation_text,
)
from .paper_companion import (
    STRUCTURE_PROMPT, SUMMARY_STYLE_PROMPT, REFERENCE_HEADING, METADATA_HEADING,
    annotation_batches, chapters_from_plans,
    extract_document, join_lines, summary_is_concise, validate_groups,
)

import edge_tts

try:
    from pypdf import PdfReader
except ImportError:
    from PyPDF2 import PdfReader


ROOT = Path(os.environ.get("PDF2PODCAST_DATA_DIR", "~/.pdf2podcast")).expanduser()
JOBS = ROOT / "jobs"
SOURCE_VOICE = os.environ.get(
    "PDF2PODCAST_SOURCE_VOICE", "en-US-AndrewMultilingualNeural"
)
GUIDE_VOICE = os.environ.get(
    "PDF2PODCAST_GUIDE_VOICE", "en-US-AvaMultilingualNeural"
)
SCRIPT_MODEL = os.environ.get("PDF2PODCAST_SCRIPT_MODEL", "gpt-4.1-mini")
LLM_BASE_URL = os.environ.get("PDF2PODCAST_LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
MAX_UPLOAD_BYTES = int(os.environ.get("PDF2PODCAST_MAX_UPLOAD_BYTES", 100 * 1024 * 1024))
SCRIPT_CHUNK_CHARS = int(os.environ.get("PDF2PODCAST_SCRIPT_CHUNK_CHARS", 28_000))
JOB_ID = re.compile(r"^pdf-[a-f0-9]{16}$")
UPLOADS: queue.Queue[str] = queue.Queue()
JOB_CONTROL_LOCK = threading.RLock()
WEB_STATE_LOCK = threading.RLock()
WEB_DB = ROOT / "web.sqlite3"
WEB_ROOT = Path(os.environ.get(
    "PDF2PODCAST_WEB_ROOT",
    str(Path(__file__).resolve().parents[2] / "apps" / "Pdf2Podcast" / "web"),
)).expanduser()
GOOGLE_CLIENT_ID = os.environ.get("PDF2PODCAST_GOOGLE_CLIENT_ID", "").strip()
PUBLIC_WEB_ORIGIN = os.environ.get("PDF2PODCAST_PUBLIC_WEB_ORIGIN", "https://pdf2podcast.tchlabs.net").rstrip("/")
ADMIN_EMAILS = {
    value.strip().lower()
    for value in os.environ.get("PDF2PODCAST_ADMIN_EMAILS", "").split(",")
    if value.strip()
}
PRO_EMAILS = {value.strip().lower() for value in os.environ.get("PDF2PODCAST_PRO_EMAILS", "").split(",") if value.strip()}
WEB_UPLOADS_PER_MINUTE = max(1, int(os.environ.get("PDF2PODCAST_WEB_UPLOADS_PER_MINUTE", "20")))
WEB_QUEUE_PER_MINUTE = max(1, int(os.environ.get("PDF2PODCAST_WEB_QUEUE_PER_MINUTE", "1")))
WEB_SESSION_SECONDS = max(3600, int(os.environ.get("PDF2PODCAST_WEB_SESSION_SECONDS", str(30 * 86400))))
WEB_SESSION_COOKIE = "pdf2podcast_session"
VISITOR_COOKIE = "pdf2podcast_visitor"
VISITOR_COOKIE_SECONDS = 365 * 86400
ENGAGEMENT_BODY_LIMIT = 4096
PUBLIC_TERMS_VERSION = "2026-10-29"
PUBLIC_TERMS_EFFECTIVE_DATE = "29 October 2026"
PRIVACY_CONTACT = os.environ.get("PDF2PODCAST_PRIVACY_CONTACT", "tanchienhao@gmail.com").strip()
PUBLIC_TERMS = [
    {
        "heading": "Plans and publication",
        "body": (
            "Free-plan web uploads are public. Pro users may choose private or public uploads and change "
            "their own paper's visibility later; new Pro uploads default to private. Public PDFs, generated "
            "audio, transcripts, titles and other metadata may be viewed, streamed, downloaded, indexed, "
            "and shared by anyone. Private entries require the owner's account or service-operator access. "
            "Making an entry private cannot retract copies already obtained or copies published by others. Your Google profile and "
            "upload attribution are kept in private account records and are not included in the public catalog."
        ),
    },
    {
        "heading": "Your rights and licence",
        "body": (
            "You confirm that you have the rights or permissions needed to upload and process the material "
            "using this service and its providers. Public publication additionally requires the right to "
            "distribute the PDF and generated media publicly. You grant the operator permission to host and "
            "process uploads and create audio and transcripts to operate the service. Permission to publicly "
            "display and distribute your entry applies only while you choose public visibility."
        ),
    },
    {
        "heading": "Prohibited uploads",
        "body": (
            "Do not upload pirated or unlawfully obtained material, malware, or material you are not permitted "
            "to process using this service and its providers. Do not publish material you lack the right to "
            "distribute. Private visibility does not override confidentiality obligations or grant copyright "
            "permissions; this service is not end-to-end encrypted."
        ),
    },
    {
        "heading": "Removal and enforcement",
        "body": (
            "The operator may reject, restrict, or remove material and suspend access at any time, including "
            "in response to a rights-holder complaint. Rights holders should identify the work, the relevant "
            "Pdf2Podcast URL, their contact details, and why the publication is unauthorized through the "
            "contact channel published at https://tchlabs.net."
        ),
    },
    {
        "heading": "Service and generated-content disclaimer",
        "body": (
            "Pdf2Podcast is provided on an as-is and as-available basis. Generated audio, summaries, "
            "timestamps, transcripts, and locations may be inaccurate or incomplete and are not professional "
            "advice. You remain responsible for checking the source and for your use and publication of it."
        ),
    },
    {
        "heading": "Responsibility and limitation",
        "body": (
            "To the fullest extent permitted by law, the operator is not liable for indirect, incidental, "
            "special, consequential, or exemplary loss arising from the service or user-uploaded material. "
            "You are responsible for claims caused by your knowing or negligent breach of these terms and, "
            "to the extent permitted by law, agree to indemnify the operator against those third-party claims. "
            "Nothing in these terms excludes or restricts liability that cannot lawfully be excluded or restricted."
        ),
    },
    {
        "heading": "Changes and governing law",
        "body": (
            "The terms version accepted for each upload is recorded. Future uploads may require acceptance "
            "of a newer version. These terms are governed by Singapore law, subject to any mandatory rights "
            "that apply to you."
        ),
    },
]
PRIVACY_SECTIONS = [
    {
        "heading": "Information we collect",
        "body": (
            "When you sign in, we receive your Google account identifier, email address, display name, and "
            "profile image. We also store your uploaded PDFs, generated audio and transcripts, conversion "
            "status, file hashes, usage totals, session records, and your Terms of Use acceptance. We store "
            "approximate listening seconds per paper, share links attributed to the sharing account or "
            "anonymous browser, reported shares, total link clicks, and approximate unique visitors per link."
        ),
    },
    {
        "heading": "How we use information",
        "body": (
            "We use this information to authenticate you, operate and secure Pdf2Podcast, process PDFs, "
            "generate and stream audio, avoid processing duplicate files, enforce usage limits, measure "
            "service usage, respond to support or rights-holder requests, and prevent abuse. Listening totals "
            "help rank public papers. Signed-in users can see their own share statistics; administrators can "
            "see all share statistics with the signed-in sharer's name and email. Guest sharers are labelled Anonymous."
        ),
    },
    {
        "heading": "What becomes public",
        "body": (
            "Free-plan web uploads are public; Pro uploads default to private unless you choose public. "
            "Private entries and their PDF, audio, transcripts and metadata are available only to the "
            "owning account and service operator, and are excluded from public discovery and sharing. "
            "For public entries, the PDF, generated audio, transcript, title and metadata may be viewed, downloaded, "
            "indexed, and shared by anyone. The public catalog includes aggregate listening seconds, shares, "
            "and share-link clicks, but no uploader names or profile images. Anyone with a share link can "
            "see its share, click, and unique-visitor counts. Shared links and public engagement responses "
            "do not expose Google names, emails, account identifiers, or anonymous visitor identifiers."
        ),
    },
    {
        "heading": "Service providers",
        "body": (
            "Information is processed through infrastructure and services used to operate Pdf2Podcast, "
            "including Google for authentication, Cloudflare for network delivery, the NUS Soclaas service "
            "for language-model processing, OpenAI for fallback processing, and Microsoft Edge text-to-speech services for audio generation. "
            "Private uploads still use these processing services; private visibility is not end-to-end encryption. "
            "Those providers may process data under their own applicable terms and privacy policies."
        ),
    },
    {
        "heading": "Cookies, retention, and security",
        "body": (
            "We use a secure session cookie to keep you signed in. For anonymous visitors, "
            "we also use a random browser cookie lasting up to one year to reuse your links, restrict share "
            "reporting to their creator, ignore self-clicks, and estimate unique visitors. Both cookies use "
            "HttpOnly, Secure, and SameSite=Lax. Signed-in engagement uses your account; clearing cookies or "
            "changing sign-in state can change the approximate unique count. Account, publication, engagement, "
            "and conversion records are retained while the service operates or as needed for security, legal, "
            "and dispute-resolution purposes. We use reasonable safeguards, but no online system can be "
            "guaranteed completely secure."
        ),
    },
    {
        "heading": "Your choices",
        "body": (
            "You may sign out at any time and may request access, correction, account deletion, or removal of "
            "a publication by contacting us. Removal from Pdf2Podcast cannot retract copies already downloaded, "
            "indexed, cached, or redistributed by other people."
        ),
    },
    {
        "heading": "Contact and updates",
        "body": (
            "Contact us with privacy questions or requests at the email address below. We may update this "
            "notice as the service changes and will publish the current version on this page."
        ),
    },
]
TTS_CONCURRENCY = max(1, min(128, int(os.environ.get("PDF2PODCAST_TTS_CONCURRENCY", "16"))))
SCRIPT_CONCURRENCY = max(1, min(128, int(os.environ.get("PDF2PODCAST_SCRIPT_CONCURRENCY", "16"))))
TTS_CLIP_CONCURRENCY = max(1, min(128, int(os.environ.get("PDF2PODCAST_TTS_CLIP_CONCURRENCY", "32"))))
SUMMARY_CONCURRENCY = max(1, min(128, int(os.environ.get("PDF2PODCAST_SUMMARY_CONCURRENCY", "16"))))
OPENAI_CONCURRENCY = max(1, min(128, int(os.environ.get(
    "PDF2PODCAST_LLM_CONCURRENCY", os.environ.get("PDF2PODCAST_OPENAI_CONCURRENCY", "16")
))))
MEDIA_CONCURRENCY = max(1, min(8, int(os.environ.get("PDF2PODCAST_MEDIA_CONCURRENCY", str(min(4, os.cpu_count() or 1))))))
MEDIA_SLOTS = threading.BoundedSemaphore(MEDIA_CONCURRENCY)
TTS_ATTEMPTS = max(1, min(6, int(os.environ.get("PDF2PODCAST_TTS_ATTEMPTS", "3"))))
TTS_TIMEOUT_SECONDS = max(30, min(1800, int(os.environ.get("PDF2PODCAST_TTS_TIMEOUT_SECONDS", "300"))))

SCRIPT_SYSTEM_PROMPT = STRUCTURE_PROMPT


class AdaptiveAPIGate:
    """Start with high IO concurrency; share provider cooldowns rather than retry storms."""

    def __init__(self, maximum: int):
        self.maximum = maximum
        self.limit = maximum
        self.active = 0
        self.successes = 0
        self.blocked_until = 0.0
        self.condition = threading.Condition()

    def __enter__(self):
        with self.condition:
            while True:
                delay = self.blocked_until - time.monotonic()
                if delay <= 0 and self.active < self.limit:
                    self.active += 1
                    return self
                self.condition.wait(timeout=delay if delay > 0 else None)

    def __exit__(self, *_):
        with self.condition:
            self.active -= 1
            self.condition.notify_all()

    def succeeded(self) -> None:
        with self.condition:
            self.successes += 1
            if self.limit < self.maximum and self.successes >= max(4, self.limit * 2):
                self.limit += 1
                self.successes = 0
                self.condition.notify_all()

    def throttled(self, delay: float) -> None:
        with self.condition:
            self.limit = max(1, self.limit // 2)
            self.successes = 0
            self.blocked_until = max(self.blocked_until, time.monotonic() + delay)
            self.condition.notify_all()


OPENAI_GATE = AdaptiveAPIGate(OPENAI_CONCURRENCY)
FALLBACK_GATE = AdaptiveAPIGate(min(8, OPENAI_CONCURRENCY))
SPEECH_GATE = AdaptiveAPIGate(TTS_CLIP_CONCURRENCY)


class OpenAIRequestError(RuntimeError):
    """Keep transport retries separate from retries of a malformed model response."""


def retry_after_seconds(value: str | None) -> float:
    if not value:
        return 0
    try:
        seconds = float(value)
        return max(0, seconds) if seconds < float("inf") else 0
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(0, (date - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0


def _request_provider_json(
    body: dict[str, Any], *, base_url: str, key: str, provider: str,
    gate: AdaptiveAPIGate, attempts: int, is_fallback: bool = False,
) -> dict[str, Any]:
    """Record every network attempt, including rejected responses and unknown usage."""
    deadline = time.monotonic() + 600
    last_error = "Language-model request failed"
    for attempt in range(attempts):
        delay = min(60, 2 ** attempt) + random.uniform(0.25, 1.25)
        request = urllib.request.Request(
            f"{base_url}/chat/completions", data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, method="POST",
        )
        shared_cooldown = False
        usage_id = None
        payload = None
        response_id = None
        http_status = None
        try:
            with gate:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise OpenAIRequestError("Language-model request deferred after its retry time budget; checkpoints are retained.")
                usage_id = model_usage.begin(provider, str(body.get("model", "")), attempt + 1, is_fallback)
                with urllib.request.urlopen(request, timeout=min(90 if provider == "soclaas" else 180, remaining)) as response:
                    http_status = response.status
                    response_id = response.headers.get("x-request-id")
                    payload = json.loads(response.read())
                if not isinstance(payload, dict):
                    raise ValueError("The language-model provider returned an invalid response envelope.")
                model_usage.finish(usage_id, "response", payload, http_status, response_id)
            gate.succeeded()
            payload["_pdf2podcast_usage_id"] = usage_id
            payload["_pdf2podcast_provider"] = provider
            payload["_pdf2podcast_model"] = payload.get("model") or body.get("model")
            return payload
        except urllib.error.HTTPError as error:
            delay = max(delay, retry_after_seconds((error.headers or {}).get("Retry-After")) + random.uniform(0.1, 0.5))
            try:
                payload = json.loads(error.read())
                detail = payload.get("error", {}) if isinstance(payload, dict) else {}
                code = detail.get("code") if isinstance(detail, dict) else None
            except (ValueError, AttributeError):
                code = None
            if usage_id is not None:
                model_usage.finish(usage_id, "http_error", payload, error.code, (error.headers or {}).get("x-request-id"))
            if code in {"insufficient_quota", "billing_hard_limit_reached"}:
                raise OpenAIRequestError(f"{provider} quota exhausted; add credits before retrying.") from error
            if error.code not in {408, 409, 429, 500, 502, 503, 504}:
                raise OpenAIRequestError(f"{provider} HTTP {error.code}; configuration or account action is required.") from error
            last_error = f"{provider} HTTP {error.code}"
            if error.code in {429, 503}:
                gate.throttled(delay)
                shared_cooldown = True
        except (OSError, ValueError) as error:
            if usage_id is not None:
                model_usage.finish(usage_id, "invalid_response" if http_status else "transport_error", payload, http_status, response_id)
            last_error = f"{provider} transport error: {type(error).__name__}"
        if attempt == attempts - 1 or time.monotonic() + delay >= deadline:
            raise OpenAIRequestError(f"{last_error}; request retries exhausted, saved work retained.")
        if not shared_cooldown:
            time.sleep(delay)
    raise OpenAIRequestError(last_error)


def request_openai_fallback(body: dict[str, Any]) -> dict[str, Any]:
    """Use the separate OpenAI credential, never send the Soclaas key to OpenAI."""
    key = (os.environ.get("PDF2PODCAST_OPENAI_FALLBACK_API_KEY", "") or os.environ.get("OPENAI_API_KEY", "")).strip()
    if not key.startswith("sk-"):
        path = Path(os.environ.get("PDF2PODCAST_OPENAI_FALLBACK_KEY_FILE", os.environ.get("PDF2PODCAST_OPENAI_KEY_FILE", "~/.pdf2podcast/openai-key"))).expanduser()
        try:
            key = path.read_text().strip()
        except OSError as error:
            raise OpenAIRequestError("The separate OpenAI fallback key is unavailable.") from error
    if not key.startswith("sk-"):
        raise OpenAIRequestError("The fallback key must be an OpenAI API key, not a Soclaas key.")
    layout = model_usage.CONTEXT.get().get("stage") in {"visual_layout", "page_boundary"}
    model = os.environ.get(
        "PDF2PODCAST_OPENAI_FALLBACK_LAYOUT_MODEL" if layout else "PDF2PODCAST_OPENAI_FALLBACK_SCRIPT_MODEL",
        "gpt-4.1" if layout else "gpt-4.1-mini",
    )
    return _request_provider_json(
        {**body, "model": model}, base_url="https://api.openai.com/v1", key=key,
        provider="openai", gate=FALLBACK_GATE, attempts=4, is_fallback=True,
    )


def request_openai_json(body: dict[str, Any]) -> dict[str, Any]:
    """Prefer Soclaas, but recover provider and structural failures through OpenAI."""
    host = urllib.parse.urlsplit(LLM_BASE_URL).hostname or "unknown"
    provider = "soclaas" if host == "soclaas-api.comp.nus.edu.sg" else "openai" if host == "api.openai.com" else host
    if provider == "soclaas" and model_usage.CONTEXT.get().get("prefer_fallback"):
        return request_openai_fallback(body)
    try:
        return _request_provider_json(
            body, base_url=LLM_BASE_URL, key=llm_key(), provider=provider,
            gate=OPENAI_GATE, attempts=2 if provider == "soclaas" else 6,
        )
    except OpenAIRequestError:
        if provider != "soclaas":
            raise
        print("Soclaas request failed; continuing with the OpenAI fallback.", flush=True)
        return request_openai_fallback(body)


def run_bounded_parallel(
    items: list[Any],
    operation: Callable[[Any], Any],
    concurrency: int,
    on_result: Callable[[Any, Any], None] | None = None,
) -> None:
    """Bound both active and submitted work; publish results only on the caller thread."""
    if not items:
        return
    workers = min(concurrency, len(items))
    remaining = iter(items)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="pdf2podcast-api") as pool:
        pending = {}

        def fill_slots() -> None:
            while len(pending) < workers:
                try:
                    item = next(remaining)
                except StopIteration:
                    break
                pending[pool.submit(copy_context().run, operation, item)] = item

        try:
            fill_slots()
            while pending:
                finished, _ = wait(tuple(pending), return_when=FIRST_COMPLETED)
                for future in finished:
                    item = pending.pop(future)
                    result = future.result()
                    if on_result is not None:
                        on_result(item, result)
                fill_slots()
        finally:
            # Stop submitting on failure. Let already-running calls finish and save
            # checkpoints before the job becomes retryable; no overlapping attempts.
            for future in pending:
                future.cancel()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temporary.replace(path)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return re.sub(r"-+", "-", value)[:80] or "section"


def safe_job(job_id: str) -> Path:
    if not JOB_ID.fullmatch(job_id):
        raise ValueError("Invalid job id")
    return JOBS / job_id


def job_path(job_id: str) -> Path:
    return safe_job(job_id) / "job.json"


def load_job(job_id: str) -> dict[str, Any]:
    job = read_json(job_path(job_id))
    if not isinstance(job, dict):
        raise FileNotFoundError(job_id)
    if job.get("source_job_id"):
        source = read_json(job_path(job["source_job_id"]))
        if not isinstance(source, dict) or source.get("source_job_id"):
            raise FileNotFoundError(job_id)
        job = {**source, **job}
    return job


def save_job(job_id: str, **changes: Any) -> dict[str, Any]:
    job = load_job(job_id)
    job.update(changes)
    job["updated_at"] = now()
    atomic_json(job_path(job_id), job)
    return job


def public_job(job: dict[str, Any], user: dict[str, Any] | None = None) -> dict[str, Any]:
    result = {key: value for key, value in job.items() if key not in {"input_path", "source_job_id"}}
    with web_db() as database:
        access = paper_access.access_flags(database, job["job_id"], user["sub"] if user else None)
    result.update(visibility="public" if access["is_public"] else "private",
                  is_owner=access["is_owner"],
                  can_change_visibility=bool(access["is_owner"] and user and user.get("plan") == "pro"))
    if job.get("status") in {"complete", "completed"}:
        legacy = is_legacy_job(job["job_id"])
        result["playback_url"] = (playback_path(job["job_id"]) if access["is_public"] or legacy
                                  else f"/api/audio/jobs/{job['job_id']}/audio")
    return result


def list_jobs(limit: int = 100) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for path in JOBS.glob("pdf-*/job.json"):
        job = read_json(path)
        if isinstance(job, dict):
            try:
                found.append(public_job(load_job(path.parent.name)))
            except (FileNotFoundError, ValueError):
                continue
    def created_timestamp(item: dict[str, Any]) -> float:
        value = item.get("created_at")
        try:
            return float(value)
        except (TypeError, ValueError):
            pass
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.timestamp()
            except (ValueError, OverflowError):
                pass
        return 0.0

    found.sort(key=created_timestamp, reverse=True)
    return found[: max(1, min(limit, 200))]


def web_db() -> sqlite3.Connection:
    connection = sqlite3.connect(WEB_DB, timeout=20)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_web_state() -> None:
    """Create the identity/usage index and register pre-existing content hashes."""
    ROOT.mkdir(parents=True, exist_ok=True)
    with WEB_STATE_LOCK, web_db() as database:
        database.execute("PRAGMA journal_mode = WAL")
        database.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                sub TEXT PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                picture TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                last_seen INTEGER NOT NULL,
                terms_version TEXT,
                terms_accepted_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_sub TEXT NOT NULL REFERENCES users(sub) ON DELETE CASCADE,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires_at);
            CREATE TABLE IF NOT EXISTS documents (
                sha256 TEXT PRIMARY KEY,
                job_id TEXT NOT NULL UNIQUE,
                source_bytes INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS user_jobs (
                user_sub TEXT NOT NULL REFERENCES users(sub) ON DELETE CASCADE,
                job_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                source_bytes INTEGER NOT NULL,
                linked_at INTEGER NOT NULL,
                public_release INTEGER NOT NULL DEFAULT 0,
                terms_version TEXT,
                terms_accepted_at INTEGER,
                PRIMARY KEY (user_sub, job_id)
            );
            CREATE INDEX IF NOT EXISTS user_jobs_user_time ON user_jobs(user_sub, linked_at DESC);
            CREATE TABLE IF NOT EXISTS upload_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_sub TEXT NOT NULL REFERENCES users(sub) ON DELETE CASCADE,
                created_at INTEGER NOT NULL,
                sha256 TEXT,
                source_bytes INTEGER NOT NULL,
                outcome TEXT NOT NULL,
                job_id TEXT
            );
            CREATE INDEX IF NOT EXISTS upload_events_user_time ON upload_events(user_sub, created_at DESC);
            """
        )
        engagement.init_schema(database)
        model_usage.init_schema(database)
        user_job_columns = {
            row["name"] for row in database.execute("PRAGMA table_info(user_jobs)").fetchall()
        }
        user_columns = {
            row["name"] for row in database.execute("PRAGMA table_info(users)").fetchall()
        }
        if "terms_version" not in user_columns:
            database.execute("ALTER TABLE users ADD COLUMN terms_version TEXT")
        if "terms_accepted_at" not in user_columns:
            database.execute("ALTER TABLE users ADD COLUMN terms_accepted_at INTEGER")
        if "public_release" not in user_job_columns:
            database.execute("ALTER TABLE user_jobs ADD COLUMN public_release INTEGER NOT NULL DEFAULT 0")
        if "terms_version" not in user_job_columns:
            database.execute("ALTER TABLE user_jobs ADD COLUMN terms_version TEXT")
        if "terms_accepted_at" not in user_job_columns:
            database.execute("ALTER TABLE user_jobs ADD COLUMN terms_accepted_at INTEGER")
        paper_access.init_schema(database, JOBS, PRO_EMAILS)
        timestamp = int(time.time())
        for path in JOBS.glob("pdf-*/job.json"):
            job = read_json(path, {})
            digest = job.get("source_sha256") if isinstance(job, dict) else None
            job_id = job.get("job_id") if isinstance(job, dict) else None
            input_path = path.parent / "input.pdf"
            if (isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest)
                    and isinstance(job_id, str) and JOB_ID.fullmatch(job_id)):
                source_bytes = input_path.stat().st_size if input_path.is_file() else 0
                database.execute(
                    "INSERT OR IGNORE INTO documents(sha256, job_id, source_bytes, created_at) VALUES(?, ?, ?, ?)",
                    (digest, job_id, source_bytes, timestamp),
                )


def verify_google_credential(credential: str) -> dict[str, str]:
    """Validate a Google Identity Services ID token with Google's token-info endpoint."""
    if not GOOGLE_CLIENT_ID:
        raise RuntimeError("Google sign-in is not configured on this server.")
    encoded = urllib.parse.urlencode({"id_token": credential})
    request = urllib.request.Request(
        f"https://oauth2.googleapis.com/tokeninfo?{encoded}",
        headers={"Accept": "application/json", "User-Agent": "Pdf2Podcast/3"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as error:
        raise ValueError("Google rejected the sign-in credential.") from error
    except (OSError, ValueError) as error:
        raise RuntimeError("Google sign-in could not be verified right now.") from error
    if not isinstance(payload, dict):
        raise ValueError("Google returned an invalid identity response.")
    if payload.get("aud") != GOOGLE_CLIENT_ID:
        raise ValueError("The Google credential belongs to a different application.")
    if payload.get("iss") not in {"accounts.google.com", "https://accounts.google.com"}:
        raise ValueError("The Google credential has an invalid issuer.")
    if str(payload.get("email_verified", "")).lower() != "true":
        raise ValueError("A verified Google email address is required.")
    try:
        expiry = int(payload.get("exp", 0))
    except (TypeError, ValueError) as error:
        raise ValueError("The Google credential has an invalid expiry.") from error
    if expiry <= int(time.time()):
        raise ValueError("The Google credential has expired.")
    subject = str(payload.get("sub") or "").strip()
    email = str(payload.get("email") or "").strip().lower()
    if not subject or not email:
        raise ValueError("Google did not provide the required account identity.")
    return {
        "sub": subject,
        "email": email,
        "name": str(payload.get("name") or email.split("@", 1)[0]).strip()[:200],
        "picture": str(payload.get("picture") or "").strip()[:2000],
    }


def create_web_session(profile: dict[str, str]) -> str:
    timestamp = int(time.time())
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    with WEB_STATE_LOCK, web_db() as database:
        database.execute(
            """INSERT INTO users(sub, email, name, picture, created_at, last_seen, plan)
               VALUES(?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(sub) DO UPDATE SET
                   email=excluded.email, name=excluded.name, picture=excluded.picture,
                   last_seen=excluded.last_seen,
                   plan=CASE WHEN excluded.plan='pro' THEN 'pro' ELSE users.plan END""",
            (profile["sub"], profile["email"], profile["name"], profile["picture"], timestamp, timestamp,
             "pro" if profile["email"].lower() in PRO_EMAILS else "free"),
        )
        database.execute("DELETE FROM sessions WHERE expires_at <= ?", (timestamp,))
        database.execute(
            "INSERT INTO sessions(token_hash, user_sub, created_at, expires_at) VALUES(?, ?, ?, ?)",
            (token_hash, profile["sub"], timestamp, timestamp + WEB_SESSION_SECONDS),
        )
    return raw_token


def browser_cookie_token(cookie_header: str, name: str) -> str:
    """Read only our opaque cookies; unrelated cookie syntax cannot hide a session."""
    if name not in {WEB_SESSION_COOKIE, VISITOR_COOKIE}:
        return ""
    values = []
    for field in cookie_header.split(";"):
        key, separator, value = field.strip().partition("=")
        if key != name:
            continue
        if not separator or not re.fullmatch(r"[A-Za-z0-9_-]{43}", value):
            return ""
        values.append(value)
    # Reject ambiguous duplicate cookies instead of choosing an account by order.
    return values[0] if len(values) == 1 else ""


def session_token(cookie_header: str) -> str:
    return browser_cookie_token(cookie_header, WEB_SESSION_COOKIE)


def web_user_for_token(raw_token: str) -> dict[str, Any] | None:
    if not raw_token:
        return None
    timestamp = int(time.time())
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    with WEB_STATE_LOCK, web_db() as database:
        row = database.execute(
            """SELECT users.sub, users.email, users.name, users.picture,
                      users.terms_version, users.terms_accepted_at, users.plan
               FROM sessions JOIN users ON users.sub = sessions.user_sub
               WHERE sessions.token_hash = ? AND sessions.expires_at > ?""",
            (token_hash, timestamp),
        ).fetchone()
        if row is None:
            return None
        database.execute("UPDATE users SET last_seen = ? WHERE sub = ?", (timestamp, row["sub"]))
    return {
        "sub": row["sub"], "email": row["email"], "name": row["name"],
        "picture": row["picture"], "is_admin": row["email"].lower() in ADMIN_EMAILS,
        "terms_accepted": row["terms_version"] == PUBLIC_TERMS_VERSION,
        "terms_version": row["terms_version"],
        "terms_accepted_at": row["terms_accepted_at"],
        "plan": row["plan"], "can_upload_private": row["plan"] == "pro",
    }


def accept_public_terms(user_sub: str, version: str) -> None:
    if version != PUBLIC_TERMS_VERSION:
        raise ValueError("The Terms of Use changed. Review and accept the current version.")
    with WEB_STATE_LOCK, web_db() as database:
        database.execute(
            "UPDATE users SET terms_version = ?, terms_accepted_at = ? WHERE sub = ?",
            (version, int(time.time()), user_sub),
        )


def delete_web_session(raw_token: str) -> None:
    if not raw_token:
        return
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    with WEB_STATE_LOCK, web_db() as database:
        database.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))


def begin_upload_event(user_sub: str, source_bytes: int) -> tuple[int | None, int]:
    timestamp = int(time.time())
    with WEB_STATE_LOCK, web_db() as database:
        rows = database.execute(
            "SELECT created_at FROM upload_events WHERE user_sub = ? AND created_at > ? ORDER BY created_at",
            (user_sub, timestamp - 60),
        ).fetchall()
        if len(rows) >= WEB_UPLOADS_PER_MINUTE:
            return None, max(1, int(rows[0]["created_at"]) + 60 - timestamp)
        cursor = database.execute(
            "INSERT INTO upload_events(user_sub, created_at, source_bytes, outcome) VALUES(?, ?, ?, 'receiving')",
            (user_sub, timestamp, source_bytes),
        )
        return int(cursor.lastrowid), 0


def update_upload_event(event_id: int | None, **values: Any) -> None:
    if event_id is None or not values:
        return
    allowed = {"sha256", "source_bytes", "outcome", "job_id"}
    values = {key: value for key, value in values.items() if key in allowed}
    if not values:
        return
    assignments = ", ".join(f"{key} = ?" for key in values)
    with WEB_STATE_LOCK, web_db() as database:
        database.execute(
            f"UPDATE upload_events SET {assignments} WHERE id = ?",
            (*values.values(), event_id),
        )


def reserve_document(
    digest: str, source_bytes: int, filename: str,
    user_sub: str | None, event_id: int | None,
    public_release: bool = False, terms_version: str | None = None,
) -> dict[str, Any]:
    """Atomically deduplicate content and reserve at most one processing slot per minute."""
    timestamp = int(time.time())
    with WEB_STATE_LOCK, web_db() as database:
        database.execute("BEGIN IMMEDIATE")
        existing = database.execute(
            "SELECT job_id FROM documents WHERE sha256 = ?", (digest,),
        ).fetchone()
        if existing is not None and not job_path(existing["job_id"]).is_file():
            database.execute("DELETE FROM user_jobs WHERE job_id = ?", (existing["job_id"],))
            database.execute("DELETE FROM documents WHERE sha256 = ?", (digest,))
            existing = None
        if existing is not None:
            job_id = str(existing["job_id"])
            if user_sub is not None:
                job_id = paper_access.link_upload(
                    database, JOBS, job_id, user_sub, filename, source_bytes, timestamp,
                    public_release, terms_version, new_source=False,
                )
            elif paper_access.access_flags(database, job_id)["has_web_owner"]:
                # Shared native credentials must never acquire another account's private entry.
                # Native uploads get their own legacy entry while reusing conversion artifacts.
                native = database.execute(
                    "SELECT job_id FROM native_job_aliases WHERE source_job_id=?", (job_id,),
                ).fetchone()
                if native is None:
                    source_job_id = job_id
                    job_id = paper_access.create_native_alias(database, JOBS, source_job_id, filename, timestamp)
                else:
                    job_id = native["job_id"]
            if event_id is not None:
                database.execute(
                    "UPDATE upload_events SET sha256=?, source_bytes=?, outcome='deduplicated', job_id=? WHERE id=?",
                    (digest, source_bytes, job_id, event_id),
                )
            return {"status": "existing", "job_id": job_id}
        if user_sub is not None:
            recent = database.execute(
                """SELECT created_at FROM upload_events
                   WHERE user_sub = ? AND outcome = 'queued' AND created_at > ?
                   ORDER BY created_at""",
                (user_sub, timestamp - 60),
            ).fetchall()
            if len(recent) >= WEB_QUEUE_PER_MINUTE:
                retry_after = max(1, int(recent[0]["created_at"]) + 60 - timestamp)
                if event_id is not None:
                    database.execute(
                        "UPDATE upload_events SET sha256=?, source_bytes=?, outcome='queue_rate_limited' WHERE id=?",
                        (digest, source_bytes, event_id),
                    )
                return {"status": "rate_limited", "retry_after": retry_after}
        job_id = f"pdf-{uuid.uuid4().hex[:16]}"
        database.execute(
            "INSERT INTO documents(sha256, job_id, source_bytes, created_at) VALUES(?, ?, ?, ?)",
            (digest, job_id, source_bytes, timestamp),
        )
        if user_sub is not None:
            paper_access.link_upload(
                database, JOBS, job_id, user_sub, filename, source_bytes, timestamp,
                public_release, terms_version, new_source=True,
            )
        else:
            database.execute("INSERT INTO native_job_aliases(job_id,source_job_id) VALUES(?,?)", (job_id, job_id))
        if event_id is not None:
            database.execute(
                "UPDATE upload_events SET sha256=?, source_bytes=?, outcome='queued', job_id=? WHERE id=?",
                (digest, source_bytes, job_id, event_id),
            )
        return {"status": "new", "job_id": job_id}


def release_document(digest: str, job_id: str, user_sub: str | None, event_id: int | None) -> None:
    with WEB_STATE_LOCK, web_db() as database:
        database.execute("DELETE FROM documents WHERE sha256 = ? AND job_id = ?", (digest, job_id))
        if user_sub is not None:
            database.execute("DELETE FROM user_jobs WHERE user_sub = ? AND job_id = ?", (user_sub, job_id))
        if event_id is not None:
            database.execute(
                "UPDATE upload_events SET outcome='storage_failed', job_id=NULL WHERE id=?", (event_id,),
            )


def web_usage(user_sub: str) -> dict[str, Any]:
    timestamp = int(time.time())
    with web_db() as database:
        row = database.execute(
            """SELECT COUNT(*) AS uploads_total, COALESCE(SUM(source_bytes), 0) AS bytes_uploaded,
                      COALESCE(SUM(CASE WHEN created_at > ? THEN 1 ELSE 0 END), 0) AS uploads_last_minute,
                      COALESCE(SUM(CASE WHEN created_at > ? AND outcome='queued' THEN 1 ELSE 0 END), 0)
                          AS queue_submissions_last_minute
               FROM upload_events WHERE user_sub = ?""",
            (timestamp - 60, timestamp - 60, user_sub),
        ).fetchone()
        unique_jobs = database.execute(
            "SELECT COUNT(*) FROM user_jobs WHERE user_sub = ?", (user_sub,),
        ).fetchone()[0]
        models = model_usage.report(database, user_sub)
    return {
        "uploads_total": int(row["uploads_total"]),
        "unique_jobs": int(unique_jobs),
        "bytes_uploaded": int(row["bytes_uploaded"]),
        "model_usage": models,
        "uploads_last_minute": int(row["uploads_last_minute"]),
        "queue_submissions_last_minute": int(row["queue_submissions_last_minute"]),
        "limits": {
            "max_upload_bytes": MAX_UPLOAD_BYTES,
            "uploads_per_minute": WEB_UPLOADS_PER_MINUTE,
            "queue_per_minute": WEB_QUEUE_PER_MINUTE,
        },
    }


def admin_usage() -> dict[str, Any]:
    with web_db() as database:
        rows = database.execute(
            """SELECT users.sub, users.email, users.name, users.picture, users.created_at, users.last_seen,
                      (SELECT COUNT(*) FROM upload_events WHERE upload_events.user_sub=users.sub) AS uploads_total,
                      COALESCE((SELECT SUM(source_bytes) FROM upload_events WHERE upload_events.user_sub=users.sub), 0)
                          AS bytes_uploaded,
                      (SELECT COUNT(*) FROM user_jobs WHERE user_jobs.user_sub=users.sub) AS unique_jobs
               FROM users ORDER BY bytes_uploaded DESC, users.last_seen DESC LIMIT 500"""
        ).fetchall()
        models = model_usage.report(database)
    return {"users": [dict(row) for row in rows], "model_usage": models}


def job_ids_for_user(user_sub: str) -> set[str]:
    with web_db() as database:
        return {
            str(row["job_id"])
            for row in database.execute("SELECT job_id FROM user_jobs WHERE user_sub = ?", (user_sub,))
        }


def is_public_job(job_id: str) -> bool:
    with web_db() as database:
        return database.execute(
            "SELECT 1 FROM user_jobs WHERE job_id = ? AND public_release = 1 LIMIT 1", (job_id,),
        ).fetchone() is not None


def is_legacy_job(job_id: str) -> bool:
    try:
        load_job(job_id)
    except (FileNotFoundError, ValueError):
        return False
    with web_db() as database:
        native = database.execute("SELECT 1 FROM native_job_aliases WHERE job_id=?", (job_id,)).fetchone()
        if native is not None:
            return True
    # Losing an owner must never turn a formerly private web paper into a native paper.
    return False


def public_catalog(limit: int = 100, sort: str = "listening") -> list[dict[str, Any]]:
    from datetime import datetime, timezone

    with web_db() as database:
        rows = database.execute(
            """SELECT publications.job_id, publications.linked_at AS published_at, publications.uploader_count,
                      COALESCE(listening.listening_seconds, 0) AS listening_seconds,
                      COALESCE(shares.share_count, 0) AS share_count,
                      COALESCE(shares.share_clicks, 0) AS share_clicks
               FROM (SELECT job_id, MIN(linked_at) AS linked_at, COUNT(*) AS uploader_count
                     FROM user_jobs WHERE public_release = 1 GROUP BY job_id) AS publications
               LEFT JOIN engagement_listening AS listening ON listening.job_id = publications.job_id
               LEFT JOIN (SELECT job_id, SUM(share_count) AS share_count, SUM(click_count) AS share_clicks
                          FROM engagement_links GROUP BY job_id) AS shares
                   ON shares.job_id = publications.job_id"""
        ).fetchall()
    result = []
    for row in rows:
        job_id = str(row["job_id"])
        try:
            job = load_job(job_id)
        except (FileNotFoundError, ValueError):
            continue
        item = {
            key: job.get(key)
            for key in (
                "job_id", "title", "status", "stage", "progress_percent",
                "estimated_seconds_remaining", "chapter_count", "section_count",
                "page_count", "duration_seconds", "created_at", "updated_at", "completed_at", "error",
            )
        }
        if job.get("status") in {"complete", "completed"}:
            item["playback_url"] = playback_path(job_id)
        item["published_at"] = row["published_at"]
        item.update(visibility="public", is_owner=False, can_change_visibility=False)
        # Legacy papers were published together; use the original upload time,
        # not that migration timestamp or another user's later duplicate upload.
        try:
            created = datetime.fromisoformat(str(job.get("created_at", "")).replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            item["added_at"] = created.timestamp()
        except (ValueError, OverflowError, OSError):
            item["added_at"] = float(row["published_at"])
        item["uploader_count"] = row["uploader_count"]
        item["listening_seconds"] = float(row["listening_seconds"])
        item["share_count"] = row["share_count"]
        item["share_clicks"] = row["share_clicks"]
        result.append(item)
    # Order the entire eligible catalogue before applying the requested limit.
    if sort in {"recent", "oldest"}:
        result.sort(key=lambda item: (item["added_at"], item["job_id"]), reverse=sort == "recent")
    else:
        result.sort(
            key=lambda item: (item["listening_seconds"], item["added_at"], item["job_id"]),
            reverse=sort != "least_listened",
        )
    return result[:max(1, min(limit, 200))]


def web_sharing(user: dict[str, Any]) -> dict[str, Any]:
    """Keep account attribution in the authenticated dashboard only."""
    with closing(web_db()) as database:
        links = engagement.sharing_rows(
            database, None if user["is_admin"] else engagement.account_key(user["sub"]),
        )
        profiles = {
            row["sub"]: row for row in database.execute("SELECT sub, name, email FROM users")
        } if user["is_admin"] else {}
    for link in links:
        sub = link.pop("user_sub")
        try:
            job = load_job(link["job_id"])
            link["title"] = job.get("title") or job.get("filename") or link["job_id"]
        except (FileNotFoundError, ValueError):
            link["title"] = link["job_id"]
        if user["is_admin"]:
            profile = profiles.get(sub)
            link["sharer_name"] = profile["name"] if profile else "Anonymous"
            if profile:
                link["sharer_email"] = profile["email"]
    return {"links": links}


def extract_pdf(pdf: Path) -> tuple[str, list[dict[str, Any]]]:
    reader = PdfReader(str(pdf))
    metadata = reader.metadata or {}
    title = str(metadata.get("/Title") or pdf.stem).strip()
    pages = []
    for index, page in enumerate(reader.pages, 1):
        text = page.extract_text() or ""
        text = text.replace("\u0000", "").replace("\r\n", "\n")
        text = re.sub(r"(?<=\w)-\n(?=\w)", "", text)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{4,}", "\n\n", text).strip()
        if text:
            pages.append({"page": index, "text": text})
    if not pages:
        raise RuntimeError("No extractable text was found. Scanned PDFs need OCR first.")
    return title or pdf.stem, pages


def chunk_pages(pages: list[dict[str, Any]]) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for page in pages:
        block = f"\n\n--- PAGE {page['page']} ---\n{page['text']}"
        if current and size + len(block) > SCRIPT_CHUNK_CHARS:
            chunks.append("".join(current).strip())
            current, size = [], 0
        current.append(block)
        size += len(block)
    if current:
        chunks.append("".join(current).strip())
    return chunks


def llm_key() -> str:
    direct = (
        os.environ.get("PDF2PODCAST_LLM_API_KEY", "").strip()
        or os.environ.get("SOCLAAS_API_KEY", "").strip()
        or os.environ.get("OPENAI_API_KEY", "").strip()
    )
    if direct:
        return direct
    path = Path(
        os.environ.get(
            "PDF2PODCAST_LLM_KEY_FILE",
            os.environ.get("PDF2PODCAST_OPENAI_KEY_FILE", "~/.pdf2podcast/openai-key"),
        )
    ).expanduser()
    try:
        return path.read_text().strip()
    except OSError as error:
        raise RuntimeError("The Pdf2Podcast language-model key file is missing.") from error


def parse_model_json(content: str) -> dict[str, Any]:
    content = content.strip()
    fence = chr(96) * 3
    if content.startswith(fence):
        content = content[len(fence):].removeprefix("json").strip()
        if content.endswith(fence):
            content = content[:-len(fence)].strip()
    value = json.loads(content)
    if not isinstance(value, dict):
        raise RuntimeError("The model response must be a JSON object.")
    return value


def create_script(
    document_title: str, chunk: dict[str, Any], index: int, total: int, *, finalize: bool = True,
) -> dict[str, Any]:
    request_body = {
        "model": SCRIPT_MODEL,
        "messages": [
            {"role": "system", "content": SCRIPT_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Document: {document_title}\n"
                    f"Annotation batch {index} of {total}, NOT a chapter boundary. "
                    "Map the supplied line IDs to actual printed headings and paragraphs. "
                    "Never generate male narration; it is copied directly from these lines. "
                    "The first paragraph may continue the preceding batch.\n\n"
                    f"{json.dumps(chunk, ensure_ascii=False)}"
                ),
            },
        ],
        "response_format": {"type": "json_object"},
    }
    last_error: Exception | None = None
    for attempt in range(5):
        payload = None
        try:
            with model_usage.request_context(stage="structure", prefer_fallback=attempt > 0):
                payload = request_openai_json(request_body)
            proposal = parse_model_json(payload["choices"][0]["message"]["content"])
            if not isinstance(proposal.get("groups"), list):
                raise RuntimeError("The structure annotation must contain a groups list.")
            if not finalize:
                model_usage.validation(payload, "accepted")
                return proposal
            plan = validate_groups(proposal, chunk)
            model_usage.validation(payload, "accepted")
            return summarize_recovered_paragraphs(plan, chunk)
        except OpenAIRequestError:
            raise
        except (OSError, KeyError, json.JSONDecodeError, RuntimeError) as error:
            model_usage.validation(payload, "rejected", str(error))
            last_error = error
        time.sleep(min(30, 2 ** attempt))
    raise RuntimeError(f"Script generation failed: {last_error}")


def summarize_one_paragraph(context: dict[str, Any], cache_root: Path | None = None) -> dict[str, str]:
    """One request owns one paragraph; the model cannot misassign or omit paragraph IDs."""
    fingerprint = hashlib.sha256(json.dumps(
        {"context": context, "style": SUMMARY_STYLE_PROMPT, "model": SCRIPT_MODEL, "contract": "one-paragraph-v1"},
        sort_keys=True, ensure_ascii=False,
    ).encode()).hexdigest()
    cache_path = cache_root / f"{fingerprint}.json" if cache_root is not None else None
    cached = read_json(cache_path, {}) if cache_path is not None else {}
    if (isinstance(cached, dict) and cached.get("fingerprint") == fingerprint
            and summary_is_concise(cached.get("summary")) and isinstance(cached.get("topic"), str)):
        return {"topic": cached["topic"], "summary": cached["summary"]}
    for attempt in range(4):
        body = {
            "model": SCRIPT_MODEL,
            "messages": [
                {"role": "system", "content": SUMMARY_STYLE_PROMPT +
                 '\nSummarize exactly ONE supplied paragraph. Return one JSON object: '
                 '{"topic":"Specific 3-9 word topic","summary":"One short sentence."}. '
                 "Do not return a list, paragraph IDs, source line ranges, or rewritten narration. " +
                 ("Keep the sentence especially short, ideally 8-12 words." if attempt else "")},
                {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": 250,
        }
        with model_usage.request_context(stage="summary", prefer_fallback=attempt > 0):
            payload = request_openai_json(body)
        try:
            result = parse_model_json(payload["choices"][0]["message"]["content"])
            if not isinstance(result, dict) or not summary_is_concise(result.get("summary")):
                model_usage.validation(payload, "rejected", "Summary is missing or too long.")
                continue
            summary = re.sub(r"\s+", " ", result["summary"]).strip()
            topic = str(result.get("topic") or " ".join(context["text"].split()[:9])).strip()[:180]
            annotation = {"topic": topic, "summary": summary}
            model_usage.validation(payload, "accepted")
            if cache_path is not None:
                atomic_json(cache_path, {"fingerprint": fingerprint, **annotation})
            return annotation
        except (KeyError, TypeError, ValueError, RuntimeError) as error:
            model_usage.validation(payload, "rejected", str(error))
            continue
    raise RuntimeError("A paragraph's short summary could not be generated after four attempts; source and completed summaries are saved.")


def summarize_recovered_paragraphs(
    plan: dict[str, Any], chunk: dict[str, Any], cache_root: Path | None = None,
) -> dict[str, Any]:
    """Retry independent summaries only; excluded metadata never enters the summary queue."""
    pending = []
    for group in plan["groups"]:
        if group.get("kind") not in {"paragraph", "caption"}:
            group.pop("needs_summary", None)
        elif group.get("needs_summary") or not summary_is_concise(group.get("summary")):
            pending.append(group)
    if not pending:
        return plan
    by_id = {line["id"]: line for line in chunk["lines"]}

    def repair(group: dict[str, Any]) -> None:
        context = {
            "document_title": chunk.get("document_title"),
            "preceding_context": chunk.get("preceding_context", ""),
            "text": join_lines([by_id[index] for index in range(group["start"], group["end"] + 1)], text_key="reading_text"),
        }
        annotation = summarize_one_paragraph(context, cache_root)
        group.update(annotation)
        group.pop("needs_summary", None)

    run_bounded_parallel(pending, repair, SUMMARY_CONCURRENCY)
    return plan


def refine_summary_briefs(root: Path, title: str, chapters: list[dict[str, Any]]) -> None:
    """Introduce merged paragraphs once, preserving all of their source narration."""
    pending = []
    for chapter in chapters:
        for section in chapter["sections"]:
            if not section.get("summary_needs_brief") and summary_is_concise(section.get("summary")):
                continue
            context = {"id": section["paragraph_id"], "document_title": title,
                       "chapter_title": chapter.get("source_heading") or chapter["title"],
                       "text": section["source_text"]}
            fingerprint = hashlib.sha256(json.dumps(
                {"context": context, "style": SUMMARY_STYLE_PROMPT, "model": SCRIPT_MODEL},
                sort_keys=True, ensure_ascii=False,
            ).encode()).hexdigest()
            cached = read_json(root / "summary-briefs" / f"{section['paragraph_id']}.json", {})
            if isinstance(cached, dict) and cached.get("fingerprint") == fingerprint and summary_is_concise(cached.get("summary")):
                section["summary"] = cached["summary"]
                section.pop("summary_needs_brief", None)
            else:
                pending.append((section, context, fingerprint))

    def generate(item: Any) -> None:
        section, context, fingerprint = item
        annotation = summarize_one_paragraph({key: value for key, value in context.items() if key != "id"},
                                             root / "paragraph-summaries")
        section["summary"] = annotation["summary"]
        section.pop("summary_needs_brief", None)
        atomic_json(root / "summary-briefs" / f"{section['paragraph_id']}.json",
                    {"fingerprint": fingerprint, "summary": section["summary"]})

    run_bounded_parallel(pending, generate, SUMMARY_CONCURRENCY)
    for chapter in chapters:
        for section in chapter["sections"]:
            for segment in section["segments"]:
                if segment["kind"] == "summary":
                    segment["lines"] = [section["summary"]]


def clean_script(value: dict[str, Any], fallback_title: str) -> dict[str, Any]:
    sections = []
    for section in value.get("sections", []):
        if not isinstance(section, dict):
            continue
        segments = []
        for segment in section.get("segments", []):
            if not isinstance(segment, dict):
                continue
            speaker = "guide" if segment.get("speaker") == "guide" else "source"
            kind = str(segment.get("kind") or ("source" if speaker == "source" else "explanation"))
            lines = [
                re.sub(r"\s+", " ", str(line)).strip()
                for line in segment.get("lines", [])
                if str(line).strip()
            ]
            if lines:
                segments.append({"speaker": speaker, "kind": kind, "lines": lines})
        if segments:
            sections.append(
                {
                    "title": str(section.get("title") or "Untitled section").strip(),
                    "segments": segments,
                }
            )
    if not sections:
        raise RuntimeError("The script model returned no usable sections.")
    return {
        "title": str(value.get("chapter_title") or fallback_title).strip(),
        "sections": sections,
    }


def split_speech_text(text: str, limit: int = 2400) -> list[str]:
    """Bound provider requests at sentence or word boundaries, without dropping text."""
    chunks = []
    remaining = text.strip()
    while len(remaining) > limit:
        window = remaining[:limit + 1]
        sentences = list(re.finditer(r"[.!?][\"')\]]?\s+", window))
        end = sentences[-1].end() if sentences and sentences[-1].end() >= limit // 2 else 0
        if not end:
            spaces = list(re.finditer(r"\s+", window))
            end = spaces[-1].end() if spaces else limit
        end = min(end, limit)
        chunks.append(remaining[:end].strip())
        remaining = remaining[end:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


WORD_TIMING_VERSION = 1


class SpeechBoundaries(list[tuple[float, str]]):
    """Keep sentence alignment compatible while carrying genuine provider word spans."""

    def __init__(self, boundaries=(), word_timings=()):
        super().__init__(boundaries)
        self.word_timings: list[dict[str, Any]] = list(word_timings)


def word_boundary_options() -> dict[str, str]:
    """Request words only on providers that explicitly accept the boundary option."""
    try:
        parameter = inspect.signature(edge_tts.Communicate).parameters.get("boundary")
    except (TypeError, ValueError):
        return {}
    if parameter is not None and parameter.kind in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY,
    ):
        return {"boundary": "WordBoundary"}
    return {}


async def synthesize(text: str, voice: str, output: Path) -> SpeechBoundaries:
    boundaries = SpeechBoundaries()
    sentences: list[tuple[float, str]] = []
    audio = bytearray()
    communicator = edge_tts.Communicate(text, voice, **word_boundary_options())
    async for item in communicator.stream():
        if item["type"] == "audio":
            audio.extend(item["data"])
        elif item["type"] in ("WordBoundary", "SentenceBoundary"):
            # Missing or malformed optional metadata must not discard usable audio.
            try:
                start = float(item["offset"]) / 10_000_000
                word = item["text"]
                if not math.isfinite(start) or start < 0 or not isinstance(word, str) or not word.strip():
                    continue
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if item["type"] == "SentenceBoundary":
                sentences.append((start, word))
                continue
            boundaries.append((start, word))
            try:
                end = start + float(item["duration"]) / 10_000_000
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(end) and end > start:
                boundaries.word_timings.append({"text": word, "start": start, "end": end})
    # Never mix sentence and word events: line_offsets consumes each text once.
    if not boundaries:
        boundaries.extend(sentences)
    output.write_bytes(audio)
    return boundaries


def duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe", "-v", "quiet", "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1", str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        return round(float(result.stdout.strip()), 3)
    except ValueError:
        return 0.0


def normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def remove_unspeakable_sections(chapters: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Discard isolated glyph artifacts before they become transcript or TTS entries."""
    cleaned = []
    removed = 0
    for chapter in chapters:
        sections = []
        for section in chapter.get("sections", []):
            source = str(section.get("spoken_text") or section.get("source_text") or "")
            if any(character.isalnum() for character in source):
                sections.append(section)
            else:
                removed += 1
        if sections:
            cleaned.append({**chapter, "sections": sections})
    return cleaned, removed


def line_offsets(
    lines: list[str], boundaries: list[tuple[float, str]], total_duration: float
) -> list[float]:
    if not boundaries:
        total_chars = max(1, sum(len(line) for line in lines))
        consumed = 0
        offsets = []
        for line in lines:
            offsets.append(total_duration * consumed / total_chars)
            consumed += len(line)
        return offsets
    offsets, pointer = [], 0
    for line in lines:
        target = normalized(line)
        start = boundaries[pointer][0] if pointer < len(boundaries) else total_duration
        accumulated = ""
        while pointer < len(boundaries) and len(accumulated) < len(target):
            accumulated += normalized(boundaries[pointer][1])
            pointer += 1
        offsets.append(round(start, 3))
    return offsets


def concatenate(inputs: list[Path], output: Path) -> None:
    if not inputs:
        raise RuntimeError("There was no audio to assemble.")
    manifest = output.parent / f".{output.stem}-concat.txt"
    lines = []
    for path in inputs:
        escaped = str(path.resolve()).replace("'", "'\\''")
        lines.append(f"file '{escaped}'\n")
    manifest.write_text("".join(lines))
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(manifest),
            "-c", "copy", str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    manifest.unlink(missing_ok=True)
    if result.returncode:
        raise RuntimeError(f"Audio assembly failed: {result.stderr[-500:]}")


def prepare_segment_speech(
    segment_dir: Path, segment_number: int, segment: dict[str, Any],
) -> list[str]:
    """Cache pronunciation separately from immutable transcript lines and audio clips."""
    lines = segment["lines"]
    fingerprint = hashlib.sha256(json.dumps(
        {"lines": lines, "model": SCRIPT_MODEL, "version": SPEECH_VERSION, "prompt": SPEECH_PROMPT},
        sort_keys=True, ensure_ascii=False,
    ).encode()).hexdigest()
    path = segment_dir / f"{segment_number:03d}-{segment['speaker']}.speech.json"
    cached = read_json(path, {})
    if isinstance(cached, dict) and cached.get("fingerprint") == fingerprint:
        spoken = cached.get("lines")
        if isinstance(spoken, list) and len(spoken) == len(lines) and all(isinstance(line, str) and line.strip() for line in spoken):
            return spoken
    spoken = []
    fallback_count = 0
    for original in lines:
        text = pronunciation_text(original)
        spans = notation_spans(text)
        if spans:
            prepared = None
            for attempt in range(2):
                body = {
                    "model": SCRIPT_MODEL,
                    "messages": [
                        {"role": "system", "content": SPEECH_PROMPT},
                        {"role": "user", "content": json.dumps({
                            "context": text,
                            "spans": [{"id": span["id"], "text": span["text"]} for span in spans],
                            "retry": bool(attempt),
                        }, ensure_ascii=False)},
                    ],
                    "response_format": {"type": "json_object"},
                    "max_tokens": min(4000, max(512, len(spans) * 160)),
                }
                with model_usage.request_context(stage="pronunciation", prefer_fallback=attempt > 0):
                    payload = request_openai_json(body)
                try:
                    proposal = parse_model_json(payload["choices"][0]["message"]["content"])
                    prepared = apply_notation_readings(text, spans, proposal)
                    model_usage.validation(payload, "accepted")
                    break
                except (KeyError, IndexError, TypeError, ValueError, RuntimeError) as error:
                    model_usage.validation(payload, "rejected", str(error))
                    continue
            if prepared is None:
                fallback_count += 1
                prepared = fallback_notation(text, spans)
                print(f"Pronunciation fallback: {segment_dir.name}/{segment_number:03d}", flush=True)
            text = prepared
        spoken.append(text)
    atomic_json(path, {"fingerprint": fingerprint, "lines": spoken, "fallback_count": fallback_count})
    return spoken


def synthesize_segment_audio(
    segment_dir: Path, segment_number: int, segment: dict[str, Any],
) -> tuple[Path, list[tuple[float, str]], float, list[str]]:
    """Checkpoint independent voice clips without making completion order meaningful."""
    speech_lines = segment.get("_prepared_speech_lines") or prepare_segment_speech(segment_dir, segment_number, segment)
    text = "\n".join(speech_lines)
    voice = GUIDE_VOICE if segment["speaker"] == "guide" else SOURCE_VOICE
    part = segment_dir / f"{segment_number:03d}-{segment['speaker']}.mp3"
    timing_key = json.dumps({"version": WORD_TIMING_VERSION, "options": word_boundary_options(),
                             "provider": getattr(edge_tts, "__version__", "unknown")}, sort_keys=True)
    speech_key = hashlib.sha256((voice + "\n" + text + "\n" + timing_key).encode()).hexdigest()
    checkpoint_path = part.with_suffix(".timing.json")
    checkpoint = read_json(checkpoint_path, {})
    cached = (isinstance(checkpoint, dict) and checkpoint.get("speech_key") == speech_key
              and checkpoint.get("word_timing_version") == WORD_TIMING_VERSION
              and isinstance(checkpoint.get("word_timings"), list)
              and part.is_file() and part.stat().st_size > 0)
    if cached:
        boundaries = SpeechBoundaries(
            [(float(offset), word) for offset, word in checkpoint.get("boundaries", [])],
            checkpoint["word_timings"],
        )
    else:
        # Never let a failed partial download inherit an older successful checkpoint.
        checkpoint_path.unlink(missing_ok=True)
        try:
            chunks = split_speech_text(text)
            if len(chunks) > 1:
                chunk_dir = segment_dir / f"{segment_number:03d}-{segment['speaker']}-chunks"
                chunk_dir.mkdir(parents=True, exist_ok=True)
                chunk_paths = []
                boundaries = SpeechBoundaries()
                elapsed = 0.0
                # Do not hold a speech slot while invoking child clips. Each child owns
                # its timeout/checkpoint, so a late failure keeps earlier chunks reusable.
                for number, chunk in enumerate(chunks, 1):
                    child = {**segment, "lines": [chunk], "_prepared_speech_lines": [chunk]}
                    chunk_path, chunk_boundaries, chunk_duration, _ = synthesize_segment_audio(chunk_dir, number, child)
                    chunk_paths.append(chunk_path)
                    boundaries.extend((round(elapsed + offset, 3), word) for offset, word in chunk_boundaries)
                    boundaries.word_timings.extend(
                        {**word, "start": elapsed + word["start"], "end": elapsed + word["end"]}
                        for word in getattr(chunk_boundaries, "word_timings", [])
                    )
                    elapsed += chunk_duration
                with MEDIA_SLOTS:
                    concatenate(chunk_paths, part)
            else:
                with SPEECH_GATE:
                    boundaries = asyncio.run(asyncio.wait_for(
                        synthesize(text, voice, part), timeout=TTS_TIMEOUT_SECONDS,
                    ))
                SPEECH_GATE.succeeded()
        except TimeoutError as error:
            raise TimeoutError(
                f"Speech request timed out after {TTS_TIMEOUT_SECONDS}s: "
                f"{segment_dir.name}/{segment_number:03d}, {len(text)} characters. Saved chunks are retained."
            ) from error
        except Exception as error:
            if getattr(error, "status", None) in {429, 503} or type(error).__name__ == "NoAudioReceived":
                SPEECH_GATE.throttled(3 + random.uniform(0, 2))
            raise
    with MEDIA_SLOTS:
        part_duration = duration(part)
    if part_duration <= 0:
        raise RuntimeError("The speech provider returned an empty or unreadable audio segment.")
    if not cached:
        atomic_json(checkpoint_path, {
            "speech_key": speech_key, "boundaries": boundaries,
            "word_timing_version": WORD_TIMING_VERSION,
            "word_timings": getattr(boundaries, "word_timings", []),
        })
    return part, boundaries, part_duration, speech_lines


def synthesize_section(
    root: Path,
    chapter_number: int,
    section_number: int,
    section: dict[str, Any],
    speech_pool: ThreadPoolExecutor | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    slug = f"{chapter_number:02d}-{section_number:02d}-{slugify(section['title'])}"
    cached_slug = section.get("audio_cache_slug")
    if isinstance(cached_slug, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", cached_slug):
        slug = cached_slug
    segment_dir = root / "segments" / slug
    segment_dir.mkdir(parents=True, exist_ok=True)
    section_audio = root / "sections" / f"{slug}.mp3"
    section_audio.parent.mkdir(parents=True, exist_ok=True)
    parts: list[Path] = []
    synced_lines: list[dict[str, Any]] = []
    elapsed = 0.0
    source_start = None
    summary_start = None
    source_end = None
    summary_end = None
    audio_segments = []
    word_timings = []
    source_speech_lines: list[str] = []

    if speech_pool is None:
        rendered = [synthesize_segment_audio(segment_dir, number, segment)
                    for number, segment in enumerate(section["segments"], 1)]
    else:
        # Only active paragraph workers submit clips to this one shared pool.
        # The title, summary and source can render together, but are joined in order.
        futures = [speech_pool.submit(synthesize_segment_audio, segment_dir, number, segment)
                   for number, segment in enumerate(section["segments"], 1)]
        try:
            rendered = [future.result() for future in futures]
        except BaseException:
            for future in futures:
                future.cancel()
            # A retry must not write the same clip while its previous attempt runs.
            wait(futures)
            raise

    for segment, (part, boundaries, part_duration, speech_lines) in zip(section["segments"], rendered):
        lines = segment["lines"]
        if segment["speaker"] == "source" and source_start is None:
            source_start = elapsed
        if segment["speaker"] == "source":
            source_end = elapsed + part_duration
            source_speech_lines.extend(speech_lines)
        if segment["kind"] == "summary" and summary_start is None:
            summary_start = elapsed
        if segment["kind"] == "summary":
            summary_end = elapsed + part_duration
        audio_segments.append({"speaker": segment["speaker"], "kind": segment["kind"],
                               "start": round(elapsed, 3), "end": round(elapsed + part_duration, 3)})
        word_timings.extend(
            {"text": word["text"], "start": elapsed + word["start"],
             "end": elapsed + word["end"], "speaker": segment["speaker"], "kind": segment["kind"]}
            for word in getattr(boundaries, "word_timings", [])
        )
        offsets = line_offsets(speech_lines, boundaries, part_duration)
        for line_index, (offset, line) in enumerate(zip(offsets, lines)):
            synced_lines.append(
                {
                    "t": round(elapsed + offset, 3),
                    "end": round(elapsed + (offsets[line_index + 1] if line_index + 1 < len(offsets) else part_duration), 3),
                    "speaker": segment["speaker"],
                    "kind": segment["kind"],
                    "text": line,
                    "spoken_text": speech_lines[line_index],
                    "paragraph_id": section.get("paragraph_id"),
                    "chapter_n": chapter_number,
                    "section_n": section_number,
                    "page_start": section.get("page_start"),
                    "page_end": section.get("page_end"),
                    "source_text": section.get("source_text") if segment["speaker"] == "source" else None,
                }
            )
        parts.append(part)
        elapsed += part_duration

    with MEDIA_SLOTS:
        concatenate(parts, section_audio)
        actual_duration = duration(section_audio)
    if actual_duration <= 0:
        raise RuntimeError("Paragraph audio assembly produced no playable audio.")
    paragraph_info = {key: section.get(key) for key in (
        "paragraph_id", "content_kind", "source_text", "spoken_text", "summary", "page_start", "page_end",
    )}
    paragraph_info["spoken_text"] = " ".join(source_speech_lines)
    paragraph_info.update(source_start=source_start, source_end=source_end,
                          summary_start=summary_start, summary_end=summary_end,
                          word_timing_version=WORD_TIMING_VERSION, word_timings=word_timings,
                          audio_segments=audio_segments, end=actual_duration)
    sync = {
        "n": section_number,
        "title": section["title"],
        "slug": slug,
        "duration": actual_duration,
        "lines": synced_lines,
        **paragraph_info,
    }
    sync_path = root / "sync" / f"{slug}.json"
    atomic_json(sync_path, sync)
    return (
        {
            "n": section_number,
            "title": section["title"],
            "slug": slug,
            "duration": actual_duration,
            **paragraph_info,
        },
        synced_lines,
    )


def synthesize_section_with_retries(
    root: Path, chapter_number: int, section_number: int, section: dict[str, Any],
    speech_pool: ThreadPoolExecutor | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Retry one paragraph with backoff, retaining any completed voice segment."""
    for attempt in range(TTS_ATTEMPTS):
        try:
            return synthesize_section(root, chapter_number, section_number, section, speech_pool)
        except Exception as error:
            detail = f"{type(error).__name__}: {str(error).strip() or 'No error message was supplied.'}"
            print(f"Audio attempt {attempt + 1}/{TTS_ATTEMPTS}, chapter {chapter_number}, "
                  f"paragraph {section_number} ({section.get('paragraph_id')}): {detail}", flush=True)
            # Configuration/storage failures do not improve with more provider calls.
            status = getattr(error, "status", None)
            if isinstance(error, (PermissionError, FileNotFoundError)) or (
                isinstance(status, int) and 400 <= status < 500 and status not in {408, 409, 425, 429}
            ) or attempt + 1 >= TTS_ATTEMPTS:
                raise RuntimeError(
                    f"Audio failed for chapter {chapter_number}, paragraph {section_number} "
                    f"({section.get('paragraph_id')}): {detail}"
                ) from error
            time.sleep(min(30, 2 ** (attempt + 1)) + random.uniform(0, 1))
    raise RuntimeError("Speech generation exhausted its attempts.")


def narrate_paragraphs_parallel(
    root: Path, job_id: str, chapters: list[dict[str, Any]],
) -> dict[tuple[int, int], tuple[dict[str, Any], list[dict[str, Any]]]]:
    """Generate paragraph audio concurrently, leaving global timing to ordered assembly."""
    items = [(chapter_number, section_number, section)
             for chapter_number, chapter in enumerate(chapters, 1)
             for section_number, section in enumerate(chapter["sections"], 1)]
    workers = min(TTS_CONCURRENCY, len(items))
    results: dict[tuple[int, int], tuple[dict[str, Any], list[dict[str, Any]]]] = {}
    started = time.monotonic()
    completed_weight = 0

    def weight(item: tuple[int, int, dict[str, Any]]) -> int:
        return max(1, sum(len(line) for segment in item[2]["segments"] for line in segment["lines"]))

    total_weight = sum(weight(item) for item in items)
    save_job(job_id, narration_concurrency=workers, speech_concurrency=TTS_CLIP_CONCURRENCY,
             stage=f"Narrating {workers} paragraphs with up to {TTS_CLIP_CONCURRENCY} voice clips at once",
             estimated_seconds_remaining=None)

    def completed(item: tuple[int, int, dict[str, Any]], result: Any) -> None:
        nonlocal completed_weight
        chapter_number, section_number, _ = item
        results[(chapter_number, section_number)] = result
        completed_weight += weight(item)
        count = len(results)
        # Use observed wall-clock throughput, not summed worker runtimes. Wait for
        # a first wave of completions before presenting a speculative parallel ETA.
        estimate = None
        if count >= workers:
            estimate = round((time.monotonic() - started) * (total_weight - completed_weight) / max(1, completed_weight))
        save_job(job_id, completed_chunks=count,
                 stage=f"Narrating: {count} of {len(items)} paragraphs complete (up to {TTS_CLIP_CONCURRENCY} voice clips at once)",
                 current_chapter=chapter_number, current_section=section_number,
                 progress_percent=42 + round(52 * count / len(items), 1),
                 estimated_seconds_remaining=estimate)

    with ThreadPoolExecutor(max_workers=TTS_CLIP_CONCURRENCY, thread_name_prefix="pdf2podcast-speech") as speech_pool:
        run_bounded_parallel(
            items,
            lambda item: synthesize_section_with_retries(root, item[0], item[1], item[2], speech_pool),
            TTS_CONCURRENCY,
            completed,
        )
    return results


def annotation_fingerprint(chunk: dict[str, Any]) -> str:
    """Keep checkpoints sensitive to both immutable source and its annotation context."""
    return hashlib.sha256(json.dumps(
        {"chunk": chunk, "prompt": SCRIPT_SYSTEM_PROMPT, "summary_style": SUMMARY_STYLE_PROMPT, "model": SCRIPT_MODEL},
        sort_keys=True, ensure_ascii=False,
    ).encode()).hexdigest()


def prepare_annotation_contexts(title: str, chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Predict context from printed headings; ordered validation remains authoritative."""
    prepared = []
    references, metadata = False, False
    for index, original in enumerate(chunks):
        prepared.append({**original, "document_title": title,
                         "preceding_context": join_lines(chunks[index - 1]["lines"], text_key="reading_text")[-1200:] if index else "",
                         "reference_section": references, "metadata_section": metadata})
        for line in original["lines"]:
            text = line["text"].strip()
            if REFERENCE_HEADING.fullmatch(text):
                references, metadata = True, False
            elif METADATA_HEADING.fullmatch(text):
                references, metadata = False, True
            elif (line.get("bold") and not line.get("caption_hint") and not line.get("figure_text_hint")
                  and len(text) <= 160 and len(text.split()) <= 18 and not text.endswith((".", ";", ","))):
                references, metadata = False, False
    return prepared


def load_annotation_proposal(
    root: Path, title: str, chunk: dict[str, Any], index: int, total: int,
) -> tuple[str, dict[str, Any]]:
    """Save parallel API output before reconciliation, so a failed neighbour wastes no calls."""
    fingerprint = annotation_fingerprint(chunk)
    cached = read_json(root / "annotations" / f"{index:03d}.json", {})
    if isinstance(cached, dict) and cached.get("source_fingerprint") == fingerprint:
        return fingerprint, cached
    proposal_path = root / "annotation-proposals" / f"{index:03d}.json"
    cached = read_json(proposal_path, {})
    if (isinstance(cached, dict) and cached.get("source_fingerprint") == fingerprint
            and isinstance(cached.get("proposal"), dict)):
        return fingerprint, cached["proposal"]
    proposal = create_script(title, chunk, index, total, finalize=False)
    atomic_json(proposal_path, {"source_fingerprint": fingerprint, "proposal": proposal})
    return fingerprint, proposal


def annotate_document_parallel(
    root: Path, job_id: str, title: str, chunks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Fan out expensive mapping calls, then reconcile actual chapter state in PDF order."""
    prepared = prepare_annotation_contexts(title, chunks)
    # A resumed job already knows its earlier chapter states. Use them as hints
    # to avoid speculative calls for completed batches; fingerprints still decide reuse.
    for index in range(1, len(prepared)):
        previous = read_json(root / "annotations" / f"{index:03d}.json", {})
        if isinstance(previous, dict) and previous.get("source_fingerprint"):
            prepared[index]["reference_section"] = bool(previous.get("reference_section"))
            prepared[index]["metadata_section"] = bool(previous.get("metadata_section"))
    items = list(enumerate(prepared, 1))
    workers = min(SCRIPT_CONCURRENCY, len(items))
    proposals: dict[int, tuple[str, dict[str, Any]]] = {}
    started = time.monotonic()
    save_job(job_id, script_concurrency=workers, summary_concurrency=SUMMARY_CONCURRENCY,
             stage=f"Mapping the paper with up to {workers} requests at once", estimated_seconds_remaining=None)

    def mapped(item: Any, result: Any) -> None:
        proposals[item[0]] = result
        count = len(proposals)
        remaining = round((time.monotonic() - started) * (len(items) - count) / count) if count >= workers else None
        save_job(job_id, completed_chunks=count,
                 stage=f"Mapping paper: {count} of {len(items)} batches complete (up to {workers} at once)",
                 progress_percent=5 + round(22 * count / len(items), 1), estimated_seconds_remaining=remaining)

    run_bounded_parallel(
        items, lambda item: load_annotation_proposal(root, title, item[1], item[0], len(items)), workers, mapped,
    )

    plans = []
    contexts = []
    context_retries = 0
    for index, predicted in items:
        actual = {**predicted,
                  "reference_section": bool(plans and plans[-1].get("reference_section")),
                  "metadata_section": bool(plans and plans[-1].get("metadata_section"))}
        fingerprint, proposal = proposals[index]
        if fingerprint != annotation_fingerprint(actual):
            # A guessed heading is never allowed to change what is retained or
            # omitted. Replay only the affected batch with its true preceding state.
            context_retries += 1
            save_job(job_id, stage=f"Resolving chapter context for batch {index} of {len(items)}",
                     context_retries=context_retries, estimated_seconds_remaining=None)
            fingerprint, proposal = load_annotation_proposal(root, title, actual, index, len(items))
        plan = validate_groups(proposal, actual)
        plan["source_fingerprint"] = fingerprint
        plans.append(plan)
        contexts.append(actual)
        save_job(job_id, stage=f"Reconciling paper order: batch {index} of {len(items)}",
                 progress_percent=27 + round(3 * index / len(items), 1), estimated_seconds_remaining=None)

    completed_summaries = 0

    def summarized(item: Any, plan: Any) -> None:
        nonlocal completed_summaries
        index = item[0]
        plans[index - 1] = plan
        atomic_json(root / "annotations" / f"{index:03d}.json", plan)
        completed_summaries += 1
        save_job(job_id, completed_chunks=completed_summaries,
                 stage=f"Preparing summaries: {completed_summaries} of {len(items)} batches complete",
                 progress_percent=30 + round(10 * completed_summaries / len(items), 1), estimated_seconds_remaining=None)

    run_bounded_parallel(
        list(enumerate(plans, 1)),
        lambda item: summarize_recovered_paragraphs(item[1], contexts[item[0] - 1], root / "paragraph-summaries"),
        SUMMARY_CONCURRENCY, summarized,
    )
    return plans


def process_job(job_id: str) -> None:
    root = safe_job(job_id)
    started = time.monotonic()
    usage_context = model_usage.bind_job(WEB_DB, job_id)
    try:
        save_job(
            job_id,
            status="processing",
            stage="Extracting readable text",
            progress_percent=2,
            error=None,
            started_at=now(),
        )
        cached_content = read_json(root / "content.json", {})
        resume_prepared = load_job(job_id).get("resume_prepared_content") is True
        if (resume_prepared and isinstance(cached_content, dict) and cached_content.get("version") == 4
                and isinstance(cached_content.get("chapters"), list) and cached_content["chapters"]):
            saved_document = read_json(root / "extracted.json", {}) or {}
            if not isinstance(saved_document, dict):
                saved_document = {}
            job = load_job(job_id)
            title = select_document_title(
                model_title=cached_content.get("title"), pdf_title=saved_document.get("title"),
                first_page_title=first_page_title(saved_document.get("lines", [])),
                current_title=job.get("title"), filename=job.get("filename"),
            )
            if title != cached_content.get("title"):
                cached_content["title"] = title
                atomic_json(root / "content.json", cached_content)
            chapters = cached_content["chapters"]
            save_job(job_id, title=title, stage="Resuming checked paragraphs from saved audio checkpoints",
                     estimated_seconds_remaining=None)
        elif load_job(job_id).get("paragraph_pipeline") == VISION_PIPELINE:
            job = load_job(job_id)
            document = extract_visual_document(root / "input.pdf", job.get("filename", "paper.pdf"))
            model = job.get("layout_model") or os.environ.get("PDF2PODCAST_LAYOUT_MODEL", "gpt-4.1")
            atomic_json(root / "extracted.json", document)
            save_job(job_id, page_count=document["page_count"], pipeline_version=5,
                     stage="Preparing visual paragraph extraction", progress_percent=3,
                     estimated_seconds_remaining=None)

            def visual_progress(stage: str, completed: int, total: int) -> None:
                if stage.startswith("Rendering"):
                    base, span = 3, 7
                elif stage.startswith("Reconciling"):
                    base, span = 35, 2
                else:
                    base, span = 10, 25
                save_job(job_id, stage=f"{stage}: {completed} of {total}",
                         completed_chunks=completed, total_chunks=total,
                         progress_percent=base + round(span * completed / max(1, total), 1),
                         estimated_seconds_remaining=None)

            chapters, omitted = build_visual_chapters(
                root, document, model, request_openai_json, run_bounded_parallel, visual_progress,
            )
            title = document["title"]
            atomic_json(root / "extracted.json", document)
            save_job(job_id, title=title, stage="Summarizing complete paragraphs, not page fragments",
                     progress_percent=37, estimated_seconds_remaining=None)
            refine_summary_briefs(root, title, chapters)
            atomic_json(root / "extraction-omissions.json", omitted)
            atomic_json(root / "content.json", {
                "version": 4, "paragraph_pipeline": VISION_PIPELINE, "layout_model": model,
                "word_timing_version": WORD_TIMING_VERSION,
                "reading_mode": "paper_companion", "title": title,
                "voices": {"source": SOURCE_VOICE, "guide": GUIDE_VOICE},
                "chapters": [{**chapter, "n": index} for index, chapter in enumerate(chapters, 1)],
            })
        else:
            job = load_job(job_id)
            document = extract_document(root / "input.pdf", job.get("filename", "paper.pdf"))
            title = select_document_title(
                pdf_title=document.get("title"), first_page_title=first_page_title(document.get("lines", [])),
                current_title=job.get("title"), filename=job.get("filename"),
            )
            document["title"] = title
            chunks = annotation_batches(document, SCRIPT_CHUNK_CHARS)
            atomic_json(root / "extracted.json", document)
            (root / "extracted.txt").write_text(
                "\n".join(f"[page {line['page']}, line {line['id']}] {line['text']}" for line in document["lines"])
            )
            save_job(
                job_id,
                title=title,
                page_count=document["page_count"],
                reading_mode="paper_companion",
                pipeline_version=4,
                total_chunks=len(chunks),
                completed_chunks=0,
                stage="Identifying original headings and paragraphs",
                progress_percent=5,
            )

            plans = annotate_document_parallel(root, job_id, title, chunks)

            chapters, omitted = chapters_from_plans(document, plans)
            save_job(job_id, stage="Preparing short paragraph summaries", estimated_seconds_remaining=None)
            refine_summary_briefs(root, title, chapters)
            atomic_json(root / "extraction-omissions.json", omitted)
            content = {
                "version": 4,
                "word_timing_version": WORD_TIMING_VERSION,
                "reading_mode": "paper_companion",
                "title": title,
                "voices": {"source": SOURCE_VOICE, "guide": GUIDE_VOICE},
                "chapters": [
                    {**chapter, "n": index}
                    for index, chapter in enumerate(chapters, 1)
                ],
            }
            atomic_json(root / "content.json", content)
        chapters, removed_speech_artifacts = remove_unspeakable_sections(chapters)
        if removed_speech_artifacts:
            cached_content = read_json(root / "content.json", {})
            if isinstance(cached_content, dict):
                cached_content["chapters"] = chapters
                atomic_json(root / "content.json", cached_content)
            print(f"Omitted {removed_speech_artifacts} glyph-only transcript artifact(s).", flush=True)
        total_sections = sum(len(chapter["sections"]) for chapter in chapters)
        save_job(
            job_id,
            completed_chunks=0,
            total_chunks=total_sections,
            chapter_count=len(chapters),
            section_count=total_sections,
            paragraph_count=total_sections,
            stage="Preparing original-text narration and paragraph summaries",
            progress_percent=42,
        )

        # Network completion order must never influence paragraph order or timestamps.
        recordings = narrate_paragraphs_parallel(root, job_id, chapters)
        manifest_chapters = []
        all_audio: list[Path] = []
        global_lines: list[dict[str, Any]] = []
        global_time = 0.0
        for chapter_number, chapter in enumerate(chapters, 1):
            manifest_sections = []
            for section_number, section in enumerate(chapter["sections"], 1):
                section_info, lines = recordings[(chapter_number, section_number)]
                section_info["start"] = round(global_time, 3)
                for timestamp in ("source_start", "source_end", "summary_start", "summary_end", "end"):
                    if section_info.get(timestamp) is not None:
                        section_info[timestamp] = round(global_time + section_info[timestamp], 3)
                section_info["audio_segments"] = [
                    {**segment, "start": round(global_time + segment["start"], 3),
                     "end": round(global_time + segment["end"], 3)}
                    for segment in section_info.get("audio_segments", [])
                ]
                section_info["word_timings"] = [
                    {**word, "start": round(global_time + word["start"], 6),
                     "end": round(global_time + word["end"], 6)}
                    for word in section_info.get("word_timings", [])
                ]
                for line in lines:
                    global_lines.append({**line, "t": round(global_time + line["t"], 3),
                                         "end": round(global_time + line["end"], 3)})
                manifest_sections.append(section_info)
                audio_path = root / "sections" / f"{section_info['slug']}.mp3"
                all_audio.append(audio_path)
                global_time += section_info["duration"]
            manifest_chapters.append(
                {
                    "n": chapter_number,
                    "title": chapter["title"],
                    "source_heading": chapter.get("source_heading"),
                    "level": chapter.get("level", 1),
                    "page_start": chapter.get("page_start"),
                    "page_end": chapter.get("page_end"),
                    "sections": manifest_sections,
                }
            )

        save_job(
            job_id,
            stage="Assembling the finished episode",
            progress_percent=96,
            estimated_seconds_remaining=30,
        )
        concatenate(all_audio, root / "audio.mp3")
        full_duration = duration(root / "audio.mp3")
        manifest = {
            "version": 4,
            "word_timing_version": WORD_TIMING_VERSION,
            "reading_mode": "paper_companion",
            "title": title,
            "voices": {"source": SOURCE_VOICE, "guide": GUIDE_VOICE},
            "duration": full_duration,
            "chapters": manifest_chapters,
        }
        atomic_json(root / "manifest.json", manifest)
        atomic_json(
            root / "transcript.json",
            {"version": 4, "reading_mode": "paper_companion", "duration": full_duration, "lines": global_lines},
        )
        (root / "transcript.txt").write_text(
            "\n\n".join(
                f"[{'Guide' if line['speaker'] == 'guide' else 'Source'}]\n{line['text']}"
                for line in global_lines
            )
        )
        save_job(
            job_id,
            status="complete",
            stage="Ready to listen",
            progress_percent=100,
            estimated_seconds_remaining=0,
            completed_chunks=total_sections,
            audio_bytes=(root / "audio.mp3").stat().st_size,
            duration_seconds=full_duration,
            completed_at=now(),
            processing_seconds=round(time.monotonic() - started),
            error=None,
        )
    except Exception as error:
        traceback.print_exc()
        save_job(
            job_id,
            status="failed",
            stage="Conversion failed",
            error=f"{type(error).__name__}: {str(error).strip() or 'No error message was supplied; see server traceback.'}",
            estimated_seconds_remaining=0,
            failed_at=now(),
        )
    finally:
        model_usage.CONTEXT.reset(usage_context)


def retry_job(job_id: str, keep_retrying: bool) -> dict[str, Any]:
    """Requeue the same saved PDF, never create another library entry or concurrent run."""
    with JOB_CONTROL_LOCK:
        job = load_job(job_id)
        if job.get("status") in {"complete", "completed"}:
            return job
        if not (safe_job(job_id) / "input.pdf").is_file():
            raise ValueError("The saved PDF is missing; upload the document again.")
        if job.get("status") in {"queued", "processing", "running"}:
            return save_job(job_id, retry_until_complete=keep_retrying)
        job = save_job(job_id, status="queued", stage="Queued to resume saved conversion",
                       retry_until_complete=keep_retrying, next_retry_at=None,
                       last_error=job.get("error"), error=None, estimated_seconds_remaining=None)
        UPLOADS.put(job_id)
        return job


def schedule_retry(job_id: str) -> None:
    with JOB_CONTROL_LOCK:
        job = load_job(job_id)
        if job.get("status") != "failed" or not job.get("retry_until_complete"):
            return
        error = str(job.get("error") or "")
        if any(marker in error.lower() for marker in ("insufficient_quota", "invalid_api_key", "http 401", "http 403", "password protected", "no extractable text was found")):
            save_job(job_id, stage="Needs attention before retrying", retry_until_complete=False)
            return
        attempts = int(job.get("retry_attempts") or 0) + 1
        delay = min(900, 30 * (2 ** min(attempts - 1, 5)))
        due = datetime.fromtimestamp(time.time() + delay, timezone.utc).isoformat()
        save_job(job_id, status="queued", stage=f"Automatic retry {attempts} in {delay} seconds",
                 retry_attempts=attempts, next_retry_at=due, last_error=error,
                 estimated_seconds_remaining=delay)

        def enqueue() -> None:
            with JOB_CONTROL_LOCK:
                current = load_job(job_id)
                if current.get("status") == "queued" and current.get("next_retry_at") == due:
                    save_job(job_id, stage="Resuming saved conversion", next_retry_at=None)
                    UPLOADS.put(job_id)

        timer = threading.Timer(delay, enqueue)
        timer.daemon = True
        timer.start()


def worker() -> None:
    while True:
        job_id = UPLOADS.get()
        try:
            with JOB_CONTROL_LOCK:
                if load_job(job_id).get("status") != "queued":
                    continue
                save_job(job_id, status="processing", stage="Resuming conversion", next_retry_at=None)
            process_job(job_id)
            schedule_retry(job_id)
        except Exception as error:
            # A missing job or failed error handler must not stop all later conversions.
            print(f"Queue worker recovered from {type(error).__name__} for {job_id}", flush=True)
            traceback.print_exc()
            try:
                with JOB_CONTROL_LOCK:
                    job = load_job(job_id)
                    if job.get("status") in {"queued", "processing", "running"}:
                        save_job(
                            job_id, status="failed", stage="Conversion failed",
                            error=f"{type(error).__name__}: {str(error).strip() or 'Unexpected worker error; see server logs.'}",
                            failed_at=now(), estimated_seconds_remaining=0, next_retry_at=None,
                        )
            except Exception:
                print(f"Could not persist failure for {job_id}; continuing with the next queued PDF", flush=True)
                traceback.print_exc()
        finally:
            UPLOADS.task_done()


def recover_jobs() -> None:
    JOBS.mkdir(parents=True, exist_ok=True)
    for path in JOBS.glob("pdf-*/job.json"):
        job = read_json(path)
        if not isinstance(job, dict):
            continue
        try:
            # Waiting jobs have already been accepted, even without automatic retries enabled.
            resumable = job.get("status") == "queued" or (
                job.get("retry_until_complete")
                and job.get("status") in {"processing", "running", "failed", "aborted"}
            )
            if resumable:
                if not (safe_job(job["job_id"]) / "input.pdf").is_file():
                    save_job(job["job_id"], status="failed", stage="Saved PDF is missing",
                             error="The saved PDF could not be found. Upload the document again.",
                             retry_until_complete=False, next_retry_at=None, estimated_seconds_remaining=0)
                    continue
                save_job(job["job_id"], status="queued", stage="Resuming after server restart",
                         next_retry_at=None, error=None, estimated_seconds_remaining=None)
                UPLOADS.put(job["job_id"])
            elif job.get("status") in {"processing", "running"}:
                job.update(
                    {
                        "status": "aborted",
                        "stage": "Interrupted by server restart",
                        "error": "The conversion was interrupted. Retry it to resume using the saved PDF.",
                        "updated_at": now(),
                    }
                )
                atomic_json(path, job)
        except Exception:
            print(f"Could not recover {path.parent.name}; continuing with other saved jobs", flush=True)
            traceback.print_exc()


def parse_pdf_upload(body: bytes, content_type: str) -> tuple[str, bytes]:
    match = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', content_type)
    if not match:
        raise ValueError("Expected multipart/form-data with a boundary.")
    boundary = (match.group(1) or match.group(2)).encode()
    for part in body.split(b"--" + boundary):
        if b"\r\n\r\n" not in part:
            continue
        raw_headers, data = part.split(b"\r\n\r\n", 1)
        headers = raw_headers.decode("utf-8", errors="replace")
        if 'name="pdf"' not in headers:
            continue
        filename_match = re.search(r'filename="([^"]*)"', headers)
        filename = Path(filename_match.group(1) if filename_match else "upload.pdf").name
        data = data.rstrip(b"\r\n-")
        if not data.startswith(b"%PDF"):
            raise ValueError("The uploaded file is not a PDF.")
        return filename or "upload.pdf", data
    raise ValueError('The multipart request must contain a "pdf" file field.')


class Handler(BaseHTTPRequestHandler):
    server_version = "Pdf2Podcast/3"

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format % args}", flush=True)

    def cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Authorization, Content-Type, Range, If-Range, "
            "X-Pdf2Podcast-Public-Consent, X-Pdf2Podcast-Terms-Version, X-Pdf2Podcast-Visibility",
        )
        self.send_header("Access-Control-Expose-Headers", "Accept-Ranges, Content-Length, Content-Range")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def json_response(self, status: int, value: Any, headers: dict[str, str] | None = None) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        response_headers = {"Cache-Control": "private, no-store", "Vary": "Cookie, Authorization", **(headers or {})}
        for name, value in response_headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def error_response(
        self, status: int, message: str, code: str, headers: dict[str, str] | None = None,
    ) -> None:
        self.json_response(status, {"detail": message, "code": code}, headers)

    def mobile_authorized(self) -> bool:
        expected = os.environ.get("PDF2PODCAST_MOBILE_TOKEN", "")
        supplied = self.headers.get("Authorization", "")
        return bool(expected) and hmac.compare_digest(supplied, f"Bearer {expected}")

    def identity(self) -> dict[str, Any] | None:
        if self.mobile_authorized():
            return {"kind": "mobile", "is_admin": True, "user": None}
        cookie_header = self.headers.get("Cookie", "")
        raw_token = session_token(cookie_header)
        user = web_user_for_token(raw_token)
        if self.path.partition("?")[0].rstrip("/") == "/api/web/me":
            cookie_present = any(
                field.strip().partition("=")[0] == WEB_SESSION_COOKIE
                for field in cookie_header.split(";")
            )
            print(
                "Web session lookup: "
                f"cookie_header_present={bool(cookie_header)} "
                f"session_cookie_present={cookie_present} "
                f"session_token_valid={bool(raw_token)} "
                f"session_found={user is not None}",
                flush=True,
            )
        if user is not None:
            return {"kind": "web", "is_admin": user["is_admin"], "user": user}
        return None

    def require_identity(self, require_terms: bool = True) -> dict[str, Any] | None:
        identity = self.identity()
        if identity is None:
            self.error_response(
                401, "Sign in with Google or provide a valid Pdf2Podcast mobile token.", "unauthorized",
                {"Cache-Control": "no-store"},
            )
        elif (require_terms and identity["kind"] == "web"
              and not identity["user"].get("terms_accepted")):
            self.error_response(
                403, "Accept the public-upload Terms of Use to finish creating your account.",
                "public_terms_required",
            )
            return None
        return identity

    def engagement_actor(self) -> tuple[str, str | None, dict[str, str]]:
        """Prefer an authenticated account; otherwise retain an opaque browser identity."""
        headers = {"Cache-Control": "no-store", "Vary": "Cookie, Authorization"}
        identity = self.identity()
        if identity is not None and identity["kind"] == "web":
            sub = identity["user"]["sub"]
            return engagement.account_key(sub), sub, headers
        token = browser_cookie_token(self.headers.get("Cookie", ""), VISITOR_COOKIE)
        if not token:
            token = secrets.token_urlsafe(32)
            headers["Set-Cookie"] = (
                f"{VISITOR_COOKIE}={token}; Path=/; Max-Age={VISITOR_COOKIE_SECONDS}; "
                "HttpOnly; Secure; SameSite=Lax"
            )
        return engagement.guest_key(token), None, headers

    def require_public_job(self, job_id: str, completed: bool = False) -> dict[str, Any] | None:
        try:
            if not JOB_ID.fullmatch(job_id) or not is_public_job(job_id):
                raise FileNotFoundError(job_id)
            job = load_job(job_id)
        except (FileNotFoundError, ValueError):
            self.error_response(404, "Conversion not found.", "job_not_found", {"Cache-Control": "no-store"})
            return None
        if completed and job.get("status") not in {"complete", "completed"}:
            self.error_response(409, "Listening is available after conversion completes.", "job_not_complete")
            return None
        return job

    def engagement_body(self) -> dict[str, Any] | None:
        """Bound reads and keep malformed engagement requests in the JSON error protocol."""
        def reject_constant(value: str) -> None:
            raise ValueError("JSON numbers must be finite.")

        try:
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or self.headers.get("Transfer-Encoding"):
                raise ValueError("A single Content-Length is required.")
            length = int(lengths[0])
            if length > ENGAGEMENT_BODY_LIMIT:
                self.error_response(413, "Engagement request is too large.", "engagement_too_large")
                return None
            if length <= 0:
                raise ValueError("A JSON object is required.")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("Incomplete JSON request.")
            body = json.loads(raw, parse_constant=reject_constant)
            if not isinstance(body, dict):
                raise ValueError("A JSON object is required.")
            return body
        except (ValueError, UnicodeError, RecursionError):
            self.error_response(400, "A valid, bounded JSON object is required.", "invalid_engagement")
            return None

    def prepare_public_share(self, job_id: str) -> None:
        if self.require_public_job(job_id) is None:
            return
        key, sub, headers = self.engagement_actor()
        with closing(web_db()) as database:
            stats = engagement.prepare_share(database, job_id, key, sub)
        self.json_response(200, stats, headers)

    def update_public_share(self, code: str, action: str) -> None:
        with closing(web_db()) as database:
            try:
                stats = engagement.get_share(database, code)
            except LookupError:
                self.error_response(404, "Share link not found.", "share_not_found", {"Cache-Control": "no-store"})
                return
            if self.require_public_job(stats["job_id"]) is None:
                return
            body = self.engagement_body()
            if body is None:
                return
            if body:
                self.error_response(400, "An empty JSON object is required.", "invalid_engagement")
                return
            key, _, headers = self.engagement_actor()
            try:
                if action == "share":
                    stats = engagement.record_share(database, code, key)
                else:
                    stats = engagement.record_click(database, code, key)
            except PermissionError as error:
                self.error_response(403, str(error), "forbidden", headers)
                return
        self.json_response(200, stats, headers)

    @staticmethod
    def can_access(job_id: str, identity: dict[str, Any]) -> bool:
        if identity["kind"] == "mobile":
            return is_public_job(job_id) or is_legacy_job(job_id)
        if identity["is_admin"]:
            return True
        return job_id in job_ids_for_user(identity["user"]["sub"])

    def allow_web_mutation(self, identity: dict[str, Any]) -> bool:
        if identity["kind"] != "web":
            return True
        origin = self.headers.get("Origin")
        if ((origin is not None and origin.rstrip("/") != PUBLIC_WEB_ORIGIN)
                or self.headers.get("Sec-Fetch-Site") == "cross-site"):
            self.error_response(403, "Use the Pdf2Podcast website to change your library.", "cross_origin_request")
            return False
        return True

    def web_index(self) -> None:
        path = WEB_ROOT / "index.html"
        try:
            payload = path.read_bytes()
        except OSError:
            self.error_response(503, "The Pdf2Podcast web client is not installed.", "web_client_missing")
            return
        _, _, visitor_headers = self.engagement_actor()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        for name, value in visitor_headers.items():
            self.send_header(name, value)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.end_headers()
        self.wfile.write(payload)

    def legal_page(
        self, title: str, effective_date: str, introduction: str,
        sections: list[dict[str, str]], contact: str,
    ) -> None:
        section_html = "".join(
            f"<section><h2>{html.escape(section['heading'])}</h2>"
            f"<p>{html.escape(section['body'])}</p></section>"
            for section in sections
        )
        contact_html = html.escape(contact)
        payload = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} · Pdf2Podcast</title><meta name="theme-color" content="#080b0f">
<style>
body{{margin:0;background:#080b0f;color:#eaf4f8;font:16px/1.7 system-ui,sans-serif}}main{{max-width:780px;margin:auto;padding:56px 24px 90px}}
a{{color:#6dd3ff}}.brand{{font-weight:800;letter-spacing:-.03em}}h1{{font-size:clamp(38px,8vw,68px);line-height:1;margin:60px 0 18px;letter-spacing:-.055em}}
.effective{{color:#8c9ca8}}section{{padding:24px 0;border-top:1px solid #24303a}}h2{{font-size:20px;margin:0 0 8px}}p{{margin:0;color:#b8c5ce}}
.notice{{margin:28px 0 42px;padding:18px 20px;border:1px solid #34505f;border-radius:14px;background:#101a21;color:#dcecf3}}
</style></head><body><main><a class="brand" href="/">Pdf2Podcast</a><h1>{html.escape(title)}</h1>
<p class="effective">Effective {html.escape(effective_date)}</p><p class="notice">{html.escape(introduction)}</p>
{section_html}<section><h2>Contact</h2><p><a href="mailto:{contact_html}">{contact_html}</a></p></section>
</main></body></html>""".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "public, max-age=300")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.end_headers()
        self.wfile.write(payload)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        path, _, _ = self.path.partition("?")
        listen_match = re.fullmatch(r"/api/public/jobs/([^/]+)/listen", path.rstrip("/"))
        if listen_match:
            job_id = listen_match.group(1)
            if self.require_public_job(job_id, completed=True) is None:
                return
            body = self.engagement_body()
            if body is None:
                return
            try:
                with closing(web_db()) as database:
                    total = engagement.add_listening(database, job_id, body.get("seconds"))
            except ValueError as error:
                self.error_response(400, str(error), "invalid_listening_seconds")
                return
            self.json_response(200, {"listening_seconds": total}, {"Cache-Control": "no-store"})
            return
        share_match = re.fullmatch(r"/api/public/shares/([^/]+)/(share|click)", path.rstrip("/"))
        if share_match:
            self.update_public_share(*share_match.groups())
            return
        if path.rstrip("/") == "/api/web/auth/google/redirect":
            import_query = ""
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 64 * 1024:
                    raise ValueError("The sign-in request is empty or too large.")
                fields = urllib.parse.parse_qs(
                    self.rfile.read(length).decode("utf-8", errors="strict"),
                    keep_blank_values=False, max_num_fields=4,
                )
                credentials = fields.get("credential", [])
                import_urls = fields.get("import_url", [])
                if import_urls:
                    if len(import_urls) != 1:
                        raise ValueError("Only one PDF URL can be imported at a time.")
                    import_query = "&" + urllib.parse.urlencode({"import": normalize_pdf_url(import_urls[0])})
                if len(credentials) != 1 or not credentials[0]:
                    raise ValueError("A Google sign-in credential is required.")
                accepted = fields.get("terms_accepted", [])
                versions = fields.get("terms_version", [])
                if accepted != ["true"] or versions != [PUBLIC_TERMS_VERSION]:
                    raise ValueError("Accept the current public-upload Terms of Use before signing in.")
                profile = verify_google_credential(credentials[0])
                raw_token = create_web_session(profile)
                accept_public_terms(profile["sub"], versions[0])
                cookie = (
                    f"{WEB_SESSION_COOKIE}={raw_token}; Path=/; Max-Age={WEB_SESSION_SECONDS}; "
                    "HttpOnly; Secure; SameSite=Lax"
                )
                self.send_response(303)
                self.send_header("Location", "/?signed_in=1" + import_query)
                self.send_header("Set-Cookie", cookie)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", "0")
                self.end_headers()
                print(
                    "Google redirect sign-in: credential_verified=True session_created=True cookie_set=True",
                    flush=True,
                )
            except (UnicodeError, ValueError, RuntimeError) as error:
                print(f"Google redirect sign-in failed: {type(error).__name__}: {error}", flush=True)
                self.send_response(303)
                self.send_header("Location", "/?auth_error=google" + import_query)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", "0")
                self.end_headers()
            return
        if path.rstrip("/") == "/api/web/auth/google":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 64 * 1024:
                    raise ValueError("The sign-in request is empty or too large.")
                body = json.loads(self.rfile.read(length))
                credential = body.get("credential") if isinstance(body, dict) else None
                if not isinstance(credential, str) or not credential:
                    raise ValueError("A Google sign-in credential is required.")
                profile = verify_google_credential(credential)
                raw_token = create_web_session(profile)
                cookie = (
                    f"{WEB_SESSION_COOKIE}={raw_token}; Path=/; Max-Age={WEB_SESSION_SECONDS}; "
                    "HttpOnly; Secure; SameSite=Lax"
                )
                self.json_response(
                    200, {"user": web_user_for_token(raw_token)},
                    {"Set-Cookie": cookie, "Cache-Control": "no-store"},
                )
            except (ValueError, json.JSONDecodeError) as error:
                self.error_response(401, str(error), "google_sign_in_rejected", {"Cache-Control": "no-store"})
            except RuntimeError as error:
                self.error_response(503, str(error), "google_sign_in_unavailable", {"Cache-Control": "no-store"})
            return
        if path.rstrip("/") == "/api/web/auth/logout":
            delete_web_session(session_token(self.headers.get("Cookie", "")))
            cookie = f"{WEB_SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax"
            self.json_response(200, {"ok": True}, {"Set-Cookie": cookie, "Cache-Control": "no-store"})
            return
        if path.rstrip("/") == "/api/web/terms/accept":
            identity = self.require_identity(require_terms=False)
            if identity is None:
                return
            if identity["kind"] != "web":
                self.error_response(400, "A signed-in web account is required.", "web_session_required")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4096:
                    raise ValueError("The terms acceptance request is empty or too large.")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict) or body.get("accepted") is not True:
                    raise ValueError("Explicit acceptance is required.")
                accept_public_terms(identity["user"]["sub"], str(body.get("version") or ""))
                self.json_response(200, {"ok": True, "version": PUBLIC_TERMS_VERSION})
            except (ValueError, json.JSONDecodeError) as error:
                self.error_response(400, str(error), "invalid_terms_acceptance")
            return
        visibility_match = re.fullmatch(r"/api/web/jobs/(pdf-[a-f0-9]{16})/visibility", path.rstrip("/"))
        if visibility_match:
            identity = self.require_identity()
            if identity is None or not self.allow_web_mutation(identity):
                return
            if identity["kind"] != "web":
                self.error_response(403, "Sign in with the paper owner's Google account.", "web_session_required")
                return
            if self.headers.get_content_type() != "application/json":
                self.error_response(415, "Send visibility as application/json.", "invalid_content_type")
                return
            body = self.engagement_body()
            if body is None:
                return
            job_id = visibility_match.group(1)
            try:
                with WEB_STATE_LOCK, web_db() as database:
                    database.execute("BEGIN IMMEDIATE")
                    paper_access.set_visibility(database, job_id, identity["user"], body.get("visibility"),
                                                PUBLIC_TERMS_VERSION, int(time.time()))
                self.json_response(200, {"job": public_job(load_job(job_id), identity["user"])})
            except FileNotFoundError:
                self.error_response(404, "Conversion not found.", "job_not_found")
            except PermissionError as error:
                self.error_response(403, str(error), "pro_required")
            except (ValueError, TypeError):
                self.error_response(400, "Visibility must be public or private.", "invalid_visibility")
            return
        retry_match = re.fullmatch(r"/api/audio/jobs/(pdf-[a-f0-9]{16})/retry", self.path.rstrip("/"))
        if retry_match:
            identity = self.require_identity()
            if identity is None:
                return
            if not self.can_access(retry_match.group(1), identity):
                self.error_response(404, "Conversion not found.", "job_not_found")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 4096:
                    raise ValueError("Retry request is too large.")
                options = json.loads(self.rfile.read(length)) if length else {}
                if not isinstance(options, dict) or type(options.get("retry_until_complete", False)) is not bool:
                    raise ValueError("retry_until_complete must be true or false.")
                entry_id = retry_match.group(1)
                entry = load_job(entry_id)
                retry_job(entry.get("source_job_id", entry_id), options.get("retry_until_complete", False))
                self.json_response(202, public_job(load_job(entry_id), identity.get("user")))
            except FileNotFoundError:
                self.error_response(404, "Conversion not found.", "job_not_found")
            except (OSError, ValueError) as error:
                self.error_response(400, str(error), "invalid_retry")
            return
        importing = path.rstrip("/") == "/api/audio/import"
        if path.rstrip("/") != "/api/audio/upload" and not importing:
            self.error_response(404, "Route not found.", "not_found")
            return
        identity = self.require_identity()
        if identity is None:
            return
        if not self.allow_web_mutation(identity):
            return
        if importing and identity["kind"] != "web":
            self.error_response(400, "Sign in with Google to import a PDF link.", "web_session_required")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > (8192 if importing else MAX_UPLOAD_BYTES):
            self.error_response(413, "PDF is empty or exceeds the upload limit.", "upload_too_large")
            return
        user_sub = identity["user"]["sub"] if identity["kind"] == "web" else None
        public_release = False
        terms_version = PUBLIC_TERMS_VERSION if user_sub is not None else None
        event_id = None
        if user_sub is not None:
            event_id, retry_after = begin_upload_event(user_sub, 0 if importing else length)
            if event_id is None:
                self.error_response(
                    429, "Upload limit reached. Try again after the rolling one-minute window.",
                    "upload_rate_limited", {"Retry-After": str(retry_after)},
                )
                return
        accepted_job_id = None
        try:
            selections = self.headers.get_all("X-Pdf2Podcast-Visibility", [])
            if len(selections) > 1:
                raise ValueError("Provide one upload visibility value.")
            requested_visibility = selections[0] if selections else None
            public_release = paper_access.upload_is_public(identity, requested_visibility)
            if importing:
                if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
                    raise URLImportError("A single Content-Length is required.")
                if self.headers.get_content_type() != "application/json":
                    raise URLImportError("Send the PDF URL as application/json.")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict) or not isinstance(body.get("url"), str):
                    raise URLImportError("Provide a direct PDF URL.")
                if "visibility" in body:
                    if requested_visibility is not None and body["visibility"] != requested_visibility:
                        raise ValueError("Upload visibility selections disagree.")
                    public_release = paper_access.upload_is_public(identity, body["visibility"])
                filename, data = download_pdf(body["url"], MAX_UPLOAD_BYTES)
                update_upload_event(event_id, source_bytes=len(data))
            else:
                filename, data = parse_pdf_upload(
                    self.rfile.read(length), self.headers.get("Content-Type", "")
                )
            digest = hashlib.sha256(data).hexdigest()
            reservation = reserve_document(
                digest, len(data), filename, user_sub, event_id,
                public_release=public_release, terms_version=terms_version,
            )
            if reservation["status"] == "rate_limited":
                retry_after = int(reservation["retry_after"])
                self.error_response(
                    429,
                    "You may queue one new PDF per minute. An already-converted identical PDF remains instant.",
                    "queue_rate_limited", {"Retry-After": str(retry_after)},
                )
                return
            job_id = reservation["job_id"]
            if reservation["status"] == "existing":
                response = public_job(load_job(job_id), identity.get("user"))
                response["deduplicated"] = True
                accepted_job_id = job_id
                self.json_response(200, response)
                return
            root = safe_job(job_id)
            root.mkdir(mode=0o700, parents=True)
            (root / "input.pdf").write_bytes(data)
            job = {
                "job_id": job_id,
                "filename": filename,
                "pipeline_version": 5,
                "paragraph_pipeline": VISION_PIPELINE,
                "layout_model": os.environ.get("PDF2PODCAST_LAYOUT_MODEL", "gpt-4.1"),
                "reading_mode": "paper_companion",
                "title": select_document_title(filename=filename),
                "status": "queued",
                "stage": "Waiting to begin",
                "progress_percent": 0,
                "estimated_seconds_remaining": None,
                "completed_chunks": 0,
                "total_chunks": None,
                "chapter_count": None,
                "section_count": None,
                "page_count": None,
                "audio_bytes": None,
                "error": None,
                "created_at": now(),
                "updated_at": now(),
                "source_sha256": digest,
            }
            atomic_json(root / "job.json", job)
            UPLOADS.put(job_id)
            accepted_job_id = job_id
            self.json_response(202, public_job(job, identity.get("user")))
        except URLImportError as error:
            update_upload_event(event_id, outcome="url_import_failed")
            self.error_response(error.status, str(error), error.code, {"Cache-Control": "no-store"})
        except PermissionError as error:
            update_upload_event(event_id, outcome="visibility_denied")
            self.error_response(403, str(error), "pro_required")
        except (OSError, ValueError) as error:
            if accepted_job_id is not None:
                # HTTP delivery can fail after the worker has started. Never undo accepted work.
                print(f"Could not deliver upload response for {accepted_job_id}: {type(error).__name__}; saved conversion preserved", flush=True)
                self.close_connection = True
                return
            if event_id is not None:
                update_upload_event(event_id, outcome="invalid_upload")
            if "reservation" in locals() and reservation.get("status") == "new":
                release_document(digest, reservation["job_id"], user_sub, event_id)
                shutil.rmtree(safe_job(reservation["job_id"]), ignore_errors=True)
            self.error_response(400, str(error), "invalid_upload")

    def do_GET(self) -> None:
        path, _, query = self.path.partition("?")
        embedded_url = self.path[1:]
        if re.match(r"^https?%3[aA]", embedded_url, re.IGNORECASE):
            embedded_url = urllib.parse.unquote(embedded_url)
        if re.match(r"^https?:/{1,2}", embedded_url, re.IGNORECASE):
            try:
                embedded_url = re.sub(r"^(https?):/{1,2}", r"\1://", embedded_url, count=1, flags=re.IGNORECASE)
                source = normalize_pdf_url(embedded_url)
            except URLImportError as error:
                self.error_response(error.status, str(error), error.code)
                return
            self.send_response(303)
            self.send_header("Location", PUBLIC_WEB_ORIGIN + "/?" + urllib.parse.urlencode({"import": source}))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path in {"/", "/index.html"}:
            self.web_index()
            return
        if path.rstrip("/") == "/terms":
            self.legal_page(
                "Terms of Use", PUBLIC_TERMS_EFFECTIVE_DATE,
                "These terms govern Pdf2Podcast accounts, uploads, public distribution, and generated media.",
                PUBLIC_TERMS, PRIVACY_CONTACT,
            )
            return
        if path.rstrip("/") == "/privacy":
            self.legal_page(
                "Privacy Policy", PUBLIC_TERMS_EFFECTIVE_DATE,
                "This notice explains what Pdf2Podcast collects, what becomes public, and how the service uses information.",
                PRIVACY_SECTIONS, PRIVACY_CONTACT,
            )
            return
        if self.path == "/health":
            self.json_response(200, {"ok": True, "service": "pdf2podcast", "version": 3})
            return
        if path.rstrip("/") == "/api/web/config":
            self.json_response(200, {
                "google_client_id": GOOGLE_CLIENT_ID,
                "max_upload_bytes": MAX_UPLOAD_BYTES,
                "public_terms_version": PUBLIC_TERMS_VERSION,
                "limits": {
                    "uploads_per_minute": WEB_UPLOADS_PER_MINUTE,
                    "queue_per_minute": WEB_QUEUE_PER_MINUTE,
                },
            })
            return
        if path.rstrip("/") == "/api/public/terms":
            self.json_response(200, {
                "version": PUBLIC_TERMS_VERSION,
                "effective_date": PUBLIC_TERMS_EFFECTIVE_DATE,
                "title": "Pdf2Podcast Terms of Use",
                "sections": PUBLIC_TERMS,
            })
            return
        if path.rstrip("/") == "/api/public/jobs":
            limit_match = re.search(r"(?:^|&)limit=(\d+)", query)
            limit = int(limit_match.group(1)) if limit_match else 100
            sort = urllib.parse.parse_qs(query).get("sort", ["listening"])[0]
            self.json_response(200, {"jobs": public_catalog(limit, sort)})
            return
        share_match = re.fullmatch(r"/api/public/jobs/([^/]+)/share", path.rstrip("/"))
        if share_match:
            self.prepare_public_share(share_match.group(1))
            return
        if path.rstrip("/") in {"/api/web/me", "/api/web/sharing"}:
            identity = self.require_identity(require_terms=False)
            if identity is None:
                return
            if identity["kind"] != "web":
                self.error_response(400, "This endpoint describes signed-in web users.", "web_session_required")
                return
            user = identity["user"]
            sharing = web_sharing(user)
            if path.rstrip("/") == "/api/web/sharing":
                self.json_response(200, sharing, {"Cache-Control": "no-store"})
                return
            payload = {
                "user": user,
                "usage": web_usage(user["sub"]),
                "sharing": sharing,
                "terms_required": not user.get("terms_accepted", False),
                "public_terms": {
                    "version": PUBLIC_TERMS_VERSION,
                    "effective_date": PUBLIC_TERMS_EFFECTIVE_DATE,
                },
            }
            if user["is_admin"]:
                payload["admin_usage"] = admin_usage()
            self.json_response(200, payload, {"Cache-Control": "no-store"})
            return
        if path.rstrip("/") in {"/api/web/admin/usage", "/api/web/admin/model-usage"}:
            identity = self.require_identity()
            if identity is None:
                return
            if identity["kind"] != "web" or not identity["is_admin"]:
                self.error_response(403, "Administrator access is required.", "forbidden")
                return
            if path.rstrip("/") == "/api/web/admin/model-usage":
                with closing(web_db()) as database:
                    report = model_usage.report(database, by_job=True)
                self.json_response(200, {"requests_by_paper": report}, {"Cache-Control": "no-store"})
            else:
                self.json_response(200, admin_usage(), {"Cache-Control": "no-store"})
            return
        job_route_match = re.fullmatch(
            r"/api/audio/jobs/(pdf-[a-f0-9]{16})(?:/(audio|transcript|manifest|sync|pdf|read-along))?",
            path.rstrip("/"),
        )
        public_access = bool(job_route_match and is_public_job(job_route_match.group(1)))
        signed_playback = bool(job_route_match and authorized_playback(path, query)
                               and (public_access or is_legacy_job(job_route_match.group(1))))
        identity = self.identity() if signed_playback or public_access else self.require_identity()
        if not signed_playback and not public_access and identity is None:
            return
        if path.rstrip("/") == "/api/audio/jobs":
            limit_match = re.search(r"(?:^|&)limit=(\d+)", query)
            limit = int(limit_match.group(1)) if limit_match else 100
            jobs = list_jobs(200)
            if identity is not None and identity["kind"] == "web":
                allowed = job_ids_for_user(identity["user"]["sub"])
                jobs = [public_job(job, identity["user"]) for job in jobs if job.get("job_id") in allowed]
            elif identity is not None:
                jobs = [job for job in jobs if self.can_access(job["job_id"], identity)]
            self.json_response(200, {"jobs": jobs[:max(1, min(limit, 200))]})
            return
        match = job_route_match
        if not match:
            self.error_response(404, "Route not found.", "not_found")
            return
        job_id, artifact = match.groups()
        if not signed_playback and not public_access and identity is not None and not self.can_access(job_id, identity):
            self.error_response(404, "Conversion not found.", "job_not_found")
            return
        try:
            job = load_job(job_id)
        except (FileNotFoundError, ValueError):
            self.error_response(404, "Conversion not found.", "job_not_found")
            return
        if artifact is None:
            self.json_response(200, public_job(job, identity.get("user") if identity else None))
            return
        if artifact == "read-along":
            if job.get("status") not in {"complete", "completed"}:
                self.error_response(409, "Paragraph locations are available after conversion completes.", "artifact_not_ready")
                return
            try:
                self.json_response(200, read_along_map(safe_job(job.get("source_job_id", job_id))))
            except ReadAlongUnavailable as error:
                self.error_response(409, str(error), "read_along_unavailable")
            except Exception:
                self.error_response(500, "The paragraph location map could not be prepared.", "read_along_failed")
            return
        choices = {
            "pdf": ("input.pdf", "application/pdf"),
            "audio": ("audio.mp3", "audio/mpeg"),
            "transcript": ("transcript.txt", "text/plain; charset=utf-8"),
            "manifest": ("manifest.json", "application/json; charset=utf-8"),
            "sync": ("transcript.json", "application/json; charset=utf-8"),
        }
        filename, content_type = choices[artifact]
        file_path = safe_job(job.get("source_job_id", job_id)) / filename
        if not file_path.is_file():
            self.error_response(409, "This artifact is not ready yet.", "artifact_not_ready")
            return
        if artifact == "audio":
            send_audio(self, file_path)
            return
        self.send_response(200)
        self.cors()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(file_path.stat().st_size))
        self.send_header("Content-Disposition", f'inline; filename="{filename}"')
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("Vary", "Cookie, Authorization")
        self.end_headers()
        with file_path.open("rb") as source:
            shutil.copyfileobj(source, self.wfile)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    if not os.environ.get("PDF2PODCAST_MOBILE_TOKEN"):
        raise SystemExit("PDF2PODCAST_MOBILE_TOKEN is required")
    init_web_state()
    recover_jobs()
    threading.Thread(target=worker, name="pdf2podcast-worker", daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Pdf2Podcast v3 listening on http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
