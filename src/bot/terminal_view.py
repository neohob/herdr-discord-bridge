"""Chat-style Pane replies with append-only history (no silent drops).

Discord has no native token stream. Pattern:
  1. Reply immediately with 「思考中…」
  2. Diff each pane.read snapshot against the previous one; append every
     inserted/replaced line into an in-memory turn buffer (never drop earlier lines)
  3. Throttle-edit the live Discord message; on failure keep pending and retry
  4. Near 2000 chars, freeze the bubble and continue on 「（续）」— sealed text stays

Approve / Yes-No buttons remain separate (choice_ui) when a real prompt appears.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import re
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
FLUSH_RETRY_DELAY = 1.5

# TUI spinners (braille + common unicode spinners) rewritten in-place each frame.
_BRAILLE_RE = re.compile(r"[\u2800-\u28FF]+")
_SPINNER_GLYPH_RE = re.compile(r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏◐◓◑◒]+")
_STATUS_LINE_RE = re.compile(
    r"^(?:Running|Working|Thinking|Waiting)\s+[\d.,]+\s*k?\s*tokens\s*$",
    re.IGNORECASE,
)


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
    # Append-only lines for this user turn (full history, not just the pane tip)
    session_lines: list[str] = field(default_factory=list)
    live_start: int = 0
    segment_index: int = 0
    last_window: list[str] = field(default_factory=list)
    last_snapshot: str = ""
    last_rendered: str = ""
    active: bool = False  # True after begin_prompt_session until next prompt
    choice_message_id: int | None = None
    choice_fingerprint: str | None = None
    bridge_cfg: BridgeConfig | None = None


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


def window_diff_lines(prev: list[str], window: list[str]) -> list[str]:
    """Every inserted/replaced line when the sliding window advances.

    This is the primary anti-leak path: scroll, append, and in-place rewrites all
    show up as insert/replace opcodes.
    """
    if not window:
        return []
    if not prev:
        return list(window)
    if prev == window:
        return []
    matcher = difflib.SequenceMatcher(a=prev, b=window, autojunk=False)
    out: list[str] = []
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag in {"insert", "replace"}:
            out.extend(window[j1:j2])
    return out


def _filter_duplicate_prefix(session: list[str], added: list[str]) -> list[str]:
    if not added:
        return []
    if not session:
        return list(added)
    max_n = min(len(session), len(added))
    for n in range(max_n, 0, -1):
        if session[-n:] == added[:n]:
            return list(added[n:])
    return list(added)


def _strip_spinner_glyphs(line: str) -> str:
    text = _BRAILLE_RE.sub("", line)
    text = _SPINNER_GLYPH_RE.sub("", text)
    return text.strip()


def _is_status_line(line: str) -> bool:
    """True for agent TUI token counters like ``Running 6.7k tokens``."""
    return bool(_STATUS_LINE_RE.match(_strip_spinner_glyphs(line)))


def _is_prefix_rewrite(previous: str, current: str) -> bool:
    """True when one line is a progressive rewrite of the other (typing echo)."""
    if not previous or not current or previous == current:
        return False
    return current.startswith(previous) or previous.startswith(current)


def merge_added_lines(session: list[str], added: list[str]) -> int:
    """Merge ``added`` into ``session`` with spinner/prefix coalesce.

    Returns the number of mutations (append or in-place replace). A pure replace
    still counts so Discord can refresh the live bubble.
    """
    mutations = 0
    for line in added:
        if not session:
            session.append(line)
            mutations += 1
            continue
        last = session[-1]
        if _is_status_line(line) and _is_status_line(last):
            if last != line:
                session[-1] = line
                mutations += 1
            continue
        if _is_prefix_rewrite(last, line):
            session[-1] = line
            mutations += 1
            continue
        session.append(line)
        mutations += 1
    return mutations


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
    max_n = min(len(base), len(window))
    for n in range(max_n, 0, -1):
        if base[-n:] == window[:n]:
            return list(window[n:])
    last = base[-1]
    try:
        idx = len(window) - 1 - window[::-1].index(last)
    except ValueError:
        return []
    return list(window[idx + 1 :])


def turn_lines_since_baseline(baseline: str | None, current: str) -> list[str]:
    """Best-effort first seed of post-prompt lines (used only when session is empty)."""
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
    return list(window)


def absorb_gateway_window(state: _TurnState, snapshot: str) -> int:
    """Append newly visible Pane lines into this turn's history.

    Prefer difflib window diffs so scrolled / rewritten tips cannot silently vanish.
    Never replaces earlier session_lines with only the current tip.
    """
    snap = str(snapshot or "")
    window = snap.splitlines()
    if snap == state.last_snapshot and window == state.last_window:
        return 0
    prev = list(state.last_window)
    state.last_window = list(window)
    state.last_snapshot = snap

    added: list[str] = []
    if not state.session_lines:
        base = str(state.baseline_text or "").splitlines()
        baseline_s = str(state.baseline_text or "")
        if window == base or snap == baseline_s:
            return 0
        added = _delta_from_baseline(base, window)
        if not added and prev:
            added = window_diff_lines(prev, window)
        if not added:
            # First post-prompt snapshot lost the baseline — seed once.
            added = turn_lines_since_baseline(state.baseline_text, snap)
    else:
        added = window_diff_lines(prev, window)
        if not added:
            added = new_lines_from_window(state.session_lines, window)
        if not added:
            tip = turn_lines_since_baseline(state.baseline_text, snap)
            if tip and len(tip) > len(state.session_lines) and tip[: len(state.session_lines)] == state.session_lines:
                added = tip[len(state.session_lines) :]
            elif tip:
                added = new_lines_from_window(state.session_lines, tip)

    added = _filter_duplicate_prefix(state.session_lines, added)
    if not added:
        return 0
    return merge_added_lines(state.session_lines, added)


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
    from src.bot.config import BridgeConfig

    state = _get(thread, pane_id)

    # Flush any unflushed previous-turn lines to Discord before resetting — no silent drop.
    if state.active and state.session_lines[state.live_start :]:
        cfg = state.bridge_cfg or BridgeConfig()
        state.pending = True
        try:
            await _flush(thread, pane_id, cfg, state, clock=time.time)
        except Exception:  # noqa: BLE001
            log.exception("failed flushing previous turn before new prompt %s", pane_id)

    _cancel_task(state.flush_task)
    state.flush_task = None
    _stop_typing(state)
    state.pending = False

    # Previous live bubble will not receive more edits.
    old_id = state.message_id
    old_rendered = state.last_rendered
    if old_id is not None and (
        old_rendered == PLACEHOLDER or old_rendered.endswith("\n…") or old_rendered.endswith("…")
    ):
        try:
            old = await thread.fetch_message(old_id)
            note = "（已结束，见下方新回复）" if old_rendered != PLACEHOLDER else "（已取消）"
            if str(getattr(old, "content", "") or "") in {PLACEHOLDER, old_rendered}:
                await old.edit(content=note)
        except Exception:  # noqa: BLE001
            log.debug("failed marking previous chat bubble %s", old_id, exc_info=True)

    state.anchor_message = prompt_message
    state.anchor_message_id = int(getattr(prompt_message, "id", 0) or 0) or None
    state.baseline_text = state.text
    state.session_lines = []
    state.live_start = 0
    state.segment_index = 0
    state.last_window = str(state.text or "").splitlines()
    state.last_snapshot = str(state.text or "")
    state.last_rendered = PLACEHOLDER
    state.active = True
    state.status = "working"
    state.choice_fingerprint = None
    if remote_id:
        state.remote_id = remote_id

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
    state.last_edit = 0.0


async def _send_new(thread: Any, state: _TurnState, content: str) -> Any:
    msg: Any = None
    anchor = state.anchor_message
    if anchor is not None and hasattr(anchor, "reply"):
        try:
            msg = await anchor.reply(content)
        except Exception:  # noqa: BLE001
            log.debug("anchor.reply failed; falling back to thread.send", exc_info=True)
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


def _schedule_flush(
    thread: Any,
    pane_id: str,
    bridge_cfg: BridgeConfig,
    state: _TurnState,
    delay: float,
    clock: Callable[[], float],
) -> None:
    state.pending = True
    if state.flush_task is None or state.flush_task.done():
        state.flush_task = asyncio.create_task(
            _flush_after(thread, pane_id, bridge_cfg, state, delay, clock),
            name=f"chat-stream-{getattr(thread, 'id', '?')}-{pane_id}",
        )


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
    state.bridge_cfg = bridge_cfg
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
        state.last_window = str(text or "").splitlines()
        state.last_snapshot = str(text or "")
        state.session_lines = []
        return state.message_id

    if message_id is not None and not state.needs_new_message and state.message_id is None:
        state.message_id = message_id

    changed = absorb_gateway_window(state, text)
    if not changed and not force:
        return state.message_id
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
        delay = cooldown - (now - state.last_edit)
        _schedule_flush(thread, pane_id, bridge_cfg, state, delay, clock)
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
    if not state.pending and not state.session_lines[state.live_start :]:
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
        log.warning(
            "chat stream update failed %s/%s (will retry): %s",
            state.remote_id,
            pane_id,
            exc,
        )
        # Keep unflushed live lines; retry so Discord does not permanently miss them.
        _schedule_flush(thread, pane_id, bridge_cfg, state, FLUSH_RETRY_DELAY, clock)
        return state.message_id
    except Exception:  # noqa: BLE001
        log.exception("chat stream update crashed %s/%s (will retry)", state.remote_id, pane_id)
        _schedule_flush(thread, pane_id, bridge_cfg, state, FLUSH_RETRY_DELAY, clock)
        return state.message_id
