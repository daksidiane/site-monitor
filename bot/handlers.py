from __future__ import annotations

import logging
import time
from collections import defaultdict
from urllib.parse import urlparse

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from bot.config import Settings
from bot.db import Database
from bot.formatters import (
    categories_inline_keyboard,
    format_categories_status,
    format_sources_list,
    parse_categories_arg,
    subscriber_categories,
    toggle_category,
)
from bot.intents import category_title, detect_intent, looks_like_url
from bot.llm import LLMClient
from bot.menu import (
    BTN_ADD,
    BTN_CATEGORIES,
    BTN_HELP,
    BTN_MENU,
    BTN_PAUSE,
    BTN_REMOVE,
    BTN_SETTINGS,
    BTN_SOURCES,
    PANEL_BUTTONS,
    keyboard_for_state,
)
from bot.monitor import deliver_pending, process_sources
from bot.parsers import detect_source_type, fetch_source_news
from bot.security import (
    CATEGORIES,
    html_escape,
    is_safe_http_url,
    strip_url_userinfo,
    redact_secrets,
    sanitize_text,
    unsafe_http_url_message,
)

logger = logging.getLogger(__name__)

_last_command_at: dict[int, float] = defaultdict(float)
_MENU_STATE_KEY = "menu_expanded"
_AWAIT_URL_KEY = "await_url_action"


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data["settings"]


def _db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["db"]


def _llm(context: ContextTypes.DEFAULT_TYPE) -> LLMClient:
    return context.application.bot_data["llm"]


def _is_admin(user_id: int, settings: Settings) -> bool:
    if not settings.admin_telegram_ids:
        return False
    return user_id in settings.admin_telegram_ids


async def _deny_unless_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    settings = _settings(context)
    user = update.effective_user
    if user and _is_admin(user.id, settings):
        return True
    if update.message:
        text = (
            "Это действие только для администратора. "
            "Укажите свой Telegram ID в ADMIN_TELEGRAM_IDS и перезапустите бота."
            if not settings.admin_telegram_ids
            else "Команда только для администратора."
        )
        await update.message.reply_text(text, reply_markup=_menu_markup(context))
    return False


def _rate_limited(user_id: int, settings: Settings) -> bool:
    now = time.monotonic()
    last = _last_command_at[user_id]
    if now - last < settings.rate_limit_seconds:
        return True
    _last_command_at[user_id] = now
    return False


def _menu_expanded(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return bool(context.user_data.get(_MENU_STATE_KEY, False))


def _set_menu_expanded(context: ContextTypes.DEFAULT_TYPE, expanded: bool) -> None:
    context.user_data[_MENU_STATE_KEY] = expanded


def _menu_markup(context: ContextTypes.DEFAULT_TYPE):
    return keyboard_for_state(_menu_expanded(context))


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    user = update.effective_user
    settings = _settings(context)
    if _rate_limited(user.id, settings):
        return

    _db(context).upsert_subscriber(
        telegram_id=user.id,
        username=user.username,
        default_categories=list(CATEGORIES.keys()),
    )
    _set_menu_expanded(context, False)
    context.user_data.pop(_AWAIT_URL_KEY, None)
    text = (
        "Мониторинг новостных сайтов.\n\n"
        "Бот проверяет источники, делает краткое саммари и присылает новые публикации.\n\n"
        "Кнопка «Меню» внизу открывает действия. Нажмите её ещё раз — панель свернётся."
    )
    await update.message.reply_text(text, reply_markup=_menu_markup(context))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    text = (
        "<b>Команды бота мониторинга новостей</b>\n"
        "/start — регистрация и приветствие\n"
        "/add_source [URL] — добавление источника новостей\n"
        "/remove_source [URL] — удаление источника\n"
        "/list_sources — список активных источников\n"
        "/settings — настройки уведомлений\n"
        "/categories — выбор интересующих категорий\n"
        "/pause — временная приостановка уведомлений\n"
        "/help — справка по командам\n\n"
        "Или нажмите кнопку <b>Меню</b> внизу экрана."
    )
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=_menu_markup(context),
    )


async def list_sources_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    if _rate_limited(update.effective_user.id, _settings(context)):
        await update.message.reply_text(
            "Слишком часто. Подождите секунду.",
            reply_markup=_menu_markup(context),
        )
        return
    sources = _db(context).list_sources(active_only=True)
    await update.message.reply_text(
        format_sources_list(sources),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=_menu_markup(context),
    )


