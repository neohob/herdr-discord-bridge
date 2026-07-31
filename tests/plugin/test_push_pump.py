from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from src.plugin.gateway.push_pump import PushPump
from src.plugin.gateway.server import PushHub


class FakeHerdrClient:
    """Injectable Herdr client for PushPump tests."""

    def __init__(self) -> None:
        self.read_results: dict[str, list[dict[str, Any]]] = {}
        self.read_calls: list[str] = []
        self._last_read: dict[str, dict[str, Any]] = {}

    def set_reads(self, pane_id: str, reads: list[dict[str, Any]]) -> None:
        self.read_results[pane_id] = list(reads)

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if method == "pane.read":
            pane_id = str((params or {}).get("pane_id", ""))
            self.read_calls.append(pane_id)
            queue = self.read_results.get(pane_id, [])
            if queue:
                result = queue.pop(0)
                self._last_read[pane_id] = result
                return result
            if pane_id in self._last_read:
                return self._last_read[pane_id]
            return {"text": "", "revision": 0, "truncated": False}
        raise AssertionError(f"unexpected method {method!r}")

    async def close(self) -> None:
        pass


class FakeHerdrSubscriber:
    """Emits a fixed sequence of Herdr events then blocks."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = list(events)
        self.started = False
        self.subscriptions: list[dict] | None = None

    async def start(self, path: str, subscriptions: list[dict]) -> None:
        self.started = True
        self.subscriptions = subscriptions

    async def recv_event(self) -> dict:
        if self._events:
            return self._events.pop(0)
        await asyncio.sleep(3600)
        raise AssertionError("recv_event called with no events left")

    async def close(self) -> None:
        pass


class EventCollectingHub(PushHub):
    """PushHub that records broadcasts and supports wait_event."""

    def __init__(self) -> None:
        super().__init__()
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def broadcast(self, obj: dict[str, Any]) -> None:
        await self.events.put(obj)
        await super().broadcast(obj)

    async def wait_event(self, event_name: str, *, timeout: float = 2.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"timed out waiting for event {event_name!r}")
            obj = await asyncio.wait_for(self.events.get(), timeout=remaining)
            if obj.get("event") == event_name:
                return obj


@pytest.fixture
def push_hub() -> EventCollectingHub:
    return EventCollectingHub()


@pytest.fixture
def fake_herdr() -> FakeHerdrClient:
    return FakeHerdrClient()


@pytest.fixture
def fake_subscriber() -> FakeHerdrSubscriber:
    return FakeHerdrSubscriber(
        events=[
            {"event": "pane.created", "data": {"pane_id": "w1:p1"}},
        ],
    )


def _make_pump(
    push_hub: EventCollectingHub,
    fake_herdr: FakeHerdrClient,
    fake_subscriber: FakeHerdrSubscriber,
    *,
    push_cooldown: float = 0.05,
    poll_interval: float = 0.02,
) -> PushPump:
    return PushPump(
        push_hub,
        herdr_socket="/tmp/fake.sock",
        herdr_factory=lambda: fake_herdr,
        subscriber_factory=lambda: fake_subscriber,
        push_cooldown=push_cooldown,
        poll_interval=poll_interval,
    )


@pytest.mark.asyncio
async def test_run_broadcasts_herdr_events(
    push_hub: EventCollectingHub,
    fake_herdr: FakeHerdrClient,
    fake_subscriber: FakeHerdrSubscriber,
):
    pump = _make_pump(push_hub, fake_herdr, fake_subscriber)
    run_task = asyncio.create_task(pump.run())
    try:
        ev = await push_hub.wait_event("pane.created", timeout=2)
        assert ev["data"]["pane_id"] == "w1:p1"
    finally:
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass
        await pump.shutdown()


@pytest.mark.asyncio
async def test_run_recreates_subscriber_after_start_failure(
    push_hub: EventCollectingHub,
    fake_herdr: FakeHerdrClient,
):
    class FailingSubscriber(FakeHerdrSubscriber):
        async def start(self, path: str, subscriptions: list[dict]) -> None:
            raise ConnectionError("socket unavailable")

    recovered = FakeHerdrSubscriber([{"event": "pane.created", "data": {"pane_id": "w2:p2"}}])
    attempts = iter([FailingSubscriber([]), recovered])
    pump = PushPump(
        push_hub,
        herdr_socket="/tmp/fake.sock",
        herdr_factory=lambda: fake_herdr,
        subscriber_factory=lambda: next(attempts),
    )
    pump._subscriber_min_backoff = 0.01  # noqa: SLF001
    pump._subscriber_max_backoff = 0.02  # noqa: SLF001
    run_task = asyncio.create_task(pump.run())
    try:
        event = await push_hub.wait_event("pane.created", timeout=1)
        assert event["data"]["pane_id"] == "w2:p2"
        assert recovered.started
    finally:
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass
        await pump.shutdown()


@pytest.mark.asyncio
async def test_observe_emits_terminal_output(
    push_hub: EventCollectingHub,
    fake_herdr: FakeHerdrClient,
    fake_subscriber: FakeHerdrSubscriber,
):
    fake_herdr.set_reads(
        "w1:p1",
        [
            {"text": "line one", "revision": 1, "truncated": False},
            {"text": "line two", "revision": 2, "truncated": False},
        ],
    )
    pump = _make_pump(push_hub, fake_herdr, fake_subscriber)
    run_task = asyncio.create_task(pump.run())
    try:
        await pump.set_observe("w1:p1", True)
        ev = await push_hub.wait_event("bridge.terminal_output", timeout=2)
        assert ev["data"]["pane_id"] == "w1:p1"
        assert ev["data"]["text"] == "line one"
        assert ev["data"]["revision"] == 1
    finally:
        await pump.set_observe("w1:p1", False)
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass
        await pump.shutdown()


@pytest.mark.asyncio
async def test_observe_strips_ansi_locally(
    push_hub: EventCollectingHub,
    fake_herdr: FakeHerdrClient,
    fake_subscriber: FakeHerdrSubscriber,
):
    fake_herdr.set_reads(
        "w1:p1",
        [{"text": "\x1b[31mred\x1b[0m", "revision": 1, "truncated": False}],
    )
    pump = _make_pump(push_hub, fake_herdr, fake_subscriber)
    run_task = asyncio.create_task(pump.run())
    try:
        await pump.set_observe("w1:p1", True)
        ev = await push_hub.wait_event("bridge.terminal_output", timeout=2)
        assert ev["data"]["text"] == "red"
    finally:
        await pump.set_observe("w1:p1", False)
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass
        await pump.shutdown()


@pytest.mark.asyncio
async def test_observe_coalesces_rapid_updates(
    push_hub: EventCollectingHub,
    fake_herdr: FakeHerdrClient,
    fake_subscriber: FakeHerdrSubscriber,
):
    fake_herdr.set_reads(
        "w1:p1",
        [
            {"text": "a", "revision": 1, "truncated": False},
            {"text": "b", "revision": 2, "truncated": False},
            {"text": "c", "revision": 3, "truncated": False},
        ],
    )
    pump = PushPump(
        push_hub,
        herdr_socket="/tmp/fake.sock",
        herdr_factory=lambda: fake_herdr,
        subscriber_factory=lambda: fake_subscriber,
        push_cooldown=0.2,
        poll_interval=0.02,
    )
    run_task = asyncio.create_task(pump.run())
    collected: list[dict[str, Any]] = []

    async def collect() -> None:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            try:
                obj = await asyncio.wait_for(push_hub.events.get(), timeout=0.1)
            except TimeoutError:
                continue
            if obj.get("event") == "bridge.terminal_output":
                collected.append(obj)

    collector = asyncio.create_task(collect())
    try:
        await pump.set_observe("w1:p1", True)
        await asyncio.sleep(0.5)
        terminal_events = [e for e in collected if e.get("event") == "bridge.terminal_output"]
        assert len(terminal_events) <= 2
        assert terminal_events[-1]["data"]["text"] == "c"
    finally:
        collector.cancel()
        try:
            await collector
        except asyncio.CancelledError:
            pass
        await pump.set_observe("w1:p1", False)
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass
        await pump.shutdown()


@pytest.mark.asyncio
async def test_set_observe_disable_stops_polling(
    push_hub: EventCollectingHub,
    fake_herdr: FakeHerdrClient,
    fake_subscriber: FakeHerdrSubscriber,
):
    fake_herdr.set_reads(
        "w1:p1",
        [{"text": "once", "revision": 1, "truncated": False}],
    )
    pump = _make_pump(push_hub, fake_herdr, fake_subscriber)
    run_task = asyncio.create_task(pump.run())
    try:
        await pump.set_observe("w1:p1", True)
        await push_hub.wait_event("bridge.terminal_output", timeout=2)
        calls_before = len(fake_herdr.read_calls)
        await pump.set_observe("w1:p1", False)
        await asyncio.sleep(0.15)
        calls_after_disable = len(fake_herdr.read_calls)
        assert calls_after_disable == calls_before
    finally:
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass
        await pump.shutdown()
