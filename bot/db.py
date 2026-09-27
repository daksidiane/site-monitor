from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(self.path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS news_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    url TEXT NOT NULL UNIQUE,
                    source_type TEXT DEFAULT 'html',
                    rss_feed TEXT,
                    category TEXT,
                    is_active INTEGER DEFAULT 1,
                    parsing_config TEXT,
                    check_interval INTEGER DEFAULT 600,
                    language TEXT DEFAULT 'ru',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS news_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id INTEGER,
                    external_id TEXT,
                    title TEXT NOT NULL,
                    description TEXT,
                    content TEXT,
                    url TEXT NOT NULL,
                    image_url TEXT,
                    published_at TEXT,
                    category TEXT,
                    tags TEXT,
                    ai_summary TEXT,
                    ai_category TEXT,
                    sentiment_score REAL,
                    is_relevant INTEGER DEFAULT 1,
                    is_duplicate INTEGER DEFAULT 0,
                    is_sent INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (source_id) REFERENCES news_sources(id),
                    UNIQUE(external_id, source_id)
                );

                CREATE TABLE IF NOT EXISTS subscribers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL UNIQUE,
                    username TEXT,
                    is_active INTEGER DEFAULT 1,
                    is_paused INTEGER DEFAULT 0,
                    notify_mode TEXT DEFAULT 'instant',
                    categories TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_news_items_sent
                    ON news_items(is_sent, is_relevant, is_duplicate);
                CREATE INDEX IF NOT EXISTS idx_news_items_url ON news_items(url);
                """
            )
            # Миграции статуса здоровья источника
            cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(news_sources)").fetchall()
            }
            alterations = {
                "last_check_at": "TEXT",
                "last_status": "TEXT",
                "last_error": "TEXT",
                "last_items_count": "INTEGER DEFAULT 0",
                "last_feed_url": "TEXT",
            }
            for name, typ in alterations.items():
                if name not in cols:
                    conn.execute(
                        f"ALTER TABLE news_sources ADD COLUMN {name} {typ}"
                    )

    def seed_sources(self, sources: list[dict[str, Any]]) -> int:
        inserted = 0
        with self.connect() as conn:
            for src in sources:
                url = (src.get("url") or "").strip()
                if not url:
                    continue
                existing = conn.execute(
                    "SELECT id FROM news_sources WHERE url = ?", (url,)
                ).fetchone()
                if existing:
                    continue
                parsing = src.get("parsing_config")
                conn.execute(
                    """
                    INSERT INTO news_sources (
                        name, url, source_type, rss_feed, category, is_active,
                        parsing_config, check_interval, language
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        src.get("name") or url,
                        url,
                        src.get("source_type") or "html",
                        src.get("rss_feed"),
                        src.get("category") or "general",
                        1 if src.get("is_active", True) else 0,
                        json.dumps(parsing, ensure_ascii=False) if parsing else None,
                        int(src.get("check_interval") or 600),
                        src.get("language") or "ru",
                    ),
                )
                inserted += 1
        return inserted

    def list_sources(self, active_only: bool = False) -> list[sqlite3.Row]:
        query = "SELECT * FROM news_sources"
        if active_only:
            query += " WHERE is_active = 1"
        query += " ORDER BY id"
        with self.connect() as conn:
            return list(conn.execute(query).fetchall())

    def add_source(
        self,
        *,
        name: str,
        url: str,
        source_type: str = "rss",
        rss_feed: str | None = None,
        category: str = "general",
        parsing_config: dict | None = None,
    ) -> int:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO news_sources (
                    name, url, source_type, rss_feed, category, is_active, parsing_config
                )
                VALUES (?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(url) DO UPDATE SET
                    is_active = 1,
                    name = excluded.name,
                    source_type = excluded.source_type,
                    rss_feed = COALESCE(excluded.rss_feed, news_sources.rss_feed),
                    category = excluded.category,
                    parsing_config = COALESCE(excluded.parsing_config, news_sources.parsing_config),
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    name,
                    url,
                    source_type,
                    rss_feed or (url if source_type == "rss" else None),
                    category,
                    json.dumps(parsing_config, ensure_ascii=False) if parsing_config else None,
                ),
            )
            row = conn.execute(
                "SELECT id FROM news_sources WHERE url = ?", (url,)
            ).fetchone()
            return int(row["id"])

    def set_discovered_feed(
        self, source_id: int, *, feed_url: str, source_type: str
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE news_sources
                SET rss_feed = ?,
                    source_type = ?,
                    last_feed_url = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (feed_url, source_type, feed_url, source_id),
            )

    def update_source_health(
        self,
        source_id: int,
        *,
        status: str,
        message: str,
        items_count: int = 0,
        feed_url: str | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE news_sources
                SET last_check_at = ?,
                    last_status = ?,
                    last_error = ?,
                    last_items_count = ?,
                    last_feed_url = COALESCE(?, last_feed_url),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    _utc_now(),
                    status,
                    (message or "")[:500],
                    items_count,
                    feed_url,
                    source_id,
                ),
            )

    def unhealthy_sources(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT * FROM news_sources
                    WHERE is_active = 1
                      AND last_status IS NOT NULL
                      AND last_status != 'ok'
                    ORDER BY id
                    """
                ).fetchall()
            )

    def deactivate_source_by_url(self, url: str) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                """
                UPDATE news_sources
                SET is_active = 0, updated_at = CURRENT_TIMESTAMP
                WHERE url = ? OR rss_feed = ?
                """,
                (url, url),
            )
            return cur.rowcount > 0

    def upsert_subscriber(
        self,
        telegram_id: int,
        username: str | None,
        default_categories: list[str],
    ) -> None:
        cats = json.dumps(default_categories, ensure_ascii=False)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO subscribers (telegram_id, username, is_active, categories)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    username = excluded.username,
                    is_active = 1
                """,
                (telegram_id, username, cats),
            )

    def get_subscriber(self, telegram_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM subscribers WHERE telegram_id = ?",
                (telegram_id,),
            ).fetchone()

    def set_paused(self, telegram_id: int, paused: bool) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE subscribers SET is_paused = ? WHERE telegram_id = ?",
                (1 if paused else 0, telegram_id),
            )

    def set_notify_mode(self, telegram_id: int, mode: str) -> None:
        if mode not in {"instant", "hourly", "daily"}:
            raise ValueError("invalid notify mode")
        with self.connect() as conn:
            conn.execute(
                "UPDATE subscribers SET notify_mode = ? WHERE telegram_id = ?",
                (mode, telegram_id),
            )

    def set_categories(self, telegram_id: int, categories: list[str]) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE subscribers SET categories = ? WHERE telegram_id = ?",
                (json.dumps(categories, ensure_ascii=False), telegram_id),
            )

    def active_subscribers(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT * FROM subscribers
                    WHERE is_active = 1 AND is_paused = 0
                    """
                ).fetchall()
            )

    def news_exists(self, source_id: int, external_id: str | None, url: str) -> bool:
        with self.connect() as conn:
            if external_id:
                row = conn.execute(
                    """
                    SELECT id FROM news_items
                    WHERE source_id = ? AND external_id = ?
                    """,
                    (source_id, external_id),
                ).fetchone()
                if row:
                    return True
            row = conn.execute(
                "SELECT id FROM news_items WHERE url = ?", (url,)
            ).fetchone()
            return row is not None

    def recent_titles(self, limit: int = 40) -> list[str]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT title FROM news_items
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [r["title"] for r in rows]

    def insert_news(self, payload: dict[str, Any]) -> int | None:
        with self.connect() as conn:
            try:
                cur = conn.execute(
                    """
                    INSERT INTO news_items (
                        source_id, external_id, title, description, content, url,
                        image_url, published_at, category, tags, ai_summary,
                        ai_category, sentiment_score, is_relevant, is_duplicate, is_sent
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                    """,
                    (
                        payload.get("source_id"),
                        payload.get("external_id"),
                        payload["title"],
                        payload.get("description"),
                        payload.get("content"),
                        payload["url"],
                        payload.get("image_url"),
                        payload.get("published_at"),
                        payload.get("category"),
                        json.dumps(payload.get("tags") or [], ensure_ascii=False),
                        payload.get("ai_summary"),
                        payload.get("ai_category"),
                        payload.get("sentiment_score"),
                        1 if payload.get("is_relevant", True) else 0,
                        1 if payload.get("is_duplicate") else 0,
                    ),
                )
                return int(cur.lastrowid)
            except sqlite3.IntegrityError:
                return None

    def mark_sent(self, news_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE news_items SET is_sent = 1 WHERE id = ?",
                (news_id,),
            )

    def pending_notifications(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT ni.*, ns.name AS source_name
                    FROM news_items ni
                    JOIN news_sources ns ON ns.id = ni.source_id
                    WHERE ni.is_sent = 0
                      AND ni.is_relevant = 1
                      AND ni.is_duplicate = 0
                      AND ni.ai_summary IS NOT NULL
                      AND TRIM(ni.ai_summary) != ''
                    ORDER BY ni.id ASC
                    LIMIT 50
                    """
                ).fetchall()
            )
