from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from urllib.parse import urlparse

from telegram.ext import Application

from bot.config import Settings
from bot.db import Database
from bot.formatters import format_news_message, subscriber_categories
from bot.llm import LLMClient
from bot.parsers import fetch_source_news
from bot.security import looks_irrelevant, redact_secrets, strip_url_userinfo

logger = logging.getLogger(__name__)


def _row_to_source_dict(row: Any) -> dict[str, Any]:
    parsing = row["parsing_config"]
    if isinstance(parsing, str) and parsing:
        try:
            parsing = json.loads(parsing)
        except json.JSONDecodeError:
            parsing = None
    return {
        "id": row["id"],
        "name": row["name"],
        "url": row["url"],
        "source_type": row["source_type"],
        "rss_feed": row["rss_feed"],
        "category": row["category"],
        "parsing_config": parsing,
        "check_interval": row["check_interval"],
        "language": row["language"],
        "last_feed_url": row["last_feed_url"] if "last_feed_url" in row.keys() else None,
    }


def _source_priority(row: Any) -> tuple[int, int]:
    """Сначала localhost и здоровые источники — чтобы тест-фид не ждал мёртвые сайты."""
    url = f"{row['url']} {row['rss_feed'] or ''}".lower()
    keys = row.keys() if hasattr(row, "keys") else []
    status = (row["last_status"] if "last_status" in keys else None) or ""
    if "127.0.0.1" in url or "localhost" in url:
        host_rank = 0
    else:
        host_rank = 1
    status_rank = {
        "ok": 0,
        "": 1,
        "empty": 2,
        "auth_required": 3,
        "http_error": 4,
        "parse_error": 4,
        "blocked": 4,
    }.get(status, 1)
    return (host_rank, status_rank)


async def process_sources(
    *,
    db: Database,
    llm: LLMClient,
    settings: Settings,
    source_ids: list[int] | None = None,
) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "fetched": 0,
        "new": 0,
        "ai_ok": 0,
        "skipped": 0,
        "errors": 0,
        "source_problems": [],
    }
    sources = db.list_sources(active_only=True)
    if source_ids is not None:
        allowed = set(source_ids)
        sources = [s for s in sources if int(s["id"]) in allowed]
    sources = sorted(sources, key=_source_priority)

    recent_titles = db.recent_titles(50)
    per_source_timeout = max(12.0, float(settings.fetch_timeout_seconds) + 4.0)

    for source_row in sources:
        source = _row_to_source_dict(source_row)
        try:
            outcome = await asyncio.wait_for(
                fetch_source_news(
                    source,
                    timeout=settings.fetch_timeout_seconds,
                    limit=settings.max_news_per_source,
                    allow_localhost=settings.allow_localhost_sources,
                ),
                timeout=per_source_timeout,
            )
        except asyncio.TimeoutError:
            outcome_msg = "Превышен лимит времени на источник — пропуск"
            db.update_source_health(
                int(source["id"]),
                status="http_error",
                message=outcome_msg,
                items_count=0,
            )
            stats["errors"] += 1
            stats["source_problems"].append(
                {
                    "name": source["name"],
                    "url": source["url"],
                    "status": "http_error",
                    "message": outcome_msg,
                }
            )
            logger.warning("Таймаут источника %s", strip_url_userinfo(str(source["url"])))
            continue

        db.update_source_health(
            int(source["id"]),
            status=outcome.status,
            message=outcome.message,
            items_count=len(outcome.items),
            feed_url=outcome.used_url,
        )
        if outcome.status != "ok" or not outcome.items:
            stats["source_problems"].append(
                {
                    "name": source["name"],
                    "url": source["url"],
                    "status": outcome.status,
                    "message": outcome.message,
                }
            )
            if outcome.status in {"auth_required", "http_error", "parse_error"}:
                stats["errors"] += 1
            continue

        # Если нашли рабочий фид — запомним его для следующих проверок
        if outcome.used_url and outcome.used_url != (source.get("rss_feed") or source.get("url")):
            used = outcome.used_url.lower()
            if any(x in used for x in ("rss", "atom", "feed", ".xml", ".json", "/api/")):
                discovered_type = (
                    "json" if (used.endswith(".json") or "/api/" in used) else "rss"
                )
                db.set_discovered_feed(
                    int(source["id"]),
                    feed_url=outcome.used_url,
                    source_type=discovered_type,
                )

        items = outcome.items
        stats["fetched"] += len(items)

        for item in items:
            if db.news_exists(int(source["id"]), item.external_id, item.url):
                stats["skipped"] += 1
                continue
            if looks_irrelevant(item.title, item.description):
                db.insert_news(
                    {
                        "source_id": source["id"],
                        "external_id": item.external_id,
                        "title": item.title,
                        "description": item.description,
                        "url": item.url,
                        "image_url": item.image_url,
                        "published_at": item.published_at,
                        "category": item.category or source.get("category") or "general",
                        "ai_summary": "",
                        "ai_category": "Нерелевантно",
                        "sentiment_score": 0.0,
                        "is_relevant": False,
                        "is_duplicate": False,
                        "tags": [],
                    }
                )
                stats["skipped"] += 1
                continue

            payload = {
                "title": item.title,
                "url": item.url,
                "published_at": item.published_at,
                "description": item.description,
                "source_name": source["name"],
                "source_category": item.category or source.get("category"),
                "language": source.get("language") or "ru",
                "existing_titles": recent_titles[:30],
            }

            try:
                ai = await llm.process_news(payload)
            except Exception as exc:
                stats["errors"] += 1
                logger.error(
                    "AI failed for %s: %s",
                    urlparse(item.url).hostname or "источник",
                    redact_secrets(str(exc)),
                )
                continue

            category = str(
                ai.get("category")
                or item.category
                or source.get("category")
                or "general"
            )
            tags = ai.get("tags") if isinstance(ai.get("tags"), list) else []
            is_relevant = bool(ai.get("is_relevant", True))
            is_duplicate = bool(ai.get("is_duplicate", False))
            summary = str(ai.get("ai_summary") or "").strip()
            title = str(ai.get("title") or item.title).strip() or item.title

            news_id = db.insert_news(
                {
                    "source_id": source["id"],
                    "external_id": item.external_id,
                    "title": title,
                    "description": item.description,
                    "url": item.url,
                    "image_url": item.image_url,
                    "published_at": item.published_at,
                    "category": category,
                    "tags": tags,
                    "ai_summary": summary,
                    "ai_category": str(ai.get("ai_category") or category),
                    "sentiment_score": ai.get("sentiment_score"),
                    "is_relevant": is_relevant,
                    "is_duplicate": is_duplicate,
                }
            )
            if news_id is None:
                stats["skipped"] += 1
                continue

            stats["new"] += 1
            if is_relevant and not is_duplicate and summary:
                stats["ai_ok"] += 1
                recent_titles.insert(0, title)

    return stats


