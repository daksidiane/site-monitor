@echo off
cd /d "%~dp0"
if not exist ".env" (
  echo Создайте файл .env из .env.example и заполните TELEGRAM_BOT_TOKEN и PROXYAPI_KEY.
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  echo Создайте виртуальное окружение: python -m venv .venv
  echo Затем: .venv\Scripts\pip install -r requirements.txt
  exit /b 1
)
".venv\Scripts\python.exe" -m bot.main
