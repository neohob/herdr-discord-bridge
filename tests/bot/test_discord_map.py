"""Tests for Remote Channel and Pane Thread helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from src.bot.config import BridgeConfig
from src.bot.discord_map import ensure_pane_thread, ensure_remote_channel, thread_name_for
from src.bot.herdr.models import PaneInfo
from src.bot.mapping import MappingStore


def _guild(*, text_channels: list | None = None) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.text_channels = text_channels or []
    guild.create_text_channel = AsyncMock()
    return guild


@pytest.mark.asyncio
async def test_ensure_remote_channel_creates_when_missing(tmp_path):
    mapping = MappingStore(tmp_path / "m.json")
    guild = _guild()
    created = MagicMock(spec=discord.TextChannel)
    created.id = 42
    created.name = "remote:lab"
    guild.create_text_channel.return_value = created
    cfg = BridgeConfig(category_prefix="remote:")

    channel = await ensure_remote_channel(
        guild, "lab", "lab", mapping=mapping, bridge_cfg=cfg
    )

    assert channel.id == 42
    guild.create_text_channel.assert_awaited_once()
    assert mapping.remotes["lab"].channel_id == 42


@pytest.mark.asyncio
async def test_ensure_remote_channel_reuses_mapped(tmp_path):
    mapping = MappingStore(tmp_path / "m.json")
    mapping.set_remote_channel("lab", 77)
    existing = MagicMock(spec=discord.TextChannel)
    existing.id = 77
    guild = _guild()
    guild.get_channel = MagicMock(return_value=existing)
    cfg = BridgeConfig()

    channel = await ensure_remote_channel(
        guild, "lab", "lab", mapping=mapping, bridge_cfg=cfg
    )

    assert channel is existing
    guild.create_text_channel.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_pane_thread_creates_thread(tmp_path):
    mapping = MappingStore(tmp_path / "m.json")
    remote_channel = MagicMock(spec=discord.TextChannel)
    remote_channel.id = 10
    remote_channel.threads = []
    remote_channel.guild = MagicMock()
    remote_channel.guild.fetch_channel = AsyncMock()
    remote_channel.get_thread = MagicMock(return_value=None)
    thread = MagicMock(spec=discord.Thread)
    thread.id = 900
    thread.name = "pane-thread"
    remote_channel.create_thread = AsyncMock(return_value=thread)

    pane = PaneInfo(
        pane_id="w1:p1",
        workspace_id="w1",
        label="my-agent",
        agent_status="idle",
    )
    cfg = BridgeConfig()

    result = await ensure_pane_thread(
        remote_channel,
        pane,
        remote_id="lab",
        mapping=mapping,
        bridge_cfg=cfg,
    )

    assert result is thread
    remote_channel.create_thread.assert_awaited_once()
    pm = mapping.get_pane("lab", "w1:p1")
    assert pm is not None
    assert pm.thread_id == 900


def test_thread_name_includes_status_emoji():
    pane = PaneInfo(
        pane_id="wB:p6",
        workspace_id="wB",
        label="Cursor Agent",
        agent="cursor",
        agent_status="idle",
        workspace_label="JinAn-MAP",
        tab_label="cursor",
    )
    name = thread_name_for(pane, BridgeConfig())
    assert name.startswith("🟢")
    assert "JinAn-MAP" in name
    assert "cursor" in name
    assert "[wB:p6]" in name


def test_pane_label_prefers_terminal_title_and_cwd():
    from src.bot.herdr.models import PaneInfo as PI

    pane = PI.from_dict(
        {
            "pane_id": "wA:p1",
            "workspace_id": "wA",
            "terminal_title_stripped": "neo@host:~/Downloads/MyNextCloud/Work/ShangLin/2026/HGT/codes",
            "cwd": "/Users/neo/Work/ShangLin/2026/HGT/codes",
        }
    )
    assert pane.label == "codes"
