from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.bot.commands import _ensure_client, _map_panes, resolve_pane_from_thread, resolve_remote_from_channel
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
