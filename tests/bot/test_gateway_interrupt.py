from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.bot.gateway_client import GatewayClient
from src.bot.registry import RemoteRecord


@pytest.mark.asyncio
async def test_send_interrupt_prefers_agent_send_keys() -> None:
    client = GatewayClient(
        RemoteRecord("lab", "127.0.0.1", 8787, "t", "f" * 64),
        AsyncMock(),
    )
    client.request = AsyncMock(return_value={"ok": True})

    await client.send_interrupt("w1:p1")

    client.request.assert_awaited_once_with(
        "agent.send_keys",
        {"target": "w1:p1", "keys": ["esc"]},
    )


@pytest.mark.asyncio
async def test_send_interrupt_falls_back_to_pane_keys() -> None:
    client = GatewayClient(
        RemoteRecord("lab", "127.0.0.1", 8787, "t", "f" * 64),
        AsyncMock(),
    )

    async def request(method: str, params=None):
        if method == "agent.send_keys":
            raise RuntimeError("no agent")
        return {"ok": True, "method": method, "params": params}

    client.request = AsyncMock(side_effect=request)

    await client.send_interrupt("w1:p1")

    assert client.request.await_count == 3
    assert client.request.await_args_list[1].args[0] == "pane.send_keys"
    assert client.request.await_args_list[1].args[1] == {"pane_id": "w1:p1", "keys": ["esc"]}
    assert client.request.await_args_list[2].args[1] == {"pane_id": "w1:p1", "keys": ["ctrl+c"]}
