from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.bot.config import AppConfig, BridgeConfig, DiscordConfig, OperatorsConfig, RemoteSeed
from src.bot.mapping import MappingStore, PaneMapping
from src.bot.registry import RemoteRecord, RemoteRegistry
from src.bot.runtime import Runtime
from src.bot.terminal_view import clear_terminal_state


@dataclass
class FakeThread:
    id: int
    edits: list[str] = field(default_factory=list)
    messages: list[Any] = field(default_factory=list)
    deleted: bool = False
    archived: bool = False

    async def edit(self, *, name: str | None = None, archived: bool | None = None, locked: bool | None = None, reason: str | None = None, **kwargs: Any) -> None:
        if name is not None:
            self.edits.append(name)
        if archived is not None:
            self.archived = archived

    async def delete(self, *, reason: str | None = None) -> None:
        self.deleted = True

    async def send(self, content: str = "", **kwargs: Any) -> Any:
        self.messages.append({"content": content, **kwargs})
        return SimpleNamespace(id=len(self.messages) * 1000)

    async def fetch_message(self, message_id: int) -> Any:
        return SimpleNamespace(id=message_id, edit=AsyncMock())



@dataclass
class FakeChannel:
    id: int
    messages: list[str] = field(default_factory=list)

    async def send(self, content: str) -> None:
        self.messages.append(content)


class FakeGuild:
    def __init__(self, entries: dict[int, Any]):
        self.entries = entries

    def get_thread(self, thread_id: int) -> None:
        return None

    def get_channel(self, channel_id: int) -> Any:
        return self.entries.get(channel_id)

    async def fetch_channel(self, channel_id: int) -> Any:
        return self.entries[channel_id]


class FakeGatewayClient:
    def __init__(self, remote, on_event, *, on_control_ready=None):
        self.remote = remote
        self.on_event = on_event
        self.on_control_ready = on_control_ready
        self.started = False
        self.stopped = False
        self.observed: list[tuple[str, bool]] = []

    async def start(self) -> None:
        self.started = True
        if self.on_control_ready:
            await self.on_control_ready()

    async def stop(self) -> None:
        self.stopped = True

    async def observe_pane(self, pane_id: str, enabled: bool) -> None:
        self.observed.append((pane_id, enabled))


def make_config(tmp_path: Path, *, seed_remotes=None) -> AppConfig:
    return AppConfig(
        discord=DiscordConfig(token="token", guild_id=1),
        bridge=BridgeConfig(),
        operators=OperatorsConfig(),
        seed_remotes=seed_remotes,
        registry_path=tmp_path / "remotes.json",
        mapping_path=tmp_path / "mapping.json",
        log_dir=tmp_path / "logs",
    )


@pytest.fixture(autouse=True)
def _clear_terminal_state():
    clear_terminal_state()
    yield
    clear_terminal_state()


