"""Chat-mode stream tests: one user message → one edited bot reply."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.config import BridgeConfig, TerminalConfig
from src.bot.terminal_view import (
    apply_terminal_view,
    begin_prompt_session,
    clear_terminal_state,
    flush_terminal_view,
    get_terminal_state,
    new_lines_from_window,
    render_chat_reply,
)


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


@pytest.fixture(autouse=True)
def _reset():
    clear_terminal_state()
    yield
    clear_terminal_state()


def _bridge(*, edit_cooldown: float = 1.0) -> BridgeConfig:
    return BridgeConfig(terminal=TerminalConfig(edit_cooldown=edit_cooldown))


def _text(call) -> str:
    if not call:
        return ""
    args, kwargs = call
    return str(args[0] if args else kwargs.get("content") or "")


def test_new_lines_no_dupes():
    assert new_lines_from_window(["a", "b"], ["b", "c"]) == ["c"]
    assert new_lines_from_window(["a", "b", "c"], ["a", "b", "c"]) == []


def test_render_chat_plain():
    text = render_chat_reply(["hello", "world"], status="working", continued=False, live=True)
    assert "```" not in text
    assert "hello" in text and text.endswith("…")


@pytest.mark.asyncio
async def test_begin_posts_placeholder_then_edits_same_reply():
    clock = FakeClock(1000.0)
    thread = AsyncMock()
    thread.id = 1
    reply = MagicMock(id=50, edit=AsyncMock())
    prompt = MagicMock(id=10)
    prompt.reply = AsyncMock(return_value=reply)
    thread.fetch_message = AsyncMock(return_value=reply)
    cfg = _bridge(edit_cooldown=0.0)

    await begin_prompt_session(thread, "p1", prompt, remote_id="r")
    prompt.reply.assert_awaited_once()
    assert "思考中" in _text(prompt.reply.await_args)
    thread.trigger_typing.assert_awaited()

    state = get_terminal_state(thread, "p1")
    assert state.active
    assert state.message_id == 50

    state.baseline_text = "pre"
    state.session_lines = []
    state.last_window = []
    await apply_terminal_view(
        thread, "p1", "pre\nhello", "working", cfg, clock=clock.now, remote_id="r"
    )
    reply.edit.assert_awaited()
    assert "hello" in _text(reply.edit.await_args)
    assert prompt.reply.await_count == 1


@pytest.mark.asyncio
async def test_no_active_turn_skips_discord_updates():
    clock = FakeClock(1000.0)
    thread = AsyncMock()
    thread.id = 2
    thread.send = AsyncMock()
    cfg = _bridge(edit_cooldown=0.0)
    mid = await apply_terminal_view(thread, "p1", "noise", "working", cfg, clock=clock.now)
    thread.send.assert_not_awaited()
    assert mid is None


@pytest.mark.asyncio
async def test_coalesce_flush():
    clock = FakeClock(1000.0)
    thread = AsyncMock()
    thread.id = 3
    reply = MagicMock(id=7, edit=AsyncMock())
    prompt = MagicMock(id=1)
    prompt.reply = AsyncMock(return_value=reply)
    thread.fetch_message = AsyncMock(return_value=reply)
    cfg = _bridge(edit_cooldown=2.0)

    await begin_prompt_session(thread, "p1", prompt, remote_id="r")
    state = get_terminal_state(thread, "p1")
    state.baseline_text = "x"
    state.session_lines = []
    state.last_window = []
    await apply_terminal_view(thread, "p1", "x\na", "working", cfg, clock=clock.now)
    await apply_terminal_view(thread, "p1", "x\na\nb", "working", cfg, clock=clock.now)
    assert reply.edit.await_count <= 1
    clock.advance(2.5)
    await flush_terminal_view(thread, "p1", cfg, clock=clock.now)
    assert "b" in _text(reply.edit.await_args)


@pytest.mark.asyncio
async def test_deferred_task_flush():
    clock = FakeClock(1000.0)
    thread = AsyncMock()
    thread.id = 4
    reply = MagicMock(id=8, edit=AsyncMock())
    prompt = MagicMock(id=2)
    prompt.reply = AsyncMock(return_value=reply)
    thread.fetch_message = AsyncMock(return_value=reply)
    # Cooldown uses max(edit_cooldown, MIN_EDIT_INTERVAL=1.0)
    cfg = _bridge(edit_cooldown=1.0)

    await begin_prompt_session(thread, "p1", prompt, remote_id="r")
    state = get_terminal_state(thread, "p1")
    state.baseline_text = ""
    state.session_lines = []
    state.last_window = []
    await apply_terminal_view(thread, "p1", "one", "working", cfg, clock=clock.now)
    assert "one" in _text(reply.edit.await_args)
    await apply_terminal_view(thread, "p1", "one\ntwo", "working", cfg, clock=clock.now)
    assert state.pending
    clock.advance(1.1)
    # Drive the scheduled flush with the same clock (task uses real sleep; flush manually).
    await flush_terminal_view(thread, "p1", cfg, clock=clock.now)
    assert "two" in _text(reply.edit.await_args)


@pytest.mark.asyncio
async def test_same_snapshot_keeps_thinking_placeholder():
    clock = FakeClock(1000.0)
    thread = AsyncMock()
    thread.id = 5
    reply = MagicMock(id=9, edit=AsyncMock())
    prompt = MagicMock(id=3)
    prompt.reply = AsyncMock(return_value=reply)
    thread.fetch_message = AsyncMock(return_value=reply)
    cfg = _bridge(edit_cooldown=0.0)

    await begin_prompt_session(thread, "p1", prompt, remote_id="r")
    state = get_terminal_state(thread, "p1")
    state.baseline_text = "line1\nline2"
    state.text = "line1\nline2"
    await apply_terminal_view(
        thread, "p1", "line1\nline2", "working", cfg, clock=clock.now
    )
    reply.edit.assert_not_awaited()
    assert state.message_id == 9


@pytest.mark.asyncio
async def test_sliding_window_still_streams_new_lines():
    clock = FakeClock(1000.0)
    thread = AsyncMock()
    thread.id = 6
    reply = MagicMock(id=11, edit=AsyncMock())
    prompt = MagicMock(id=4)
    prompt.reply = AsyncMock(return_value=reply)
    thread.fetch_message = AsyncMock(return_value=reply)
    cfg = _bridge(edit_cooldown=0.0)

    await begin_prompt_session(thread, "p1", prompt, remote_id="r")
    state = get_terminal_state(thread, "p1")
    base = [f"L{i}" for i in range(10)]
    state.baseline_text = "\n".join(base)
    state.session_lines = []
    state.last_window = list(base)

    await apply_terminal_view(
        thread, "p1", "\n".join(base), "working", cfg, clock=clock.now
    )
    reply.edit.assert_not_awaited()

    scrolled = base[2:] + ["agent says hi"]
    await apply_terminal_view(
        thread, "p1", "\n".join(scrolled), "working", cfg, clock=clock.now
    )
    reply.edit.assert_awaited()
    assert "agent says hi" in _text(reply.edit.await_args)
    assert "思考中" not in _text(reply.edit.await_args)


@pytest.mark.asyncio
async def test_lost_baseline_still_shows_window():
    """If pane.read scrolls the prompt baseline away, still update past 思考中."""
    clock = FakeClock(1000.0)
    thread = AsyncMock()
    thread.id = 7
    reply = MagicMock(id=12, edit=AsyncMock())
    prompt = MagicMock(id=5)
    prompt.reply = AsyncMock(return_value=reply)
    thread.fetch_message = AsyncMock(return_value=reply)
    cfg = _bridge(edit_cooldown=0.0)

    await begin_prompt_session(thread, "p1", prompt, remote_id="r")
    state = get_terminal_state(thread, "p1")
    state.baseline_text = "old-a\nold-b\nold-c"
    state.last_window = state.baseline_text.splitlines()
    state.session_lines = []

    # Completely different window (baseline scrolled/cleared off).
    await apply_terminal_view(
        thread,
        "p1",
        "agent line 1\nagent line 2",
        "working",
        cfg,
        clock=clock.now,
    )
    reply.edit.assert_awaited()
    body = _text(reply.edit.await_args)
    assert "agent line 2" in body
    assert "思考中" not in body


@pytest.mark.asyncio
async def test_append_keeps_earlier_lines_after_scroll():
    """Sliding pane tip must not wipe earlier turn history from Discord buffer."""
    clock = FakeClock(1000.0)
    thread = AsyncMock()
    thread.id = 8
    reply = MagicMock(id=13, edit=AsyncMock())
    cont = MagicMock(id=14, edit=AsyncMock())
    prompt = MagicMock(id=6)
    prompt.reply = AsyncMock(side_effect=[reply, cont])
    thread.fetch_message = AsyncMock(return_value=reply)
    thread.send = AsyncMock(return_value=cont)
    cfg = _bridge(edit_cooldown=0.0)

    await begin_prompt_session(thread, "p1", prompt, remote_id="r")
    state = get_terminal_state(thread, "p1")
    state.baseline_text = "base"
    state.last_window = ["base"]
    state.session_lines = []

    await apply_terminal_view(thread, "p1", "base\nearly\nmiddle", "working", cfg, clock=clock.now)
    assert state.session_lines == ["early", "middle"]

    # Window scrolled: "early" fell off the pane tip; only middle+late visible.
    clock.advance(2.0)
    await apply_terminal_view(thread, "p1", "middle\nlate", "working", cfg, clock=clock.now)
    assert state.session_lines == ["early", "middle", "late"]
    assert "early" in _text(reply.edit.await_args)
    assert "late" in _text(reply.edit.await_args)


@pytest.mark.asyncio
async def test_long_turn_seals_and_continues_new_message():
    clock = FakeClock(1000.0)
    thread = AsyncMock()
    thread.id = 9
    reply = MagicMock(id=20, edit=AsyncMock())
    cont = MagicMock(id=21, edit=AsyncMock())
    prompt = MagicMock(id=7)
    prompt.reply = AsyncMock(side_effect=[reply, cont])
    thread.fetch_message = AsyncMock(side_effect=[reply, cont])
    cfg = _bridge(edit_cooldown=0.0)

    await begin_prompt_session(thread, "p1", prompt, remote_id="r")
    state = get_terminal_state(thread, "p1")
    state.baseline_text = ""
    state.last_window = []
    state.session_lines = []

    # Build enough lines to exceed SOFT_LIMIT when rendered.
    lines = [f"line-{i}-" + ("x" * 80) for i in range(40)]
    await apply_terminal_view(thread, "p1", "\n".join(lines), "working", cfg, clock=clock.now)
    assert state.segment_index >= 1 or state.live_start > 0 or prompt.reply.await_count >= 2
    # First bubble was edited (sealed or live); history not discarded from buffer.
    assert len(state.session_lines) == 40
