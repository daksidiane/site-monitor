from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin, urlparse

import feedparser
import httpx
from bs4 import BeautifulSoup

from bot.security import is_safe_fetch_target, is_safe_http_url, redact_secrets, strip_url_userinfo

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (compatible; NewsMonitorBot/1.1; +https://t.me/; educational)"
)

# Универсальные селекторы для HTML-лент
DEFAULT_HTML_CONFIG = {
    "container": "article, .news-item, .post, .entry, .story, li.news",
    "title": "h1, h2, h3, .title, .news-title, a",
    "link": "a[href]",
    "description": "p, .description, .news-description, .summary, .lead, .excerpt",
    "date": "time, .date, .news-date, [datetime], [data-date]",
}

# Короткие типовые пути — только если основной URL открылся, но новостей нет
FEED_PATH_CANDIDATES = (
    "/rss",
    "/feed",
    "/rss.xml",
    "/atom.xml",
    "/news.json",
)

AUTH_MARKERS = (
    "login redirect",
    "edge-access",
    "sign in",
    "sign-in",
    "log in",
    "войдите",
    "авторизац",
    "access denied",
    "403 forbidden",
    "401 unauthorized",
    "captcha",
    "cloudflare",
    "attention required",
    "verify you are human",
)


@dataclass
class ParsedNews:
    title: str
    url: str
    description: str = ""
    published_at: str | None = None
    image_url: str | None = None
    external_id: str | None = None
    category: str | None = None


@dataclass
class FetchOutcome:
    items: list[ParsedNews] = field(default_factory=list)
    status: str = "ok"  # ok | http_error | auth_required | empty | parse_error | blocked
    message: str = ""
    http_status: int | None = None
    used_url: str | None = None


def _make_external_id(url: str, title: str) -> str:
    digest = hashlib.sha256(f"{url}|{title}".encode("utf-8")).hexdigest()[:24]
    return digest


