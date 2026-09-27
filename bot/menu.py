from __future__ import annotations

from telegram import KeyboardButton, ReplyKeyboardMarkup

# Кнопка открытия/сворачивания панели
BTN_MENU = "Меню"

# CTA действий — понятные формулировки, не названия команд
BTN_SOURCES = "Показать источники"
BTN_ADD = "Добавить источник"
BTN_REMOVE = "Удалить источник"
BTN_SETTINGS = "Настроить уведомления"
BTN_CATEGORIES = "Выбрать темы"
BTN_PAUSE = "Приостановить рассылку"
BTN_HELP = "Как пользоваться"

PANEL_BUTTONS = frozenset(
    {
        BTN_SOURCES,
        BTN_ADD,
        BTN_REMOVE,
        BTN_SETTINGS,
        BTN_CATEGORIES,
        BTN_PAUSE,
        BTN_HELP,
    }
)


def collapsed_keyboard() -> ReplyKeyboardMarkup:
    """Свёрнуто: только кнопка «Меню»."""
    return ReplyKeyboardMarkup(
        [[KeyboardButton(BTN_MENU)]],
        resize_keyboard=True,
        is_persistent=False,
        input_field_placeholder="Откройте меню",
    )


def expanded_keyboard() -> ReplyKeyboardMarkup:
    """Развёрнуто: CTA-действия + «Меню» для сворачивания."""
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(BTN_SOURCES)],
            [KeyboardButton(BTN_ADD), KeyboardButton(BTN_REMOVE)],
            [KeyboardButton(BTN_SETTINGS), KeyboardButton(BTN_CATEGORIES)],
            [KeyboardButton(BTN_PAUSE), KeyboardButton(BTN_HELP)],
            [KeyboardButton(BTN_MENU)],
        ],
        resize_keyboard=True,
        is_persistent=False,
        input_field_placeholder="Выберите действие",
    )


def keyboard_for_state(expanded: bool) -> ReplyKeyboardMarkup:
    return expanded_keyboard() if expanded else collapsed_keyboard()
