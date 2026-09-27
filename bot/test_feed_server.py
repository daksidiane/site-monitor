from __future__ import annotations

"""
Локальный тестовый новостной сайт для проверки уведомлений бота.

Запуск:
  .venv\\Scripts\\python.exe -m bot.test_feed_server

Откройте http://127.0.0.1:8765/
Добавьте источник в боте: http://127.0.0.1:8765/rss
В .env: ALLOW_LOCALHOST_SOURCES=1
"""

import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "data" / "test_news.json"
HOST = "127.0.0.1"
PORT = 8765
BASE = f"http://{HOST}:{PORT}"

_lock = threading.Lock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _load() -> list[dict]:
    if not DATA_PATH.exists():
        return []
    try:
        data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(items: list[dict]) -> None:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(
        json.dumps(items, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _to_rss(items: list[dict]) -> str:
    def esc(text: str) -> str:
        return (
            (text or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    rows = []
    for item in items[:50]:
        link = esc(str(item.get("url") or f"{BASE}/#news-{item.get('id')}"))
        rows.append(
            f"""
    <item>
      <title>{esc(str(item.get('title') or ''))}</title>
      <link>{link}</link>
      <guid isPermaLink=\"false\">{esc(str(item.get('id') or link))}</guid>
      <description>{esc(str(item.get('description') or ''))}</description>
      <category>{esc(str(item.get('category') or 'general'))}</category>
      <pubDate>{esc(str(item.get('published_at') or ''))}</pubDate>
    </item>"""
        )
    return f"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<rss version=\"2.0\">
  <channel>
    <title>Локальный тестовый фид новостей</title>
    <link>{BASE}/</link>
    <description>Фид для проверки Telegram-бота</description>
    {''.join(rows)}
  </channel>
</rss>
"""


def _page_html() -> str:
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Тестовые новости для бота</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; background: #f5f5f5; }}
    .card {{ background: #fff; padding: 16px; border-radius: 8px; margin-bottom: 16px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
    input, textarea, select, button {{ width: 100%; padding: 10px; margin: 6px 0 12px; box-sizing: border-box; }}
    button {{ background: #2f6fed; color: #fff; border: 0; border-radius: 6px; cursor: pointer; }}
    button.secondary {{ background: #5a6a85; }}
    .actions {{ display: flex; gap: 10px; }}
    .actions button {{ flex: 1; }}
    .news-item {{ border-bottom: 1px solid #eee; padding: 12px 0; }}
    .meta {{ color: #888; font-size: 12px; }}
    code {{ background: #eee; padding: 2px 6px; border-radius: 4px; }}
  </style>
</head>
<body>
  <h1>Тестовый новостной сайт</h1>
  <div class="card">
    <p>Для бота добавьте источник: <code>{BASE}/rss</code></p>
    <p>JSON: <code>{BASE}/news.json</code></p>
  </div>
  <div class="card">
    <h2>Добавить новость</h2>
    <form id="form">
      <label>Заголовок</label>
      <input id="title" required />
      <label>Описание</label>
      <textarea id="description" required></textarea>
      <label>Категория</label>
      <select id="category">
        <option value="general">general</option>
        <option value="tech">tech</option>
        <option value="business">business</option>
        <option value="science">science</option>
        <option value="sports">sports</option>
        <option value="politics">politics</option>
        <option value="culture">culture</option>
      </select>
      <div class="actions">
        <button type="button" class="secondary" id="randomBtn">Опубликовать случайную</button>
        <button type="submit">Опубликовать</button>
      </div>
    </form>
  </div>
  <div class="card">
    <h2>Последние новости</h2>
    <div id="list"></div>
  </div>
  <script>
    const listEl = document.getElementById('list');
    const SAMPLES = {{
      general: [
        {{
          title: 'В столице открыли новый общественный парк у набережной',
          description: 'Горожане уже гуляют по дорожкам и смотровым площадкам. Власти обещают до конца сезона запустить кафе и детские зоны.'
        }},
        {{
          title: 'Сильный ливень затопил несколько улиц в центре города',
          description: 'Коммунальные службы откачивают воду и перекрыли проблемные участки. Движение общественного транспорта временно изменено.'
        }},
      ],
      tech: [
        {{
          title: 'Стартап представил ИИ-ассистента для редактирования кода',
          description: 'Новый инструмент предлагает правки прямо в IDE и объясняет изменения. Первые пользователи отмечают ускорение ревью на треть.'
        }},
        {{
          title: 'Крупный вендор выпустил смартфон с камерой 200 МП',
          description: 'Флагман получил обновлённый процессор и спутниковую связь. Старт продаж запланирован на следующий месяц.'
        }},
      ],
      business: [
        {{
          title: 'Ритейлер отчитался о росте выручки на 18% за квартал',
          description: 'Компания связывает результат с онлайн-каналом и программой лояльности. Аналитики повысили целевую цену акций.'
        }},
        {{
          title: 'Банк снизил ставку по ипотеке для семей с детьми',
          description: 'Новые условия действуют с понедельника. Минимальный первоначальный взнос также уменьшен для части программ.'
        }},
      ],
      science: [
        {{
          title: 'Астрономы подтвердили атмосферу у экзопланеты в зоне обитаемости',
          description: 'Наблюдения телескопа показали следы водяного пара. Учёные планируют дополнительные спектроскопические измерения.'
        }},
        {{
          title: 'Биологи нашли способ ускорить восстановление тканей после травм',
          description: 'В экспериментах на клеточных культурах заживление шло заметно быстрее. Клинические испытания ещё не начаты.'
        }},
      ],
      sports: [
        {{
          title: 'Сборная вышла в финал чемпионата после серии пенальти',
          description: 'Основное время матча закончилось вничью. Решающий удар реализовал капитан команды на последней попытке.'
        }},
        {{
          title: 'Городской марафон собрал рекордные 25 тысяч участников',
          description: 'Победитель преодолел дистанцию быстрее прошлогоднего результата. Организаторы уже анонсировали следующий забег.'
        }},
      ],
      politics: [
        {{
          title: 'Парламент одобрил пакет поправок к закону о цифровых услугах',
          description: 'Документ ужесточает требования к прозрачности платформ. Закон вступит в силу через три месяца после публикации.'
        }},
        {{
          title: 'Главы регионов обсудили единый план развития транспорта',
          description: 'На встрече согласовали приоритетные маршруты и сроки финансирования. Итоговый протокол подпишут на следующей неделе.'
        }},
      ],
      culture: [
        {{
          title: 'В музее открылась выставка современного искусства Восточной Европы',
          description: 'Экспозиция включает живопись, видеоарт и инсталляции тридцати авторов. Билеты уже доступны онлайн.'
        }},
        {{
          title: 'Городской оркестр даст бесплатный концерт под открытым небом',
          description: 'В программе — классика и современные обработки. Мероприятие пройдёт в субботу на главной площади.'
        }},
      ],
    }};

    function pickRandomNews() {{
      const categories = Object.keys(SAMPLES);
      const category = categories[Math.floor(Math.random() * categories.length)];
      const options = SAMPLES[category];
      const sample = options[Math.floor(Math.random() * options.length)];
      return {{
        title: sample.title,
        description: sample.description,
        category,
      }};
    }}

    async function publishNews(payload) {{
      const res = await fetch('/api/news', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify(payload),
      }});
      if (!res.ok) {{
        throw new Error('publish failed');
      }}
      return res.json();
    }}

    async function loadNews() {{
      const res = await fetch('/news.json');
      const news = await res.json();
      if (!news.length) {{
        listEl.innerHTML = '<p>Пока пусто — добавьте новость.</p>';
        return;
      }}
      const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (ch) => ({{
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
      }}[ch]));
      listEl.innerHTML = news.map(n => {{
        const rawUrl = String(n.url || '');
        const href = /^https?:\\/\\//i.test(rawUrl) ? esc(rawUrl) : '#';
        return `
        <article class="news-item">
          <h3>${{esc(n.title)}}</h3>
          <p>${{esc(n.description || '')}}</p>
          <div class="meta">${{esc(n.category)}} · ${{esc(n.published_at)}} · <a href="${{href}}">${{esc(rawUrl)}}</a></div>
        </article>
      `;
      }}).join('');
    }}

    document.getElementById('randomBtn').addEventListener('click', async () => {{
      const payload = pickRandomNews();
      document.getElementById('category').value = payload.category;
      document.getElementById('title').value = payload.title;
      document.getElementById('description').value = payload.description;
      try {{
        await publishNews(payload);
        await loadNews();
        alert('Случайная новость опубликована. В боте нажмите /check.');
      }} catch (err) {{
        alert('Ошибка публикации');
      }}
    }});

    document.getElementById('form').addEventListener('submit', async (e) => {{
      e.preventDefault();
      const payload = {{
        title: document.getElementById('title').value,
        description: document.getElementById('description').value,
        category: document.getElementById('category').value,
      }};
      try {{
        await publishNews(payload);
        e.target.reset();
        await loadNews();
        alert('Новость сохранена на сервере. В боте нажмите /check.');
      }} catch (err) {{
        alert('Ошибка публикации');
      }}
    }});
    loadNews();
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        print(f"[test-feed] {self.address_string()} {fmt % args}")

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, payload: object) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(code, raw, "application/json; charset=utf-8")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in {"/", "/index.html"}:
            self._send(200, _page_html().encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/news.json":
            with _lock:
                items = _load()
            self._send_json(200, items)
            return
        if path == "/rss":
            with _lock:
                items = _load()
            self._send(200, _to_rss(items).encode("utf-8"), "application/rss+xml; charset=utf-8")
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path != "/api/news":
            self._send_json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > 8192:
            self._send_json(413, {"error": "body too large"})
            return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            self._send_json(400, {"error": "invalid json"})
            return
        title = str(payload.get("title") or "").strip()
        description = str(payload.get("description") or "").strip()
        category = str(payload.get("category") or "general").strip() or "general"
        if not title:
            self._send_json(400, {"error": "title required"})
            return
        news_id = str(int(datetime.now(timezone.utc).timestamp() * 1000))
        item = {
            "id": news_id,
            "title": title,
            "description": description,
            "category": category,
            "url": f"{BASE}/#news-{news_id}",
            "published_at": _utc_now(),
        }
        with _lock:
            items = _load()
            items.insert(0, item)
            _save(items)
        self._send_json(201, item)


def main() -> None:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not DATA_PATH.exists():
        _save([])
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Тестовый сайт: {BASE}/")
    print(f"RSS для бота:  {BASE}/rss")
    print("Остановка: Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлен")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
