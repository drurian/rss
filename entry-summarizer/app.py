#!/usr/bin/env python3

import hashlib
import html
import json
import logging
import os
import random
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from youtube_transcript_api import YouTubeTranscriptApi

try:
    from youtube_transcript_api.proxies import GenericProxyConfig
except ImportError:  # pragma: no cover - depends on upstream package version
    GenericProxyConfig = None


LOG_LEVEL = os.environ.get("ENTRY_SUMMARIZER_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("entry_summarizer")

PLACEHOLDER_VALUE_MARKERS = (
    "replace-me",
    "changeme",
    "your-",
    "example",
    "placeholder",
)
NO_SUMMARY_HTML = "<blockquote><p>No summary available.</p></blockquote>"
ARTICLE_RETRY_BASE_HOURS = 6
YOUTUBE_NO_TRANSCRIPT_DAYS = int(os.environ.get("YT_TRANSCRIPT_NEGATIVE_CACHE_DAYS", "30"))
YOUTUBE_BLOCK_COOLDOWN_HOURS = int(os.environ.get("YT_TRANSCRIPT_GLOBAL_BLOCK_COOLDOWN_HOURS", "6"))
ARTICLE_MAX_CHARS = int(os.environ.get("ENTRY_SUMMARIZER_MAX_ARTICLE_CHARS", "20000"))
TRANSCRIPT_MAX_CHARS = int(os.environ.get("ENTRY_SUMMARIZER_MAX_TRANSCRIPT_CHARS", "48000"))
SUMMARY_MARKER_RE = re.compile(
    r"<!-- entry-summarizer:start source:(?P<source>[^\s>]+) hash:(?P<hash>[^\s>]+)(?: video:(?P<video_id>[^\s>]+))? -->"
)


def get_env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default if default is not None else "")
    return value.strip() if isinstance(value, str) else ""


def is_missing_or_placeholder(value: str) -> bool:
    normalized = value.strip()
    if not normalized:
        return True
    lowered = normalized.lower()
    return any(marker in lowered for marker in PLACEHOLDER_VALUE_MARKERS)


def now_utc() -> datetime:
    return datetime.now(UTC)