async def _add_source_from_url(
    update: Update, context: ContextTypes.DEFAULT_TYPE, raw: str
) -> None:
    if not await _deny_unless_admin(update, context):
        return
    settings = _settings(context)
    if not is_safe_http_url(raw, allow_localhost=settings.allow_localhost_sources):
        await update.message.reply_text(
            unsafe_http_url_message(
                raw, allow_localhost=settings.allow_localhost_sources
            ),
            reply_markup=_menu_markup(context),
        )
        return

    db = _db(context)
    active = db.list_sources(active_only=True)
    if len(active) >= settings.max_sources_per_user and not any(
        str(s["url"]) == raw or str(s["rss_feed"] or "") == raw for s in active
    ):
        await update.message.reply_text(
            f"Достигнут лимит активных источников ({settings.max_sources_per_user}).",
            reply_markup=_menu_markup(context),
        )
        return

    await update.message.reply_text(
        "Проверяю источник…",
        reply_markup=_menu_markup(context),
    )

    source_type, rss_feed, parsing_config = detect_source_type(raw)
    probe_source = {
        "name": urlparse(raw).hostname or "источник",
        "url": raw,
        "source_type": source_type,
        "rss_feed": rss_feed,
        "parsing_config": parsing_config,
        "category": "general",
        "language": "ru",
    }
    outcome = await fetch_source_news(
        probe_source,
        timeout=settings.fetch_timeout_seconds,
        limit=min(5, settings.max_news_per_source),
        allow_localhost=settings.allow_localhost_sources,
    )

    if outcome.status != "ok" or not outcome.items:
        await update.message.reply_text(
            "Источник не добавлен: бот не смог прочитать новости.\n\n"
            f"Причина: {outcome.message}\n\n"
            "Что обычно помогает:\n"
            "• прямая ссылка на RSS/Atom (…/rss, …/feed, …/atom.xml)\n"
            "• публичный JSON API новостей\n"
            "• страница без авторизации и с серверным HTML/фидом",
            reply_markup=_menu_markup(context),
        )
        return

    # Уточняем тип по тому, что реально сработало
    used = (outcome.used_url or raw).lower()
    if used.endswith(".json") or "/api/" in used:
        source_type = "json"
        rss_feed = outcome.used_url
    elif any(x in used for x in ("rss", "atom", "feed", ".xml")):
        source_type = "rss"
        rss_feed = outcome.used_url
    else:
        source_type = "html"
        rss_feed = outcome.used_url if outcome.used_url != raw else rss_feed

    host = urlparse(raw).hostname or "источник"
    source_id = db.add_source(
        name=host,
        url=raw,
        source_type=source_type,
        rss_feed=rss_feed,
        category="general",
        parsing_config=parsing_config,
    )
    db.update_source_health(
        source_id,
        status=outcome.status,
        message=outcome.message,
        items_count=len(outcome.items),
        feed_url=outcome.used_url,
    )
    await update.message.reply_text(
        f"Источник добавлен (id={source_id}, type={source_type}).\n"
        f"Проверка OK: найдено {len(outcome.items)} записей.\n"
        f"Рабочий адрес: {strip_url_userinfo(outcome.used_url or raw)}",
        reply_markup=_menu_markup(context),
    )


async def add_source_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    settings = _settings(context)
    user_id = update.effective_user.id
    if not await _deny_unless_admin(update, context):
        return
    if _rate_limited(user_id, settings):
        await update.message.reply_text(
            "Слишком часто. Подождите секунду.",
            reply_markup=_menu_markup(context),
        )
        return

    raw = sanitize_text(" ".join(context.args or []), settings.max_user_message_chars)
    if not raw:
        context.user_data[_AWAIT_URL_KEY] = "add"
        await update.message.reply_text(
            "Пришлите ссылку на источник новостей (http/https).",
            reply_markup=_menu_markup(context),
        )
        return
    context.user_data.pop(_AWAIT_URL_KEY, None)
    await _add_source_from_url(update, context, raw)


