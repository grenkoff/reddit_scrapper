# Reddit Scraper → Telegram Bot

Автоматически парсит топ-посты Reddit и публикует их в Telegram-канал с полной поддержкой медиа (картинки, видео, галереи, текст).

[Read in English](README.md)

## Возможности

- Парсинг топ-постов Reddit через публичные Atom-фиды (`www.reddit.com/.rss`)
- Поддержка всех типов медиа: картинки, видео, текстовые посты, ссылки
- Дедупликация — один пост не публикуется дважды
- Фильтрация NSFW контента
- Автоматический повтор при ошибках сети
- Запуск в Docker на собственной машине, публичный адрес через Tailscale Funnel

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

## Хостинг со своей машины

Бот и база поднимаются одной командой `docker compose up -d`. У обоих стоит
`restart: unless-stopped`, поэтому после перезагрузки они стартуют сами.

Ссылка «AI-объяснение» под каждым постом ведёт на `https://t.me/<бот>?startapp=<id поста>` — это
Telegram Mini App, открывающий веб-приложение бота. Telegram обращается к нему снаружи, а машина за
NAT входящих соединений не принимает, поэтому веб-приложение публикуется через Tailscale Funnel: он
даёт постоянный адрес `https://<машина>.<tailnet>.ts.net` с валидным сертификатом.

Настройка один раз:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up                  # выдаст ссылку для входа в браузере
sudo tailscale funnel --bg 8080    # опубликует веб-приложение бота
tailscale funnel status            # покажет итоговый адрес
```

Конфигурация Funnel сохраняется, так что после ребута адрес остаётся прежним.

Затем в @BotFather → Bot Settings → Configure Mini App указать этот адрес (`https://<хост>/`).
Именно его открывает ссылка «AI-объяснение», поэтому адрес должен быть постоянным.

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
