"""Forward human Pane Thread messages to their mapped Herdr Pane."""

from __future__ import annotations

import logging
from typing import Any

from src.bot.choice_ui import clear_choice_message
from src.bot.terminal_view import begin_prompt_session

log = logging.getLogger(__name__)

# Discord message content hard limit.
_DISCORD_MSG_LIMIT = 2000


async def forward_pane_input(
    bot: Any,
    channel: Any,
    pane: Any,
    text: str,
    anchor_message: Any,
) -> bool:
    """Start a chat turn and send ``text`` into the mapped Pane.

    Returns True if input was handed to the remote client.
    """
    runtime = getattr(bot, "runtime", None)
    client = runtime.clients.get(pane.remote_id) if runtime is not None else None
    if client is None:
        log.warning("ignoring input for offline remote %s", pane.remote_id)
        return False

    trigger = getattr(channel, "trigger_typing", None)
    if callable(trigger):
        try:
            await trigger()
        except Exception:  # noqa: BLE001
            log.debug("trigger_typing failed", exc_info=True)

    await begin_prompt_session(channel, pane.pane_id, anchor_message, remote_id=pane.remote_id)
    clear_map = getattr(bot.mapping, "set_terminal_message", None)
    if callable(clear_map):
        clear_map(pane.remote_id, pane.pane_id, None)

    try:
        await client.send_input(pane.pane_id, text, keys=["enter"])
    except Exception:  # noqa: BLE001
        log.exception("failed forwarding input to %s:%s", pane.remote_id, pane.pane_id)
        return False

    try:
        await clear_choice_message(channel, pane.pane_id, note="_(superseded by new prompt)_")
    except Exception:  # noqa: BLE001
        log.debug("clear_choice_message failed", exc_info=True)
    return True


def format_agent_anchor(user: Any, text: str) -> str:
    """Public thread message that anchors the `/agent` chat turn."""
    mention = getattr(user, "mention", None) or str(getattr(user, "id", "user"))
    body = f"{mention}: {text}"
    if len(body) <= _DISCORD_MSG_LIMIT:
        return body
    # Keep mention; truncate payload.
    prefix = f"{mention}: "
    return prefix + text[: max(0, _DISCORD_MSG_LIMIT - len(prefix) - 1)] + "…"


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

    await forward_pane_input(bot, channel, pane, text, message)


def _is_command(bot: Any, text: str) -> bool:
    """Skip discord.py *text* commands only (prefix ``!``).

    Discord slash commands (``/herdr …``) are Interactions and never arrive here as
    message content. Agent / Herdr skills often start with ``/`` and must be
    forwarded into the Pane unchanged.
    """
    prefix = getattr(bot, "command_prefix", None)
    prefixes = prefix if isinstance(prefix, tuple | list | set) else (prefix,)
    return any(isinstance(value, str) and value and text.startswith(value) for value in prefixes)
