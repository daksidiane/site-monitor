# Мониторинг сайтов и лент

Проблема бизнеса: Специалист тратил по несколько часов в день на просмотр сайтов и чатов. Важные новости терялись в рекламе.

Бот обходит список источников, отсекает рекламу и ненужные темы и присылает в Telegram короткую выжимку со ссылкой на оригинал.

Заказчик получает новости по своему профилю без информационного шума.

## Установка и запуск

Нужны Python 3.10+ и токен бота от [@BotFather](https://t.me/BotFather).

```cmd
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
```

В `.env` заполните `TELEGRAM_BOT_TOKEN` и, если бот делает выжимку через внешнюю модель, `PROXYAPI_KEY`. В `ADMIN_TELEGRAM_IDS` впишите свой Telegram ID — иначе источники добавлять нельзя.

Запуск:

```cmd
run.bat
```

Или:

```cmd
.venv\Scripts\python.exe -m bot.main
```

Интервал проверки задаётся в `.env`: `MONITOR_INTERVAL_SECONDS` (по умолчанию 300 секунд).

## Как пользоваться

1. Откройте бота в Telegram, команда `/start`.
2. Добавьте сайт или ленту: `/add_source URL` или кнопка «Добавить источник».
3. Список: `/list_sources`. Удаление: `/remove_source URL`.
4. Новая публикация приходит сообщением: заголовок, два-три предложения, ссылка.

Для локальной проверки можно включить `ALLOW_LOCALHOST_SOURCES=1` и добавить тестовую ленту. На сервере это поле оставьте пустым.

## Лицензия

Все права принадлежат Дарье Данилко. Смотреть репозиторий можно. Копировать, менять и использовать код в продукте — только с письменного разрешения. Полный текст: файл [LICENSE](LICENSE).

## Контакты

- Telegram: [https://t.me/Dariodora](https://t.me/Dariodora)
- Почта: [daksidiane@gmail.com](mailto:daksidiane@gmail.com)
- LinkedIn: [https://www.linkedin.com/in/daria-danilko-8b7a081b5](https://www.linkedin.com/in/daria-danilko-8b7a081b5)
