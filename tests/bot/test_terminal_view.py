"""Unit tests for Terminal View edit coalescing (no Discord API)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.config import BridgeConfig, TerminalConfig
from src.bot.terminal_view import apply_terminal_view, clear_terminal_state, flush_terminal_view


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


def _sent_content(thread: AsyncMock) -> str:
    if not thread.send.await_args:
        return ""
    args, kwargs = thread.send.await_args
    return str(args[0] if args else kwargs.get("content", ""))


def _edited_content(msg: MagicMock) -> str:
    if not msg.edit.await_args:
        return ""
    args, kwargs = msg.edit.await_args
    return str(args[0] if args else kwargs.get("content", ""))


@pytest.fixture(autouse=True)
def _reset_terminal_state():
    clear_terminal_state()
    yield
    clear_terminal_state()


def _bridge(*, edit_cooldown: float = 2.0) -> BridgeConfig:
    return BridgeConfig(terminal=TerminalConfig(edit_cooldown=edit_cooldown))


@pytest.mark.asyncio
async def test_first_apply_sends_terminal_message():
    clock = FakeClock(1000.0)
    thread, _msg = _make_thread()
    cfg = _bridge()

    mid = await apply_terminal_view(
        thread, "pane-1", "hello", "idle", cfg, clock=clock.now
    )

    assert mid == 42
    thread.send.assert_awaited_once()
    thread.fetch_message.assert_not_awaited()
    assert "hello" in _sent_content(thread)


@pytest.mark.asyncio
async def test_coalesce_skips_edit_within_cooldown():
    clock = FakeClock(1000.0)
    thread, msg = _make_thread()
    cfg = _bridge(edit_cooldown=2.0)

    await apply_terminal_view(thread, "pane-1", "first", "idle", cfg, clock=clock.now)
    assert "first" in _sent_content(thread)

    await apply_terminal_view(thread, "pane-1", "second", "working", cfg, clock=clock.now)

    thread.send.assert_awaited_once()
    msg.edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_flush_after_cooldown_applies_pending_edit():
    clock = FakeClock(1000.0)
    thread, msg = _make_thread()
    cfg = _bridge(edit_cooldown=2.0)

    await apply_terminal_view(thread, "pane-1", "first", "idle", cfg, clock=clock.now)
    await apply_terminal_view(thread, "pane-1", "second", "working", cfg, clock=clock.now)

    msg.edit.assert_not_awaited()

    clock.advance(2.1)
    mid = await flush_terminal_view(thread, "pane-1", cfg, clock=clock.now)

    assert mid == 42
    msg.edit.assert_awaited_once()
    edited = _edited_content(msg)
    assert "second" in edited
    assert "working" in edited


@pytest.mark.asyncio
async def test_force_bypasses_cooldown():
    clock = FakeClock(1000.0)
    thread, msg = _make_thread()
    cfg = _bridge(edit_cooldown=10.0)

    await apply_terminal_view(thread, "pane-1", "first", "idle", cfg, clock=clock.now)
    await apply_terminal_view(
        thread, "pane-1", "forced", "blocked", cfg, clock=clock.now, force=True
    )

    msg.edit.assert_awaited_once()
    edited = _edited_content(msg)
    assert "forced" in edited


@pytest.mark.asyncio
async def test_reuses_existing_message_id():
    clock = FakeClock(1000.0)
    thread, msg = _make_thread(message_id=99)
    cfg = _bridge(edit_cooldown=0.0)

    mid = await apply_terminal_view(
        thread,
        "pane-1",
        "hello",
        "idle",
        cfg,
        clock=clock.now,
        message_id=99,
    )

    assert mid == 99
    thread.send.assert_not_awaited()
    thread.fetch_message.assert_awaited_once_with(99)
    msg.edit.assert_awaited_once()
