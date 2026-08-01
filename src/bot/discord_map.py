"""Ensure Discord Remote Channels and Pane Threads for Herdr remotes."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

import discord

from src.bot.herdr.models import PaneInfo
from src.bot.mapping import MappingStore, PaneMapping

if TYPE_CHECKING:
    from src.bot.config import BridgeConfig

log = logging.getLogger(__name__)

_CHANNEL_SAFE = re.compile(r"[^a-z0-9\-]+")
_THREAD_UNSAFE = re.compile(r"[\r\n#@]+")


def sanitize_channel_name(name: str, *, max_len: int = 90) -> str:
    slug = name.strip().lower().replace(" ", "-")
    slug = _CHANNEL_SAFE.sub("-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    if not slug:
        slug = "pane"
    return slug[:max_len]


def sanitize_thread_name(name: str, *, max_len: int = 90) -> str:
    """Keep unicode / CJK for Discord thread titles; only strip control noise."""
    text = _THREAD_UNSAFE.sub(" ", (name or "").strip())
    text = re.sub(r"\s+", " ", text).strip(" -_|")
    if not text:
        text = "pane"
    return text[:max_len]


def remote_channel_name(prefix: str, remote_id: str, name: str | None = None) -> str:
    base = sanitize_channel_name(name or remote_id)
    prefix_slug = sanitize_channel_name(prefix.rstrip(":"), max_len=32)
    if prefix.endswith(":"):
        return f"{prefix_slug}:{base}"[:100]
    return f"{prefix_slug}-{base}"[:100]


def thread_name_for(pane: PaneInfo, bridge_cfg: BridgeConfig) -> str:
    """Stable Pane Thread title — no agent-status emoji.

    Format: ``{workspace} › {tab} · {name} [{pane_id}]``

    Status flips (idle/working/done) every few minutes; putting them in the
    Discord thread name floods the channel audit log. Status stays on the
    Terminal View message instead. ``bridge_cfg`` is kept for call-site
    compatibility.
    """
    _ = bridge_cfg
    workspace = sanitize_thread_name(pane.workspace_label or pane.workspace_id or "", max_len=28)
    tab = sanitize_thread_name(pane.tab_label or "", max_len=20)
    name = sanitize_thread_name(pane.agent or pane.label or "", max_len=32)
    pane_id = (pane.pane_id or "").strip()

    head_parts: list[str] = []
    if workspace:
        head_parts.append(workspace)
    if tab and tab.lower() not in {p.lower() for p in head_parts}:
        head_parts.append(tab)
    if name and name.lower() not in {p.lower() for p in head_parts}:
        head_parts.append(name)
    if not head_parts:
        head_parts.append(pane_id or "pane")

    # workspace › tab · name  (tab is the "grouped" tab label)
    if workspace and tab and name:
        body = f"{workspace} › {tab} · {name}"
    elif workspace and tab:
        body = f"{workspace} › {tab}"
    elif len(head_parts) >= 2:
        body = " · ".join(head_parts)
    else:
        body = head_parts[0]

    suffix = f" [{pane_id}]" if pane_id else ""
    budget = 100 - len(suffix)
    if budget < 8:
        return (pane_id or body)[:100]
    return (body[:budget].rstrip(" ·›") + suffix)[:100]


async def ensure_remote_channel(
    guild: discord.Guild,
    remote_id: str,
    name: str,
    *,
    mapping: MappingStore,
    bridge_cfg: BridgeConfig,
) -> discord.TextChannel:
    """Return the guild text channel representing a Remote."""
    rm = mapping.ensure_remote(remote_id)
    if rm.channel_id:
        ch = guild.get_channel(rm.channel_id)
        if isinstance(ch, discord.TextChannel):
            return ch

    desired = remote_channel_name(bridge_cfg.category_prefix, remote_id, name)
    for ch in guild.text_channels:
        if ch.name == desired:
            mapping.set_remote_channel(remote_id, ch.id)
            return ch

    if bridge_cfg.read_only:
        raise RuntimeError(f"read_only: missing remote channel {desired}")

    channel = await guild.create_text_channel(
        desired,
        reason=f"herdr remote {remote_id}",
    )
    mapping.set_remote_channel(remote_id, channel.id)
    return channel


async def _resolve_thread(
    guild: discord.Guild,
    remote_channel: discord.TextChannel,
    thread_id: int,
) -> discord.Thread | None:
    thread = remote_channel.get_thread(thread_id)
    if isinstance(thread, discord.Thread):
        return thread
    try:
        fetched = await guild.fetch_channel(thread_id)
    except discord.NotFound:
        return None
    if isinstance(fetched, discord.Thread):
        return fetched
    return None


async def ensure_pane_thread(
    remote_channel: discord.TextChannel,
    pane: PaneInfo,
    *,
    remote_id: str,
    mapping: MappingStore,
    bridge_cfg: BridgeConfig,
) -> discord.Thread:
    """Return the Pane Thread under a Remote Channel."""
    existing = mapping.get_pane(remote_id, pane.pane_id)
    thread: discord.Thread | None = None
    if existing:
        thread = await _resolve_thread(remote_channel.guild, remote_channel, existing.thread_id)

    desired = thread_name_for(pane, bridge_cfg)
    if thread is None:
        if bridge_cfg.read_only:
            raise RuntimeError(f"read_only: missing thread for {pane.pane_id}")
        for active in remote_channel.threads:
            if active.name == desired:
                thread = active
                break
        if thread is None:
            thread = await remote_channel.create_thread(
                name=desired,
                auto_archive_duration=10080,
                reason=f"herdr pane {pane.pane_id}",
            )
    elif thread.name != desired and not bridge_cfg.read_only:
        try:
            await thread.edit(name=desired)
        except discord.HTTPException as exc:
            log.debug("thread rename skip %s: %s", thread.id, exc)

    pm = PaneMapping(
        remote_id=remote_id,
        pane_id=pane.pane_id,
        thread_id=thread.id,
        terminal_message_id=existing.terminal_message_id if existing else None,
        label=pane.label,
        agent_status=pane.agent_status,
    )
    mapping.upsert_pane(pm)
    return thread
