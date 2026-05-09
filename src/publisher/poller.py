import asyncio
import json
import logging
from collections import OrderedDict

import httpx

from src.config import Config
from src.db import get_explanation, save_explanation
from src.explainer.gemini import generate_explanation

logger = logging.getLogger(__name__)

_LRU_CAP = 1000
_CALLBACK_PREFIX = "explain:"


class UpdatePoller:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._token = config.telegram_bot_token
        self._offset = 0
        self._post_context: OrderedDict[str, dict] = OrderedDict()
        self._pending_discussion: dict[int, asyncio.Future] = {}

    def _api(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self._token}/{method}"

    def register_post(self, channel_msg_id: int, post: dict) -> None:
        reddit_id = post["reddit_id"]
        self._post_context[reddit_id] = post
        self._post_context.move_to_end(reddit_id)
        if len(self._post_context) > _LRU_CAP:
            self._post_context.popitem(last=False)

        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending_discussion[channel_msg_id] = fut

    async def wait_for_discussion_id(self, channel_msg_id: int, wait_seconds: float = 30.0) -> int | None:
        fut = self._pending_discussion.get(channel_msg_id)
        if fut is None:
            return None
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout=wait_seconds)
        except TimeoutError:
            self._pending_discussion.pop(channel_msg_id, None)
            return None

    async def run(self, stop_event: asyncio.Event) -> None:
        async with httpx.AsyncClient(timeout=None) as client:
            while not stop_event.is_set():
                try:
                    updates = await asyncio.wait_for(
                        self._poll_once(client),
                        timeout=40,
                    )
                    for update in updates:
                        asyncio.create_task(self._dispatch(client, update))
                        self._offset = max(self._offset, update["update_id"] + 1)
                except TimeoutError:
                    pass
                except Exception:
                    logger.warning("Poller error", exc_info=True)
                    await asyncio.sleep(5)

    async def _poll_once(self, client: httpx.AsyncClient) -> list[dict]:
        response = await client.get(
            self._api("getUpdates"),
            params={
                "offset": self._offset,
                "timeout": 30,
                "allowed_updates": json.dumps(["message", "callback_query"]),
            },
        )
        if response.status_code != 200:
            return []
        return response.json().get("result", [])

    async def _dispatch(self, client: httpx.AsyncClient, update: dict) -> None:
        if "callback_query" in update:
            await self._handle_callback(client, update["callback_query"])
        elif "message" in update:
            self._handle_message(update["message"])

    def _handle_message(self, msg: dict) -> None:
        if not msg.get("is_automatic_forward"):
            return
        channel_msg_id = msg.get("forward_from_message_id")
        discussion_msg_id = msg.get("message_id")
        if not channel_msg_id or not discussion_msg_id:
            return
        fut = self._pending_discussion.pop(channel_msg_id, None)
        if fut and not fut.done():
            fut.set_result(discussion_msg_id)

    async def _handle_callback(self, client: httpx.AsyncClient, cb: dict) -> None:
        callback_id = cb["id"]
        data = cb.get("data", "")
        if not data.startswith(_CALLBACK_PREFIX):
            return

        # Answer immediately to remove spinner
        await client.post(self._api("answerCallbackQuery"), data={"callback_query_id": callback_id})

        reddit_id = data[len(_CALLBACK_PREFIX) :]
        channel_msg_id = cb["message"]["message_id"]
        chat_id = cb["message"]["chat"]["id"]

        # Check DB cache first
        cached = await get_explanation(reddit_id)
        if cached:
            await self._send_explanation(client, chat_id, channel_msg_id, cached)
            return

        # Find post context (in-memory or fallback to DB fields)
        post = self._post_context.get(reddit_id)
        if post is None:
            await self._send_explanation(client, chat_id, channel_msg_id, "Данные о посте недоступны.")
            return

        try:
            explanation = await generate_explanation(self._config, post)
        except Exception:
            logger.warning("Gemini error for %s", reddit_id, exc_info=True)
            await self._send_explanation(client, chat_id, channel_msg_id, "Не удалось сгенерировать объяснение.")
            return

        await save_explanation(reddit_id, explanation)
        await self._send_explanation(client, chat_id, channel_msg_id, explanation)

        # Remove the button after first use
        await client.post(
            self._api("editMessageReplyMarkup"),
            json={"chat_id": chat_id, "message_id": channel_msg_id, "reply_markup": {"inline_keyboard": []}},
        )

    async def _send_explanation(self, client: httpx.AsyncClient, chat_id: int, reply_to: int, text: str) -> None:
        await client.post(
            self._api("sendMessage"),
            data={
                "chat_id": chat_id,
                "text": text[:4096],
                "reply_to_message_id": reply_to,
                "disable_web_page_preview": "true",
            },
        )
