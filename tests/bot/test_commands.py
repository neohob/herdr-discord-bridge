from __future__ import annotations

from src.bot.commands import resolve_pane_from_thread, resolve_remote_from_channel
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
