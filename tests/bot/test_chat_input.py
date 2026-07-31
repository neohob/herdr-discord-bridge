from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.bot.chat_input import on_message


@pytest.mark.asyncio
async def test_chat_input_forwards_mapped_human_message() -> None:
    client = SimpleNamespace(send_input=AsyncMock())
    bot = SimpleNamespace(
        mapping=SimpleNamespace(
            find_by_thread=lambda thread_id: (
                SimpleNamespace(remote_id="lab", pane_id="w1:p1") if thread_id == 20 else None
            )
        ),
        runtime=SimpleNamespace(clients={"lab": client}),
        command_prefix="!",
    )
    message = SimpleNamespace(
        channel=SimpleNamespace(id=20),
        author=SimpleNamespace(bot=False),
        content="continue",
    )

    await on_message(bot, message)

    client.send_input.assert_awaited_once_with("w1:p1", "continue", keys=["enter"])


@pytest.mark.asyncio
async def test_chat_input_ignores_bot_messages() -> None:
    client = SimpleNamespace(send_input=AsyncMock())
    bot = SimpleNamespace(
        mapping=SimpleNamespace(find_by_thread=lambda _: SimpleNamespace(remote_id="lab", pane_id="w1:p1")),
        runtime=SimpleNamespace(clients={"lab": client}),
        command_prefix="!",
    )
    message = SimpleNamespace(
        channel=SimpleNamespace(id=20),
        author=SimpleNamespace(bot=True),
        content="continue",
    )

    await on_message(bot, message)

    client.send_input.assert_not_awaited()
