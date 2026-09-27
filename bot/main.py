from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from bot.config import load_settings
from bot.db import Database
from bot.handlers import (
    add_source_command,
    categories_command,
    check_command,
    error_handler,
    handle_menu_text,
    help_command,
    list_sources_command,
    on_callback,
    pause_command,
    remove_source_command,
    settings_command,
    start_command,
)
from bot.knowledge import load_seed_sources, load_system_prompt
from bot.llm import LLMClient
from bot.monitor import monitor_job
from bot.security import redact_secrets

logging.basicConfig(
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        original = super().format(record)
        return redact_secrets(original)


def _install_redacting_logging() -> None:
    root = logging.getLogger()
    for handler in root.handlers:
        handler.setFormatter(
            RedactingFormatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
        )


async def _post_init(app: Application) -> None:
    # Убираем стандартный список slash-команд у кнопки Menu Telegram —
    # управление через reply-клавиатуру «Меню».
    await app.bot.delete_my_commands()
    logger.info("Reply-меню кнопок активно")


def main() -> None:
    _install_redacting_logging()
    settings = load_settings()
    system_prompt = load_system_prompt(settings.system_prompt_path)
    db = Database(settings.db_path)
    seeded = db.seed_sources(load_seed_sources(settings.sources_json_path))
    llm = LLMClient(settings, system_prompt)

    app = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .post_init(_post_init)
        .build()
    )
    app.bot_data["settings"] = settings
    app.bot_data["db"] = db
    app.bot_data["llm"] = llm

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("add_source", add_source_command))
    app.add_handler(CommandHandler("remove_source", remove_source_command))
    app.add_handler(CommandHandler("list_sources", list_sources_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("categories", categories_command))
    app.add_handler(CommandHandler("pause", pause_command))
    app.add_handler(CommandHandler("check", check_command))
    app.add_handler(CallbackQueryHandler(on_callback))
    # Свободный текст и кнопки reply-клавиатуры (не slash-команды)
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu_text),
        group=0,
    )
    app.add_error_handler(error_handler)

    if app.job_queue is not None:
        app.job_queue.run_repeating(
            monitor_job,
            interval=settings.monitor_interval_seconds,
            first=15,
            name="news_monitor",
        )
    else:
        logger.warning(
            "JobQueue недоступен. Установите: pip install \"python-telegram-bot[job-queue]\". "
            "Фоновый мониторинг отключён — используйте /check."
        )

    logger.info(
        "Бот запущен. model=%s sources_seeded=%s interval=%ss db=%s",
        settings.proxyapi_model,
        seeded,
        settings.monitor_interval_seconds,
        settings.db_path.name,
    )
    app.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=True)


if __name__ == "__main__":
    main()
