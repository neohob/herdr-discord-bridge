"""Reconcile Discord Pane Threads against live Herdr ``pane.list``."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord

from src.bot.config import BridgeConfig
from src.bot.discord_map import ensure_pane_thread
from src.bot.herdr.models import PaneInfo, extract_list
from src.bot.mapping import MappingStore
from src.bot.pane_lifecycle import retire_mapped_pane
from src.bot.registry import RemoteRecord

log = logging.getLogger(__name__)


async def workspace_labels(client: Any) -> dict[str, str]:
    try:
        result = await client.request("workspace.list")
    except Exception:  # noqa: BLE001
        return {}
    labels: dict[str, str] = {}
    for item in extract_list(result, "workspaces", "items"):
        workspace_id = str(item.get("workspace_id") or item.get("id") or "")
        label = str(item.get("label") or "").strip()
        if workspace_id and label:
            labels[workspace_id] = label
    return labels


async def tab_labels(client: Any) -> dict[str, str]:
    try:
        result = await client.request("tab.list")
    except Exception:  # noqa: BLE001
        return {}
    labels: dict[str, str] = {}
    for item in extract_list(result, "tabs", "items"):
        tab_id = str(item.get("tab_id") or item.get("id") or "")
        label = str(item.get("label") or "").strip()
        if tab_id and label:
            labels[tab_id] = label
    return labels


async def prune_stale_panes(
    *,
    guild: discord.Guild | Any | None,
    mapping: MappingStore,
    client: Any | None,
    remote: RemoteRecord,
    live_pane_ids: set[str],
) -> int:
    """Archive/delete Discord threads for Pane ids that no longer exist on Herdr."""
    pruned = 0
    for pane in list(mapping.all_panes(remote.id)):
        if pane.pane_id in live_pane_ids:
            continue
        retired = await retire_mapped_pane(
            guild=guild,
            mapping=mapping,
            client=client,
            remote_id=remote.id,
            pane_id=pane.pane_id,
            reason=f"Herdr pane {pane.pane_id} no longer exists",
        )
        if retired is not None:
            pruned += 1
    return pruned


async def map_panes(
    *,
    channel: discord.abc.GuildChannel | Any,
    client: Any,
    remote: RemoteRecord,
    mapping: MappingStore,
    bridge_cfg: BridgeConfig,
    panes: list[dict[str, Any]],
    sleep_between: float = 1.0,
) -> int:
    """Ensure each live pane has a Discord thread and is observed."""
    ws_labels = await workspace_labels(client)
    t_labels = await tab_labels(client)
    mapped = 0
    for pane_data in panes:
        pane = PaneInfo.from_dict(pane_data)
        if not pane.pane_id:
            continue
        pane.workspace_label = ws_labels.get(pane.workspace_id, pane.workspace_label)
        pane.tab_label = t_labels.get(pane.tab_id, pane.tab_label)
        await ensure_pane_thread(
            channel,
            pane,
            remote_id=remote.id,
            mapping=mapping,
            bridge_cfg=bridge_cfg,
        )
        await client.observe_pane(pane.pane_id, True)
        mapped += 1
        if sleep_between > 0:
            await asyncio.sleep(sleep_between)
    return mapped


async def reconcile_remote(
    *,
    guild: discord.Guild | Any | None,
    channel: discord.abc.GuildChannel | Any | None,
    client: Any,
    remote: RemoteRecord,
    mapping: MappingStore,
    bridge_cfg: BridgeConfig,
    sleep_between: float = 1.0,
) -> tuple[int, int]:
    """Return ``(mapped_count, pruned_count)`` after syncing ``pane.list``."""
    if channel is None:
        return 0, 0
    result = await client.request("pane.list")
    panes = extract_list(result, "panes", "items")
    live_ids = {
        str(item.get("pane_id") or item.get("id") or "")
        for item in panes
        if item.get("pane_id") or item.get("id")
    }
    pruned = await prune_stale_panes(
        guild=guild,
        mapping=mapping,
        client=client,
        remote=remote,
        live_pane_ids=live_ids,
    )
    mapped = await map_panes(
        channel=channel,
        client=client,
        remote=remote,
        mapping=mapping,
        bridge_cfg=bridge_cfg,
        panes=panes,
        sleep_between=sleep_between,
    )
    log.info(
        "reconciled %s: mapped=%d pruned=%d live=%d",
        remote.id,
        mapped,
        pruned,
        len(live_ids),
    )
    return mapped, pruned
