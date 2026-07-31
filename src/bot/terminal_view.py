"""Terminal View: prompt-following scrollback with clear bot/user styling.

User messages stay plain Discord chat. Bot terminal output uses Embeds
(coloured sidebar + 「终端输出」 title) so the two are visually distinct.

Within one prompt turn, Gateway sliding windows are merged into a session
buffer; when a segment fills, it is sealed and a new Embed continues so the
full turn remains readable by scrolling.
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

# Embed.description limit is 4096; leave room for fences / header noise.
EMBED_BODY_LIMIT = 3500
SEGMENT_MAX_LINES = 45

COLOR_LIVE = 0x2563EB  # blue — currently updating
COLOR_SEALED = 0x64748B  # slate — frozen history segment
COLOR_BLOCKED = 0xDC2626  # red


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
    sealed_line_count: int = 0
    segment_index: int = 0
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


def merge_sliding_window(buffer: list[str], window: list[str]) -> None:
    """Merge a Gateway sliding window into accumulated scrollback."""
    if not window:
        return
    if not buffer:
        buffer.extend(window)
        return

    for start in range(max(0, len(buffer) - len(window)), len(buffer) + 1):
        chunk = buffer[start:]
        n = len(chunk)
        if n == 0:
            buffer.extend(window)
            return
        if window[:n] == chunk:
            buffer[start:] = window
            return

    for n in range(min(len(buffer), len(window)), 0, -1):
        if buffer[-n:] == window[:n]:
            buffer.extend(window[n:])
            return

    buffer.extend(window)


def absorb_gateway_window(state: _TerminalState, snapshot: str) -> None:
    window = str(snapshot or "").splitlines()
    if state.baseline_text is not None and not state.session_lines:
        base = state.baseline_text.splitlines()
        if base and window[: len(base)] == base:
            state.session_lines.extend(window[len(base) :])
            return
        if base:
            probe = list(base)
            merge_sliding_window(probe, window)
            state.session_lines.extend(probe[len(base) :])
            return
    merge_sliding_window(state.session_lines, window)


def session_body(baseline: str | None, current: str, max_lines: int) -> str:
    """Test helper — production uses session_lines."""
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


def _status_color(status: str, *, live: bool) -> int:
    if str(status).lower() in {"blocked", "waiting", "needs_input", "need_input"}:
        return COLOR_BLOCKED
    return COLOR_LIVE if live else COLOR_SEALED


def build_terminal_embed(
    *,
    remote_id: str,
    pane_id: str,
    status: str,
    bridge_cfg: BridgeConfig,
    lines: list[str],
    segment_index: int,
    live: bool,
) -> discord.Embed:
    emoji = bridge_cfg.status_emoji.get(status, bridge_cfg.status_emoji.get("unknown", "❓"))
    body_lines = list(lines)
    body = "\n".join(body_lines)
    # Trim to embed budget
    while body_lines and len(body) + 8 > EMBED_BODY_LIMIT:
        body_lines = body_lines[1:]
        body = "\n".join(body_lines)

    if live:
        title = f"{emoji} 终端输出 · 实时"
        if segment_index > 1:
            title = f"{emoji} 终端输出 · 第 {segment_index} 段 · 实时"
    else:
        title = f"{emoji} 终端输出 · 第 {segment_index} 段"

    desc = f"```\n{body}\n```" if body else "```\n(empty)\n```"
    embed = discord.Embed(
        title=title,
        description=desc,
        colour=_status_color(status, live=live),
    )
    embed.set_footer(text=f"Pane {remote_id}:{pane_id} · {status} · 上方是你的输入")
    return embed


def _lines_fit(lines: list[str], *, max_lines: int = SEGMENT_MAX_LINES) -> int:
    """Largest prefix of *lines* that fits in one embed body."""
    if not lines:
        return 0
    limit = min(len(lines), max_lines)
    for count in range(limit, 0, -1):
        body = "\n".join(lines[:count])
        if len(body) + 8 <= EMBED_BODY_LIMIT:
            return count
    return 1


async def begin_prompt_session(
    thread: discord.Thread | Any,
    pane_id: str,
    prompt_message: Any,
    *,
    remote_id: str = "",
) -> None:
    """Start a new scrollback session replied under the user's prompt."""
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
    state.session_lines = []
    state.sealed_line_count = 0
    state.segment_index = 0
    if remote_id:
        state.remote_id = remote_id
    state.choice_fingerprint = None


async def _post_or_edit_embed(
    thread: discord.Thread | Any,
    state: _TerminalState,
    embed: discord.Embed,
) -> Any:
    if state.message_id is not None and not state.needs_new_message:
        try:
            msg = await thread.fetch_message(state.message_id)
            await msg.edit(content=None, embed=embed)
            return msg
        except discord.NotFound:
            state.message_id = None
            state.needs_new_message = True

    msg: Any = None
    anchor = state.anchor_message
    if anchor is not None and hasattr(anchor, "reply"):
        try:
            msg = await anchor.reply(embed=embed)
        except Exception:  # noqa: BLE001
            log.debug("prompt reply failed; falling back to thread.send", exc_info=True)
            msg = None
    if msg is None:
        msg = await thread.send(embed=embed)
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
    state = _get_state(thread, pane_id)
    if message_id is not None and not state.needs_new_message and state.message_id is None:
        state.message_id = message_id
    if remote_id:
        state.remote_id = remote_id
    state.text = text
    state.status = status or "unknown"
    absorb_gateway_window(state, text)

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
    state.pending = False
    remote_id = state.remote_id or "remote"

    if not state.session_lines:
        # Bootstrap from raw window when absorb produced nothing yet.
        absorb_gateway_window(state, state.text)

    pending = state.session_lines[state.sealed_line_count :]
    if not pending and state.text:
        pending = str(state.text).splitlines()

    try:
        # Seal complete segments while remainder still overflows one embed.
        while True:
            fit = _lines_fit(pending)
            if fit <= 0:
                break
            remainder = pending[fit:]
            whole_fits = _lines_fit(pending) >= len(pending) and len("\n".join(pending)) + 8 <= EMBED_BODY_LIMIT
            if whole_fits or not remainder:
                break
            # Seal prefix, open a new message for the rest.
            state.segment_index += 1
            sealed = pending[:fit]
            embed = build_terminal_embed(
                remote_id=remote_id,
                pane_id=pane_id,
                status=state.status,
                bridge_cfg=bridge_cfg,
                lines=sealed,
                segment_index=state.segment_index,
                live=False,
            )
            await _post_or_edit_embed(thread, state, embed)
            state.sealed_line_count += len(sealed)
            state.needs_new_message = True
            state.message_id = None
            pending = remainder
            state.last_edit = clock()

        live_index = state.segment_index + 1 if state.sealed_line_count else max(1, state.segment_index or 1)
        embed = build_terminal_embed(
            remote_id=remote_id,
            pane_id=pane_id,
            status=state.status,
            bridge_cfg=bridge_cfg,
            lines=pending,
            segment_index=live_index,
            live=True,
        )
        await _post_or_edit_embed(thread, state, embed)
        state.last_edit = clock()
        return state.message_id
    except discord.HTTPException as exc:
        log.warning("terminal view edit failed %s/%s: %s", remote_id, pane_id, exc)
        return state.message_id
