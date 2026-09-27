from __future__ import annotations

import json
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.security import CATEGORIES, html_escape, is_safe_http_url, strip_url_userinfo


def format_news_message(item: dict[str, Any] | Any) -> str:
    def get(key: str, default: str = "") -> str:
        if isinstance(item, dict):
            value = item.get(key, default)
        else:
            value = item[key] if key in item.keys() else default
        return "" if value is None else str(value)

    title = html_escape(get("title"))
    summary = html_escape(get("ai_summary"))
    category = html_escape(get("ai_category") or get("category") or "Новости")
    url = strip_url_userinfo(get("url"))
    published = html_escape(get("published_at") or "—")
    source = html_escape(get("source_name") or "Источник")
    if is_safe_http_url(url, allow_localhost=False):
        link = f'🔗 <a href="{html_escape(url)}">Читать оригинал</a>'
    else:
        link = "🔗 ссылка не показана"

    lines = [
        f"<b>{title}</b>",
        "",
        summary,
        "",
        f"🏷 {category}",
        link,
        f"🕐 {published}",
        f"📰 {source}",
    ]
    text = "\n".join(lines)
    return text[:4000]


def format_sources_list(sources: list[Any]) -> str:
    if not sources:
        return "Список источников пуст."
    lines = ["<b>Активные источники</b>:"]
    for src in sources:
        active = "✅" if int(src["is_active"]) else "⏸"
        name = html_escape(strip_url_userinfo(str(src["name"])))
        url = html_escape(strip_url_userinfo(str(src["url"])))
        category = html_escape(str(src["category"] or "general"))
        status = ""
        keys = src.keys() if hasattr(src, "keys") else []
        if "last_status" in keys and src["last_status"] and src["last_status"] != "ok":
            err = html_escape(str(src["last_error"] or src["last_status"])[:120])
            status = f"\n⚠️ {err}"
        elif "last_status" in keys and src["last_status"] == "ok":
            status = "\n✓ проверка OK"
        lines.append(f"{active} <b>{name}</b> [{category}]\n{url}{status}")
    return "\n\n".join(lines)[:4000]


def format_categories_status(selected: list[str] | None = None) -> str:
    if selected is None:
        selected_set = set(CATEGORIES.keys())
    else:
        selected_set = set(selected)
    lines = [
        "<b>Темы рассылки</b>",
        "Нажмите на тему, чтобы добавить или убрать её.",
        "",
    ]
    for code, title in CATEGORIES.items():
        mark = "✅" if code in selected_set else "▫️"
        lines.append(f"{mark} {html_escape(title)}")
    if not selected_set:
        lines.append("")
        lines.append("Сейчас темы не выбраны — новости приходить не будут.")
    return "\n".join(lines)


def categories_inline_keyboard(selected: list[str] | None = None) -> InlineKeyboardMarkup:
    if selected is None:
        selected_set: set[str] = set()
    else:
        selected_set = set(selected)
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for code, title in CATEGORIES.items():
        mark = "✅" if code in selected_set else "➕"
        row.append(
            InlineKeyboardButton(f"{mark} {title}", callback_data=f"cat:{code}")
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [
            InlineKeyboardButton("Выбрать все", callback_data="cat:all"),
            InlineKeyboardButton("Снять все", callback_data="cat:none"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def parse_categories_arg(raw: str) -> list[str]:
    parts = [p.strip().lower() for p in (raw or "").replace(";", ",").split(",")]
    result = [p for p in parts if p in CATEGORIES]
    return result


def subscriber_categories(row: Any) -> list[str]:
    raw = row["categories"] if row and "categories" in row.keys() else None
    if not raw:
        return list(CATEGORIES.keys())
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [c for c in data if c in CATEGORIES]
    except Exception:
        pass
    return list(CATEGORIES.keys())


def toggle_category(selected: list[str], code: str) -> list[str]:
    current = [c for c in selected if c in CATEGORIES]
    if code in current:
        return [c for c in current if c != code]
    if code in CATEGORIES:
        current.append(code)
    return [c for c in CATEGORIES if c in set(current)]
