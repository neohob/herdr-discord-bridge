from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.bot.mapping import MappingStore, PaneMapping
from src.bot.pane_lifecycle import is_pane_missing_error, retire_mapped_pane
from src.shared.ndjson import HerdrApiError


def test_is_pane_missing_error():
    assert is_pane_missing_error(HerdrApiError("pane_not_found", "pane wH:p1 not found"))
    assert is_pane_missing_error(RuntimeError("pane_not_found: pane x not found"))
    assert not is_pane_missing_error(RuntimeError("timeout"))


@pytest.mark.asyncio
async def test_retire_mapped_pane_deletes_thread_and_mapping(tmp_path):
    mapping = MappingStore(tmp_path / "mapping.json")
    mapping.upsert_pane(PaneMapping("lab", "wH:p1", thread_id=20, label="old"))
    thread = SimpleNamespace(delete=AsyncMock(), edit=AsyncMock())
    guild = SimpleNamespace(get_thread=lambda _id: thread, fetch_channel=AsyncMock())
    client = SimpleNamespace(observe_pane=AsyncMock())

    retired = await retire_mapped_pane(
        guild=guild,
        mapping=mapping,
        client=client,
        remote_id="lab",
        pane_id="wH:p1",
        reason="gone",
    )

    assert retired is not None
    assert mapping.get_pane("lab", "wH:p1") is None
    client.observe_pane.assert_awaited_once_with("wH:p1", False)
    thread.delete.assert_awaited_once()