async def remove_source_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    settings = _settings(context)
    user_id = update.effective_user.id
    if _rate_limited(user_id, settings):
        await update.message.reply_text(
            "Слишком часто. Подождите секунду.",
            reply_markup=_menu_markup(context),
        )
        return

    if not await _deny_unless_admin(update, context):
        return
    raw = sanitize_text(" ".join(context.args or []), settings.max_user_message_chars)
    if not raw:
        context.user_data[_AWAIT_URL_KEY] = "remove"
        await update.message.reply_text(
            "Пришлите ссылку источника, который нужно удалить.",
            reply_markup=_menu_markup(context),
        )
        return
    if not is_safe_http_url(raw, allow_localhost=settings.allow_localhost_sources):
        await update.message.reply_text(
            unsafe_http_url_message(
                raw, allow_localhost=settings.allow_localhost_sources
            ),
            reply_markup=_menu_markup(context),
        )
        return
    context.user_data.pop(_AWAIT_URL_KEY, None)
    ok = _db(context).deactivate_source_by_url(raw)
    await update.message.reply_text(
        "Источник удалён." if ok else "Источник не найден.",
        reply_markup=_menu_markup(context),
    )


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    db = _db(context)
    db.upsert_subscriber(
        telegram_id=update.effective_user.id,
        username=update.effective_user.username,
        default_categories=list(CATEGORIES.keys()),
    )
    sub = db.get_subscriber(update.effective_user.id)
    mode = sub["notify_mode"] if sub else "instant"
    paused = bool(sub["is_paused"]) if sub else False
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Мгновенно", callback_data="settings:instant"),
                InlineKeyboardButton("Раз в час", callback_data="settings:hourly"),
            ],
            [
                InlineKeyboardButton("Раз в день", callback_data="settings:daily"),
            ],
        ]
    )
    # Сначала возвращаем reply-меню, затем inline-выбор режима
    await update.message.reply_text(
        f"Режим уведомлений: <b>{html_escape(str(mode))}</b>\nПауза: <b>{'да' if paused else 'нет'}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=_menu_markup(context),
    )
    await update.message.reply_text(
        "Выберите новый режим:",
        reply_markup=keyboard,
    )


async def categories_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    db = _db(context)
    db.upsert_subscriber(
        telegram_id=update.effective_user.id,
        username=update.effective_user.username,
        default_categories=list(CATEGORIES.keys()),
    )
    args = sanitize_text(" ".join(context.args or []), 200)
    if args:
        cats = parse_categories_arg(args)
        if not cats:
            await update.message.reply_text(
                "Не распознал темы. Откройте «Выбрать темы» и нажмите на нужные.",
                reply_markup=_menu_markup(context),
            )
            return
        db.set_categories(update.effective_user.id, cats)
        await update.message.reply_text(
            "Темы обновлены: " + ", ".join(CATEGORIES[c] for c in cats),
            reply_markup=_menu_markup(context),
        )
        return

    sub = db.get_subscriber(update.effective_user.id)
    selected = subscriber_categories(sub)
    await update.message.reply_text(
        "Управление темами рассылки:",
        reply_markup=_menu_markup(context),
    )
    await update.message.reply_text(
        format_categories_status(selected),
        parse_mode=ParseMode.HTML,
        reply_markup=categories_inline_keyboard(selected),
    )


async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    db = _db(context)
    db.upsert_subscriber(
        telegram_id=update.effective_user.id,
        username=update.effective_user.username,
        default_categories=list(CATEGORIES.keys()),
    )
    sub = db.get_subscriber(update.effective_user.id)
    paused = bool(sub["is_paused"]) if sub else False
    db.set_paused(update.effective_user.id, not paused)
    await update.message.reply_text(
        "Уведомления на паузе." if not paused else "Уведомления возобновлены.",
        reply_markup=_menu_markup(context),
    )


