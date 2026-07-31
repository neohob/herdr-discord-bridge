"""Live Terminal View messages that follow Discord prompts."""

from __future__ import annotations

import asyncio
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
    flush_task: asyncio.Task[None] | None = None
    # Prompt-following session
    needs_new_message: bool = False
    anchor_message: Any | None = None
    anchor_message_id: int | None = None
    baseline_text: str | None = None
    # Choice UI bookkeeping (message lives separately under the live terminal)
    choice_message_id: int | None = None
    choice_fingerprint: str | None = None


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


def get_terminal_state(thread: discord.Thread | Any, pane_id: str) -> _TerminalState:
    """Expose in-memory state for choice UI wiring."""
    return _get_state(thread, pane_id)


def clear_terminal_state(thread_id: int | None = None, pane_id: str | None = None) -> None:
    """Drop in-memory coalesce state (primarily for tests)."""
    if thread_id is None and pane_id is None:
        for state in _states.values():
            if state.flush_task is not None:
                state.flush_task.cancel()
        _states.clear()
        return
    drop = [
        key
        for key in _states
        if (thread_id is None or key[0] == thread_id)
        and (pane_id is None or key[1] == pane_id)
    ]
    for key in drop:
        state = _states.pop(key, None)
        if state is not None and state.flush_task is not None:
            state.flush_task.cancel()


def session_body(baseline: str | None, current: str, max_lines: int) -> str:
    """Prefer lines added after *baseline*; fall back to a sliding window."""
    current_lines = current.splitlines()
    if not current_lines:
        return ""
    if baseline is None:
        return "\n".join(current_lines[-max_lines:])

    base_lines = baseline.splitlines()
    if base_lines and current_lines[: len(base_lines)] == base_lines:
        new_lines = current_lines[len(base_lines) :]
        if not new_lines:
            return "\n".join(current_lines[-min(max_lines, 8) :])
        context = current_lines[max(0, len(base_lines) - 2) : len(base_lines)]
        body = context + new_lines
        return "\n".join(body[-max_lines:])

    return "\n".join(current_lines[-max_lines:])


def render_terminal_content(
    *,
    remote_id: str,
    pane_id: str,
    text: str,
    status: str,
    bridge_cfg: BridgeConfig,
    max_lines: int | None = None,
    baseline_text: str | None = None,
    reply_mark: bool = False,
) -> str:
    limit = max_lines if max_lines is not None else bridge_cfg.terminal.max_lines
    body = session_body(baseline_text, text, limit)
    lines = body.splitlines()
    emoji = bridge_cfg.status_emoji.get(status, bridge_cfg.status_emoji.get("unknown", "❓"))
    mark = " ↳" if reply_mark else ""
    header = f"{emoji}{mark} [{remote_id}:{pane_id}] {status}"
    content = f"```\n{header}\n{'─' * 40}\n{body}\n```"
    while len(content) > DISCORD_MSG_LIMIT and lines:
        lines = lines[1:]
        body = "\n".join(lines)
        content = f"```\n{header}\n{'─' * 40}\n{body}\n```"
    return content


async def begin_prompt_session(
    thread: discord.Thread | Any,
    pane_id: str,
    prompt_message: Any,
    *,
    remote_id: str = "",
) -> None:
    """Freeze the previous live message and bind the next view to *prompt_message*."""
    state = _get_state(thread, pane_id)
    if state.flush_task is not None and not state.flush_task.done():
        state.flush_task.cancel()
        state.flush_task = None
    state.pending = False
    state.needs_new_message = True
    state.message_id = None
    state.anchor_message = prompt_message
    state.anchor_message_id = int(getattr(prompt_message, "id", 0) or 0) or None
    state.baseline_text = state.text
    if remote_id:
        state.remote_id = remote_id
    # New prompt invalidates any pending choice buttons (caller also clears Discord msg).
    state.choice_fingerprint = None


async def _post_or_edit(
    thread: discord.Thread | Any,
    state: _TerminalState,
    content: str,
) -> Any:
    if state.message_id is not None and not state.needs_new_message:
        try:
            msg = await thread.fetch_message(state.message_id)
            await msg.edit(content=content)
            return msg
        except discord.NotFound:
            state.message_id = None
            state.needs_new_message = True

    msg: Any = None
    anchor = state.anchor_message
    if anchor is not None and hasattr(anchor, "reply"):
        try:
            msg = await anchor.reply(content)
        except Exception:  # noqa: BLE001
            log.debug("prompt reply failed; falling back to thread.send", exc_info=True)
            msg = None
    if msg is None:
        msg = await thread.send(content)
    state.message_id = int(msg.id)
    state.needs_new_message = False
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
    """Update the live Terminal Message, coalescing edits by cooldown."""
    state = _get_state(thread, pane_id)
    # Mapping recovery: only reuse a stored id when we are not opening a fresh prompt reply.
    if message_id is not None and not state.needs_new_message and state.message_id is None:
        state.message_id = message_id
    if remote_id:
        state.remote_id = remote_id
    state.text = text
    state.status = status or "unknown"

    now = clock()
    cooldown = bridge_cfg.terminal.edit_cooldown
    if (
        not force
        and state.message_id is not None
        and not state.needs_new_message
        and (now - state.last_edit) < cooldown
    ):
        state.pending = True
        if state.flush_task is None or state.flush_task.done():
            delay = cooldown - (now - state.last_edit)
            state.flush_task = asyncio.create_task(
                _flush_after_cooldown(thread, pane_id, bridge_cfg, state, delay, clock),
                name=f"terminal-view-flush-{thread.id}-{pane_id}",
            )
        return state.message_id

    return await _flush_state(thread, pane_id, bridge_cfg, state, clock=clock)


async def _flush_after_cooldown(
    thread: discord.Thread | Any,
    pane_id: str,
    bridge_cfg: BridgeConfig,
    state: _TerminalState,
    delay: float,
    clock: Callable[[], float],
) -> None:
    try:
        await asyncio.sleep(max(0.0, delay))
        if state.pending:
            await flush_terminal_view(thread, pane_id, bridge_cfg, clock=clock)
    except asyncio.CancelledError:
        raise
    finally:
        if state.flush_task is asyncio.current_task():
            state.flush_task = None


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
        baseline_text=state.baseline_text if state.anchor_message_id else None,
        reply_mark=state.anchor_message_id is not None,
    )
    try:
        await _post_or_edit(thread, state, content)
        state.last_edit = clock()
        return state.message_id
    except discord.HTTPException as exc:
        log.warning("terminal view edit failed %s/%s: %s", remote_id, pane_id, exc)
        return state.message_id
