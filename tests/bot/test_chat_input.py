from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.bot.chat_input import format_agent_anchor, forward_pane_input, on_message


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
    reply = SimpleNamespace(id=99)
    channel = SimpleNamespace(id=20, send=AsyncMock(), trigger_typing=AsyncMock())
    message = SimpleNamespace(
        channel=channel,
        author=SimpleNamespace(bot=False),
        content="continue",
        id=42,
        reply=AsyncMock(return_value=reply),
    )

    await on_message(bot, message)

    channel.trigger_typing.assert_awaited()
    message.reply.assert_awaited_once()
    client.send_input.assert_awaited_once_with("w1:p1", "continue", keys=["enter"])


@pytest.mark.asyncio
async def test_chat_input_forwards_agent_slash_skills() -> None:
    """Agent skills like /compact must reach the Pane; / is not a Discord text prefix."""
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
    reply = SimpleNamespace(id=99)
    channel = SimpleNamespace(id=20, send=AsyncMock(), trigger_typing=AsyncMock())
    message = SimpleNamespace(
        channel=channel,
        author=SimpleNamespace(bot=False),
        content="/compact",
        id=42,
        reply=AsyncMock(return_value=reply),
    )

    await on_message(bot, message)

    client.send_input.assert_awaited_once_with("w1:p1", "/compact", keys=["enter"])


@pytest.mark.asyncio
async def test_chat_input_skips_discord_text_prefix_commands() -> None:
    client = SimpleNamespace(send_input=AsyncMock())
    bot = SimpleNamespace(
        mapping=SimpleNamespace(find_by_thread=lambda _: SimpleNamespace(remote_id="lab", pane_id="w1:p1")),
        runtime=SimpleNamespace(clients={"lab": client}),
        command_prefix="!",
    )
    message = SimpleNamespace(
        channel=SimpleNamespace(id=20, trigger_typing=AsyncMock()),
        author=SimpleNamespace(bot=False),
        content="!help",
        id=42,
        reply=AsyncMock(),
    )

    await on_message(bot, message)

    client.send_input.assert_not_awaited()


@pytest.mark.asyncio
async def test_forward_pane_input_shared_path() -> None:
    client = SimpleNamespace(send_input=AsyncMock())
    bot = SimpleNamespace(
        mapping=SimpleNamespace(set_terminal_message=lambda *a, **k: None),
        runtime=SimpleNamespace(clients={"lab": client}),
    )
    pane = SimpleNamespace(remote_id="lab", pane_id="w1:p1")
    reply = SimpleNamespace(id=99)
    channel = SimpleNamespace(id=20, trigger_typing=AsyncMock())
    anchor = SimpleNamespace(id=42, reply=AsyncMock(return_value=reply))

    ok = await forward_pane_input(bot, channel, pane, "/grilling", anchor)

    assert ok is True
    channel.trigger_typing.assert_awaited()
    anchor.reply.assert_awaited_once()
    client.send_input.assert_awaited_once_with("w1:p1", "/grilling", keys=["enter"])


def test_format_agent_anchor_includes_mention_and_text() -> None:
    user = SimpleNamespace(mention="<@123>")
    assert format_agent_anchor(user, "/compact") == "<@123>: /compact"


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
