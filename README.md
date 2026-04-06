# ShopTGTool

Telegram-бот магазина, подготовленный для двух режимов запуска:

- `polling` для локального запуска на компьютере
- `webhook` для бесплатного веб-хостинга вроде Koyeb

## Что уже настроено

- токен бота берётся из переменной окружения `BOT_TOKEN`
- для деплоя добавлен HTTP-сервер на `Flask`
- Telegram webhook настраивается автоматически
- если сервис запущен на Koyeb, публичный домен подхватывается автоматически через `KOYEB_PUBLIC_DOMAIN`
- база данных работает с локальным `SQLite` и с внешним `Postgres` через `DATABASE_URL`
- добавлен `Procfile` для запуска через `gunicorn`

## Локальный запуск

1. Создай виртуальное окружение:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

2. Установи зависимости:

```powershell
pip install -r requirements.txt
```

3. Задай токен и запусти бота:

```powershell
$env:BOT_TOKEN="твой_токен"
python Program.py
```

По умолчанию бот стартует в режиме `polling`.

## Деплой на Koyeb

Проще всего деплоить этот проект как `Web Service`.

### Что указать в переменных окружения

Минимум:

```text
BOT_TOKEN=твой_токен
APP_MODE=webhook
```

Для постоянного хранения данных лучше сразу добавить внешний Postgres:

```text
DATABASE_URL=postgres://user:password@host:5432/database
```

Если не указывать `WEBHOOK_BASE_URL`, на Koyeb проект сам соберёт адрес webhook из `KOYEB_PUBLIC_DOMAIN`.

### Параметры сервиса

- Builder: `buildpack`
- Port: `8000`
- Route: `/:8000`
- Health check path: `/healthz`

### Через GitHub

1. Загрузи проект в GitHub-репозиторий.
2. В Koyeb создай новый `Web Service`.
3. Подключи репозиторий.
4. Оставь builder `buildpack`.
5. Добавь переменные окружения.
6. Нажми `Deploy`.

## Важное замечание по базе

Если оставить только `SQLite`, на бесплатном хостинге данные могут сбрасываться после пересоздания инстанса. Для нормального хранения пользователей, товаров и баланса лучше использовать `DATABASE_URL` с внешним `Postgres`.

## Деплой на Render

Если Koyeb просит карту, этот проект можно поднять на Render как бесплатный `Web Service`.

### Что создать

1. Открой `Dashboard` в Render.
2. Нажми `New` -> `Web Service`.
3. Подключи GitHub-репозиторий `Tice-Gen/ShopTGTool`.
4. Выбери ветку `main`.

### Настройки сервиса

- Runtime: `Python 3`
- Build Command: `pip install -r requirements.txt`
- Start Command: `gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120 Program:app`

### Переменные окружения

Обязательно:

```text
BOT_TOKEN=твой_токен
APP_MODE=webhook
```

Render сам выдаст публичный URL и передаст его в `RENDER_EXTERNAL_URL`, бот подхватит его автоматически.

### База данных

Для сохранения данных лучше добавить `Render Postgres` и передать:

```text
DATABASE_URL=<External Database URL>
```

Если оставить только `SQLite`, данные могут сброситься после пересоздания бесплатного инстанса.
