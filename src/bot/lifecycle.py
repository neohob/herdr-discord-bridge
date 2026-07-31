"""Keep local bindings aligned with deleted Discord channels and threads."""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


async def on_guild_channel_delete(bot: Any, channel: Any) -> None:
    """Unbind a deleted Remote Channel and stop its Gateway client."""
    channel_id = getattr(channel, "id", None)
    for remote in bot.registry.list():
        if remote.channel_id != channel_id:
            continue
        bot.registry.unbind_channel(remote.id)
        bot.mapping.remove_remote(remote.id)
        runtime = getattr(bot, "runtime", None)
        client = runtime.clients.pop(remote.id, None) if runtime is not None else None
        if client is not None:
            try:
                await client.stop()
            except Exception:  # noqa: BLE001
                log.exception("failed stopping deleted remote %s", remote.id)
        return


async def on_thread_delete(bot: Any, thread: Any) -> None:
    """Remove a deleted Pane Thread mapping and disable observation."""
    await _remove_thread_mapping(bot, getattr(thread, "id", None))


async def on_raw_thread_delete(bot: Any, payload: Any) -> None:
    """Handle deletion of an uncached Pane Thread."""
    await _remove_thread_mapping(bot, getattr(payload, "thread_id", None))


async def _remove_thread_mapping(bot: Any, thread_id: int | None) -> None:
    if thread_id is None:
        return
    pane = bot.mapping.find_by_thread(thread_id)
    if pane is None:
        return

    bot.mapping.remove_pane(pane.remote_id, pane.pane_id)
    runtime = getattr(bot, "runtime", None)
    client = runtime.clients.get(pane.remote_id) if runtime is not None else None
    if client is None:
        return
    try:
        await client.observe_pane(pane.pane_id, False)
    except Exception:  # noqa: BLE001
        log.exception("failed disabling observe for deleted pane %s:%s", pane.remote_id, pane.pane_id)
