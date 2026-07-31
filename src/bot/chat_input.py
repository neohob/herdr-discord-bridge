"""Forward human Pane Thread messages to their mapped Herdr Pane."""

from __future__ import annotations

import logging
from typing import Any

from src.bot.choice_ui import clear_choice_message
from src.bot.terminal_view import begin_prompt_session

log = logging.getLogger(__name__)


async def on_message(bot: Any, message: Any) -> None:
    """Forward a non-command human message from a mapped Pane Thread."""
    if getattr(getattr(message, "author", None), "bot", False):
        return

    text = str(getattr(message, "content", "") or "")
    if not text or _is_command(bot, text):
        return

    channel = getattr(message, "channel", None)
    thread_id = getattr(channel, "id", None)
    pane = bot.mapping.find_by_thread(thread_id)
    if pane is None:
        return

    runtime = getattr(bot, "runtime", None)
    client = runtime.clients.get(pane.remote_id) if runtime is not None else None
    if client is None:
        log.warning("ignoring input for offline remote %s", pane.remote_id)
        return

    # Open a new live terminal reply under this prompt; dismiss stale choices.
    await clear_choice_message(channel, pane.pane_id, note="_(superseded by new prompt)_")
    await begin_prompt_session(channel, pane.pane_id, message, remote_id=pane.remote_id)
    # Force the next terminal push to create a new message (not edit the old live).
    bot.mapping.set_terminal_message(pane.remote_id, pane.pane_id, None)

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
