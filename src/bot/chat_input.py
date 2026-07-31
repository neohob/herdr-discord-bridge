"""Forward human Pane Thread messages to their mapped Herdr Pane."""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


async def on_message(bot: Any, message: Any) -> None:
    """Forward a non-command human message from a mapped Pane Thread."""
    if getattr(getattr(message, "author", None), "bot", False):
        return

    text = str(getattr(message, "content", "") or "")
    if not text or _is_command(bot, text):
        return

    thread_id = getattr(getattr(message, "channel", None), "id", None)
    pane = bot.mapping.find_by_thread(thread_id)
    if pane is None:
        return

    runtime = getattr(bot, "runtime", None)
    client = runtime.clients.get(pane.remote_id) if runtime is not None else None
    if client is None:
        log.warning("ignoring input for offline remote %s", pane.remote_id)
        return

    try:
        await client.send_input(pane.pane_id, text, keys=["enter"])
    except Exception:  # noqa: BLE001
        log.exception("failed forwarding input to %s:%s", pane.remote_id, pane.pane_id)


def _is_command(bot: Any, text: str) -> bool:
    prefix = getattr(bot, "command_prefix", None)
    prefixes = prefix if isinstance(prefix, tuple | list | set) else (prefix,)
    return text.startswith("/") or any(
        isinstance(value, str) and value and text.startswith(value) for value in prefixes
    )
