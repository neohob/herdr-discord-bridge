from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.bot.commands import (
    _ensure_client,
    _map_panes,
    compose_agent_payload,
    handle_agent_command,
    handle_stop_command,
    resolve_pane_from_thread,
    resolve_remote_from_channel,
)
from src.bot.mapping import MappingStore, PaneMapping
from src.bot.registry import RemoteRecord, RemoteRegistry


def test_resolve_remote_from_channel(tmp_path):
    registry = RemoteRegistry(tmp_path / "remotes.json")
    registry.upsert(RemoteRecord("lab", "localhost", 8787, "token", "f" * 64, channel_id=10))
    mapping = MappingStore(tmp_path / "mapping.json")

    assert resolve_remote_from_channel(10, registry, mapping) == "lab"
    assert resolve_remote_from_channel(999, registry, mapping) is None


def test_resolve_remote_from_mapping_channel_when_registry_is_stale(tmp_path):
    registry = RemoteRegistry(tmp_path / "remotes.json")
    registry.upsert(RemoteRecord("lab", "localhost", 8787, "token", "f" * 64))
    mapping = MappingStore(tmp_path / "mapping.json")
    mapping.set_remote_channel("lab", 10)

    assert resolve_remote_from_channel(10, registry, mapping) == "lab"


def test_resolve_pane_from_thread(tmp_path):
    mapping = MappingStore(tmp_path / "mapping.json")
    mapping.upsert_pane(PaneMapping("lab", "w1:p1", thread_id=20))

    assert resolve_pane_from_thread(20, mapping) == ("lab", "w1:p1")
    assert resolve_pane_from_thread(999, mapping) is None


def test_compose_agent_payload_joins_optional_text() -> None:
    assert compose_agent_payload("/grilling") == "/grilling"
    assert compose_agent_payload("/grilling", "extra args here") == "/grilling extra args here"
    assert compose_agent_payload("  /compact  ", "  keep going  ") == "/compact keep going"


@pytest.mark.asyncio
async def test_handle_agent_rejects_non_pane_thread() -> None:
    bot = SimpleNamespace(
        mapping=SimpleNamespace(find_by_thread=lambda _: None),
        runtime=SimpleNamespace(clients={}),
    )
    interaction = SimpleNamespace(
        channel=SimpleNamespace(id=1, send=AsyncMock()),
        user=SimpleNamespace(mention="<@1>"),
        response=SimpleNamespace(is_done=lambda: False, send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    await handle_agent_command(bot, interaction, "/grilling")
    interaction.response.send_message.assert_awaited()
    assert "Pane thread" in interaction.response.send_message.await_args.args[0]
    interaction.channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_stop_rejects_non_pane_thread() -> None:
    bot = SimpleNamespace(
        mapping=SimpleNamespace(find_by_thread=lambda _: None),
        runtime=SimpleNamespace(clients={}),
    )
    interaction = SimpleNamespace(
        channel=SimpleNamespace(id=1, send=AsyncMock()),
        user=SimpleNamespace(mention="<@1>"),
        response=SimpleNamespace(is_done=lambda: False, send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    await handle_stop_command(bot, interaction)
    interaction.response.send_message.assert_awaited()
    assert "Pane thread" in interaction.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_handle_stop_sends_interrupt() -> None:
    client = SimpleNamespace(send_interrupt=AsyncMock())
    bot = SimpleNamespace(
        mapping=SimpleNamespace(
            find_by_thread=lambda _: SimpleNamespace(remote_id="lab", pane_id="w1:p1"),
        ),
        runtime=SimpleNamespace(clients={"lab": client}),
    )
    channel = SimpleNamespace(id=20, send=AsyncMock())
    # First respond uses response; later uses followup after is_done flips.
    done = {"v": False}

    async def send_message(*_a, **_k):
        done["v"] = True

    interaction = SimpleNamespace(
        channel=channel,
        user=SimpleNamespace(mention="<@9>"),
        response=SimpleNamespace(is_done=lambda: done["v"], send_message=AsyncMock(side_effect=send_message)),
        followup=SimpleNamespace(send=AsyncMock()),
    )

    await handle_stop_command(bot, interaction)

    client.send_interrupt.assert_awaited_once_with("w1:p1")
    channel.send.assert_awaited()
    assert "/stop" in channel.send.await_args.args[0]
    interaction.followup.send.assert_awaited()
    assert "interrupt" in interaction.followup.send.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_handle_agent_forwards_in_pane_thread() -> None:
    client = SimpleNamespace(send_input=AsyncMock())
    bot = SimpleNamespace(
        mapping=SimpleNamespace(
            find_by_thread=lambda _: SimpleNamespace(remote_id="lab", pane_id="w1:p1"),
            set_terminal_message=lambda *a, **k: None,
        ),
        runtime=SimpleNamespace(clients={"lab": client}),
    )
    reply = SimpleNamespace(id=99)
    anchor = SimpleNamespace(id=42, reply=AsyncMock(return_value=reply))
    channel = SimpleNamespace(id=20, send=AsyncMock(return_value=anchor), trigger_typing=AsyncMock())
    interaction = SimpleNamespace(
        channel=channel,
        user=SimpleNamespace(mention="<@9>"),
        response=SimpleNamespace(is_done=lambda: False, send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )

    await handle_agent_command(bot, interaction, "/grilling", "extra args here")

    interaction.response.send_message.assert_awaited()
    assert interaction.response.send_message.await_args.kwargs.get("ephemeral") is True
    channel.send.assert_awaited_once()
    assert "/grilling extra args here" in channel.send.await_args.args[0]
    client.send_input.assert_awaited_once_with(
        "w1:p1", "/grilling extra args here", keys=["enter"]
    )
    anchor.reply.assert_awaited_once()


@pytest.mark.asyncio
async def test_ensure_client_starts_bound_remote_when_runtime_has_none(tmp_path):
    registry = RemoteRegistry(tmp_path / "remotes.json")
    remote = RemoteRecord("lab", "localhost", 8787, "token", "f" * 64, channel_id=10)
    registry.upsert(remote)
    client = object()
    runtime = SimpleNamespace(clients={}, start_remote=AsyncMock(return_value=client))
    bot = SimpleNamespace(registry=registry, runtime=runtime)

    assert await _ensure_client(bot, "lab") is client
    runtime.start_remote.assert_awaited_once_with(remote)


@pytest.mark.asyncio
async def test_map_panes_counts_only_entries_with_a_pane_id(tmp_path, monkeypatch):
    registry = RemoteRegistry(tmp_path / "remotes.json")
    remote = RemoteRecord("lab", "localhost", 8787, "token", "f" * 64, channel_id=10)
    registry.upsert(remote)
    client = SimpleNamespace(observe_pane=AsyncMock())
    bot = SimpleNamespace(
        registry=registry,
        runtime=SimpleNamespace(clients={"lab": client}),
        mapping=MappingStore(tmp_path / "mapping.json"),
        config=SimpleNamespace(bridge=object()),
    )
    monkeypatch.setattr("src.bot.commands._remote_channel", AsyncMock(return_value=object()))
    ensure_thread = AsyncMock()
    monkeypatch.setattr("src.bot.commands.ensure_pane_thread", ensure_thread)

    count = await _map_panes(
        interaction=SimpleNamespace(),
        bot=bot,
        remote=remote,
        panes=[{}, {"pane_id": "w1:p1", "workspace_id": "w1"}],
    )

    assert count == 1
    ensure_thread.assert_awaited_once()
    client.observe_pane.assert_awaited_once_with("w1:p1", True)
