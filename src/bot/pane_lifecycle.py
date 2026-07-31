"""Retire Discord Pane threads when Herdr panes disappear."""

from __future__ import annotations

import logging
from typing import Any

import discord

from src.bot.mapping import MappingStore, PaneMapping
from src.shared.ndjson import HerdrApiError

log = logging.getLogger(__name__)


def is_pane_missing_error(exc: BaseException) -> bool:
    """True when Herdr reports the Pane id no longer exists."""
    if isinstance(exc, HerdrApiError) and str(exc.code).lower() in {
        "pane_not_found",
        "not_found",
    }:
        return True
    text = str(exc).lower()
    return "pane_not_found" in text or "pane not found" in text


async def retire_mapped_pane(
    *,
    guild: discord.Guild | Any | None,
    mapping: MappingStore,
    client: Any | None,
    remote_id: str,
    pane_id: str,
    reason: str,
) -> PaneMapping | None:
    """Archive/delete the Discord thread (best-effort), stop observe, drop mapping.

    Returns the retired mapping, or ``None`` if it was not mapped.
    """
    pane = mapping.get_pane(remote_id, pane_id)
    if pane is None:
        return None

    if client is not None:
        try:
            await client.observe_pane(pane_id, False)
        except Exception:  # noqa: BLE001
            log.debug("observe disable failed for %s:%s", remote_id, pane_id, exc_info=True)

    if guild is not None and pane.thread_id:
        thread: Any | None = None
        getter = getattr(guild, "get_thread", None)
        if callable(getter):
            thread = getter(pane.thread_id)
        if thread is None:
            fetch = getattr(guild, "fetch_channel", None)
            if callable(fetch):
                try:
                    fetched = await fetch(pane.thread_id)
                    if isinstance(fetched, discord.Thread) or hasattr(fetched, "edit"):
                        thread = fetched
                except Exception:  # noqa: BLE001
                    thread = None
        if thread is not None:
            try:
                await thread.delete(reason=reason)
            except Exception:  # noqa: BLE001
                try:
                    await thread.edit(archived=True, locked=True, reason=reason)
                except Exception:  # noqa: BLE001
                    log.debug(
                        "failed retiring Discord thread %s for %s:%s",
                        pane.thread_id,
                        remote_id,
                        pane_id,
                        exc_info=True,
                    )

    mapping.remove_pane(remote_id, pane_id)
    return pane
