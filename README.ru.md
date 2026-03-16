# Reddit Scraper → Telegram Bot

Автоматически парсит топ-посты Reddit и публикует их в Telegram-канал с полной поддержкой медиа (картинки, видео, галереи, текст).

[Read in English](README.md)

## Возможности

- Парсинг топ-постов Reddit каждые 20 минут через публичный JSON API
- Поддержка всех типов медиа: картинки, галереи, видео, текстовые посты, ссылки
- Дедупликация — один пост не публикуется дважды
- Фильтрация NSFW контента
- Автоматический повтор при ошибках сети
- Запуск в Docker, автодеплой при пуше в `main`

## Быстрый старт

### Docker (рекомендуется)

```bash
git clone https://github.com/your_username/reddit_scrapper.git
cd reddit_scrapper
cp .env.example .env
# Заполни .env своими токенами
docker compose up -d
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

- `main` — продакшн, автодеплой при merge
- `feature/*` — фича-ветки, мержатся в `main` через PR
- `fix/*` — баг-фиксы, мержатся в `main` через PR

## Лицензия

[MIT](LICENSE) © 2026 Alexander Grenkov
