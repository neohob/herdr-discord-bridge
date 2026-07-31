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

    # Instant Discord feedback (typing dots) before any slower work.
    trigger = getattr(channel, "trigger_typing", None)
    if callable(trigger):
        try:
            await trigger()
        except Exception:  # noqa: BLE001
            log.debug("trigger_typing failed", exc_info=True)

    # Reply bubble + typing keepalive; then forward input.
    await begin_prompt_session(channel, pane.pane_id, message, remote_id=pane.remote_id)
    clear_map = getattr(bot.mapping, "set_terminal_message", None)
    if callable(clear_map):
        clear_map(pane.remote_id, pane.pane_id, None)

    try:
        await client.send_input(pane.pane_id, text, keys=["enter"])
    except Exception:  # noqa: BLE001
        log.exception("failed forwarding input to %s:%s", pane.remote_id, pane.pane_id)
        return

    # Dismiss stale Yes/No after the turn has already started (non-blocking feel).
    try:
        await clear_choice_message(channel, pane.pane_id, note="_(superseded by new prompt)_")
    except Exception:  # noqa: BLE001
        log.debug("clear_choice_message failed", exc_info=True)


def _is_command(bot: Any, text: str) -> bool:
    prefix = getattr(bot, "command_prefix", None)
    prefixes = prefix if isinstance(prefix, tuple | list | set) else (prefix,)
    return text.startswith("/") or any(
        isinstance(value, str) and value and text.startswith(value) for value in prefixes
    )
