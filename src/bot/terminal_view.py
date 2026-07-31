"""Chat-style Pane replies: one user message → one Bot reply (streamed via edits).

Discord has no native token stream. The standard pattern is:
  1. Reply immediately with a placeholder
  2. Throttle-edit that same message as new Pane output arrives
  3. If the reply hits the 2000-char cap, freeze it and continue on a new
     message with only the remainder (previous text is never rewritten)

Approve / Yes-No buttons remain separate (choice_ui) when a real prompt appears.
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

MSG_LIMIT = 2000
# Leave room so mid-stream edits don't constantly hit the ceiling.
SOFT_LIMIT = 1800
# Default stream cadence if config cooldown is very low.
MIN_EDIT_INTERVAL = 1.0
# Discord typing indicator expires ~10s; refresh while waiting/streaming.
TYPING_REFRESH = 8.0
PLACEHOLDER = "思考中…"


@dataclass
class _TurnState:
    message_id: int | None = None
    last_edit: float = 0.0
    pending: bool = False
    text: str = ""
    status: str = "unknown"
    remote_id: str = ""
    flush_task: asyncio.Task[None] | None = None
    typing_task: asyncio.Task[None] | None = None
    needs_new_message: bool = False
    anchor_message: Any | None = None
    anchor_message_id: int | None = None
    baseline_text: str | None = None
    # Deduped lines for this user turn only
    session_lines: list[str] = field(default_factory=list)
    live_start: int = 0
    segment_index: int = 0
    last_window: list[str] = field(default_factory=list)
    last_rendered: str = ""
    active: bool = False  # True after begin_prompt_session until next prompt
    choice_message_id: int | None = None
    choice_fingerprint: str | None = None


_states: dict[tuple[int, str], _TurnState] = {}


def _key(thread: Any, pane_id: str) -> tuple[int, str]:
    return (int(thread.id), pane_id)


def _get(thread: Any, pane_id: str) -> _TurnState:
    k = _key(thread, pane_id)
    state = _states.get(k)
    if state is None:
        state = _TurnState()
        _states[k] = state
    return state


def get_terminal_state(thread: Any, pane_id: str) -> _TurnState:
    return _get(thread, pane_id)


def _cancel_task(task: asyncio.Task[None] | None) -> None:
    if task is not None and not task.done():
        task.cancel()


def clear_terminal_state(thread_id: int | None = None, pane_id: str | None = None) -> None:
    if thread_id is None and pane_id is None:
        for state in _states.values():
            _cancel_task(state.flush_task)
            _cancel_task(state.typing_task)
        _states.clear()
        return
    drop = [
        k
        for k in _states
        if (thread_id is None or k[0] == thread_id) and (pane_id is None or k[1] == pane_id)
    ]
    for k in drop:
        state = _states.pop(k, None)
        if state is not None:
            _cancel_task(state.flush_task)
            _cancel_task(state.typing_task)


def new_lines_from_window(session: list[str], window: list[str]) -> list[str]:
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


def _delta_from_baseline(base: list[str], window: list[str]) -> list[str]:
    """New lines after a prompt baseline, tolerant of pane.read sliding windows."""
    if not window:
        return []
    if not base:
        return list(window)
    if window == base:
        return []
    if len(window) >= len(base) and window[: len(base)] == base:
        return list(window[len(base) :])
    # Scrolled: longest suffix(base) == prefix(window)
    max_n = min(len(base), len(window))
    for n in range(max_n, 0, -1):
        if base[-n:] == window[:n]:
            return list(window[n:])
    # Last baseline line still visible somewhere in the window
    last = base[-1]
    try:
        idx = len(window) - 1 - window[::-1].index(last)
    except ValueError:
        return []
    return list(window[idx + 1 :])


def turn_lines_since_baseline(baseline: str | None, current: str) -> list[str]:
    """Full chat-turn body for the latest pane snapshot (not an incremental append).

    Returns [] when nothing has changed since the user prompt — caller keeps 「思考中…」.
    If the sliding window loses the baseline, still recover visible post-prompt output.
    """
    window = str(current or "").splitlines()
    if not window:
        return []
    base = str(baseline or "").splitlines()
    if not base:
        return list(window)
    if window == base:
        return []

    delta = _delta_from_baseline(base, window)
    if delta:
        return delta

    # In-place edits / partial scroll: walk baseline from the end for an anchor.
    for line in reversed(base):
        if line == "":
            continue
        try:
            idx = len(window) - 1 - window[::-1].index(line)
        except ValueError:
            continue
        after = list(window[idx + 1 :])
        if after:
            return after
        # Anchor is the last line but the line itself changed elsewhere — fall through.

    # Screen cleared or baseline fully gone: the current window *is* the turn.
    return list(window)


def absorb_gateway_window(state: _TurnState, snapshot: str) -> int:
    """Recompute this turn's session_lines from the latest Gateway window.

    Returns 0 when the visible turn body is unchanged (keep 「思考中…」 / skip edit).
    """
    window = str(snapshot or "").splitlines()
    turn = turn_lines_since_baseline(state.baseline_text, snapshot)
    state.last_window = list(window)
    if turn == state.session_lines:
        return 0
    # Sealed Discord segments stay put; only replace the unsent tail when possible.
    sealed = state.session_lines[: state.live_start]
    if sealed and len(turn) >= len(sealed) and turn[: len(sealed)] == sealed:
        state.session_lines = list(turn)
    elif sealed:
        # Prefix no longer matches (hard scroll) — restart live rendering on new body.
        state.session_lines = list(turn)
        state.live_start = 0
        state.segment_index = 0
        state.needs_new_message = True
        state.message_id = None
    else:
        state.session_lines = list(turn)
    return 1


def session_body(baseline: str | None, current: str, max_lines: int) -> str:
    lines = turn_lines_since_baseline(baseline, current)
    if not lines:
        return ""
    return "\n".join(lines[-max_lines:])


def render_chat_reply(
    lines: list[str],
    *,
    status: str,
    continued: bool,
    live: bool,
) -> str:
    """Plain chatbot-style body — no code fences, no heavy chrome."""
    body = "\n".join(lines).rstrip()
    if continued and body:
        body = f"（续）\n{body}"
    if not body:
        # Never replace an on-screen 「思考中…」 with a bare ellipsis via edit.
        body = PLACEHOLDER if live else "…"
    elif live and str(status).lower() in {"working", "unknown", ""}:
        if not body.endswith("…"):
            body = f"{body}\n…"
    if len(body) > MSG_LIMIT:
        body = "…" + body[-(MSG_LIMIT - 1) :]
    return body


def _fit_prefix(lines: list[str], *, continued: bool) -> int:
    if not lines:
        return 0
    for count in range(len(lines), 0, -1):
        text = render_chat_reply(lines[:count], status="idle", continued=continued, live=False)
        if len(text) <= SOFT_LIMIT:
            return count
    return 1


async def _trigger_typing(thread: Any) -> None:
    """Show Discord's native 「正在输入…」 indicator (the channel typing dots)."""
    trigger = getattr(thread, "trigger_typing", None)
    if not callable(trigger):
        return
    try:
        await trigger()
    except Exception:  # noqa: BLE001
        log.debug("trigger_typing failed", exc_info=True)


