# Reddit Scraper → Telegram Bot

Автоматически парсит топ-посты Reddit и публикует их в Telegram-канал с полной поддержкой медиа (картинки, видео, галереи, текст).

[Read in English](README.md)

## Возможности

- Парсинг топ-постов Reddit через публичные Atom-фиды (`www.reddit.com/.rss`)
- Поддержка всех типов медиа: картинки, видео, текстовые посты, ссылки
- Дедупликация — один пост не публикуется дважды
- Фильтрация NSFW контента
- Автоматический повтор при ошибках сети
- Запуск в Docker на своём сервере, HTTPS через Caddy и Let's Encrypt

## Быстрый старт

### Docker (рекомендуется)

```bash
git clone https://github.com/your_username/reddit_scrapper.git
cd reddit_scrapper
cp .env.example .env
# Заполни .env своими токенами
docker compose up -d
```

### Тестовый контур

Тот же стек, но с тестовым ботом и тестовым каналом из `.env.local`, без туннеля:

```bash
docker compose -f docker-compose.yml -f docker-compose.test.yml up -d bot
```

### Вручную

```bash
git clone https://github.com/your_username/reddit_scrapper.git
cd reddit_scrapper
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
# Заполни .env своими токенами
python -m src.main
```

## Хостинг на сервере

Прод живёт на сервере Hetzner в `/opt/reddit_scrapper`:

```bash
docker compose --profile server up -d --build
```

Профиль `server` добавляет к боту и базе контейнер Caddy: он занимает порты 80 и 443, сам
получает сертификат Let's Encrypt и проксирует запросы в веб-приложение. Без профиля
поднимаются только бот и база — так стек запускают локально, не трогая порты и сертификаты.

Ссылка «AI-объяснение» под каждым постом ведёт на `https://t.me/<бот>?startapp=<id поста>` —
Telegram Mini App, который открывает веб-приложение бота, поэтому ему нужен публичный HTTPS-адрес.
Домена у сервера нет, и он не нужен: `46-62-193-217.sslip.io` резолвится в IP сервера сам по себе,
и Let's Encrypt выдаёт на это имя обычный сертификат. Адрес задаётся в `.env` переменной
`PUBLIC_HOST`, его же нужно указать в @BotFather → Bot Settings → Configure Mini App.

Сам бот слушает только петлевой интерфейс (`127.0.0.1:8080`) — снаружи доступен лишь Caddy.

## Конфигурация

Скопируй `.env.example` в `.env` и заполни значения:

| Переменная | Описание | По умолчанию |
|------------|----------|--------------|
| `TELEGRAM_BOT_TOKEN` | Токен бота от @BotFather | обязательно |
| `TELEGRAM_CHAT_ID` | ID канала (например `-1001234567890`) | обязательно |
| `REDDIT_USER_AGENT` | User-Agent для запросов к Reddit | `reddit-scrapper/0.1` |
| `SCRAPE_INTERVAL` | Секунды между парсингами | `1200` (20 мин) |
| `POSTS_LIMIT` | Максимум постов за запрос | `50` |
| `SKIP_NSFW` | Пропускать NSFW посты | `true` |
| `PAUSE_BETWEEN_POSTS` | Секунды между сообщениями в Telegram | `3.0` |
| `DATABASE_URL` | Строка подключения к Postgres | обязательно |
| `GEMINI_API_KEY` | Ключ Gemini для AI-объяснений | без него ссылка не показывается |
| `PUBLIC_HOST` | Публичное имя для Mini App (нужно профилю `server`) | — |

## Разработка

```bash
pip install -e ".[dev]"

# Линтер
ruff check .
ruff format .

# Тесты
pytest
```

### Ветки

- `main` — продакшн, деплой через pull и рестарт compose-стека
- `feature/*` — фича-ветки, мержатся в `main` через PR
- `fix/*` — баг-фиксы, мержатся в `main` через PR

## Лицензия

[MIT](LICENSE) © 2026 Alexander Grenkov
