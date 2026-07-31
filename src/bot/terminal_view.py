"""Terminal View: plain-text continuation from the last message's end.

Rules:
- No Embeds, no code fences — plain Discord text (readable).
- Previously posted messages are never edited again.
- Only **new** lines (after the last posted line) go into the current live
  message; when it fills, we leave it alone and start a new message for the
  remainder.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import discord

if TYPE_CHECKING:
    from src.bot.config import BridgeConfig

log = logging.getLogger(__name__)

# Discord content limit 2000; keep headroom for the header line.
MSG_BODY_LIMIT = 1700
SEGMENT_MAX_LINES = 35


@dataclass
class _TerminalState:
    message_id: int | None = None
    last_edit: float = 0.0
    pending: bool = False
    text: str = ""
    status: str = "unknown"
    remote_id: str = ""
    flush_task: asyncio.Task[None] | None = None
    needs_new_message: bool = False
    anchor_message: Any | None = None
    anchor_message_id: int | None = None
    baseline_text: str | None = None
    session_lines: list[str] = field(default_factory=list)
    # Lines already committed into Discord (sealed + current live buffer posted).
    posted_line_count: int = 0
    # Start offset of the current live message within session_lines.
    live_start: int = 0
    segment_index: int = 0
    last_window: list[str] = field(default_factory=list)
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
    return _get_state(thread, pane_id)


def clear_terminal_state(thread_id: int | None = None, pane_id: str | None = None) -> None:
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


def new_lines_from_window(session: list[str], window: list[str]) -> list[str]:
    """Only lines in *window* not already covered by *session*."""
    if not window:
        return []
    if not session:
        return list(window)
    if len(window) <= len(session) and session[-len(window) :] == window:
        return []
    max_n = min(len(session), len(window))
    for n in range(max_n, 0, -1):
        if session[-n:] == window[:n]:
            return list(window[n:])
    for start in range(max(0, len(session) - len(window)), len(session)):
        chunk = session[start:]
        if chunk and window[: len(chunk)] == chunk:
            return list(window[len(chunk) :])
    last = session[-1]
    try:
        idx = len(window) - 1 - window[::-1].index(last)
    except ValueError:
        return []
    return list(window[idx + 1 :])


def absorb_gateway_window(state: _TerminalState, snapshot: str) -> int:
    window = str(snapshot or "").splitlines()
    if window == state.last_window:
        return 0
    state.last_window = list(window)

    if state.baseline_text is not None and not state.session_lines:
        base = state.baseline_text.splitlines()
        if base and window[: len(base)] == base:
            added = list(window[len(base) :])
        elif base:
            added = new_lines_from_window(list(base), window)
        else:
            added = list(window)
    else:
        added = new_lines_from_window(state.session_lines, window)

    if added:
        state.session_lines.extend(added)
    return len(added)


def session_body(baseline: str | None, current: str, max_lines: int) -> str:
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
        return "\n".join((context + new_lines)[-max_lines:])
    return "\n".join(current_lines[-max_lines:])


def render_plain(
    *,
    remote_id: str,
    pane_id: str,
    status: str,
    bridge_cfg: BridgeConfig,
    lines: list[str],
    segment_index: int,
    live: bool,
) -> str:
    emoji = bridge_cfg.status_emoji.get(status, bridge_cfg.status_emoji.get("unknown", "❓"))
    if live:
        head = f"{emoji} 【终端】{remote_id}:{pane_id} · {status}"
        if segment_index > 1:
            head += f" · 续{segment_index}"
    else:
        head = f"{emoji} 【终端·已固定】续{segment_index} · {remote_id}:{pane_id}"
    body = "\n".join(lines)
    content = f"{head}\n{body}" if body else head
    # Hard trim if somehow over limit (should be prevented by fit).
    if len(content) > 2000:
        overflow = len(content) - 1990
        content = content[: 1990 - overflow] + "\n…"
    return content


def _fit_count(header: str, lines: list[str]) -> int:
    if not lines:
        return 0
    limit = min(len(lines), SEGMENT_MAX_LINES)
    for count in range(limit, 0, -1):
        body = "\n".join(lines[:count])
        if len(header) + 1 + len(body) <= MSG_BODY_LIMIT:
            return count
    return 1


def _overflows(header: str, lines: list[str]) -> bool:
    if len(lines) > SEGMENT_MAX_LINES:
        return True
    return len(header) + 1 + len("\n".join(lines)) > MSG_BODY_LIMIT


async def begin_prompt_session(
    thread: discord.Thread | Any,
    pane_id: str,
    prompt_message: Any,
    *,
    remote_id: str = "",
) -> None:
    state = _get_state(thread, pane_id)
    if state.flush_task is not None and not state.flush_task.done():
        state.flush_task.cancel()
        state.flush_task = None
    state.pending = False
    # Leave any previous live message untouched; open a new chain under this prompt.
    state.needs_new_message = True
    state.message_id = None
    state.anchor_message = prompt_message
    state.anchor_message_id = int(getattr(prompt_message, "id", 0) or 0) or None
    state.baseline_text = state.text
    state.session_lines = []
    state.posted_line_count = 0
    state.live_start = 0
    state.segment_index = 0
    state.last_window = []
    if remote_id:
        state.remote_id = remote_id
    state.choice_fingerprint = None


async def _send_new(
    thread: discord.Thread | Any,
    state: _TerminalState,
    content: str,
) -> Any:
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


async def _edit_live(
    thread: discord.Thread | Any,
    state: _TerminalState,
    content: str,
) -> Any:
    if state.message_id is None or state.needs_new_message:
        return await _send_new(thread, state, content)
    try:
        msg = await thread.fetch_message(state.message_id)
        await msg.edit(content=content)
        return msg
    except discord.NotFound:
        state.message_id = None
        state.needs_new_message = True
        return await _send_new(thread, state, content)


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
    state = _get_state(thread, pane_id)
    if message_id is not None and not state.needs_new_message and state.message_id is None:
        state.message_id = message_id
    if remote_id:
        state.remote_id = remote_id
    state.text = text
    state.status = status or "unknown"
    added = absorb_gateway_window(state, text)

    if (
        added == 0
        and not force
        and state.message_id is not None
        and not state.needs_new_message
        and state.session_lines
    ):
        return state.message_id

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
    """Publish only new lines after what was already posted; never rewrite sealed msgs."""
    state.pending = False
    remote_id = state.remote_id or "remote"

    if not state.session_lines and state.text:
        absorb_gateway_window(state, state.text)

    # Lines not yet reflected in any Discord message content for the live card.
    # live card covers session_lines[live_start : ...]
    # We grow the live card until full, then freeze it (never edit again) and continue.

    try:
        while True:
            live_lines = state.session_lines[state.live_start :]
            if not live_lines:
                return state.message_id

            seg = state.segment_index + 1
            header = render_plain(
                remote_id=remote_id,
                pane_id=pane_id,
                status=state.status,
                bridge_cfg=bridge_cfg,
                lines=[],
                segment_index=seg,
                live=True,
            ).split("\n", 1)[0]

            if not _overflows(header, live_lines):
                content = render_plain(
                    remote_id=remote_id,
                    pane_id=pane_id,
                    status=state.status,
                    bridge_cfg=bridge_cfg,
                    lines=live_lines,
                    segment_index=seg,
                    live=True,
                )
                await _edit_live(thread, state, content)
                state.posted_line_count = len(state.session_lines)
                state.last_edit = clock()
                return state.message_id

            # Live card would overflow: finalize a fitted prefix as a frozen message,
            # then open a new live message for the rest (previous content stays).
            fit = _fit_count(header, live_lines)
            if fit <= 0:
                fit = 1
            if fit >= len(live_lines):
                # Can't split usefully; trim oldest within this live card only.
                fit = max(1, len(live_lines) - 5)

            sealed_lines = live_lines[:fit]
            state.segment_index += 1
            sealed_content = render_plain(
                remote_id=remote_id,
                pane_id=pane_id,
                status=state.status,
                bridge_cfg=bridge_cfg,
                lines=sealed_lines,
                segment_index=state.segment_index,
                live=False,
            )
            # Write final content onto current live message, then detach so we never edit it again.
            await _edit_live(thread, state, sealed_content)
            state.live_start += fit
            state.posted_line_count = state.live_start
            state.message_id = None
            state.needs_new_message = True
            state.last_edit = clock()
            # Loop to publish remainder as a new live message.
    except discord.HTTPException as exc:
        log.warning("terminal view update failed %s/%s: %s", remote_id, pane_id, exc)
        return state.message_id