@pytest.mark.asyncio
async def test_runtime_starts_bound_remote_routes_pushes_and_restores_observe(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    registry = RemoteRegistry(config.registry_path)
    registry.upsert(
        RemoteRecord("lab", "127.0.0.1", 8787, "token", "f" * 64, channel_id=10),
    )
    mapping = MappingStore(config.mapping_path)
    mapping.upsert_pane(PaneMapping("lab", "w1:p1", thread_id=20, label="agent"))
    thread = FakeThread(20)
    channel = FakeChannel(10)
    runtime = Runtime(
        config,
        FakeGuild({10: channel, 20: thread}),
        registry=registry,
        mapping=mapping,
        client_factory=FakeGatewayClient,
    )
    renders: list[dict[str, Any]] = []

    async def fake_apply(*args, **kwargs):
        renders.append({"args": args, "kwargs": kwargs})
        return 99

    monkeypatch.setattr("src.bot.runtime.apply_terminal_view", fake_apply)

    await runtime.start()
    client = runtime.clients["lab"]
    assert client.started
    assert client.observed == [("w1:p1", True)]

    await client.on_event(
        {
            "event": "bridge.terminal_output",
            "data": {"pane_id": "w1:p1", "text": "hello"},
        },
    )
    assert renders[0]["args"][2] == "hello"
    assert mapping.get_pane("lab", "w1:p1").terminal_message_id == 99

    await client.on_event(
        {
            "event": "pane.agent_status_changed",
            "data": {"pane_id": "w1:p1", "agent_status": "working"},
        },
    )
    assert mapping.get_pane("lab", "w1:p1").agent_status == "working"
    assert thread.edits
    assert "working" in thread.edits[0] or "🔵" in thread.edits[0]

    created_thread = FakeThread(21)

    async def fake_ensure(channel, pane, *, remote_id, mapping, bridge_cfg):
        mapping.upsert_pane(
            PaneMapping(remote_id, pane.pane_id, thread_id=created_thread.id, label=pane.label)
        )
        return created_thread

    monkeypatch.setattr("src.bot.runtime.ensure_pane_thread", fake_ensure)
    await client.on_event(
        {
            "event": "pane.created",
            "data": {"pane_id": "w1:p2", "workspace_id": "w1", "label": "new"},
        }
    )
    assert mapping.get_pane("lab", "w1:p2") is not None
    assert ("w1:p2", True) in client.observed
    assert channel.messages[-1] == "Pane `w1:p2` created — thread ready."

    await client.on_event({"event": "pane.closed", "data": {"pane_id": "w1:p1"}})
    assert mapping.get_pane("lab", "w1:p1") is None
    assert thread.deleted is True
    assert ("w1:p1", False) in client.observed
    assert "closed" in channel.messages[-1]

    await runtime.stop()
    assert client.stopped


@pytest.mark.asyncio
async def test_runtime_posts_choice_ui_on_approval_text(tmp_path):
    config = make_config(tmp_path)
    registry = RemoteRegistry(config.registry_path)
    registry.upsert(
        RemoteRecord("lab", "127.0.0.1", 8787, "token", "f" * 64, channel_id=10),
    )
    mapping = MappingStore(config.mapping_path)
    mapping.upsert_pane(PaneMapping("lab", "w1:p1", thread_id=20, label="agent"))
    thread = FakeThread(20)
    runtime = Runtime(
        config,
        FakeGuild({10: FakeChannel(10), 20: thread}),
        registry=registry,
        mapping=mapping,
        client_factory=FakeGatewayClient,
    )
    await runtime.start()
    client = runtime.clients["lab"]

    await client.on_event(
        {
            "event": "bridge.terminal_output",
            "data": {
                "pane_id": "w1:p1",
                "text": "Do you want to proceed?\n(y/n)",
                "revision": 1,
            },
        },
    )
    choice_msgs = [m for m in thread.messages if isinstance(m, dict) and m.get("view") is not None]
    assert len(choice_msgs) == 1

    await client.on_event(
        {
            "event": "bridge.terminal_output",
            "data": {
                "pane_id": "w1:p1",
                "text": "Do you want to proceed?\n(y/n)",
                "revision": 2,
            },
        },
    )
    choice_msgs = [m for m in thread.messages if isinstance(m, dict) and m.get("view") is not None]
    assert len(choice_msgs) == 1


@pytest.mark.asyncio
async def test_reenable_observe_retires_missing_panes(tmp_path):
    from src.shared.ndjson import HerdrApiError

    config = make_config(tmp_path)
    registry = RemoteRegistry(config.registry_path)
    registry.upsert(
        RemoteRecord("lab", "127.0.0.1", 8787, "token", "f" * 64, channel_id=10),
    )
    mapping = MappingStore(config.mapping_path)
    mapping.upsert_pane(PaneMapping("lab", "gone:p1", thread_id=20, label="old"))
    thread = FakeThread(20)

    class MissingPaneClient(FakeGatewayClient):
        async def observe_pane(self, pane_id: str, enabled: bool) -> None:
            self.observed.append((pane_id, enabled))
            if enabled and pane_id == "gone:p1":
                raise HerdrApiError("pane_not_found", f"pane {pane_id} not found")

    runtime = Runtime(
        config,
        FakeGuild({10: FakeChannel(10), 20: thread}),
        registry=registry,
        mapping=mapping,
        client_factory=MissingPaneClient,
    )
    await runtime.start()
    assert mapping.get_pane("lab", "gone:p1") is None
    assert thread.deleted is True


@pytest.mark.asyncio
async def test_runtime_imports_unregistered_seed_without_starting_it(tmp_path):
    seed = RemoteSeed("seed", "127.0.0.1", 8787, "token", "f" * 64)
    config = make_config(tmp_path, seed_remotes=[seed])
    runtime = Runtime(config, FakeGuild({}), client_factory=FakeGatewayClient)

    await runtime.start()

    record = runtime.registry.get("seed")
    assert record is not None
    assert record.channel_id is None
    assert runtime.clients == {}
