from __future__ import annotations

import json
import logging
from typing import Any

from openai import AsyncOpenAI

from bot.config import Settings
from bot.knowledge import extract_json_payload
from bot.security import redact_secrets

logger = logging.getLogger(__name__)


class LLMClient:
    def __init__(self, settings: Settings, system_prompt: str) -> None:
        self.settings = settings
        self.system_prompt = system_prompt
        self.client = AsyncOpenAI(
            api_key=settings.proxyapi_key,
            base_url=settings.proxyapi_base_url,
            timeout=settings.llm_timeout_seconds,
        )
        self.model = settings.proxyapi_model

    async def process_news(self, news_payload: dict[str, Any]) -> dict[str, Any]:
        user_content = json.dumps(news_payload, ensure_ascii=False)
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.system_prompt},
                    {
                        "role": "user",
                        "content": (
                            "Обработай новость и верни только JSON-объект "
                            "по формату системного промпта.\n\n"
                            f"{user_content}"
                        ),
                    },
                ],
                temperature=0.2,
                max_tokens=900,
            )
        except Exception as exc:
            logger.error("ProxyAPI error: %s", redact_secrets(str(exc)))
            raise

        choice = response.choices[0].message
        content = (choice.content or "").strip()
        if not content:
            raise RuntimeError("Пустой ответ модели")

        parsed = extract_json_payload(content)
        if isinstance(parsed, list):
            if not parsed:
                raise RuntimeError("Модель вернула пустой массив")
            parsed = parsed[0]
        if not isinstance(parsed, dict):
            raise RuntimeError("Модель вернула не JSON-объект")
        return parsed
