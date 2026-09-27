from __future__ import annotations

import ipaddress
import json
import re
import socket
from html import escape
from urllib.parse import urlparse, urlunparse

_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
_SECRET_RE = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{8,}|Bot\d+:[A-Za-z0-9_-]{20,}|Bearer\s+[A-Za-z0-9._-]+)",
    re.IGNORECASE,
)
_URL_USERINFO_RE = re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@", re.IGNORECASE)


CATEGORIES = {
    "general": "Главные новости",
    "tech": "Технологии",
    "business": "Бизнес и финансы",
    "science": "Наука",
    "politics": "Политика",
    "sports": "Спорт",
    "culture": "Культура",
}

EXCLUDE_KEYWORDS = (
    "гороскоп",
    "прогноз погоды",
    "реклама",
    "партнерский материал",
    "партнёрский материал",
)

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1"})


def sanitize_text(text: str, max_chars: int) -> str:
    cleaned = (text or "").replace("\x00", "").strip()
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars]
    return cleaned


def redact_secrets(text: str) -> str:
    cleaned = _SECRET_RE.sub("[REDACTED]", text or "")
    return _URL_USERINFO_RE.sub(r"\1", cleaned)


def is_localhost_host(host: str | None) -> bool:
    return (host or "").lower() in _LOCAL_HOSTS


def is_safe_http_url(url: str, *, allow_localhost: bool = False) -> bool:
    raw = (url or "").strip()
    if not raw or len(raw) > 500 or not _URL_RE.match(raw):
        return False
    try:
        parsed = urlparse(raw)
    except Exception:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if is_localhost_host(host):
        return allow_localhost
    if host.startswith("10.") or host.startswith("192.168.") or host.startswith("169.254."):
        return False
    if re.match(r"^172\.(1[6-9]|2\d|3[0-1])\.", host):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and _ip_blocked(ip, allow_localhost=allow_localhost):
        return False
    return True


def _ip_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address, *, allow_localhost: bool) -> bool:
    if allow_localhost and ip.is_loopback:
        return False
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def is_safe_fetch_target(url: str, *, allow_localhost: bool = False) -> bool:
    """URL безопасен и не резолвится во внутренний адрес."""
    if not is_safe_http_url(url, allow_localhost=allow_localhost):
        return False
    host = (urlparse(url).hostname or "").lower()
    if is_localhost_host(host):
        return allow_localhost
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if _ip_blocked(ip, allow_localhost=allow_localhost):
            return False
    return True


def unsafe_http_url_message(url: str, *, allow_localhost: bool) -> str:
    """Понятное объяснение, почему URL отклонён."""
    raw = (url or "").strip()
    try:
        host = urlparse(raw).hostname
    except Exception:
        host = None
    if is_localhost_host(host) and not allow_localhost:
        return (
            "Локальный URL (localhost) заблокирован. "
            "Добавьте ALLOW_LOCALHOST_SOURCES=1 в .env и перезапустите бота."
        )
    if allow_localhost and is_localhost_host(host):
        return "Нужен корректный http(s) URL (localhost разрешён)."
    return "Нужен публичный http(s) URL."


def html_escape(text: str) -> str:
    return escape(text or "", quote=True)


def strip_url_userinfo(url: str) -> str:
    """Убирает логин и пароль из URL перед показом или ссылкой."""
    raw = (url or "").strip()
    try:
        parsed = urlparse(raw)
    except Exception:
        return raw
    if parsed.username or parsed.password:
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return urlunparse(parsed._replace(netloc=host))
    if "://" not in raw and "@" in raw:
        return raw.split("@", 1)[-1]
    return raw


def looks_irrelevant(title: str, description: str = "") -> bool:
    blob = f"{title} {description}".lower()
    return any(word in blob for word in EXCLUDE_KEYWORDS)