async def _typing_keepalive(thread: Any, state: _TurnState) -> None:
    """Refresh typing while the turn is active and still working."""
    try:
        while state.active:
            status = str(state.status).lower()
            if status not in {"working", "unknown", ""}:
                break
            await _trigger_typing(thread)
            await asyncio.sleep(TYPING_REFRESH)
    except asyncio.CancelledError:
        raise
    finally:
        if state.typing_task is asyncio.current_task():
            state.typing_task = None


def _start_typing(thread: Any, state: _TurnState) -> None:
    _cancel_task(state.typing_task)
    state.typing_task = None
    try:
        state.typing_task = asyncio.create_task(
            _typing_keepalive(thread, state),
            name=f"chat-typing-{getattr(thread, 'id', '?')}",
        )
    except RuntimeError:
        # No running loop in some unit tests — typing is best-effort.
        pass


def _stop_typing(state: _TurnState) -> None:
    _cancel_task(state.typing_task)
    state.typing_task = None


async def begin_prompt_session(
    thread: Any,
    pane_id: str,
    prompt_message: Any,
    *,
    remote_id: str = "",
) -> None:
    """Start a chat turn: reply under the user message with a streaming placeholder."""
    state = _get(thread, pane_id)
    _cancel_task(state.flush_task)
    state.flush_task = None
    _stop_typing(state)
    state.pending = False
    state.anchor_message = prompt_message
    state.anchor_message_id = int(getattr(prompt_message, "id", 0) or 0) or None
    state.baseline_text = state.text
    state.session_lines = []
    state.live_start = 0
    state.segment_index = 0
    # Seed so the first post-prompt push can diff against the pre-prompt tip.
    state.last_window = str(state.text or "").splitlines()
    state.last_rendered = PLACEHOLDER
    state.active = True
    state.status = "working"
    state.choice_fingerprint = None
    if remote_id:
        state.remote_id = remote_id

    # Instant feedback: typing dots + placeholder bubble under the user message.
    await _trigger_typing(thread)
    _start_typing(thread, state)
    try:
        if hasattr(prompt_message, "reply"):
            msg = await prompt_message.reply(PLACEHOLDER)
        else:
            msg = await thread.send(PLACEHOLDER)
    except Exception:  # noqa: BLE001
        log.exception("failed posting chat placeholder")
        state.message_id = None
        state.needs_new_message = True
        return
    state.message_id = int(msg.id)
    state.needs_new_message = False
    # Allow the first content edit immediately after the placeholder.
    state.last_edit = 0.0