async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    settings = _settings(context)
    if not _is_admin(update.effective_user.id, settings):
        await update.message.reply_text(
            "Команда только для админа.",
            reply_markup=_menu_markup(context),
        )
        return
    if _rate_limited(update.effective_user.id, settings):
        await update.message.reply_text(
            "Слишком часто. Подождите секунду.",
            reply_markup=_menu_markup(context),
        )
        return

    db = _db(context)
    sub = db.get_subscriber(update.effective_user.id)
    selected = subscriber_categories(sub) if sub else []
    mode = (sub["notify_mode"] if sub else "instant") or "instant"
    paused = bool(sub["is_paused"]) if sub else False

    hints: list[str] = []
    if paused:
        hints.append("Рассылка на паузе — уведомления не уходят.")
    if mode != "instant":
        hints.append(f"Режим сейчас «{mode}», не instant.")
    if not selected:
        hints.append("Темы не выбраны — новости не придут.")
    elif len(selected) == 1:
        title = CATEGORIES.get(selected[0], selected[0])
        hints.append(
            f"Включена только тема «{title}». Новости других тем отфильтруются. "
            "При необходимости: «Выбрать темы» → «Выбрать все»."
        )

    await update.message.reply_text(
        "Проверяю источники и обрабатываю новости…",
        reply_markup=_menu_markup(context),
    )
    try:
        stats = await process_sources(
            db=db,
            llm=_llm(context),
            settings=settings,
        )
        delivery = await deliver_pending(context.application)
        text = (
            "Готово.\n"
            f"Собрано: {stats['fetched']}\n"
            f"Новых: {stats['new']}\n"
            f"AI OK: {stats['ai_ok']}\n"
            f"Пропущено: {stats['skipped']}\n"
            f"Ошибок: {stats['errors']}\n"
            f"Отправлено сообщений: {delivery['sent']}\n"
            f"Отфильтровано по темам: {delivery['filtered_by_category']}"
        )
        problems = stats.get("source_problems") or []
        if problems:
            text += "\n\nПроблемные источники:"
            for problem in problems[:5]:
                text += f"\n• {problem['name']}: {problem['message']}"
        if hints:
            text += "\n\nДиагностика подписки:\n- " + "\n- ".join(hints)
        await update.message.reply_text(text, reply_markup=_menu_markup(context))
    except Exception as exc:
        logger.exception("check failed")
        await update.message.reply_text(
            f"Ошибка проверки: {redact_secrets(str(exc))[:300]}",
            reply_markup=_menu_markup(context),
        )


async def toggle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    expanded = not _menu_expanded(context)
    _set_menu_expanded(context, expanded)
    context.user_data.pop(_AWAIT_URL_KEY, None)
    if expanded:
        await update.message.reply_text(
            "Выберите действие. Чтобы свернуть панель — снова нажмите «Меню».",
            reply_markup=_menu_markup(context),
        )
    else:
        await update.message.reply_text(
            "Меню свёрнуто.",
            reply_markup=_menu_markup(context),
        )


async def _apply_categories(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    selected: list[str],
    *,
    note: str,
) -> None:
    user = update.effective_user
    if not user or not update.message:
        return
    db = _db(context)
    db.upsert_subscriber(user.id, user.username, list(CATEGORIES.keys()))
    db.set_categories(user.id, selected)
    await update.message.reply_text(
        note,
        reply_markup=_menu_markup(context),
    )
    await update.message.reply_text(
        format_categories_status(selected),
        parse_mode=ParseMode.HTML,
        reply_markup=categories_inline_keyboard(selected),
    )


async def _dispatch_intent(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    action: str,
    category: str | None,
) -> bool:
    """Выполняет распознанное действие. True если обработано."""
    user = update.effective_user
    if not user or not update.message:
        return False

    if action == "menu":
        _set_menu_expanded(context, False)
        await toggle_menu(update, context)
        return True
    if action == "help":
        await help_command(update, context)
        return True
    if action == "list_sources":
        await list_sources_command(update, context)
        return True
    if action == "settings":
        await settings_command(update, context)
        return True
    if action == "pause":
        await pause_command(update, context)
        return True
    if action == "categories":
        await categories_command(update, context)
        return True
    if action == "add_source":
        context.args = []
        await add_source_command(update, context)
        return True
    if action == "remove_source":
        context.args = []
        await remove_source_command(update, context)
        return True
    if action == "categories_all":
        await _apply_categories(
            update,
            context,
            list(CATEGORIES.keys()),
            note="Включены все темы.",
        )
        return True
    if action == "categories_none":
        await _apply_categories(
            update,
            context,
            [],
            note="Все темы сняты. Рассылка тематических новостей остановлена.",
        )
        return True
    if action in {"category_on", "category_off"} and category:
        db = _db(context)
        db.upsert_subscriber(user.id, user.username, list(CATEGORIES.keys()))
        selected = subscriber_categories(db.get_subscriber(user.id))
        if action == "category_on":
            if category not in selected:
                selected = toggle_category(selected, category)
            note = f"Тема «{category_title(category)}» включена."
        else:
            if category in selected:
                selected = toggle_category(selected, category)
            note = f"Тема «{category_title(category)}» выключена."
        await _apply_categories(update, context, selected, note=note)
        return True
    return False


