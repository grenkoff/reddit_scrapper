import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import respx
from httpx import Response

from src.config import Config
from src.publisher.poller import UpdatePoller

CONFIG = Config(
    telegram_bot_token="testtoken",
    telegram_chat_id="@testchan",
    database_url="postgresql://test",
)


def _make_poller():
    return UpdatePoller(CONFIG)


# --- register_post / wait_for_discussion_id ---

async def test_register_creates_future():
    poller = _make_poller()
    poller.register_post(100, {"reddit_id": "t3_abc"})
    assert 100 in poller._pending_discussion


async def test_wait_resolves_when_future_set():
    poller = _make_poller()
    poller.register_post(100, {})

    async def resolver():
        await asyncio.sleep(0.01)
        fut = poller._pending_discussion.get(100)
        if fut and not fut.done():
            fut.set_result(999)

    asyncio.create_task(resolver())
    result = await poller.wait_for_discussion_id(100, wait_seconds=1.0)
    assert result == 999


async def test_wait_returns_none_on_timeout():
    poller = _make_poller()
    poller.register_post(200, {})
    result = await poller.wait_for_discussion_id(200, wait_seconds=0.05)
    assert result is None


async def test_wait_returns_none_when_not_registered():
    poller = _make_poller()
    result = await poller.wait_for_discussion_id(999, wait_seconds=0.05)
    assert result is None


# --- _handle_message ---

def test_handle_message_auto_forward_resolves_future():
    poller = _make_poller()
    loop = asyncio.new_event_loop()
    fut = loop.create_future()
    poller._pending_discussion[21840] = fut

    msg = {
        "is_automatic_forward": True,
        "forward_from_message_id": 21840,
        "message_id": 55555,
        "chat": {"title": "Test Chat"},
    }
    poller._handle_message(msg)
    assert fut.done()
    assert fut.result() == 55555
    loop.close()


def test_handle_message_non_auto_forward_ignored():
    poller = _make_poller()
    loop = asyncio.new_event_loop()
    fut = loop.create_future()
    poller._pending_discussion[100] = fut

    msg = {
        "is_automatic_forward": False,
        "forward_from_message_id": 100,
        "message_id": 200,
    }
    poller._handle_message(msg)
    assert not fut.done()
    loop.close()


def test_handle_message_uses_forward_origin_for_new_api():
    poller = _make_poller()
    loop = asyncio.new_event_loop()
    fut = loop.create_future()
    poller._pending_discussion[777] = fut

    msg = {
        "is_automatic_forward": True,
        "forward_origin": {"type": "channel", "message_id": 777},
        "message_id": 888,
    }
    poller._handle_message(msg)
    assert fut.done()
    assert fut.result() == 888
    loop.close()


def test_handle_message_no_match_leaves_future_pending():
    poller = _make_poller()
    loop = asyncio.new_event_loop()
    fut = loop.create_future()
    poller._pending_discussion[100] = fut

    msg = {
        "is_automatic_forward": True,
        "forward_from_message_id": 999,  # different id
        "message_id": 200,
    }
    poller._handle_message(msg)
    assert not fut.done()
    loop.close()


# --- _poll_once ---

@respx.mock
async def test_poll_once_advances_offset():
    updates_response = {
        "result": [
            {"update_id": 10, "message": {"message_id": 1, "is_automatic_forward": False}},
            {"update_id": 11, "message": {"message_id": 2, "is_automatic_forward": False}},
        ]
    }
    respx.get("https://api.telegram.org/bottesttoken/getUpdates").mock(
        return_value=Response(200, json=updates_response)
    )
    poller = _make_poller()
    import httpx
    async with httpx.AsyncClient(timeout=None) as client:
        updates = await poller._poll_once(client)
    assert len(updates) == 2


@respx.mock
async def test_poll_once_returns_empty_on_non_200():
    respx.get("https://api.telegram.org/bottesttoken/getUpdates").mock(
        return_value=Response(409, json={"description": "Conflict"})
    )
    poller = _make_poller()
    import httpx
    async with httpx.AsyncClient(timeout=None) as client:
        updates = await poller._poll_once(client)
    assert updates == []