async def _send_new(thread: Any, state: _TurnState, content: str) -> Any:
    msg: Any = None
    anchor = state.anchor_message
    if anchor is not None and hasattr(anchor, "reply"):
        try:
            msg = await anchor.reply(content)
        except Exception:  # noqa: BLE001
            msg = None
    if msg is None:
        msg = await thread.send(content)
    state.message_id = int(msg.id)
    state.needs_new_message = False
    return msg


async def _edit_live(thread: Any, state: _TurnState, content: str) -> Any:
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
    thread: Any,
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
    """Stream Pane output into the active chat reply (no-op without an active turn)."""
    state = _get(thread, pane_id)
    if remote_id:
        state.remote_id = remote_id
    state.text = text
    state.status = status or "unknown"
    status_l = str(state.status).lower()
    if state.active and status_l not in {"working", "unknown", ""}:
        _stop_typing(state)
    elif state.active and status_l in {"working", "unknown", ""} and (
        state.typing_task is None or state.typing_task.done()
    ):
        _start_typing(thread, state)

    # Chatbot mode: ignore background Pane noise until the user sends a message.
    if not state.active and not force:
        # Keep last snapshot so the next turn's baseline is fresh; do not touch Discord.
        state.last_window = str(text or "").splitlines()
        state.session_lines = []
        return state.message_id

    if message_id is not None and not state.needs_new_message and state.message_id is None:
        state.message_id = message_id

    changed = absorb_gateway_window(state, text)
    # Nothing new since the prompt — keep 「思考中…」, do not edit to "…".
    if not changed and not force:
        return state.message_id
    # Still only the placeholder state (empty turn body).
    if not state.session_lines and not force:
        return state.message_id

    now = clock()
    cooldown = max(float(bridge_cfg.terminal.edit_cooldown), MIN_EDIT_INTERVAL)
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
                _flush_after(thread, pane_id, bridge_cfg, state, delay, clock),
                name=f"chat-stream-{thread.id}-{pane_id}",
            )
        return state.message_id

    return await _flush(thread, pane_id, bridge_cfg, state, clock=clock)


async def _flush_after(
    thread: Any,
    pane_id: str,
    bridge_cfg: BridgeConfig,
    state: _TurnState,
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
    thread: Any,
    pane_id: str,
    bridge_cfg: BridgeConfig,
    *,
    clock: Callable[[], float] = time.time,
) -> int | None:
    state = _get(thread, pane_id)
    if not state.pending:
        return state.message_id
    return await _flush(thread, pane_id, bridge_cfg, state, clock=clock)


async def _flush(
    thread: Any,
    pane_id: str,
    bridge_cfg: BridgeConfig,
    state: _TurnState,
    *,
    clock: Callable[[], float],
) -> int | None:
    state.pending = False
    if not state.active:
        return state.message_id

    if not state.session_lines and state.text:
        absorb_gateway_window(state, state.text)

    # Still waiting for Pane output after the user prompt.
    if not state.session_lines[state.live_start :]:
        return state.message_id

    try:
        while True:
            live_lines = state.session_lines[state.live_start :]
            if not live_lines:
                return state.message_id
            continued = state.segment_index > 0
            content = render_chat_reply(
                live_lines,
                status=state.status,
                continued=continued,
                live=True,
            )
            if content == state.last_rendered and not state.needs_new_message:
                state.last_edit = clock()
                return state.message_id

            if len(content) <= SOFT_LIMIT or len(live_lines) <= 1:
                await _edit_live(thread, state, content)
                state.last_rendered = content
                state.last_edit = clock()
                return state.message_id

            # Freeze a fitted prefix; continue streaming on a new reply.
            fit = _fit_prefix(live_lines, continued=continued)
            if fit >= len(live_lines):
                fit = max(1, len(live_lines) - 1)
            sealed = render_chat_reply(
                live_lines[:fit],
                status=state.status,
                continued=continued,
                live=False,
            )
            await _edit_live(thread, state, sealed)
            state.last_rendered = sealed
            state.live_start += fit
            state.segment_index += 1
            state.message_id = None
            state.needs_new_message = True
            state.last_edit = clock()
    except discord.HTTPException as exc:
        log.warning("chat stream update failed %s/%s: %s", state.remote_id, pane_id, exc)
        return state.message_id
