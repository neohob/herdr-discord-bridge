"""Edit a Discord Thread message as a live Terminal View."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import discord

if TYPE_CHECKING:
    from src.bot.config import BridgeConfig

log = logging.getLogger(__name__)

DISCORD_MSG_LIMIT = 1900


@dataclass
class _TerminalState:
    message_id: int | None = None
    last_edit: float = 0.0
    pending: bool = False
    text: str = ""
    status: str = "unknown"
    remote_id: str = ""


_states: dict[tuple[int, str], _TerminalState] = {}


def _state_key(thread: discord.Thread | Any, pane_id: str) -> tuple[int, str]:
    return (int(thread.id), pane_id)


def _get_state(thread: discord.Thread | Any, pane_id: str) -> _TerminalState:
    key = _state_key(thread, pane_id)
    state = _states.get(key)
    if state is None:
        state = _TerminalState()
        _states[key] = state
    return state


def clear_terminal_state(thread_id: int | None = None, pane_id: str | None = None) -> None:
    """Drop in-memory coalesce state (primarily for tests)."""
    if thread_id is None and pane_id is None:
        _states.clear()
        return
    drop = [
        key
        for key in _states
        if (thread_id is None or key[0] == thread_id)
        and (pane_id is None or key[1] == pane_id)
    ]
    for key in drop:
        _states.pop(key, None)


def render_terminal_content(
    *,
    remote_id: str,
    pane_id: str,
    text: str,
    status: str,
    bridge_cfg: BridgeConfig,
    max_lines: int | None = None,
) -> str:
    lines = text.splitlines()
    limit = max_lines if max_lines is not None else bridge_cfg.terminal.max_lines
    if len(lines) > limit:
        lines = lines[-limit:]
    body = "\n".join(lines)
    emoji = bridge_cfg.status_emoji.get(status, bridge_cfg.status_emoji.get("unknown", "❓"))
    header = f"{emoji} [{remote_id}:{pane_id}] {status}"
    content = f"```\n{header}\n{'─' * 40}\n{body}\n```"
    while len(content) > DISCORD_MSG_LIMIT and lines:
        lines = lines[1:]
        body = "\n".join(lines)
        content = f"```\n{header}\n{'─' * 40}\n{body}\n```"
    return content


async def _ensure_message(
    thread: discord.Thread | Any,
    state: _TerminalState,
    content: str,
) -> discord.Message | Any:
    if state.message_id:
        try:
            return await thread.fetch_message(state.message_id)
        except discord.NotFound:
            state.message_id = None
    msg = await thread.send(content)
    state.message_id = msg.id
    return msg


async def apply_terminal_view(
    thread: discord.Thread | Any,
    pane_id: str,
    text: str,
    status: str,
    bridge_cfg: BridgeConfig,
    *,
    remote_id: str = "",
    force: bool = False,
    message_id: int | None = None,
    clock: Callable[[], float] = time.time,
) -> int | None:
    """Update the Terminal Message in a Pane Thread, coalescing edits by cooldown."""
    state = _get_state(thread, pane_id)
    if message_id is not None:
        state.message_id = message_id
    if remote_id:
        state.remote_id = remote_id
    state.text = text
    state.status = status or "unknown"

    now = clock()
    cooldown = bridge_cfg.terminal.edit_cooldown
    if not force and state.message_id is not None and (now - state.last_edit) < cooldown:
        state.pending = True
        return state.message_id

    return await _flush_state(thread, pane_id, bridge_cfg, state, clock=clock)


async def flush_terminal_view(
    thread: discord.Thread | Any,
    pane_id: str,
    bridge_cfg: BridgeConfig,
    *,
    clock: Callable[[], float] = time.time,
) -> int | None:
    """Apply a pending Terminal View edit after cooldown."""
    state = _get_state(thread, pane_id)
    if not state.pending:
        return state.message_id
    return await _flush_state(thread, pane_id, bridge_cfg, state, clock=clock)


async def _flush_state(
    thread: discord.Thread | Any,
    pane_id: str,
    bridge_cfg: BridgeConfig,
    state: _TerminalState,
    *,
    clock: Callable[[], float],
) -> int | None:
    state.pending = False
    remote_id = state.remote_id or "remote"
    content = render_terminal_content(
        remote_id=remote_id,
        pane_id=pane_id,
        text=state.text,
        status=state.status,
        bridge_cfg=bridge_cfg,
    )
    try:
        if state.message_id is None:
            msg = await thread.send(content)
            state.message_id = msg.id
        else:
            msg = await _ensure_message(thread, state, content)
            await msg.edit(content=content)
        state.last_edit = clock()
        return state.message_id
    except discord.HTTPException as exc:
        log.warning("terminal view edit failed %s/%s: %s", remote_id, pane_id, exc)
        return state.message_id