async def handle_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопки меню, URL и свободные запросы — всегда с ответом и клавиатурой."""
    try:
        if not update.message or not update.effective_user:
            return
        text = (update.message.text or update.message.caption or "").strip()
        if not text:
            await update.message.reply_text(
                "Напишите запрос или откройте «Меню».",
                reply_markup=_menu_markup(context),
            )
            return

        settings = _settings(context)

        # Ожидание URL: если пришла не ссылка, а обычная фраза — обрабатываем как запрос
        await_action = context.user_data.get(_AWAIT_URL_KEY)
        if await_action in {"add", "remove"}:
            if text in PANEL_BUTTONS or text == BTN_MENU:
                context.user_data.pop(_AWAIT_URL_KEY, None)
            elif looks_like_url(text):
                raw = sanitize_text(text, settings.max_user_message_chars)
                context.user_data.pop(_AWAIT_URL_KEY, None)
                if await_action == "add":
                    await _add_source_from_url(update, context, raw)
                else:
                    if not is_safe_http_url(
                        raw, allow_localhost=settings.allow_localhost_sources
                    ):
                        await update.message.reply_text(
                            unsafe_http_url_message(
                                raw,
                                allow_localhost=settings.allow_localhost_sources,
                            ),
                            reply_markup=_menu_markup(context),
                        )
                        return
                    ok = _db(context).deactivate_source_by_url(raw)
                    await update.message.reply_text(
                        "Источник удалён." if ok else "Источник не найден.",
                        reply_markup=_menu_markup(context),
                    )
                return
            else:
                # Пользователь передумал и написал обычный запрос
                context.user_data.pop(_AWAIT_URL_KEY, None)

        if text == BTN_MENU:
            await toggle_menu(update, context)
            return
        if text == BTN_SOURCES:
            await list_sources_command(update, context)
            return
        if text == BTN_SETTINGS:
            await settings_command(update, context)
            return
        if text == BTN_CATEGORIES:
            await categories_command(update, context)
            return
        if text == BTN_PAUSE:
            await pause_command(update, context)
            return
        if text == BTN_HELP:
            await help_command(update, context)
            return
        if text == BTN_ADD:
            context.args = []
            await add_source_command(update, context)
            return
        if text == BTN_REMOVE:
            context.args = []
            await remove_source_command(update, context)
            return

        intent = detect_intent(text)
        action = intent.get("action")
        category = intent.get("category")
        if isinstance(action, str) and await _dispatch_intent(
            update, context, action, category if isinstance(category, str) else None
        ):
            return

        await update.message.reply_text(
            "Понял сообщение, но не распознал действие.\n"
            "Можно написать, например: «выбери все категории», "
            "«покажи источники», «добавь технологии».\n"
            "Или откройте «Меню».",
            reply_markup=_menu_markup(context),
        )
    except Exception:
        logger.exception("Ошибка обработки сообщения")
        if update.message:
            try:
                await update.message.reply_text(
                    "Не удалось обработать сообщение. Нажмите «Меню» и попробуйте ещё раз.",
                    reply_markup=keyboard_for_state(False),
                )
            except Exception:
                logger.exception("Не удалось отправить ответ об ошибке")


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not update.effective_user:
        return
    await query.answer()
    data = query.data or ""
    user_id = update.effective_user.id
    db = _db(context)

    if data.startswith("settings:"):
        mode = data.split(":", 1)[1]
        if mode in {"instant", "hourly", "daily"}:
            db.upsert_subscriber(user_id, update.effective_user.username, list(CATEGORIES.keys()))
            db.set_notify_mode(user_id, mode)
            await query.edit_message_text(f"Режим уведомлений: {mode}")
            if query.message:
                await query.message.reply_text(
                    "Готово. Меню ниже.",
                    reply_markup=_menu_markup(context),
                )
        return

    if data.startswith("cat:"):
        action = data.split(":", 1)[1]
        db.upsert_subscriber(user_id, update.effective_user.username, list(CATEGORIES.keys()))
        sub = db.get_subscriber(user_id)
        selected = subscriber_categories(sub)

        if action == "all":
            selected = list(CATEGORIES.keys())
        elif action == "none":
            selected = []
        elif action in CATEGORIES:
            selected = toggle_category(selected, action)
        else:
            return

        db.set_categories(user_id, selected)
        await query.edit_message_text(
            format_categories_status(selected),
            parse_mode=ParseMode.HTML,
            reply_markup=categories_inline_keyboard(selected),
        )
        return


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Update error: %s", redact_secrets(str(context.error)))
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "Произошла ошибка. Нажмите «Меню» или /start.",
                reply_markup=keyboard_for_state(False),
            )
        except Exception:
            logger.debug("Не удалось отправить сообщение об ошибке", exc_info=True)
