from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT_DIR / ".env"

load_dotenv(ENV_PATH)


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    proxyapi_key: str
    proxyapi_model: str
    proxyapi_base_url: str
    admin_telegram_ids: frozenset[int]
    system_prompt_path: Path
    sources_json_path: Path
    db_path: Path
    monitor_interval_seconds: int
    llm_timeout_seconds: float = 45.0
    max_user_message_chars: int = 500
    max_sources_per_user: int = 100
    rate_limit_seconds: float = 1.5
    fetch_timeout_seconds: float = 10.0
    max_news_per_source: int = 8
    allow_localhost_sources: bool = False


def _require(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(
            f"Не задана переменная {name}. "
            f"Скопируйте .env.example в .env и заполните ключи локально."
        )
    return value


def _parse_admin_ids(raw: str) -> frozenset[int]:
    ids: set[int] = set()
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            continue
    return frozenset(ids)


def load_settings() -> Settings:
    data_dir = ROOT_DIR / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    return Settings(
        telegram_bot_token=_require("TELEGRAM_BOT_TOKEN"),
        proxyapi_key=_require("PROXYAPI_KEY"),
        proxyapi_model=os.getenv("PROXYAPI_MODEL", "qwen/qwen3.8-flash").strip(),
        proxyapi_base_url=os.getenv(
            "PROXYAPI_BASE_URL", "https://api.proxyapi.ru/v1"
        ).strip().rstrip("/"),
        admin_telegram_ids=_parse_admin_ids(os.getenv("ADMIN_TELEGRAM_IDS", "")),
        system_prompt_path=ROOT_DIR / "system_prompt.md",
        sources_json_path=ROOT_DIR / "Qwen_json_20260912_kglt6494b.json",
        db_path=data_dir / "news.db",
        monitor_interval_seconds=max(
            60, int(os.getenv("MONITOR_INTERVAL_SECONDS", "600") or "600")
        ),
        allow_localhost_sources=(
            os.getenv("ALLOW_LOCALHOST_SOURCES", "").strip().lower()
            in {"1", "true", "yes", "on"}
        ),
    )
