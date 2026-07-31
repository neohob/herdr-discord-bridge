"""Unit tests for Terminal View — no duplicate scrollback."""

from __future__ import annotations

import asyncio
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
    session_body,
)


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def _make_thread(*, message_id: int = 42) -> tuple[AsyncMock, MagicMock]:
    thread = AsyncMock()
    thread.id = 1001
    msg = MagicMock()
    msg.id = message_id
    msg.edit = AsyncMock()
    thread.fetch_message = AsyncMock(return_value=msg)
    thread.send = AsyncMock(return_value=msg)
    return thread, msg


def _embed_text(call) -> str:
    if not call:
        return ""
    _args, kwargs = call
    embed = kwargs.get("embed")
    if embed is None:
        return str(kwargs.get("content") or "")
    footer = embed.footer.text if embed.footer else ""
    return f"{embed.title}\n{embed.description}\n{footer}"


def _sent_text(thread: AsyncMock) -> str:
    return _embed_text(thread.send.await_args)


def _edited_text(msg: MagicMock) -> str:
    return _embed_text(msg.edit.await_args)


@pytest.fixture(autouse=True)
def _reset_terminal_state():
    clear_terminal_state()
    yield
    clear_terminal_state()


def _bridge(*, edit_cooldown: float = 2.0) -> BridgeConfig:
    return BridgeConfig(terminal=TerminalConfig(edit_cooldown=edit_cooldown))


def test_new_lines_from_sliding_window_no_dupes():
    session = ["a", "b", "c", "d"]
    # Typical slide: drop a, add e
    assert new_lines_from_window(session, ["b", "c", "d", "e"]) == ["e"]
    # Same window again
    assert new_lines_from_window(session + ["e"], ["b", "c", "d", "e"]) == []
    # Unrelated full window must NOT be re-appended
    assert new_lines_from_window(["x", "y"], ["1", "2", "3"]) == []


def test_session_body_prefers_new_lines_after_baseline():
    assert session_body("a\nb\nc", "a\nb\nc\nd\ne", 50) == "b\nc\nd\ne"


@pytest.mark.asyncio
async def test_sliding_updates_edit_one_message_without_dup_lines():
    clock = FakeClock(1000.0)
    thread, msg = _make_thread()
    cfg = _bridge(edit_cooldown=0.0)

    await apply_terminal_view(thread, "p1", "a\nb\nc", "working", cfg, clock=clock.now)
    await apply_terminal_view(thread, "p1", "b\nc\nd", "working", cfg, clock=clock.now)
    await apply_terminal_view(thread, "p1", "c\nd\ne", "working", cfg, clock=clock.now)

    # One send, then edits — never a new message per line
    assert thread.send.await_count == 1
    state = get_terminal_state(thread, "p1")
    assert state.session_lines == ["a", "b", "c", "d", "e"]
    # Live embed body should list each line once
    body = _edited_text(msg) or _sent_text(thread)
    assert body.count("```") >= 2
    for line in ("a", "b", "c", "d", "e"):
        # description contains line once inside fences
        assert _edited_text(msg).count(line) == 1 or (
            thread.send.await_count == 1 and "e" in _edited_text(msg)
        )
    desc = msg.edit.await_args.kwargs["embed"].description
    for line in ("a", "b", "c", "d", "e"):
        assert desc.count(line) == 1


@pytest.mark.asyncio
async def test_first_apply_sends_terminal_embed():
    clock = FakeClock(1000.0)
    thread, _msg = _make_thread()
    cfg = _bridge()

    mid = await apply_terminal_view(thread, "pane-1", "hello", "idle", cfg, clock=clock.now)
    assert mid == 42
    text = _sent_text(thread)
    assert "终端输出" in text
    assert "hello" in text


@pytest.mark.asyncio
async def test_coalesce_skips_edit_within_cooldown():
    clock = FakeClock(1000.0)
    thread, msg = _make_thread()
    cfg = _bridge(edit_cooldown=2.0)

    await apply_terminal_view(thread, "pane-1", "first", "idle", cfg, clock=clock.now)
    await apply_terminal_view(thread, "pane-1", "first\nsecond", "working", cfg, clock=clock.now)

    thread.send.assert_awaited_once()
    msg.edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_flush_after_cooldown_applies_pending_edit():
    clock = FakeClock(1000.0)
    thread, msg = _make_thread()
    cfg = _bridge(edit_cooldown=2.0)

    await apply_terminal_view(thread, "pane-1", "first", "idle", cfg, clock=clock.now)
    await apply_terminal_view(thread, "pane-1", "first\nsecond", "working", cfg, clock=clock.now)
    clock.advance(2.1)
    await flush_terminal_view(thread, "pane-1", cfg, clock=clock.now)
    assert "second" in _edited_text(msg)


@pytest.mark.asyncio
async def test_deferred_edit_flushes_when_no_later_push_arrives():
    thread, msg = _make_thread()
    cfg = _bridge(edit_cooldown=0.02)
    await apply_terminal_view(thread, "pane-1", "first", "idle", cfg)
    await apply_terminal_view(thread, "pane-1", "first\nfinal", "working", cfg)
    await asyncio.sleep(0.05)
    assert "final" in _edited_text(msg)


@pytest.mark.asyncio
async def test_begin_prompt_only_keeps_new_lines():
    clock = FakeClock(1000.0)
    thread = AsyncMock()
    thread.id = 7
    first = MagicMock(id=1, edit=AsyncMock())
    second = MagicMock(id=2, edit=AsyncMock())
    thread.send = AsyncMock(return_value=first)
    thread.fetch_message = AsyncMock(side_effect=lambda mid: {1: first, 2: second}[mid])
    cfg = _bridge(edit_cooldown=0.0)

    await apply_terminal_view(thread, "p1", "old1\nold2", "idle", cfg, clock=clock.now, remote_id="r")
    prompt = MagicMock(id=99)
    prompt.reply = AsyncMock(return_value=second)
    await begin_prompt_session(thread, "p1", prompt, remote_id="r")
    await apply_terminal_view(
        thread, "p1", "old1\nold2\nnewA\nnewB", "working", cfg, clock=clock.now, remote_id="r"
    )
    state = get_terminal_state(thread, "p1")
    assert state.session_lines == ["newA", "newB"]
    desc = prompt.reply.await_args.kwargs["embed"].description
    assert "newA" in desc and "newB" in desc
    assert "old1" not in desc
