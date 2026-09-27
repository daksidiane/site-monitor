from __future__ import annotations

import re

from bot.security import CATEGORIES

# Простые русские формулировки → действие бота
_CATEGORY_ALIASES: dict[str, tuple[str, ...]] = {
    "general": ("главн", "общие", "general"),
    "tech": ("технолог", "tech", "айти", "it ", " гаджет"),
    "business": ("бизнес", "финанс", "business"),
    "science": ("наук", "science"),
    "politics": ("политик", "politics"),
    "sports": ("спорт", "sports"),
    "culture": ("культур", "culture"),
}


def _norm(text: str) -> str:
    text = (text or "").lower().replace("ё", "е").strip()
    text = re.sub(r"\s+", " ", text)
    return text


def looks_like_url(text: str) -> bool:
    raw = (text or "").strip()
    if raw.startswith(("http://", "https://")):
        return True
    # короткое «домен.зона» без пробелов
    if " " not in raw and re.search(r"\.[a-zа-я]{2,}", raw, re.IGNORECASE):
        return True
    return False


def detect_intent(text: str) -> dict[str, str | None]:
    """
    Распознаёт свободный запрос.
    Возвращает {"action": ..., "category": ...|None}.
    """
    t = _norm(text)
    if not t:
        return {"action": None, "category": None}

    if t in {"меню", "menu", "открыть меню", "показать меню"}:
        return {"action": "menu", "category": None}

    if any(w in t for w in ("справк", "помощ", "что умеешь", "как польз", "help")):
        return {"action": "help", "category": None}

    if any(w in t for w in ("источник", "подписк на сайт", "список сайт")):
        if any(w in t for w in ("добав", "добавь", "новый")):
            return {"action": "add_source", "category": None}
        if any(w in t for w in ("удал", "убер", "убери", "отключ")):
            return {"action": "remove_source", "category": None}
        return {"action": "list_sources", "category": None}

    if any(w in t for w in ("уведомлен", "рассылк", "частот", "настрой")):
        return {"action": "settings", "category": None}

    if any(w in t for w in ("пауз", "приостанов", "возобнов", "останови рассыл", "включи рассыл")):
        return {"action": "pause", "category": None}

    # Темы / категории
    mentions_topics = any(w in t for w in ("категор", "тем ", "темы", "тему", "тематик"))
    select_all = ("все" in t or "всё" in t) and any(
        w in t for w in ("категор", "тем", "выбер", "включ", "добав")
    )
    clear_all = any(w in t for w in ("снять все", "убрать все", "очист", "никаких тем", "без тем"))

    if select_all or ("выбер" in t and "все" in t and mentions_topics):
        return {"action": "categories_all", "category": None}
    if clear_all:
        return {"action": "categories_none", "category": None}

    # «добавь технологии» / «убери спорт»
    for code, aliases in _CATEGORY_ALIASES.items():
        if any(a.strip() in t for a in aliases):
            if any(w in t for w in ("убер", "удал", "отключ", "сними", "без ")):
                return {"action": "category_off", "category": code}
            if any(w in t for w in ("добав", "включ", "выбер", "хочу", "нужн")) or mentions_topics:
                return {"action": "category_on", "category": code}

    if mentions_topics or any(w in t for w in ("выбер тем", "мои тем", "интересн")):
        return {"action": "categories", "category": None}

    if any(w in t for w in ("добав источник", "добавь источник", "новый источник")):
        return {"action": "add_source", "category": None}

    return {"action": None, "category": None}


def category_title(code: str) -> str:
    return CATEGORIES.get(code, code)
