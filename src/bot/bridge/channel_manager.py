"""Create/update Discord categories and pane channels for remotes.

Legacy Category/Channel model — TODO(Task 10): migrate bot.py to src.bot.discord_map.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

import discord

from src.bot.bridge.mapping import MappingStore, PaneMapping
from src.bot.bridge.terminal_sim import TerminalSimulator
from src.bot.herdr.client import HerdrClient
from src.bot.herdr.models import PaneInfo

if TYPE_CHECKING:
    from src.bot.config import AppConfig

log = logging.getLogger(__name__)

_CHANNEL_SAFE = re.compile(r"[^a-z0-9\-]+")


def sanitize_channel_name(name: str, *, max_len: int = 90) -> str:
    slug = name.strip().lower().replace(" ", "-")
    slug = _CHANNEL_SAFE.sub("-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    if not slug:
        slug = "pane"
    return slug[:max_len]


class ChannelManager:
    def __init__(
        self,
        guild: discord.Guild,
        config: AppConfig,
        mapping: MappingStore,
    ):
        self.guild = guild
        self.config = config
        self.mapping = mapping
        self.terminals: dict[tuple[str, str], TerminalSimulator] = {}

    def category_name(self, remote_id: str) -> str:
        return f"{self.config.bridge.category_prefix}{remote_id}"

    def channel_name_for(self, pane: PaneInfo) -> str:
        emoji = self.config.bridge.status_emoji.get(pane.agent_status, "❓")
        base = sanitize_channel_name(pane.label or pane.agent or pane.pane_id)
        suffix = sanitize_channel_name(pane.pane_id.replace(":", "-"))[-8:]
        # Keep status emoji prefix; Discord accepts unicode in channel names.
        return f"{emoji}-{base}-{suffix}"[:100]

    async def ensure_category(self, remote_id: str) -> discord.CategoryChannel:
        rm = self.mapping.ensure_remote(remote_id)
        if rm.category_id:
            cat = self.guild.get_channel(rm.category_id)
            if isinstance(cat, discord.CategoryChannel):
                return cat
        name = self.category_name(remote_id)
        for ch in self.guild.categories:
            if ch.name == name:
                self.mapping.set_category(remote_id, ch.id)
                return ch
        if self.config.bridge.read_only:
            raise RuntimeError(f"read_only: missing category {name}")
        cat = await self.guild.create_category(name, reason="herdr-discord-bridge sync")
        self.mapping.set_category(remote_id, cat.id)
        return cat

    async def sync_remote(self, client: HerdrClient) -> None:
        remote_id = client.remote_id
        category = await self.ensure_category(remote_id)
        panes = await client.pane_list()
        seen: set[str] = set()
        for pane in panes:
            seen.add(pane.pane_id)
            await self.ensure_pane_channel(remote_id, pane, category)
        # Optionally leave orphan Discord channels; do not delete by default.

    async def ensure_pane_channel(
        self,
        remote_id: str,
        pane: PaneInfo,
        category: discord.CategoryChannel | None = None,
    ) -> discord.TextChannel:
        if category is None:
            category = await self.ensure_category(remote_id)
        existing = self.mapping.get_pane(remote_id, pane.pane_id)
        channel: discord.TextChannel | None = None
        if existing:
            ch = self.guild.get_channel(existing.channel_id)
            if isinstance(ch, discord.TextChannel):
                channel = ch
        desired = self.channel_name_for(pane)
        if channel is None:
            if self.config.bridge.read_only:
                raise RuntimeError(f"read_only: missing channel for {pane.pane_id}")
            # Reuse same-named channel under category if present.
            for ch in category.text_channels:
                if ch.name == desired or ch.name.endswith(sanitize_channel_name(pane.pane_id)[-12:]):
                    channel = ch
                    break
            if channel is None:
                channel = await self.guild.create_text_channel(
                    desired[:100],
                    category=category,
                    reason=f"herdr pane {pane.pane_id}",
                )
        else:
            # Rename when status/label changes (best-effort).
            if channel.name != desired[:100] and not self.config.bridge.read_only:
                try:
                    await channel.edit(name=desired[:100])
                except discord.HTTPException as exc:
                    log.debug("rename skip %s: %s", channel.id, exc)

        pm = PaneMapping(
            remote_id=remote_id,
            pane_id=pane.pane_id,
            channel_id=channel.id,
            terminal_message_id=existing.terminal_message_id if existing else None,
            label=pane.label,
            agent_status=pane.agent_status,
        )
        self.mapping.upsert_pane(pm)

        key = (remote_id, pane.pane_id)
        sim = self.terminals.get(key)
        if sim is None:
            sim = TerminalSimulator(
                channel,
                remote_id=remote_id,
                pane_id=pane.pane_id,
                bridge=self.config.bridge,
                message_id=pm.terminal_message_id,
            )
            self.terminals[key] = sim
            await sim.update_status(pane.agent_status, force=True)
            await sim.ensure_message()
            pm.terminal_message_id = sim.message_id
            self.mapping.upsert_pane(pm)
        else:
            await sim.update_status(pane.agent_status, force=False)
        return channel

    def get_terminal(self, remote_id: str, pane_id: str) -> TerminalSimulator | None:
        return self.terminals.get((remote_id, pane_id))

    async def on_pane_closed(self, remote_id: str, pane_id: str) -> None:
        self.terminals.pop((remote_id, pane_id), None)
        pm = self.mapping.get_pane(remote_id, pane_id)
        self.mapping.remove_pane(remote_id, pane_id)
        if pm and not self.config.bridge.read_only:
            ch = self.guild.get_channel(pm.channel_id)
            if isinstance(ch, discord.TextChannel):
                try:
                    await ch.edit(name=f"archived-{ch.name}"[:100])
                except discord.HTTPException:
                    pass
