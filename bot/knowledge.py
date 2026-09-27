from __future__ import annotations

import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


def load_system_prompt(path: Path) -> str:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise RuntimeError(f"Пустой системный промпт: {path}")
    return text


def load_seed_sources(path: Path) -> list[dict]:
    if not path.exists():
        logger.warning("Файл источников не найден: %s", path)
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    sources = data.get("news_sources") if isinstance(data, dict) else data
    if not isinstance(sources, list):
        return []
    return [s for s in sources if isinstance(s, dict)]


def extract_json_payload(raw: str) -> dict | list:
    text = (raw or "").strip()
    text = _FENCE_RE.sub("", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start_obj = text.find("{")
        start_arr = text.find("[")
        candidates = [i for i in (start_obj, start_arr) if i >= 0]
        if not candidates:
            raise
        start = min(candidates)
        end_obj = text.rfind("}")
        end_arr = text.rfind("]")
        end = max(end_obj, end_arr)
        if end <= start:
            raise
        return json.loads(text[start : end + 1])