def _normalize_datetime(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    text = str(value).strip()
    if not text:
        return None
    try:
        dt = parsedate_to_datetime(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    except Exception:
        pass
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    except Exception:
        return text


def _clean_text(value: str | None) -> str:
    if not value:
        return ""
    text = BeautifulSoup(value, "lxml").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def _looks_like_auth_wall(content: str, http_status: int | None = None) -> bool:
    if http_status in {401, 403}:
        return True
    blob = (content or "")[:8000].lower()
    return any(marker in blob for marker in AUTH_MARKERS)


def _looks_like_empty_shell(content: str) -> bool:
    """SPA/пустая оболочка: много JS, мало текста и нет карточек новостей."""
    soup = BeautifulSoup(content or "", "lxml")
    text = soup.get_text(" ", strip=True)
    scripts = len(soup.find_all("script"))
    articles = len(soup.select("article, .news-item, .post, .entry"))
    if articles > 0:
        return False
    if scripts >= 3 and len(text) < 400:
        return True
    if "id=\"root\"" in (content or "") or 'id="app"' in (content or ""):
        if articles == 0 and len(text) < 800:
            return True
    return False


_MAX_RESPONSE_BYTES = 1_000_000


def _guard_hook(allow_localhost: bool):
    async def _hook(request: httpx.Request) -> None:
        if not is_safe_fetch_target(str(request.url), allow_localhost=allow_localhost):
            raise httpx.RequestError("Unsafe URL blocked", request=request)

    return _hook


async def _fetch_response(
    url: str,
    timeout: float,
    *,
    allow_localhost: bool,
    client: httpx.AsyncClient | None = None,
) -> httpx.Response:
    if not is_safe_fetch_target(url, allow_localhost=allow_localhost):
        raise ValueError("Unsafe URL blocked")
    request_timeout = httpx.Timeout(timeout, connect=min(5.0, timeout))
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if client is not None:
        response = await client.get(url, timeout=request_timeout, headers=headers)
    else:
        async with httpx.AsyncClient(
            timeout=request_timeout,
            follow_redirects=True,
            headers=headers,
            event_hooks={"request": [_guard_hook(allow_localhost)]},
        ) as owned:
            response = await owned.get(url)
            await response.aread()
    if len(response.content) > _MAX_RESPONSE_BYTES:
        raise ValueError("Response too large")
    return response


def _exc_reason(exc: BaseException) -> str:
    name = type(exc).__name__
    text = str(exc).strip()
    if not text:
        cause = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None)
        if cause:
            text = str(cause).strip() or type(cause).__name__
    return redact_secrets(f"{name}: {text}" if text else name)


def _parse_rss(
    content: str, limit: int, *, allow_localhost: bool
) -> list[ParsedNews]:
    feed = feedparser.parse(content)
    if getattr(feed, "bozo", False) and not feed.entries:
        return []
    items: list[ParsedNews] = []
    for entry in feed.entries[:limit]:
        title = _clean_text(getattr(entry, "title", "") or "")
        link = (getattr(entry, "link", "") or "").strip()
        if not title or not link:
            continue
        if not is_safe_http_url(link, allow_localhost=allow_localhost):
            continue
        description = _clean_text(
            getattr(entry, "summary", "") or getattr(entry, "description", "") or ""
        )
        published = None
        if getattr(entry, "published", None):
            published = _normalize_datetime(entry.published)
        elif getattr(entry, "updated", None):
            published = _normalize_datetime(entry.updated)
        external = getattr(entry, "id", None) or getattr(entry, "guid", None)
        category = None
        tags = getattr(entry, "tags", None)
        if tags:
            try:
                category = str(tags[0].get("term") or tags[0].get("label") or "") or None
            except Exception:
                category = None
        items.append(
            ParsedNews(
                title=title,
                url=link,
                description=description[:1000],
                published_at=published,
                external_id=str(external) if external else _make_external_id(link, title),
                category=category,
            )
        )
    return items


def _parse_json_feed(
    content: str, page_url: str, limit: int, *, allow_localhost: bool
) -> list[ParsedNews]:
    data = json.loads(content)
    if isinstance(data, dict):
        rows = (
            data.get("news")
            or data.get("items")
            or data.get("results")
            or data.get("data")
            or data.get("posts")
            or []
        )
    else:
        rows = data
    if not isinstance(rows, list):
        return []

    items: list[ParsedNews] = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        title = _clean_text(str(row.get("title") or row.get("name") or ""))
        link = str(row.get("url") or row.get("link") or row.get("permalink") or "").strip()
        if not link and row.get("id") is not None:
            link = f"{page_url.rstrip('/')}/#news-{row.get('id')}"
        if not title or not link:
            continue
        if not is_safe_http_url(link, allow_localhost=allow_localhost):
            continue
        items.append(
            ParsedNews(
                title=title,
                url=link,
                description=_clean_text(
                    str(row.get("description") or row.get("summary") or row.get("content") or "")
                )[:1000],
                published_at=_normalize_datetime(
                    row.get("published_at") or row.get("pubDate") or row.get("created_at") or row.get("date")
                ),
                external_id=str(row.get("id") or _make_external_id(link, title)),
                category=str(row.get("category") or "") or None,
            )
        )
    return items


def _abs_url(base: str, href: str | None, *, allow_localhost: bool) -> str | None:
    if not href:
        return None
    absolute = urljoin(base, href.strip())
    return absolute if is_safe_http_url(absolute, allow_localhost=allow_localhost) else None


def _extract_alternate_feeds(html: str, page_url: str, *, allow_localhost: bool) -> list[str]:
    soup = BeautifulSoup(html or "", "lxml")
    feeds: list[str] = []
    for link in soup.select('link[rel="alternate"]'):
        href = link.get("href")
        typ = (link.get("type") or "").lower()
        if href and (
            "rss" in typ
            or "atom" in typ
            or "xml" in typ
            or "json" in typ
            or "rss" in href.lower()
            or "atom" in href.lower()
            or href.lower().endswith(".json")
        ):
            abs_url = _abs_url(page_url, href, allow_localhost=allow_localhost)
            if abs_url and abs_url not in feeds:
                feeds.append(abs_url)
    return feeds


def _feed_candidates_for(url: str) -> list[str]:
    """Несколько коротких путей фидов на том же origin — не полный перебор."""
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    candidates = [origin + path for path in FEED_PATH_CANDIDATES]
    seen: set[str] = set()
    result: list[str] = []
    for item in candidates:
        if item not in seen and item.rstrip("/") != url.rstrip("/"):
            seen.add(item)
            result.append(item)
    return result


def _parse_html(
    content: str,
    page_url: str,
    parsing_config: dict[str, Any] | None,
    limit: int,
    *,
    allow_localhost: bool,
) -> list[ParsedNews]:
    soup = BeautifulSoup(content, "lxml")
    cfg = {**DEFAULT_HTML_CONFIG, **(parsing_config or {})}
    containers = soup.select(cfg["container"])
    if not containers:
        containers = soup.select("article, .post, .news-item, .entry")

    items: list[ParsedNews] = []
    for node in containers:
        if len(items) >= limit:
            break
        title_el = None
        for sel in cfg["title"].split(","):
            title_el = node.select_one(sel.strip())
            if title_el and _clean_text(title_el.get_text()):
                break
            title_el = None
        if not title_el:
            continue
        title = _clean_text(title_el.get_text())
        link_el = None
        for sel in (cfg.get("link") or "a[href]").split(","):
            link_el = node.select_one(sel.strip())
            if link_el and link_el.get("href"):
                break
            link_el = None
        if link_el is None and title_el.name == "a":
            link_el = title_el
        href = link_el.get("href") if link_el else None
        url = _abs_url(page_url, href, allow_localhost=allow_localhost)
        if not url:
            data_id = node.get("data-id") or node.get("id")
            if data_id:
                url = f"{page_url.rstrip('/')}/#{data_id}"
            elif is_safe_http_url(page_url, allow_localhost=allow_localhost):
                # Без уникальной ссылки дедуп по заголовку всё равно сработает
                url = f"{page_url.rstrip('/')}/#{_make_external_id(page_url, title)}"
        if not title or not url:
            continue

        description = ""
        for sel in (cfg.get("description") or "p").split(","):
            desc_el = node.select_one(sel.strip())
            if desc_el:
                description = _clean_text(
                    desc_el.get("content") if desc_el.name == "meta" else desc_el.get_text()
                )
                if description:
                    break

        published = None
        if node.get("data-date"):
            published = _normalize_datetime(node.get("data-date"))
        if not published:
            for sel in (cfg.get("date") or "time").split(","):
                date_el = node.select_one(sel.strip())
                if date_el:
                    raw = (
                        date_el.get("datetime")
                        or date_el.get("data-date")
                        or date_el.get("content")
                        or date_el.get_text()
                    )
                    published = _normalize_datetime(raw)
                    if published:
                        break

        external = node.get("data-id") or node.get("id") or _make_external_id(url, title)
        items.append(
            ParsedNews(
                title=title,
                url=url,
                description=description[:1000],
                published_at=published,
                external_id=str(external),
            )
        )
    return items


def _try_parse_payload(
    content: str,
    url: str,
    *,
    limit: int,
    allow_localhost: bool,
    content_type: str = "",
) -> list[ParsedNews]:
    ctype = (content_type or "").lower()
    text = content or ""
    stripped = text.lstrip()

    if "json" in ctype or stripped.startswith("{") or stripped.startswith("["):
        try:
            items = _parse_json_feed(text, url, limit, allow_localhost=allow_localhost)
            if items:
                return items
        except Exception:
            pass

    if "xml" in ctype or "rss" in ctype or "atom" in ctype or stripped.startswith("<?xml") or "<rss" in stripped[:200].lower() or "<feed" in stripped[:200].lower():
        items = _parse_rss(text, limit, allow_localhost=allow_localhost)
        if items:
            return items

    # Слепой разбор: сначала фид, потом json, потом html
    items = _parse_rss(text, limit, allow_localhost=allow_localhost)
    if items:
        return items
    try:
        items = _parse_json_feed(text, url, limit, allow_localhost=allow_localhost)
        if items:
            return items
    except Exception:
        pass
    return _parse_html(text, url, None, limit, allow_localhost=allow_localhost)


def detect_source_type(url: str) -> tuple[str, str | None, dict | None]:
    """Грубая эвристика до пробы. Точный тип уточняется probe/fetch."""
    lower = url.lower().rstrip("/")
    if lower.endswith(".json") or "/api/" in lower:
        return "json", None, None
    if any(lower.endswith(sfx) for sfx in ("/rss", "/feed", ".xml", "/atom")):
        return "rss", url, None
    if "rss" in lower or "atom" in lower:
        return "rss", url, None
    return "html", None, None


RSS_FALLBACKS = {
    "https://vc.ru": "https://vc.ru/rss/all",
    "https://meduza.io": "https://meduza.io/rss/all",
    "https://habr.com/ru/all/": "https://habr.com/ru/rss/all/?fl=ru",
    "https://www.rbc.ru/business/": "https://rssexport.rbc.ru/rbcnews/news/30/full.rss",
}


async def fetch_source_news(
    source: dict[str, Any],
    *,
    timeout: float,
    limit: int,
    allow_localhost: bool = False,
) -> FetchOutcome:
    """
    Сбор новостей с быстрым fail:
    1) last_feed_url / rss_feed / основной URL
    2) known fallback
    3) alternate из HTML (если страница открылась)
    4) максимум несколько типовых /rss,/feed — и только если сеть жива
    """
    url = (source.get("url") or "").strip()
    source_type = (source.get("source_type") or "html").lower()
    rss_feed = source.get("rss_feed") or RSS_FALLBACKS.get(url.rstrip("/")) or RSS_FALLBACKS.get(url)
    last_feed_url = (source.get("last_feed_url") or "").strip() or None
    parsing_config = source.get("parsing_config")
    if isinstance(parsing_config, str) and parsing_config:
        try:
            parsing_config = json.loads(parsing_config)
        except json.JSONDecodeError:
            parsing_config = None

    # Короткий таймаут на пробы, чтобы мониторинг не зависал минутами
    probe_timeout = min(float(timeout), 8.0)
    tried: list[str] = []
    last_http: int | None = None
    auth_seen = False
    empty_shell_seen = False
    network_failures = 0
    max_network_failures = 2
    pending_alternates: list[str] = []

    async with httpx.AsyncClient(
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
        event_hooks={"request": [_guard_hook(allow_localhost)]},
    ) as client:

        async def _consume(candidate: str, prefer: str | None = None) -> FetchOutcome | None:
            nonlocal last_http, auth_seen, empty_shell_seen, network_failures
            if candidate in tried:
                return None
            if network_failures >= max_network_failures:
                return None
            if not is_safe_http_url(candidate, allow_localhost=allow_localhost):
                return None
            tried.append(candidate)
            try:
                response = await _fetch_response(
                    candidate,
                    probe_timeout,
                    allow_localhost=allow_localhost,
                    client=client,
                )
            except Exception as exc:
                network_failures += 1
                logger.info(
                    "Не удалось открыть %s (%s)",
                    strip_url_userinfo(candidate),
                    _exc_reason(exc),
                )
                return None

            network_failures = 0
            last_http = response.status_code
            body = response.text or ""
            if response.status_code in {401, 403} or _looks_like_auth_wall(
                body, response.status_code
            ):
                auth_seen = True
                return None
            if response.status_code >= 400:
                return None

            ctype = (response.headers.get("content-type") or "").lower()
            is_html = (
                "html" in ctype
                or body.lstrip().lower().startswith("<!doctype")
                or body.lstrip().lower().startswith("<html")
            )

            if prefer == "html" or (prefer is None and is_html and source_type == "html"):
                if _looks_like_empty_shell(body):
                    empty_shell_seen = True
                    for feed in _extract_alternate_feeds(
                        body, candidate, allow_localhost=allow_localhost
                    ):
                        if feed not in tried and feed not in pending_alternates:
                            pending_alternates.append(feed)
                    return None
                items = _parse_html(
                    body,
                    candidate,
                    parsing_config,
                    limit,
                    allow_localhost=allow_localhost,
                )
            else:
                items = _try_parse_payload(
                    body,
                    candidate,
                    limit=limit,
                    allow_localhost=allow_localhost,
                    content_type=ctype,
                )
                if not items and is_html:
                    if _looks_like_empty_shell(body):
                        empty_shell_seen = True
                    else:
                        items = _parse_html(
                            body,
                            candidate,
                            parsing_config,
                            limit,
                            allow_localhost=allow_localhost,
                        )

            if items:
                return FetchOutcome(
                    items=items,
                    status="ok",
                    message=f"Найдено записей: {len(items)}",
                    http_status=response.status_code,
                    used_url=candidate,
                )

            if is_html:
                for feed in _extract_alternate_feeds(
                    body, candidate, allow_localhost=allow_localhost
                ):
                    if feed not in tried and feed not in pending_alternates:
                        pending_alternates.append(feed)
                if _looks_like_empty_shell(body):
                    empty_shell_seen = True
            return None

        try:
            priority: list[tuple[str, str | None]] = []
            if last_feed_url:
                priority.append((last_feed_url, None))
            if source_type == "json":
                priority.append((url, "json"))
            if rss_feed:
                priority.append((rss_feed, "rss"))
            if source_type == "rss":
                priority.append((url, "rss"))
            priority.append(
                (url, source_type if source_type in {"json", "rss", "html"} else None)
            )

            for candidate, prefer in priority:
                outcome = await _consume(candidate, prefer=prefer)
                if outcome and outcome.items:
                    return outcome
                if network_failures >= max_network_failures:
                    break

            # Alternate-фиды из успешно открытого HTML
            for feed in pending_alternates[:3]:
                if network_failures >= max_network_failures:
                    break
                outcome = await _consume(feed)
                if outcome and outcome.items:
                    return outcome

            # Типовые пути — только если сеть ещё отвечает
            if network_failures < max_network_failures:
                for candidate in _feed_candidates_for(url)[:3]:
                    if network_failures >= max_network_failures:
                        break
                    outcome = await _consume(candidate)
                    if outcome and outcome.items:
                        return outcome

            if network_failures >= max_network_failures:
                return FetchOutcome(
                    status="http_error",
                    message=(
                        "Источник недоступен по сети (таймаут/блок). "
                        "Пропуск лишних путей фида, чтобы не тормозить мониторинг."
                    ),
                    http_status=last_http,
                    used_url=url,
                )
            if auth_seen:
                return FetchOutcome(
                    status="auth_required",
                    message=(
                        "Источник требует авторизацию или закрыт (401/403). "
                        "Бот читает только публичные страницы/RSS/JSON."
                    ),
                    http_status=last_http,
                    used_url=url,
                )
            if empty_shell_seen:
                return FetchOutcome(
                    status="empty",
                    message=(
                        "Страница открывается, но список новостей на сервере пуст "
                        "(часто SPA/localStorage). Нужен публичный RSS/Atom/JSON."
                    ),
                    http_status=last_http,
                    used_url=url,
                )
            if last_http and last_http >= 400:
                return FetchOutcome(
                    status="http_error",
                    message=f"Источник недоступен (HTTP {last_http}).",
                    http_status=last_http,
                    used_url=url,
                )
            return FetchOutcome(
                status="empty",
                message=(
                    "Не удалось извлечь новости. "
                    "Добавьте прямую ссылку на фид (…/rss, …/feed, …/news.json)."
                ),
                http_status=last_http,
                used_url=url,
            )
        except Exception as exc:
            logger.warning(
                "Ошибка парсинга источника %s: %s",
                urlparse(url).hostname or "источник",
                _exc_reason(exc),
            )
            return FetchOutcome(
                status="parse_error",
                message=f"Ошибка чтения источника: {_exc_reason(exc)[:200]}",
                used_url=url,
            )


# Обратная совместимость для старых вызовов
async def fetch_source_news_items(
    source: dict[str, Any],
    *,
    timeout: float,
    limit: int,
    allow_localhost: bool = False,
) -> list[ParsedNews]:
    return (
        await fetch_source_news(
            source,
            timeout=timeout,
            limit=limit,
            allow_localhost=allow_localhost,
        )
    ).items
