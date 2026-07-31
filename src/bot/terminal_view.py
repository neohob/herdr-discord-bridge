"""Terminal View: one live Embed per turn, no duplicated scrollback.

Design:
- User chat = plain messages.
- Bot output = Embed titled 「终端输出」 (blue while live, grey when sealed).
- Gateway sends a sliding window; we only **append lines that are truly new**.
- We **edit one live Embed** in place. When it is full, we seal it once and
  start a new live Embed with **only the remainder** (no re-print of sealed text).
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

EMBED_BODY_LIMIT = 3200
SEGMENT_MAX_LINES = 40
COLOR_LIVE = 0x2563EB
COLOR_SEALED = 0x64748B
COLOR_BLOCKED = 0xDC2626


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
    # Deduped scrollback for this prompt turn only
    session_lines: list[str] = field(default_factory=list)
    sealed_line_count: int = 0
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
    """Return only lines in *window* that are not already covered by *session*.

    Never re-appends an overlapping sliding window. If nothing new, returns [].
    """
    if not window:
        return []
    if not session:
        return list(window)

    # Identical to current suffix → no change.
    if len(window) <= len(session) and session[-len(window) :] == window:
        return []

    # Window extends session: session suffix == window prefix.
    max_n = min(len(session), len(window))
    for n in range(max_n, 0, -1):
        if session[-n:] == window[:n]:
            return list(window[n:])

    # Window replaces a trailing region of session (same start, refreshed tail).
    for start in range(max(0, len(session) - len(window)), len(session)):
        chunk = session[start:]
        if not chunk:
            break
        if window[: len(chunk)] == chunk:
            # Net-new lines after the overlapping head.
            return list(window[len(chunk) :])

    # Last matching line heuristic (avoid full-window re-append).
    last = session[-1]
    try:
        idx = len(window) - 1 - window[::-1].index(last)
    except ValueError:
        idx = -1
    if idx >= 0:
        return list(window[idx + 1 :])

    # Discontinuous jump: do **not** dump the whole window (that caused dup spam).
    log.debug("terminal window did not align; skipping %d lines to avoid dupes", len(window))
    return []


def absorb_gateway_window(state: _TerminalState, snapshot: str) -> int:
    """Append only new lines into session_lines. Returns number added."""
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
    """Kept for older tests; prefer absorb_gateway_window in production."""
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
    while body_lines and len(body) + 8 > EMBED_BODY_LIMIT:
        body_lines = body_lines[1:]
        body = "\n".join(body_lines)

    if live:
        title = f"{emoji} 终端输出"
        if segment_index > 1:
            title = f"{emoji} 终端输出（续 {segment_index}）"
    else:
        title = f"{emoji} 终端输出（第 {segment_index} 段）"

    embed = discord.Embed(
        title=title,
        description=f"```\n{body}\n```" if body else "```\n…\n```",
        colour=_status_color(status, live=live),
    )
    embed.set_footer(text=f"{remote_id}:{pane_id} · {status} · 普通消息=你的输入")
    return embed


def _fit_count(lines: list[str]) -> int:
    if not lines:
        return 0
    limit = min(len(lines), SEGMENT_MAX_LINES)
    for count in range(limit, 0, -1):
        if len("\n".join(lines[:count])) + 8 <= EMBED_BODY_LIMIT:
            return count
    return 1


def _live_overflows(lines: list[str]) -> bool:
    if len(lines) > SEGMENT_MAX_LINES:
        return True
    return len("\n".join(lines)) + 8 > EMBED_BODY_LIMIT


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
    state.needs_new_message = True
    state.message_id = None
    state.anchor_message = prompt_message
    state.anchor_message_id = int(getattr(prompt_message, "id", 0) or 0) or None
    state.baseline_text = state.text
    state.session_lines = []
    state.sealed_line_count = 0
    state.segment_index = 0
    state.last_window = []
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
    added = absorb_gateway_window(state, text)

    # Nothing new and we already have a live message → skip Discord work.
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
    state.pending = False
    remote_id = state.remote_id or "remote"

    if not state.session_lines and state.text:
        absorb_gateway_window(state, state.text)

    live_lines = state.session_lines[state.sealed_line_count :]
    if not live_lines:
        return state.message_id

    try:
        # Only seal when the live buffer is too big — never per-line messages.
        while _live_overflows(live_lines):
            fit = _fit_count(live_lines)
            if fit <= 0 or fit >= len(live_lines):
                # Can't seal a proper prefix; trim live display only.
                break
            state.segment_index += 1
            sealed = live_lines[:fit]
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
            state.sealed_line_count += fit
            state.needs_new_message = True
            state.message_id = None
            live_lines = state.session_lines[state.sealed_line_count :]
            state.last_edit = clock()

        live_index = state.segment_index + 1 if state.sealed_line_count else 1
        embed = build_terminal_embed(
            remote_id=remote_id,
            pane_id=pane_id,
            status=state.status,
            bridge_cfg=bridge_cfg,
            lines=live_lines,
            segment_index=live_index,
            live=True,
        )
        await _post_or_edit_embed(thread, state, embed)
        state.last_edit = clock()
        return state.message_id
    except discord.HTTPException as exc:
        log.warning("terminal view edit failed %s/%s: %s", remote_id, pane_id, exc)
        return state.message_id