def dt_to_str(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat()


def str_to_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def html_to_text(value: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def strip_our_summary(value: str) -> str:
    return re.sub(
        r"(?s)<!-- entry-summarizer:start.*?<!-- entry-summarizer:end -->\s*(?:<hr\s*/?>\s*)?",
        "",
        value,
    ).strip()


def extract_summary_marker(value: str) -> dict[str, str] | None:
    match = SUMMARY_MARKER_RE.search(value)
    if not match:
        return None
    return {key: found for key, found in match.groupdict(default="").items() if found}


def clamp_text(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return value[:max_chars]


def is_youtube_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return "youtu.be" in host or "youtube.com" in host


def extract_video_id(url: str) -> str | None:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.strip("/")

    if "youtu.be" in host and path:
        return path.split("/")[0]

    if "youtube.com" not in host:
        return None

    if path == "watch":
        return parse_qs(parsed.query).get("v", [None])[0]

    if path.startswith("shorts/"):
        return path.split("/", 1)[1].split("/")[0]

    if path.startswith("embed/"):
        return path.split("/", 1)[1].split("/")[0]

    return None


def jitter_sleep(min_seconds: int, max_seconds: int) -> None:
    delay = random.uniform(min_seconds, max_seconds)
    time.sleep(delay)


def build_summary_prompt(source_type: str, entry: dict[str, Any], content: str) -> str:
    title = entry.get("title") or "Untitled"
    url = entry.get("url") or ""

    if source_type == "youtube":
        return (
            f"Title: {title}\n"
            f"URL: {url}\n"
            "Below is a YouTube transcript.\n"
            "If it is empty, too sparse, or not actually a transcript, return exactly:\n"
            f"{NO_SUMMARY_HTML}\n\n"
            "Otherwise summarize the transcript in 3 concise sentences.\n"
            "Return HTML only using exactly this structure:\n"
            "<blockquote><p>Sentence 1. Sentence 2. Sentence 3.</p></blockquote>\n"
            "Do not use Markdown fences.\n"
            "Do not apologize.\n"
            "---\n"
            f"{content}"
        )

    return (
        f"Title: {title}\n"
        f"URL: {url}\n"
        "If the content below is empty, missing, or clearly not the article body, return exactly:\n"
        f"{NO_SUMMARY_HTML}\n\n"
        "Otherwise summarize the content in 3 concise sentences.\n"
        "Return HTML only using exactly this structure:\n"
        "<blockquote><p>Sentence 1. Sentence 2. Sentence 3.</p></blockquote>\n"
        "Do not use Markdown fences.\n"
        "Do not apologize.\n"
        "---\n"
        f"{content}"
    )


def build_summary_block(
    source_type: str,
    source_hash: str,
    summary_html: str,
    original_content: str,
    video_id: str | None = None,
) -> str:
    video_fragment = f" video:{video_id}" if video_id else ""
    injected = (
        f"<!-- entry-summarizer:start source:{source_type} hash:{source_hash}{video_fragment} -->\n"
        f"{summary_html}\n"
        "<!-- entry-summarizer:end -->"
    )
    base = original_content.strip()
    return f"{injected}\n<hr />\n{base}" if base else injected


def is_useful_article_content(content: str) -> bool:
    return len(content) >= 200


@dataclass
class StateRecord:
    entry_id: int
    url: str
    source_type: str
    video_id: str | None
    status: str
    source_hash: str | None
    summary_hash: str | None
    transcript_hash: str | None
    last_attempt_at: datetime | None
    next_retry_at: datetime | None
    retry_count: int
    last_error: str | None


class StateStore:
    def __init__(self, db_path: str) -> None:
        db_dir = os.path.dirname(db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS processing_state (
              entry_id INTEGER PRIMARY KEY,
              url TEXT NOT NULL,
              source_type TEXT NOT NULL,
              video_id TEXT,
              status TEXT NOT NULL,
              source_hash TEXT,
              summary_hash TEXT,
              transcript_hash TEXT,
              last_attempt_at TEXT,
              next_retry_at TEXT,
              retry_count INTEGER NOT NULL DEFAULT 0,
              last_error TEXT
            );

            CREATE TABLE IF NOT EXISTS runtime_state (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    def get_state(self, entry_id: int) -> StateRecord | None:
        row = self.conn.execute(
            "SELECT * FROM processing_state WHERE entry_id = ?",
            (entry_id,),
        ).fetchone()
        if row is None:
            return None
        return StateRecord(
            entry_id=row["entry_id"],
            url=row["url"],
            source_type=row["source_type"],
            video_id=row["video_id"],
            status=row["status"],
            source_hash=row["source_hash"],
            summary_hash=row["summary_hash"],
            transcript_hash=row["transcript_hash"],
            last_attempt_at=str_to_dt(row["last_attempt_at"]),
            next_retry_at=str_to_dt(row["next_retry_at"]),
            retry_count=row["retry_count"],
            last_error=row["last_error"],
        )

    def upsert_state(
        self,
        *,
        entry_id: int,
        url: str,
        source_type: str,
        video_id: str | None,
        status: str,
        source_hash: str | None,
        summary_hash: str | None,
        transcript_hash: str | None,
        next_retry_at: datetime | None,
        retry_count: int,
        last_error: str | None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO processing_state (
              entry_id, url, source_type, video_id, status, source_hash, summary_hash,
              transcript_hash, last_attempt_at, next_retry_at, retry_count, last_error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(entry_id) DO UPDATE SET
              url = excluded.url,
              source_type = excluded.source_type,
              video_id = excluded.video_id,
              status = excluded.status,
              source_hash = excluded.source_hash,
              summary_hash = excluded.summary_hash,
              transcript_hash = excluded.transcript_hash,
              last_attempt_at = excluded.last_attempt_at,
              next_retry_at = excluded.next_retry_at,
              retry_count = excluded.retry_count,
              last_error = excluded.last_error
            """,
            (
                entry_id,
                url,
                source_type,
                video_id,
                status,
                source_hash,
                summary_hash,
                transcript_hash,
                dt_to_str(now_utc()),
                dt_to_str(next_retry_at),
                retry_count,
                last_error,
            ),
        )
        self.conn.commit()

    def get_runtime(self, key: str) -> datetime | None:
        row = self.conn.execute(
            "SELECT value FROM runtime_state WHERE key = ?",
            (key,),
        ).fetchone()
        return str_to_dt(row["value"]) if row else None

    def list_states(self, source_type: str | None = None) -> list[StateRecord]:
        if source_type:
            rows = self.conn.execute(
                "SELECT * FROM processing_state WHERE source_type = ? ORDER BY entry_id ASC",
                (source_type,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM processing_state ORDER BY entry_id ASC"
            ).fetchall()

        return [
            StateRecord(
                entry_id=row["entry_id"],
                url=row["url"],
                source_type=row["source_type"],
                video_id=row["video_id"],
                status=row["status"],
                source_hash=row["source_hash"],
                summary_hash=row["summary_hash"],
                transcript_hash=row["transcript_hash"],
                last_attempt_at=str_to_dt(row["last_attempt_at"]),
                next_retry_at=str_to_dt(row["next_retry_at"]),
                retry_count=row["retry_count"],
                last_error=row["last_error"],
            )
            for row in rows
        ]

    def delete_state(self, entry_id: int) -> None:
        self.conn.execute("DELETE FROM processing_state WHERE entry_id = ?", (entry_id,))
        self.conn.commit()

    def set_runtime(self, key: str, value: datetime | None) -> None:
        if value is None:
            self.conn.execute("DELETE FROM runtime_state WHERE key = ?", (key,))
        else:
            self.conn.execute(
                """
                INSERT INTO runtime_state(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, dt_to_str(value)),
            )
        self.conn.commit()


class MinifluxClient:
    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"X-Auth-Token": api_key, "Content-Type": "application/json"})

    def ping(self) -> None:
        response = self.session.get(f"{self.base_url}/v1/me", timeout=30)
        response.raise_for_status()

    def unread_entries(self, limit: int = 10000) -> list[dict[str, Any]]:
        response = self.session.get(
            f"{self.base_url}/v1/entries",
            params={"status": "unread", "limit": limit},
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        return payload.get("entries", [])

    def entry(self, entry_id: int) -> dict[str, Any]:
        response = self.session.get(
            f"{self.base_url}/v1/entries/{entry_id}",
            timeout=60,
        )
        response.raise_for_status()
        return response.json()

    def update_entry(self, entry_id: int, content: str) -> None:
        response = self.session.put(
            f"{self.base_url}/v1/entries/{entry_id}",
            data=json.dumps({"content": content}),
            timeout=60,
        )
        response.raise_for_status()


class LLMClient:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: int) -> None:
        endpoint = base_url.rstrip("/")
        if not endpoint.endswith("/chat/completions"):
            endpoint = f"{endpoint}/chat/completions"
        self.endpoint = endpoint
        self.model = model
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})

    def summarize(self, prompt: str) -> str:
        response = self.session.post(
            self.endpoint,
            json={
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        return (
            payload["choices"][0]["message"]["content"].strip()
            if payload.get("choices")
            else NO_SUMMARY_HTML
        )


class YouTubeTranscriptFetcher:
    def __init__(self, languages: list[str], proxy_url: str | None) -> None:
        if proxy_url:
            if GenericProxyConfig is None:
                raise SystemExit("YT_TRANSCRIPT_PROXY_URL requires youtube-transcript-api proxy support")
            proxy_config = GenericProxyConfig(http_url=proxy_url, https_url=proxy_url)
            self.client = YouTubeTranscriptApi(proxy_config=proxy_config)
        else:
            self.client = YouTubeTranscriptApi()
        self.languages = languages

    def fetch(self, video_id: str) -> tuple[str, str]:
        transcript = self.client.fetch(video_id, languages=self.languages)
        text = " ".join(snippet.text.strip() for snippet in transcript if getattr(snippet, "text", "").strip())
        language_code = getattr(transcript, "language_code", "") or ""
        return text.strip(), language_code


class EntrySummarizer:
    def __init__(self) -> None:
        miniflux_base_url = get_env("MINIFLUX_BASE_URL")
        miniflux_api_key = get_env("MINIFLUX_API_KEY")
        llm_base_url = get_env("ENTRY_SUMMARIZER_LLM_BASE_URL", get_env("MINIFLUX_AI_PROVIDER_BASE_URL"))
        llm_api_key = get_env("ENTRY_SUMMARIZER_LLM_API_KEY", get_env("MINIFLUX_AI_PROVIDER_API_KEY"))
        llm_model = get_env("ENTRY_SUMMARIZER_LLM_MODEL", get_env("MINIFLUX_AI_MODEL"))

        missing = [
            name
            for name, value in (
                ("MINIFLUX_BASE_URL", miniflux_base_url),
                ("MINIFLUX_API_KEY", miniflux_api_key),
                ("ENTRY_SUMMARIZER_LLM_BASE_URL or MINIFLUX_AI_PROVIDER_BASE_URL", llm_base_url),
                ("ENTRY_SUMMARIZER_LLM_API_KEY or MINIFLUX_AI_PROVIDER_API_KEY", llm_api_key),
                ("ENTRY_SUMMARIZER_LLM_MODEL or MINIFLUX_AI_MODEL", llm_model),
            )
            if is_missing_or_placeholder(value)
        ]
        if missing:
            raise SystemExit(f"Missing required environment variables: {', '.join(missing)}")

        self.poll_interval = int(get_env("ENTRY_SUMMARIZER_POLL_INTERVAL", "300"))
        self.youtube_interval = int(get_env("YT_TRANSCRIPT_FETCH_INTERVAL_SECONDS", "600"))
        self.llm_interval = max(1, int(60 / max(int(get_env("ENTRY_SUMMARIZER_MAX_ARTICLE_RPM", "1")), 1)))
        self.command = get_env("ENTRY_SUMMARIZER_COMMAND", "run").lower().replace("_", "-")
        self.state = StateStore(get_env("ENTRY_SUMMARIZER_STATE_PATH", "/data/entry_summarizer.db"))
        self.miniflux = MinifluxClient(miniflux_base_url.rstrip("/"), miniflux_api_key)
        self.llm = LLMClient(llm_base_url, llm_api_key, llm_model, int(get_env("ENTRY_SUMMARIZER_LLM_TIMEOUT", "120")))
        languages = [item.strip() for item in get_env("YT_TRANSCRIPT_LANGUAGES", "en,en-US").split(",") if item.strip()]
        self.youtube = YouTubeTranscriptFetcher(languages, get_env("YT_TRANSCRIPT_PROXY_URL") or None)

    def run(self) -> None:
        self.miniflux.ping()
        logger.info("Connected to Miniflux")

        if self.command == "purge-articles":
            self.purge_article_summaries()
            return
        if self.command != "run":
            raise SystemExit(f"Unsupported ENTRY_SUMMARIZER_COMMAND: {self.command}")

        while True:
            started_at = time.monotonic()
            try:
                self.process_cycle()
            except Exception as exc:  # pragma: no cover - top-level safety
                logger.exception("processing cycle failed: %s", exc)

            elapsed = time.monotonic() - started_at
            sleep_for = max(1, self.poll_interval - int(elapsed))
            logger.info("cycle complete sleep_seconds=%s", sleep_for)
            time.sleep(sleep_for)

    def purge_article_summaries(self) -> None:
        article_states = self.state.list_states("article")
        removed = 0
        unchanged = 0
        missing = 0

        logger.info("purging article summaries tracked_entries=%s", len(article_states))
        for state in article_states:
            try:
                entry = self.miniflux.entry(state.entry_id)
            except requests.HTTPError as exc:
                response = exc.response
                if response is not None and response.status_code == 404:
                    self.state.delete_state(state.entry_id)
                    missing += 1
                    logger.info("purge skipped missing entry_id=%s", state.entry_id)
                    continue
                raise

            current_content = str(entry.get("content") or "")
            stripped = strip_our_summary(current_content)
            if stripped != current_content.strip():
                self.miniflux.update_entry(state.entry_id, stripped)
                removed += 1
            else:
                unchanged += 1

            source_hash = sha256_text(clamp_text(html_to_text(stripped), ARTICLE_MAX_CHARS)) if stripped else None
            self.state.upsert_state(
                entry_id=state.entry_id,
                url=state.url,
                source_type="article",
                video_id=None,
                status="purged",
                source_hash=source_hash,
                summary_hash=None,
                transcript_hash=None,
                next_retry_at=None,
                retry_count=0,
                last_error="article summary purged",
            )

        logger.info(
            "purge complete removed=%s unchanged=%s missing=%s",
            removed,
            unchanged,
            missing,
        )

    def process_cycle(self) -> None:
        entries = self.miniflux.unread_entries()
        logger.info("unread entries=%s", len(entries))

        youtube_entries: list[tuple[dict[str, Any], str | None]] = []
        article_entries: list[tuple[dict[str, Any], str | None]] = []

        for entry in entries:
            source_type, video_id = self.classify_entry(entry)
            if source_type == "youtube":
                youtube_entries.append((entry, video_id))
            else:
                article_entries.append((entry, video_id))

        for entry, video_id in youtube_entries:
            if self.process_entry(entry, "youtube", video_id):
                return

        for entry, video_id in article_entries:
            if self.process_entry(entry, "article", video_id):
                return

    def classify_entry(self, entry: dict[str, Any]) -> tuple[str, str | None]:
        url = str(entry.get("url") or "")
        if is_youtube_url(url):
            return "youtube", extract_video_id(url)
        return "article", None

    def process_entry(self, entry: dict[str, Any], source_type: str, video_id: str | None) -> bool:
        entry_id = int(entry["id"])
        url = str(entry.get("url") or "")
        current_state = self.state.get_state(entry_id)
        now = now_utc()

        if current_state and current_state.next_retry_at and current_state.next_retry_at > now:
            return False

        last_llm = self.state.get_runtime("last_llm_request_at")
        if last_llm and now < last_llm + timedelta(seconds=self.llm_interval):
            return False

        raw_content = str(entry.get("content") or "")
        existing_marker = extract_summary_marker(raw_content)
        current_content = strip_our_summary(raw_content)
        current_text = clamp_text(html_to_text(current_content), ARTICLE_MAX_CHARS)

        if source_type == "youtube":
            if not video_id:
                self.record_retry(entry_id, url, source_type, video_id, current_state, "missing video id")
                return False
            blocked_until = self.state.get_runtime("youtube_blocked_until")
            if blocked_until and blocked_until > now:
                return False
            last_fetch = self.state.get_runtime("youtube_last_fetch_at")
            if last_fetch and now < last_fetch + timedelta(seconds=self.youtube_interval):
                return False
            return self.process_youtube_entry(entry, current_state, video_id)

        if not is_useful_article_content(current_text):
            self.record_retry(
                entry_id,
                url,
                source_type,
                None,
                current_state,
                "article content missing or too short",
                hours=ARTICLE_RETRY_BASE_HOURS,
            )
            return False
        source_hash = sha256_text(current_text)
        if current_state and current_state.status == "purged" and current_state.source_hash == source_hash:
            return False
        if existing_marker and existing_marker.get("source") == "article" and existing_marker.get("hash") == source_hash:
            if not current_state or current_state.status != "done" or current_state.source_hash != source_hash:
                self.state.upsert_state(
                    entry_id=entry_id,
                    url=url,
                    source_type="article",
                    video_id=None,
                    status="done",
                    source_hash=source_hash,
                    summary_hash=current_state.summary_hash if current_state else None,
                    transcript_hash=None,
                    next_retry_at=None,
                    retry_count=0,
                    last_error="detected existing article summary marker",
                )
            return False
        return self.summarize_article_entry(entry, current_state, current_text)

    def process_youtube_entry(
        self,
        entry: dict[str, Any],
        state: StateRecord | None,
        video_id: str,
    ) -> bool:
        entry_id = int(entry["id"])
        url = str(entry.get("url") or "")
        retry_count = state.retry_count if state else 0

        jitter_sleep(2, 5)
        self.state.set_runtime("youtube_last_fetch_at", now_utc())
        try:
            transcript_text, language_code = self.youtube.fetch(video_id)
        except Exception as exc:
            error_type = exc.__class__.__name__
            error_message = f"{error_type}: {exc}"
            logger.warning("youtube transcript failed entry_id=%s error=%s", entry_id, error_message)

            if error_type in {"RequestBlocked", "IpBlocked"}:
                cooldown = timedelta(hours=min(72, YOUTUBE_BLOCK_COOLDOWN_HOURS * max(retry_count + 1, 1)))
                blocked_until = now_utc() + cooldown
                self.state.set_runtime("youtube_blocked_until", blocked_until)
                self.record_retry(
                    entry_id,
                    url,
                    "youtube",
                    video_id,
                    state,
                    error_message,
                    next_retry_at=blocked_until,
                )
                return False

            if error_type in {
                "NoTranscriptFound",
                "TranscriptsDisabled",
                "VideoUnavailable",
                "AgeRestricted",
                "VideoUnplayable",
            }:
                self.state.upsert_state(
                    entry_id=entry_id,
                    url=url,
                    source_type="youtube",
                    video_id=video_id,
                    status="no_transcript",
                    source_hash=None,
                    summary_hash=None,
                    transcript_hash=None,
                    next_retry_at=now_utc() + timedelta(days=YOUTUBE_NO_TRANSCRIPT_DAYS),
                    retry_count=retry_count + 1,
                    last_error=error_message,
                )
                return False

            self.record_retry(
                entry_id,
                url,
                "youtube",
                video_id,
                state,
                error_message,
                hours=ARTICLE_RETRY_BASE_HOURS,
            )
            return False

        transcript_text = clamp_text(transcript_text, TRANSCRIPT_MAX_CHARS)
        if len(transcript_text) < 100:
            self.state.upsert_state(
                entry_id=entry_id,
                url=url,
                source_type="youtube",
                video_id=video_id,
                status="no_transcript",
                source_hash=None,
                summary_hash=None,
                transcript_hash=None,
                next_retry_at=now_utc() + timedelta(days=YOUTUBE_NO_TRANSCRIPT_DAYS),
                retry_count=retry_count + 1,
                last_error="transcript missing or too short",
            )
            return False

        transcript_hash = sha256_text(transcript_text)
        if state and state.status == "done" and state.transcript_hash == transcript_hash:
            return False

        try:
            summary_html = self.generate_summary("youtube", entry, transcript_text)
            content = build_summary_block(
                "youtube-transcript",
                transcript_hash,
                summary_html,
                strip_our_summary(str(entry.get("content") or "")),
                video_id=video_id,
            )
            self.miniflux.update_entry(entry_id, content)
            self.state.upsert_state(
                entry_id=entry_id,
                url=url,
                source_type="youtube",
                video_id=video_id,
                status="done",
                source_hash=transcript_hash,
                summary_hash=sha256_text(summary_html),
                transcript_hash=transcript_hash,
                next_retry_at=None,
                retry_count=0,
                last_error=f"language={language_code}",
            )
            logger.info("summarized youtube entry_id=%s video_id=%s", entry_id, video_id)
            return True
        except Exception as exc:
            self.record_retry(
                entry_id,
                url,
                "youtube",
                video_id,
                state,
                f"summary failed: {exc}",
            )
            logger.warning("youtube summary failed entry_id=%s error=%s", entry_id, exc)
            return False

    def summarize_article_entry(self, entry: dict[str, Any], state: StateRecord | None, text: str) -> bool:
        entry_id = int(entry["id"])
        url = str(entry.get("url") or "")
        source_hash = sha256_text(text)

        if state and state.status == "done" and state.source_hash == source_hash:
            return False

        try:
            summary_html = self.generate_summary("article", entry, text)
            content = build_summary_block(
                "article",
                source_hash,
                summary_html,
                strip_our_summary(str(entry.get("content") or "")),
            )
            self.miniflux.update_entry(entry_id, content)
            self.state.upsert_state(
                entry_id=entry_id,
                url=url,
                source_type="article",
                video_id=None,
                status="done",
                source_hash=source_hash,
                summary_hash=sha256_text(summary_html),
                transcript_hash=None,
                next_retry_at=None,
                retry_count=0,
                last_error=None,
            )
            logger.info("summarized article entry_id=%s", entry_id)
            return True
        except Exception as exc:
            self.record_retry(
                entry_id,
                url,
                "article",
                None,
                state,
                f"summary failed: {exc}",
            )
            logger.warning("article summary failed entry_id=%s error=%s", entry_id, exc)
            return False

    def generate_summary(self, source_type: str, entry: dict[str, Any], content: str) -> str:
        prompt = build_summary_prompt(source_type, entry, content)
        self.state.set_runtime("last_llm_request_at", now_utc())
        summary_html = self.llm.summarize(prompt)
        return summary_html or NO_SUMMARY_HTML

    def record_retry(
        self,
        entry_id: int,
        url: str,
        source_type: str,
        video_id: str | None,
        state: StateRecord | None,
        error_message: str,
        *,
        hours: int | None = None,
        next_retry_at: datetime | None = None,
    ) -> None:
        retry_count = (state.retry_count if state else 0) + 1
        if next_retry_at is None:
            delay_hours = hours if hours is not None else min(72, ARTICLE_RETRY_BASE_HOURS * (2 ** max(retry_count - 1, 0)))
            next_retry_at = now_utc() + timedelta(hours=delay_hours)

        self.state.upsert_state(
            entry_id=entry_id,
            url=url,
            source_type=source_type,
            video_id=video_id,
            status="retry_later",
            source_hash=state.source_hash if state else None,
            summary_hash=state.summary_hash if state else None,
            transcript_hash=state.transcript_hash if state else None,
            next_retry_at=next_retry_at,
            retry_count=retry_count,
            last_error=error_message[:2000],
        )


def main() -> int:
    summarizer = EntrySummarizer()
    summarizer.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
