from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.bot.lifecycle import on_guild_channel_delete, on_raw_thread_delete, on_thread_delete


@pytest.mark.asyncio
async def test_channel_delete_unbinds_remote_and_stops_client() -> None:
    registry = SimpleNamespace(
        list=lambda: [SimpleNamespace(id="lab", channel_id=10)],
        unbind_channel=Mock(),
    )
    client = SimpleNamespace(stop=AsyncMock())
    bot = SimpleNamespace(registry=registry, runtime=SimpleNamespace(clients={"lab": client}))

    await on_guild_channel_delete(bot, SimpleNamespace(id=10))

    registry.unbind_channel.assert_called_once_with("lab")
    client.stop.assert_awaited_once()
    assert bot.runtime.clients == {}


@pytest.mark.asyncio
async def test_thread_delete_unmaps_pane_and_stops_observation() -> None:
    pane = SimpleNamespace(remote_id="lab", pane_id="w1:p1")
    mapping = SimpleNamespace(find_by_thread=Mock(return_value=pane), remove_pane=Mock())
    client = SimpleNamespace(observe_pane=AsyncMock())
    bot = SimpleNamespace(mapping=mapping, runtime=SimpleNamespace(clients={"lab": client}))

    await on_thread_delete(bot, SimpleNamespace(id=20))

    client.observe_pane.assert_awaited_once_with("w1:p1", False)
    mapping.remove_pane.assert_called_once_with("lab", "w1:p1")


@pytest.mark.asyncio
async def test_raw_thread_delete_unmaps_pane() -> None:
    pane = SimpleNamespace(remote_id="lab", pane_id="w1:p1")
    mapping = SimpleNamespace(find_by_thread=Mock(return_value=pane), remove_pane=Mock())
    client = SimpleNamespace(observe_pane=AsyncMock())
    bot = SimpleNamespace(mapping=mapping, runtime=SimpleNamespace(clients={"lab": client}))

    await on_raw_thread_delete(bot, SimpleNamespace(thread_id=20))

    client.observe_pane.assert_awaited_once_with("w1:p1", False)
    mapping.remove_pane.assert_called_once_with("lab", "w1:p1")
