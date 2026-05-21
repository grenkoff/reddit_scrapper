import asyncio
import json
import logging

import httpx

from src.config import Config

logger = logging.getLogger(__name__)


class UpdatePoller:
    """Long-polls Telegram updates to find auto-forwarded messages in the discussion group."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._token = config.telegram_bot_token
        self._offset = 0
        self._pending_discussion: dict[int, asyncio.Future] = {}

    def _api(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self._token}/{method}"

    def register_post(self, channel_msg_id: int, post: dict) -> None:
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
                    updates = await asyncio.wait_for(self._poll_once(client), timeout=40)
                    for update in updates:
                        if "message" in update:
                            self._handle_message(update["message"])
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
                "allowed_updates": json.dumps(["message"]),
            },
        )
        if response.status_code != 200:
            return []
        return response.json().get("result", [])

    def _handle_message(self, msg: dict) -> None:
        if not msg.get("is_automatic_forward"):
            return
        # Bot API 7.0+ moved forward fields into forward_origin.
        # Older API still has forward_from_message_id at top level.
        channel_msg_id = msg.get("forward_from_message_id") or msg.get("forward_origin", {}).get("message_id")
        discussion_msg_id = msg.get("message_id")
        if not channel_msg_id or not discussion_msg_id:
            return
        fut = self._pending_discussion.pop(channel_msg_id, None)
        if fut and not fut.done():
            fut.set_result(discussion_msg_id)