async def deliver_pending(app: Application) -> dict[str, int]:
    db: Database = app.bot_data["db"]
    pending = db.pending_notifications()
    if not pending:
        return {"sent": 0, "filtered_by_category": 0}

    subscribers = db.active_subscribers()
    if not subscribers:
        for row in pending:
            db.mark_sent(int(row["id"]))
        return {"sent": 0, "filtered_by_category": 0}

    sent_count = 0
    filtered = 0
    for row in pending:
        category = (row["category"] or "general").lower()
        message = format_news_message(row)
        delivered_any = False
        has_instant_match = False

        for sub in subscribers:
            mode = (sub["notify_mode"] or "instant").lower()
            if mode != "instant":
                continue
            cats = [c.lower() for c in subscriber_categories(sub)]
            if category not in cats:
                continue
            has_instant_match = True
            try:
                await app.bot.send_message(
                    chat_id=int(sub["telegram_id"]),
                    text=message,
                    parse_mode="HTML",
                    disable_web_page_preview=False,
                )
                delivered_any = True
                sent_count += 1
            except Exception as exc:
                logger.warning(
                    "Не удалось отправить пользователю %s: %s",
                    sub["telegram_id"],
                    redact_secrets(str(exc)),
                )

        if not has_instant_match:
            filtered += 1

        if delivered_any or not has_instant_match:
            db.mark_sent(int(row["id"]))

    return {"sent": sent_count, "filtered_by_category": filtered}


async def monitor_job(context: Any) -> None:
    app: Application = context.application
    settings: Settings = app.bot_data["settings"]
    db: Database = app.bot_data["db"]
    llm: LLMClient = app.bot_data["llm"]

    logger.info("Мониторинг источников…")
    stats = await process_sources(db=db, llm=llm, settings=settings)
    delivery = await deliver_pending(app)
    if stats["source_problems"]:
        for problem in stats["source_problems"][:10]:
            logger.warning(
                "Источник «%s»: %s — %s",
                problem["name"],
                problem["status"],
                problem["message"],
            )
    logger.info(
        "Мониторинг завершён: fetched=%s new=%s ai_ok=%s skipped=%s errors=%s sent=%s filtered=%s",
        stats["fetched"],
        stats["new"],
        stats["ai_ok"],
        stats["skipped"],
        stats["errors"],
        delivery["sent"],
        delivery["filtered_by_category"],
    )
