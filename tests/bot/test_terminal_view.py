"""Unit tests for plain-text terminal continuation."""

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


def _text(call) -> str:
    if not call:
        return ""
    args, kwargs = call
    if args:
        return str(args[0])
    return str(kwargs.get("content") or "")


@pytest.fixture(autouse=True)
def _reset():
    clear_terminal_state()
    yield
    clear_terminal_state()


def _bridge(*, edit_cooldown: float = 2.0) -> BridgeConfig:
    return BridgeConfig(terminal=TerminalConfig(edit_cooldown=edit_cooldown))


def test_new_lines_no_dupes():
    assert new_lines_from_window(["a", "b", "c"], ["b", "c", "d"]) == ["d"]
    assert new_lines_from_window(["a", "b", "c", "d"], ["b", "c", "d"]) == []
    assert new_lines_from_window(["x"], ["1", "2"]) == []


@pytest.mark.asyncio
async def test_plain_text_no_code_fence():
    clock = FakeClock(1000.0)
    thread, _msg = _make_thread()
    cfg = _bridge(edit_cooldown=0.0)
    await apply_terminal_view(thread, "p1", "hello world", "idle", cfg, clock=clock.now)
    sent = _text(thread.send.await_args)
    assert "```" not in sent
    assert "【终端】" in sent
    assert "hello world" in sent
    assert "embed" not in (thread.send.await_args.kwargs or {})


@pytest.mark.asyncio
async def test_sliding_updates_edit_same_message_no_dupes():
    clock = FakeClock(1000.0)
    thread, msg = _make_thread()
    cfg = _bridge(edit_cooldown=0.0)
    await apply_terminal_view(thread, "p1", "a\nb\nc", "working", cfg, clock=clock.now)
    await apply_terminal_view(thread, "p1", "b\nc\nd", "working", cfg, clock=clock.now)
    await apply_terminal_view(thread, "p1", "c\nd\ne", "working", cfg, clock=clock.now)
    assert thread.send.await_count == 1
    state = get_terminal_state(thread, "p1")
    assert state.session_lines == ["a", "b", "c", "d", "e"]
    edited = _text(msg.edit.await_args)
    body = edited.split("\n", 1)[1]
    assert body.splitlines() == ["a", "b", "c", "d", "e"]


@pytest.mark.asyncio
async def test_seal_leaves_previous_message_untouched():
    """When live fills, finalize it then continue on a NEW message; old id not edited again."""
    clock = FakeClock(1000.0)
    thread = AsyncMock()
    thread.id = 9
    msgs: dict[int, MagicMock] = {}
    n = {"i": 0}

    async def send(content: str = "", **kwargs):
        n["i"] += 1
        m = MagicMock()
        m.id = n["i"]
        m.edit = AsyncMock()
        msgs[m.id] = m
        return m

    async def fetch(mid: int):
        return msgs[mid]

    thread.send = AsyncMock(side_effect=send)
    thread.fetch_message = AsyncMock(side_effect=fetch)
    cfg = _bridge(edit_cooldown=0.0)

    # Build a very long single dump so one message overflows
    lines = [f"L{i:04d} " + ("x" * 60) for i in range(60)]
    await apply_terminal_view(thread, "p1", "\n".join(lines), "working", cfg, clock=clock.now)

    assert thread.send.await_count >= 2
    first = msgs[1]
    first_edits_before = first.edit.await_count
    # Further small update should not touch the first sealed message
    await apply_terminal_view(
        thread, "p1", "\n".join(lines[-40:] + ["NEW_ONLY"]), "working", cfg, clock=clock.now
    )
    assert first.edit.await_count == first_edits_before
    state = get_terminal_state(thread, "p1")
    assert "NEW_ONLY" in state.session_lines
    assert state.session_lines.count("NEW_ONLY") == 1


@pytest.mark.asyncio
async def test_coalesce_and_flush():
    clock = FakeClock(1000.0)
    thread, msg = _make_thread()
    cfg = _bridge(edit_cooldown=2.0)
    await apply_terminal_view(thread, "p1", "first", "idle", cfg, clock=clock.now)
    await apply_terminal_view(thread, "p1", "first\nsecond", "working", cfg, clock=clock.now)
    msg.edit.assert_not_awaited()
    clock.advance(2.1)
    await flush_terminal_view(thread, "p1", cfg, clock=clock.now)
    assert "second" in _text(msg.edit.await_args)


@pytest.mark.asyncio
async def test_deferred_flush():
    thread, msg = _make_thread()
    cfg = _bridge(edit_cooldown=0.02)
    await apply_terminal_view(thread, "p1", "first", "idle", cfg)
    await apply_terminal_view(thread, "p1", "first\nfinal", "working", cfg)
    await asyncio.sleep(0.05)
    assert "final" in _text(msg.edit.await_args)


@pytest.mark.asyncio
async def test_begin_prompt_keeps_only_new():
    clock = FakeClock(1000.0)
    thread = AsyncMock()
    thread.id = 3
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
        thread, "p1", "old1\nold2\nnewA", "working", cfg, clock=clock.now, remote_id="r"
    )
    body = prompt.reply.await_args.args[0]
    assert "newA" in body and "old1" not in body
    assert "```" not in body
